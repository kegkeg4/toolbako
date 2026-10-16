import asyncio
import hashlib
import hmac
import json
import time
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.config import Settings
from app.data import DemoStore
from app.identity import verification_url
from app.integrations import StripeOutcomeUnknown
from app.webhooks import StripeWebhookService


@pytest.fixture
def provider_webhook():
    store = DemoStore()
    store.identity_applications['identity-owner'] = {'status':'pending','provider':'stripe_identity','provider_reference':'vs_owned'}
    async def no_email(*args):
        pytest.fail('Identity status changes must not send email in this test')
    service = StripeWebhookService(Settings(stripe_webhook_secret='test-signing-secret'),store,no_email)
    app = FastAPI()
    app.post('/hook')(service.handle)
    return TestClient(app), store


def deliver(client,status,*,reference='vs_owned',created=100,event_id=None,signature_valid=True,event_type=None):
    event={'id':event_id or f'evt_{status}_{created}','created':created,'type':event_type or f'identity.verification_session.{status}','data':{'object':{'id':reference,'status':status,'metadata':{'user_id':'identity-owner'},'verified_outputs':{'private':'must not persist'}}}}
    payload=json.dumps(event).encode()
    timestamp=int(time.time())
    digest=hmac.new(b'test-signing-secret',f'{timestamp}.'.encode()+payload,hashlib.sha256).hexdigest()
    return client.post('/hook',content=payload,headers={'stripe-signature':f't={timestamp},v1={digest if signature_valid else "bad"}'})


def test_verified_signed_result_updates_once_without_sensitive_outputs(provider_webhook):
    client,store=provider_webhook
    assert deliver(client,'verified').status_code==200
    assert deliver(client,'verified').json()['duplicate'] is True
    application=store.identity_applications['identity-owner']
    assert application['status']=='verified'
    assert 'verified_outputs' not in application
    assert 'private' not in repr(application)
    assert deliver(client,'requires_input',created=99).status_code==200
    assert deliver(client,'processing',created=101).status_code==200
    assert application['status']=='verified'


@pytest.mark.parametrize('overrides',[{'reference':'vs_someone_else'},{'signature_valid':False},{'event_type':'identity.verification_session.verified'}])
def test_invalid_provider_result_does_not_verify(provider_webhook,overrides):
    client,store=provider_webhook
    assert deliver(client,'requires_input',**overrides).status_code==400
    assert store.identity_applications['identity-owner']['status']=='pending'


def test_retry_and_cancel_results_are_not_approval(provider_webhook):
    client,store=provider_webhook
    assert deliver(client,'requires_input',created=101).status_code==200
    assert store.identity_applications['identity-owner']['status']=='rejected'
    assert deliver(client,'processing',created=100).status_code==200
    assert store.identity_applications['identity-owner']['status']=='rejected'
    assert deliver(client,'canceled',created=102).status_code==200
    assert store.identity_applications['identity-owner']['status']=='rejected'


@pytest.fixture
def identity_route(monkeypatch):
    import app.main as main
    monkeypatch.setattr(main,'settings',replace(main.settings,demo_mode=False,stripe_secret_key='sk_test_fake',stripe_webhook_secret='whsec_fake'))
    monkeypatch.setattr(main,'current_user',lambda request:{'id':'identity-owner'})
    monkeypatch.setattr(main.store,'identity_applications',{})
    calls=[]
    async def create(user_id):
        calls.append('create')
        return {'id':'vs_owned','metadata':{'user_id':user_id},'url':'https://verify.stripe.com/start/test'}
    async def retrieve(reference):
        calls.append('retrieve')
        return {'id':reference,'metadata':{'user_id':'identity-owner'},'status':'requires_input','url':'https://verify.stripe.com/start/test'}
    monkeypatch.setattr(main.stripe,'create_identity_session',create)
    monkeypatch.setattr(main.stripe,'retrieve_identity_session',retrieve)
    request=Request({'type':'http','headers':[],'session':{}})
    return main,request,calls


def test_resume_reuses_session_instead_of_creating_a_second_one(identity_route):
    main,request,calls=identity_route
    first=asyncio.run(main.start_identity_verification(request))
    second=asyncio.run(main.start_identity_verification(request))
    assert first.headers['location']==second.headers['location']=='https://verify.stripe.com/start/test'
    assert calls==['create','retrieve']


def test_unknown_creation_is_held_without_automatic_retry(identity_route,monkeypatch):
    main,request,calls=identity_route
    async def unknown(user_id):
        calls.append('create')
        raise StripeOutcomeUnknown('identity-held-operation')
    monkeypatch.setattr(main.stripe,'create_identity_session',unknown)
    assert asyncio.run(main.start_identity_verification(request)).status_code==303
    assert asyncio.run(main.start_identity_verification(request)).status_code==303
    assert calls==['create']
    assert main.store.identity_applications['identity-owner']['creation_review_required'] is True


def test_processing_or_verified_provider_result_waits_for_webhook(identity_route,monkeypatch):
    main,request,calls=identity_route
    main.store.identity_applications['identity-owner']={'status':'pending','provider_reference':'vs_owned'}
    async def processing(reference):
        return {'id':reference,'metadata':{'user_id':'identity-owner'},'status':'processing'}
    monkeypatch.setattr(main.stripe,'retrieve_identity_session',processing)
    response=asyncio.run(main.start_identity_verification(request))
    assert response.headers['location']=='/verification?processing=1'
    assert calls==[]
    assert main.store.identity_applications['identity-owner']['status']=='pending'


@pytest.mark.parametrize('url',['https://evil.example/start','https://verify.stripe.com.evil.example/','http://verify.stripe.com/','https://user@verify.stripe.com/'])
def test_provider_redirect_is_restricted(url):
    with pytest.raises(RuntimeError):
        verification_url({'id':'vs_owned','metadata':{'user_id':'identity-owner'},'url':url},user_id='identity-owner',reference='vs_owned')


def test_unconfigured_screen_does_not_accept_documents(identity_route,monkeypatch):
    main,request,calls=identity_route
    monkeypatch.setattr(main,'settings',replace(main.settings,stripe_secret_key='',stripe_webhook_secret=''))
    page=TestClient(main.app).get('/verification')
    assert page.status_code==200
    assert '本人確認サービスを接続準備中' in page.text
    assert 'action="/verification/start"' not in page.text
    assert asyncio.run(main.start_identity_verification(request)).headers['location']=='/verification?unavailable=1'
    assert calls==[]


def test_provider_setup_screen_explains_automatic_results(identity_route):
    main,request,calls=identity_route
    page=TestClient(main.app).get('/verification')
    assert page.status_code==200
    assert '外部サービスの確認結果を自動で反映' in page.text
    assert '通常1〜3営業日' not in page.text
    assert 'action="/verification/start"' in page.text
