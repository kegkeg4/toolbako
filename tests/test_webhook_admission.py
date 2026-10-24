"""Webhook admission must not consume DB capacity for untrusted messages."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import hashlib
import hmac
import json
import time

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.config import Settings
from app.data import DemoStore
from app.database import DatabaseBoundaryMiddleware, StorageUnavailable, PostgresStateStore
from app.middleware import ProductionGuardMiddleware
from app.request_limits import RequestBodyLimitMiddleware
from app.webhooks import StripeWebhookGuardMiddleware, StripeWebhookService
from test_webhook_security import identity_event, account_event, deliver
from test_database import database, webhook_request


def guarded_app():
    settings = Settings(stripe_secret_key='rk_test_fixture', stripe_webhook_secret='platform-fixture',
                        stripe_connect_webhook_secret='connect-fixture')
    calls = []
    class Offline:
        @asynccontextmanager
        async def request(self, store):
            calls.append('database')
            raise StorageUnavailable()
            yield
    app = FastAPI()
    app.add_middleware(DatabaseBoundaryMiddleware, backend=Offline(), store=None)
    app.add_middleware(StripeWebhookGuardMiddleware, settings=settings)
    app.add_middleware(ProductionGuardMiddleware, settings=settings)
    @app.post('/webhooks/stripe')
    async def route():
        pytest.fail('offline DB must never acknowledge an event')
    return TestClient(app), calls


@pytest.mark.parametrize('case', ['missing', 'invalid', 'expired', 'scope', 'mode', 'duplicate_header', 'malformed', 'oversized'])
def test_untrusted_events_rejected_before_offline_database(case):
    client, calls = guarded_app()
    event = identity_event()
    if case == 'missing':
        response = client.post('/webhooks/stripe', json=event)
    elif case == 'invalid':
        response = deliver(client, event, secret='wrong-key')
    elif case == 'expired':
        response = deliver(client, event, timestamp=int(time.time())-301)
    elif case == 'scope':
        response = deliver(client, event, secret='connect-fixture')
    elif case == 'mode':
        response = deliver(client, {**event, 'livemode': True})
    elif case == 'duplicate_header':
        response = client.post('/webhooks/stripe', json=event,
            headers=[('stripe-signature','one'),('stripe-signature','two')])
    elif case == 'malformed':
        response = deliver(client, raw=b'{broken-json')
    else:
        response = deliver(client, raw=b'x'*1_000_001)
    assert response.status_code == (413 if case == 'oversized' else 400)
    assert calls == []
    assert 'no-store' in response.headers['cache-control']
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert response.headers.get('x-request-id')


@pytest.mark.parametrize('connected', [False, True])
def test_valid_event_does_not_get_false_ack_when_database_unavailable(connected):
    client, calls = guarded_app()
    response = deliver(client, account_event() if connected else identity_event(),
                       secret='connect-fixture' if connected else 'platform-fixture')
    assert response.status_code == 503 and calls == ['database']
    assert response.json().get('received') is not True


def test_chunked_raw_bytes_preserved_through_guard_and_limiter():
    async def run():
        payload = json.dumps(identity_event(), ensure_ascii=False, indent=2).encode() + b' \n'
        timestamp = int(time.time())
        digest = hmac.new(b'platform-fixture', f'{timestamp}.'.encode()+payload, hashlib.sha256).hexdigest()
        scope={'type':'http','path':'/webhooks/stripe','method':'POST','headers':[
            (b'stripe-signature',f't={timestamp},v1={digest}'.encode())]}
        chunks=[{'type':'http.request','body':payload[:37],'more_body':True},
                {'type':'http.request','body':payload[37:],'more_body':False}]
        received=[]
        async def receive():
            return chunks.pop(0) if chunks else {'type':'http.disconnect'}
        async def send(message):
            pytest.fail('valid body must reach downstream')
        async def downstream(scope, receive, send):
            received.append(await Request(scope,receive).body())
        app=RequestBodyLimitMiddleware(StripeWebhookGuardMiddleware(downstream,
            settings=Settings(stripe_webhook_secret='platform-fixture')), max_bytes=1_000_000)
        await app(scope,receive,send)
        assert received == [payload]
    asyncio.run(run())


def test_committed_replay_after_restart_skips_state_resolution_and_side_effects(database, monkeypatch):
    backend, dsn = database
    store=DemoStore(seed=False)
    event=identity_event()
    settings=Settings(stripe_secret_key='rk_test_fixture',stripe_webhook_secret='platform-fixture')
    async def no_email(*args):
        pytest.fail('no mail on identity or duplicate')
    async def run():
        async with backend.request(store):
            store.identity_applications['member']={'status':'pending','provider_reference':'vs_member'}
            service=StripeWebhookService(settings,store,no_email,journal=backend)
            await service.handle(webhook_request(event,'platform-fixture'))
            await backend.save()
        restarted=PostgresStateStore(dsn)
        async with restarted.request(store):
            before=deepcopy((store.identity_applications,store.audit_logs,store.notifications))
            service=StripeWebhookService(settings,store,no_email,journal=restarted)
            def no_resolution(*args):
                pytest.fail('committed duplicate must not resolve current business state')
            monkeypatch.setattr(service,'_resolve',no_resolution)
            response=await service.handle(webhook_request(event,'platform-fixture'))
            assert response == {'received':True,'duplicate':True}
            assert (store.identity_applications,store.audit_logs,store.notifications)==before
            await restarted.save()
    asyncio.run(run())


def test_main_admission_guard_precedes_database_and_follows_body_limiter():
    from app.main import app
    classes=[m.cls for m in app.user_middleware]
    assert classes.index(ProductionGuardMiddleware) < classes.index(StripeWebhookGuardMiddleware)
    if DatabaseBoundaryMiddleware in classes:
        assert classes.index(StripeWebhookGuardMiddleware) < classes.index(DatabaseBoundaryMiddleware)
