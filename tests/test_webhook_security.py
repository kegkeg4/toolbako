import hashlib
import hmac
import json
import time
from copy import deepcopy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.data import DemoStore
from app.webhooks import StripeWebhookService


@pytest.fixture
def webhook():
    store = DemoStore(seed=False)
    store.identity_applications['member'] = {'status': 'pending', 'provider_reference': 'vs_member'}
    store.connected_accounts['seller'] = {'account_id': 'acct_seller', 'payouts_enabled': False}

    async def no_mail(*args):
        pytest.fail('These events must not send email')

    settings = Settings(stripe_secret_key='rk_test_fixture', stripe_webhook_secret='platform-fixture',
                        stripe_connect_webhook_secret='connect-fixture')
    service = StripeWebhookService(settings, store, no_mail)
    app = FastAPI()
    app.post('/webhooks/stripe')(service.handle)
    return TestClient(app, raise_server_exceptions=False), store, service


def identity_event():
    return {'id': 'evt_identity', 'type': 'identity.verification_session.verified', 'created': 100,
            'livemode': False, 'data': {'object': {'id': 'vs_member', 'status': 'verified', 'metadata': {'user_id': 'member'}}}}


def account_event():
    return {'id': 'evt_account', 'type': 'account.updated', 'account': 'acct_seller', 'livemode': False,
            'data': {'object': {'id': 'acct_seller', 'charges_enabled': True, 'payouts_enabled': True, 'details_submitted': True}}}


def deliver(client, event=None, *, secret='platform-fixture', timestamp=None, signature=None, raw=None):
    payload = raw if raw is not None else json.dumps(event).encode()
    timestamp = int(time.time()) if timestamp is None else timestamp
    digest = hmac.new(secret.encode(), f'{timestamp}.'.encode() + payload, hashlib.sha256).hexdigest()
    return client.post('/webhooks/stripe', content=payload, headers={
        'stripe-signature': signature or f't={timestamp},v1={digest}', 'content-type': 'application/json'})


@pytest.mark.parametrize('event,secret', [(identity_event(), 'connect-fixture'), (account_event(), 'platform-fixture')])
def test_wrong_scope_signature_cannot_change_state(webhook, event, secret):
    client, store, _ = webhook
    before = deepcopy((store.identity_applications, store.connected_accounts))
    assert deliver(client, event, secret=secret).status_code == 400
    assert (store.identity_applications, store.connected_accounts) == before
    assert not store.processed_webhook_events


def test_connected_account_cannot_supply_platform_identity_event(webhook):
    client, store, _ = webhook
    event = {**identity_event(), 'account': 'acct_seller'}
    assert deliver(client, event, secret='connect-fixture').status_code == 400
    assert store.identity_applications['member']['status'] == 'pending'


def test_account_update_requires_connected_account_scope(webhook):
    client, store, _ = webhook
    event = account_event()
    del event['account']
    assert deliver(client, event).status_code == 400
    assert not store.connected_accounts['seller']['payouts_enabled']


def test_valid_scopes_and_duplicate_are_supported(webhook):
    client, store, _ = webhook
    assert deliver(client, identity_event()).status_code == 200
    assert deliver(client, identity_event()).json()['duplicate'] is True
    assert store.identity_applications['member']['status'] == 'verified'
    assert deliver(client, account_event(), secret='connect-fixture').status_code == 200
    assert store.connected_accounts['seller']['payouts_enabled']


@pytest.mark.parametrize('changes', [
    {'id': []}, {'id': None}, {'type': []}, {'data': None}, {'data': {'object': []}},
    {'account': []}, {'account': ''}, {'livemode': True}, {'livemode': 'false'},
])
def test_invalid_envelope_never_mutates_identity(webhook, changes):
    client, store, _ = webhook
    assert deliver(client, {**identity_event(), **changes}).status_code == 400
    assert store.identity_applications['member']['status'] == 'pending'
    assert not store.processed_webhook_events


@pytest.mark.parametrize('metadata', [{'user_id': []}, {'user_id': {}}, [], {'user_id': 123}])
def test_invalid_metadata_returns_client_error(webhook, metadata):
    client, store, _ = webhook
    event = identity_event()
    event['data']['object']['metadata'] = metadata
    assert deliver(client, event).status_code == 400
    assert store.identity_applications['member']['status'] == 'pending'


@pytest.mark.parametrize('raw', [b'\xff', b'{', b'[' * 1100 + b']' * 1100])
def test_invalid_json_encoding_or_depth_returns_client_error(webhook, raw):
    client, store, _ = webhook
    assert deliver(client, raw=raw).status_code == 400
    assert not store.processed_webhook_events


def test_signature_with_non_ascii_input_is_rejected_without_exception(webhook):
    _, _, service = webhook
    assert not service.verify_signature(b'{}', f't={int(time.time())},v1=\u00ff')


def test_stale_signature_cannot_mutate_identity(webhook):
    client, store, _ = webhook
    assert deliver(client, identity_event(), timestamp=int(time.time()) - 301).status_code == 400
    assert store.identity_applications['member']['status'] == 'pending'


def test_unhandled_payout_event_cannot_skip_payout_validation(webhook):
    client, store, _ = webhook
    store.payouts.append({'id': 'payout_fixture', 'finance_v2': True, 'account_id': 'acct_seller',
                         'provider_payout_id': 'po_fixture', 'status': 'bank_pending'})
    before = deepcopy(store.payouts)
    event = {'id': 'evt_unsupported', 'type': 'payout.reconciliation_completed', 'account': 'acct_unrelated',
             'livemode': False, 'data': {'object': {'id': 'po_fixture', 'status': 'paid'}}}
    assert deliver(client, event, secret='connect-fixture').status_code == 200
    assert store.payouts == before


def test_absent_connect_secret_cannot_fall_back_to_platform_secret(webhook):
    client, store, service = webhook
    from dataclasses import replace
    service.settings = replace(service.settings, stripe_connect_webhook_secret='')
    assert deliver(client, account_event()).status_code == 400
    assert not store.connected_accounts['seller']['payouts_enabled']


def test_signature_accepts_multiple_v1_values_for_rotation(webhook):
    _, _, service = webhook
    payload, timestamp = b'{}', int(time.time())
    digest = hmac.new(b'platform-fixture', f'{timestamp}.'.encode() + payload, hashlib.sha256).hexdigest()
    assert service.verify_signature(payload, f't={timestamp},v1={"0" * 64},v1={digest}')
