"""All four purchase entrances must use the scoped executor, never a cached URL.

Synthetic route integration only; executor and PostgreSQL proofs live in their
own tests. No requests are sent to Stripe.
"""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.config import Settings
from app.finance import FinanceConflict
from app.integrations import StripeOutcomeUnknown


def fixture(monkeypatch, kind, *, enabled=True, created=True, failure=None):
    import app.main as main
    user = {'id': 'buyer', 'username': 'buyer', 'email': 'synthetic@example.invalid'}
    order = {'id': 'order', 'buyer_id': 'buyer', 'seller_id': 'seller', 'status': 'completed',
             'payment_status': 'paid' if kind == 'extra' else 'pending',
             'checkout_session_url': 'https://checkout.stripe.com/c/pay/stale', 'pending_extras': []}
    extra = {'id': 'extra', 'checkout_session_url': 'https://checkout.stripe.com/c/pay/stale-extra'}
    application = {'id': 'application', 'applicant_id': 'seller', 'status': 'submitted'}
    item = {'id': 'request', 'owner_id': 'buyer', 'application_list': [application]}
    proposal = {'id': 'proposal', 'status': 'open'}
    conversation = {'id': 'conversation', 'buyer_id': 'buyer', 'seller_id': 'seller',
                    'seller_username': 'seller', 'proposals': [proposal]}
    calls = []
    store = SimpleNamespace(orders=[order], subscriptions=[], requests=[item], conversations=[conversation],
        registered_users={'seller': {}}, connected_accounts={'seller': {'account_id':'acct_synthetic', 'charges_enabled':True}},
        get=lambda slug: {'author_id':'seller'}, get_order=lambda *args: order)
    import threading
    store._lock = threading.RLock()
    store.release_coupon = lambda value: calls.append('coupon_released')
    store.buy = lambda *a, **k: (order, created)
    store.contract_request = lambda *a, **k: (item, application, order)
    def rollback_contract(*args):
        calls.append('contract_rollback'); store.orders.remove(order)
    store.rollback_request_contract = rollback_contract
    store.reserve_extra_payment = lambda *a, **k: (extra, created)
    store.rollback_extra_payment = lambda *a: calls.append('extra_rollback')
    store.buy_proposal = lambda *a, **k: order
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(main, 'current_user', lambda request: user)
    monkeypatch.setattr(main, 'settings', Settings(environment='staging', demo_mode=False,
        stripe_connection_mode='live_financial', stripe_live_checkout_enabled=enabled))
    monkeypatch.setattr(main, 'checkout_configuration_ready', lambda settings: enabled)
    async def execute(backend, actual_store, provider, settings, sessions, request, actual_order, *, extra=None):
        assert actual_order is order and actual_store is store
        assert (extra is not None) == (kind == 'extra')
        calls.append('scoped_executor')
        if failure: raise failure
        return {'id': 'cs_live_validated', 'url': 'https://checkout.stripe.com/c/pay/validated'}
    monkeypatch.setattr(main, 'execute_live_checkout', execute)
    monkeypatch.setattr(main, 'payment_handoff', lambda request, url: ('handoff', url))
    async def forbidden(*args, **kwargs):
        pytest.fail('Generic sandbox Checkout must not execute for live')
    monkeypatch.setattr(main, 'stripe', SimpleNamespace(create_checkout=forbidden, create_extra_checkout=forbidden))
    request = Request({'type':'http', 'method':'POST', 'path':'/checkout/tool', 'headers':[]})
    async def run():
        if kind == 'tool': return await main.checkout_complete(request, 'tool', [], '', 'one_time', 'yes')
        if kind == 'request': return await main.select_application(request, 'request', 'application')
        if kind == 'extra': return await main.extra_payment(request, 'order', 500, '追加対応')
        return await main.buy_proposal(request, 'conversation', 'proposal')
    return run, store, order, calls


@pytest.mark.parametrize('kind', ['tool', 'request', 'extra', 'proposal'])
def test_each_purchase_uses_scoped_executor(monkeypatch, kind):
    run, store, order, calls = fixture(monkeypatch, kind)
    assert asyncio.run(run()) == ('handoff', 'https://checkout.stripe.com/c/pay/validated')
    assert calls == ['scoped_executor']


@pytest.mark.parametrize('kind', ['tool', 'extra'])
def test_cached_url_is_revalidated(monkeypatch, kind):
    run, store, order, calls = fixture(monkeypatch, kind, created=False)
    assert asyncio.run(run())[1].endswith('/validated')
    assert calls == ['scoped_executor']


@pytest.mark.parametrize('kind', ['tool', 'request', 'extra', 'proposal'])
def test_unknown_result_keeps_reservation(monkeypatch, kind):
    run, store, order, calls = fixture(monkeypatch, kind, failure=StripeOutcomeUnknown('checkout-order'))
    with pytest.raises(StripeOutcomeUnknown): asyncio.run(run())
    assert store.orders == [order] and calls == ['scoped_executor']


@pytest.mark.parametrize('kind', ['tool', 'request', 'extra', 'proposal'])
def test_precommit_failure_releases_only_new_reservation(monkeypatch, kind):
    run, store, order, calls = fixture(monkeypatch, kind, failure=FinanceConflict('synthetic precommit refusal'))
    with pytest.raises(HTTPException) as error: asyncio.run(run())
    assert error.value.status_code == 409
    if kind != 'extra': assert store.orders == []
    else: assert calls == ['scoped_executor', 'extra_rollback']


@pytest.mark.parametrize('kind', ['tool', 'extra'])
def test_precommit_failure_does_not_delete_existing_order(monkeypatch, kind):
    run, store, order, calls = fixture(monkeypatch, kind, created=False, failure=FinanceConflict('synthetic refusal'))
    with pytest.raises(HTTPException): asyncio.run(run())
    assert store.orders == [order] and calls == ['scoped_executor']


@pytest.mark.parametrize('kind', ['tool', 'request', 'extra', 'proposal'])
def test_disabled_checkout_never_calls_executor(monkeypatch, kind):
    run, store, order, calls = fixture(monkeypatch, kind, enabled=False)
    with pytest.raises(HTTPException) as error: asyncio.run(run())
    assert error.value.status_code == 503 and calls == []


@pytest.mark.parametrize('actor,status,enabled,allowed', [
    ('buyer','pending',True,True), ('seller','pending',True,False),
    ('buyer','paid',True,False), ('buyer','expired',True,False),
    ('buyer','pending',False,False),
])
def test_existing_checkout_resume_does_not_create_another_order(monkeypatch,actor,status,enabled,allowed):
    import app.main as main
    run, store, order, calls = fixture(monkeypatch, 'tool', created=False, enabled=enabled)
    order['payment_status'] = status
    monkeypatch.setattr(main,'current_user',lambda request:{'id':actor})
    store.buy = lambda *a,**k: pytest.fail('Resume must never reserve another order')
    request = Request({'type':'http','method':'POST','path':'/orders/order/checkout','headers':[]})
    if allowed:
        assert asyncio.run(main.resume_order_checkout(request,'order'))[1].endswith('/validated')
        assert calls == ['scoped_executor']
    else:
        with pytest.raises(HTTPException): asyncio.run(main.resume_order_checkout(request,'order'))
        assert calls == []
    assert store.orders == [order]


def test_checkout_resume_unknown_preserves_original(monkeypatch):
    import app.main as main
    run, store, order, calls = fixture(monkeypatch,'tool',created=False,failure=StripeOutcomeUnknown('checkout-order'))
    request = Request({'type':'http','method':'POST','path':'/orders/order/checkout','headers':[]})
    with pytest.raises(StripeOutcomeUnknown): asyncio.run(main.resume_order_checkout(request,'order'))
    assert store.orders == [order] and calls == ['scoped_executor']


@pytest.mark.parametrize('status,hold',[('succeeded',False),('pending',True),('unknown',True),(None,True)])
def test_unknown_handler_does_not_hold_verified_session_after_transient_get(monkeypatch,status,hold):
    import app.main as main
    from app.data import DemoStore
    store=DemoStore(seed=False)
    store.orders=[{'id':'order','payment_status':'pending'}]
    monkeypatch.setattr(main,'store',store)
    monkeypatch.setattr(main,'current_user',lambda request:None)
    monkeypatch.setattr(main,'settings',SimpleNamespace(stripe_connection_mode='live_financial'))
    async def details(key): return {'status':status} if status else None
    async def persist(): pass
    async def render(request,exc): return exc
    monkeypatch.setattr(main,'database_store',SimpleNamespace(operation_details=details))
    monkeypatch.setattr(main,'persist_state',persist)
    monkeypatch.setattr(main,'friendly_http_error',render)
    request=Request({'type':'http','method':'POST','path':'/orders/order/checkout','headers':[]})
    response=asyncio.run(main.stripe_outcome_unknown(request,StripeOutcomeUnknown('checkout-order')))
    assert response.status_code==502
    assert bool(store.orders[0].get('payment_reconciliation_required')) is hold
