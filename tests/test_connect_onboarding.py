import asyncio
from dataclasses import replace

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.data import DemoStore
from app.database import OperationConflict
from app.integrations import StripeIntegration, StripeOutcomeUnknown
from test_database import database, query


@pytest.fixture
def provider(monkeypatch):
    requests = []
    responses = []
    original = httpx.AsyncClient
    def handle(request):
        requests.append(request)
        return responses.pop(0)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    return requests, responses


def test_rejected_account_can_retry_then_replay_without_duplicate(database, provider):
    backend, dsn = database
    requests, responses = provider
    responses.extend([httpx.Response(400), httpx.Response(200, json={"id": "acct_test"})])
    store = DemoStore(seed=False)
    stripe = StripeIntegration("sk_test_fake", "https://toolbako.example", journal=backend, charge_mode="separate")
    async def run():
        async with backend.request(store):
            with pytest.raises(RuntimeError):
                await stripe.create_express_account({"id": "seller"}, "connect-seller")
        async with backend.request(store):
            assert (await stripe.create_express_account({"id": "seller"}, "connect-seller"))["id"] == "acct_test"
        async with backend.request(store):
            assert (await stripe.create_express_account({"id": "seller"}, "connect-seller"))["id"] == "acct_test"
    asyncio.run(run())
    assert [r.headers["idempotency-key"] for r in requests] == ["connect-seller", "connect-seller-retry-1"]
    assert query(dsn, "select status from toolbako_runtime.stripe_operations order by operation_id") == [("rejected",), ("succeeded",)]


@pytest.mark.parametrize("first_status", [409, 500])
def test_unknown_account_is_never_recreated(database, provider, first_status):
    backend, _ = database
    requests, responses = provider
    responses.append(httpx.Response(first_status))
    store = DemoStore(seed=False)
    stripe = StripeIntegration("sk_test_fake", "https://toolbako.example", journal=backend)
    async def run():
        for _ in range(2):
            async with backend.request(store):
                with pytest.raises(StripeOutcomeUnknown):
                    await stripe.create_express_account({"id": "seller"}, "connect-seller")
    asyncio.run(run())
    assert len(requests) == 1


def test_rejected_account_parameters_cannot_be_changed_on_retry(database, provider):
    backend, _ = database
    requests, responses = provider
    responses.append(httpx.Response(400))
    store = DemoStore(seed=False)
    stripe = StripeIntegration("sk_test_fake", "https://toolbako.example", journal=backend)
    async def run():
        async with backend.request(store):
            with pytest.raises(RuntimeError):
                await stripe.create_express_account({"id": "seller"}, "connect-seller")
        async with backend.request(store):
            with pytest.raises(OperationConflict):
                await stripe.create_express_account({"id": "other"}, "connect-seller")
    asyncio.run(run())
    assert len(requests) == 1


def test_account_link_without_id_is_accepted_but_url_is_not_persisted(database, provider):
    backend, dsn = database
    _, responses = provider
    result = {"object": "account_link", "created": 100, "expires_at": 200,
              "url": "https://connect.stripe.com/setup/test-access"}
    responses.append(httpx.Response(200, json=result))
    stripe = StripeIntegration("sk_test_fake", "https://toolbako.example", journal=backend)
    async def run():
        async with backend.request(DemoStore(seed=False)):
            assert await stripe.create_account_link("acct_test") == result
    asyncio.run(run())
    row = query(dsn, "select status,response from toolbako_runtime.stripe_operations")[0]
    assert row[0] == "succeeded" and row[1]["object"] == "account_link"
    assert "url" not in row[1] and "test-access" not in str(row)


@pytest.mark.parametrize("destination", ["https://evil.example/setup", "http://connect.stripe.com/setup", "https://connect.stripe.com@evil.example", "https://connect.stripe.com:444/setup"])
def test_invalid_account_link_is_not_trusted(provider, destination):
    _, responses = provider
    responses.append(httpx.Response(200, json={"object": "account_link", "created": 100, "expires_at": 200, "url": destination}))
    stripe = StripeIntegration("sk_test_fake", "https://toolbako.example")
    with pytest.raises(StripeOutcomeUnknown):
        asyncio.run(stripe.create_account_link("acct_test"))


def test_connect_route_reuses_account_and_finishes_form_locally(monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, "settings", replace(main.settings, demo_mode=False, stripe_secret_key="sk_test_fake",
        stripe_webhook_secret="whsec_fake", stripe_connect_webhook_secret="whsec_connect_fake",
        stripe_connect_client_id="ca_fake"))
    monkeypatch.setattr(main, "current_user", lambda request: {"id": "seller"})
    monkeypatch.setattr(main.store, "connected_accounts", {})
    calls = []
    async def create(user, key):
        calls.append("create")
        return {"id": "acct_test"}
    async def link(account):
        calls.append("link")
        assert account == "acct_test"
        return {"url": "https://connect.stripe.com/setup/test"}
    monkeypatch.setattr(main.stripe, "create_express_account", create)
    monkeypatch.setattr(main.stripe, "create_account_link", link)
    client = TestClient(main.app)
    for _ in range(2):
        response = client.post("/seller/payments/onboard", follow_redirects=False)
        assert response.status_code == 200
        assert "location" not in response.headers
        assert 'href="https://connect.stripe.com/setup/test"' in response.text
        assert "form-action 'self'" in response.headers["content-security-policy"]
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "no-store" in response.headers["cache-control"]
    assert calls == ["create", "link", "link"]


@pytest.mark.parametrize("destination,onboarding", [
    ("https://checkout.stripe.com/pay/test", True), ("https://connect.stripe.com/setup/test", False),
    ("https://checkout.stripe.com.evil.example/pay", False), ("javascript:alert(1)", False),
    ("https://user:pass@checkout.stripe.com/pay", False), ("https://checkout.stripe.com:bad/pay", False),
])
def test_handoff_refuses_unexpected_destination(destination, onboarding):
    import app.main as main
    with pytest.raises(HTTPException) as error:
        main.payment_handoff(Request({"type": "http"}), destination, onboarding=onboarding)
    assert error.value.status_code == 502
