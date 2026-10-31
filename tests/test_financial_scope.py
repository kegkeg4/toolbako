import asyncio
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from app.config import Settings
from app.data import DemoStore
from app.database import OperationConflict
from app.financial_scope import FinancialScope, FinancialScopeError, configured_scope, SCOPE_OPERATION_ID
from app.financial_scope_check import inspect_scope
from app.integrations import StripeIntegration, StripeOutcomeUnknown
from app.webhooks import StripeWebhookService
from test_database import database, query, webhook_request


SCOPE = FinancialScope("test", "acct_platformA", "c5a0aed3-9260-47c8-a529-a28678c12f20")


@pytest.mark.parametrize("changes", [
    {"mode": "staging"}, {"mode": True}, {"platform_account": "acct_A/B"},
    {"platform_account": ""}, {"ledger_id": "secret-key"}, {"ledger_id": None},
])
def test_invalid_scope_is_rejected_without_echoing_values(changes):
    with pytest.raises(FinancialScopeError) as exc:
        replace(SCOPE, **changes)
    assert "secret-key" not in str(exc.value)


def test_configuration_is_all_or_none_and_does_not_enable_live():
    assert configured_scope(Settings(stripe_platform_account_id="", stripe_ledger_id="")) is None
    with pytest.raises(FinancialScopeError):
        configured_scope(Settings(stripe_platform_account_id=SCOPE.platform_account, stripe_ledger_id=""))
    settings = Settings(stripe_secret_key="rk_live_fixture", stripe_platform_account_id=SCOPE.platform_account,
                        stripe_ledger_id=SCOPE.ledger_id)
    assert configured_scope(settings).livemode is True


def test_binding_survives_restart_and_cannot_be_relabelled(database):
    backend, dsn = database
    async def run():
        store = DemoStore(seed=False)
        async with backend.request(store):
            await backend.bind_empty_financial_scope(SCOPE)
        original = query(dsn, "select * from toolbako_runtime.stripe_operations")
        async with backend.request(DemoStore(seed=False)):
            await backend.assert_financial_scope(SCOPE)
            await backend.bind_empty_financial_scope(SCOPE)
            for wrong in (None, replace(SCOPE, mode="live"), replace(SCOPE, platform_account="acct_other"),
                          replace(SCOPE, ledger_id=str(uuid4()))):
                with pytest.raises(FinancialScopeError):
                    await backend.assert_financial_scope(wrong)
                if wrong is not None:
                    with pytest.raises(FinancialScopeError):
                        await backend.bind_empty_financial_scope(wrong)
            with pytest.raises(OperationConflict):
                await backend.begin_operation(SCOPE_OPERATION_ID, "refunds", {})
        assert query(dsn, "select * from toolbako_runtime.stripe_operations") == original
    asyncio.run(run())


@pytest.mark.parametrize("field", ["orders", "payouts", "connected_accounts", "subscriptions",
                                  "identity_applications", "processed_webhook_events", "coupon_redemptions"])
def test_existing_snapshot_is_not_silently_converted_to_live(database, field):
    backend, dsn = database
    async def run():
        async with backend.request(DemoStore(seed=False)):
            value = getattr(backend._store, field)
            if isinstance(value, dict): value["historical"] = {}
            elif isinstance(value, set): value.add("historical")
            else: value.append({"historical": True})
            with pytest.raises(FinancialScopeError):
                await backend.bind_empty_financial_scope(replace(SCOPE, mode="live"))
            # No business save: discard only this synthetic in-memory fixture.
            value.clear()
        assert query(dsn, "select count(*) from toolbako_runtime.stripe_operations") == [(0,)]
    asyncio.run(run())


def test_old_journal_blocks_binding_even_with_empty_snapshot(database):
    backend, dsn = database
    async def run():
        async with backend.request(DemoStore(seed=False)):
            await backend.begin_operation("historic-unknown", "refunds", {"payment_intent": "pi_old"})
            with pytest.raises(FinancialScopeError):
                await backend.bind_empty_financial_scope(replace(SCOPE, mode="live"))
        assert query(dsn, "select operation_id,status from toolbako_runtime.stripe_operations") == [("historic-unknown", "pending")]
    asyncio.run(run())


def test_normalized_receipts_also_block_binding(database):
    from test_finance import order
    backend, dsn = database
    async def run():
        async with backend.request(DemoStore(seed=False)):
            backend._store.orders.append(order())
            await backend.save()
            backend._store.orders.clear()  # Simulate a bad, unsaved cutover projection.
            with pytest.raises(FinancialScopeError):
                await backend.bind_empty_financial_scope(replace(SCOPE, mode="live"))
        assert query(dsn, "select count(*) from toolbako_runtime.finance_receipts") == [(1,)]
        assert query(dsn, "select count(*) from toolbako_runtime.stripe_operations") == [(0,)]
    asyncio.run(run())


def test_binding_and_audit_are_atomic_and_catalog_is_preserved(database, monkeypatch):
    backend, dsn = database
    async def run():
        original_save = backend._save
        async def fail(*args, **kwargs): raise RuntimeError("synthetic commit failure")
        async with backend.request(DemoStore(seed=False)):
            backend._store.tools.append({"slug": "real-catalog-item", "name": "Catalog fixture"})
            await backend.save()
            monkeypatch.setattr(backend, "_save", fail)
            with pytest.raises(RuntimeError): await backend.bind_empty_financial_scope(SCOPE)
        assert query(dsn, "select count(*) from toolbako_runtime.stripe_operations") == [(0,)]
        monkeypatch.setattr(backend, "_save", original_save)
        async with backend.request(DemoStore(seed=False)):
            assert not backend._store.audit_logs
            await backend.bind_empty_financial_scope(SCOPE)
        async with backend.request(DemoStore(seed=False)):
            assert backend._store.tools == [{"slug": "real-catalog-item", "name": "Catalog fixture"}]
            assert len(backend._store.audit_logs) == 1
    asyncio.run(run())


class ScopeProvider:
    def __init__(self):
        self.account = {"object": "account", "id": SCOPE.platform_account}
        self.balance = {"object": "balance", "livemode": False}
        self.posts = []
        self.requests = []
        self.timeout_post = False
        self.response_mode = False
        self.event = None

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        if path in {"/v1/account", "/v1/balance"}:
            assert "stripe-account" not in request.headers
            return httpx.Response(200, json=self.account if path.endswith("account") else self.balance)
        if request.method == "POST":
            self.posts.append(request)
            if self.timeout_post: raise httpx.ReadTimeout("private provider detail", request=request)
            return httpx.Response(200, json={"id": "tr_scoped", "livemode": self.response_mode})
        if path == "/v1/events/evt_scoped":
            return httpx.Response(200, json=self.event)
        raise AssertionError("Unexpected provider request")


@pytest.fixture
def provider(monkeypatch):
    instance = ScopeProvider()
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(instance), **kwargs))
    return instance


def integration(backend, scope=SCOPE, key="rk_test_fixture"):
    return StripeIntegration(key, "https://example.test", charge_mode="separate", journal=backend, financial_scope=scope)


@pytest.mark.parametrize("fault", ["unbound", "wrong_account", "wrong_mode", "missing_mode", "integer_mode", "missing_scope", "wrong_ledger", "key_mode"])
def test_mismatch_blocks_post_before_operation_or_provider_mutation(database, provider, fault):
    backend, dsn = database
    async def run():
        async with backend.request(DemoStore(seed=False)):
            if fault != "unbound": await backend.bind_empty_financial_scope(SCOPE)
            scope, key = SCOPE, "rk_test_fixture"
            if fault == "wrong_account": provider.account["id"] = "acct_wrong"
            if fault == "wrong_mode": provider.balance["livemode"] = True
            if fault == "missing_mode": del provider.balance["livemode"]
            if fault == "integer_mode": provider.balance["livemode"] = 0
            if fault == "missing_scope": scope = None
            if fault == "wrong_ledger": scope = replace(SCOPE, ledger_id=str(uuid4()))
            if fault == "key_mode": key = "rk_live_fixture"
            with pytest.raises(RuntimeError):
                await integration(backend, scope, key)._post("transfers", {"_idempotency_key": "unique-transfer", "amount": "640"})
        assert not provider.posts
        assert query(dsn, "select count(*) from toolbako_runtime.stripe_operations where operation_id='unique-transfer'") == [(0,)]
    asyncio.run(run())


def test_cached_success_still_checks_account_and_never_duplicates(database, provider):
    backend, _ = database
    async def run():
        stripe = integration(backend)
        data = {"_idempotency_key": "unique-transfer", "amount": "640"}
        async with backend.request(DemoStore(seed=False)):
            await backend.bind_empty_financial_scope(SCOPE)
            first = await stripe._post("transfers", data)
        async with backend.request(DemoStore(seed=False)):
            assert await stripe._post("transfers", data) == first
            provider.account["id"] = "acct_other"
            with pytest.raises(FinancialScopeError): await stripe._post("transfers", data)
        assert len(provider.posts) == 1
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["timeout", "response_mode"])
def test_uncertain_result_stays_held_without_second_post(database, provider, fault):
    backend, dsn = database
    async def run():
        async with backend.request(DemoStore(seed=False)):
            await backend.bind_empty_financial_scope(SCOPE)
        provider.timeout_post = fault == "timeout"
        provider.response_mode = True if fault == "response_mode" else False
        for _ in range(2):
            async with backend.request(DemoStore(seed=False)):
                with pytest.raises(StripeOutcomeUnknown):
                    await integration(backend)._post("transfers", {"_idempotency_key": "held-transfer", "amount": "640"})
        assert len(provider.posts) == 1
        assert query(dsn, "select status from toolbako_runtime.stripe_operations where operation_id='held-transfer'") == [("unknown",)]
    asyncio.run(run())


def test_live_binding_is_not_live_write_approval(database, provider):
    backend, _ = database
    async def run():
        live = replace(SCOPE, mode="live")
        async with backend.request(DemoStore(seed=False)):
            await backend.bind_empty_financial_scope(live)
            with pytest.raises(RuntimeError, match="本番決済"):
                await integration(backend, live, "rk_live_fixture")._post("payouts", {"_idempotency_key": "no-live"})
        assert not provider.requests
    asyncio.run(run())


def test_inspection_does_not_initialize_or_bind_dataset(database, provider):
    backend, dsn = database
    async def run():
        result = await inspect_scope(backend, integration(backend), SCOPE)
        assert result == {"configured": True, "provider_matches": True, "bound": False, "mode": "test", "live_writes_enabled": False}
        assert query(dsn, "select count(*) from toolbako_runtime.marketplace_state") == [(0,)]
        assert query(dsn, "select count(*) from toolbako_runtime.stripe_operations") == [(0,)]
        assert not provider.posts
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["wrong_ledger", "wrong_provider", "wrong_event", "none", "connected"])
def test_signed_webhook_is_also_bound_before_duplicate_ack(database, provider, fault):
    backend, _ = database
    async def run():
        store = DemoStore(seed=False)
        event = {"id": "evt_scoped", "type": "checkout.session.expired", "livemode": False,
                 "data": {"object": {"id": "cs_scoped"}}}
        if fault == "connected":
            event.update(type="payout.paid", account="acct_seller", data={"object": {"id": "po_scoped"}})
        provider.event = event if fault != "wrong_event" else {**event, "data": {"object": {"id": "cs_other"}}}
        settings = Settings(stripe_secret_key="rk_test_fixture", stripe_webhook_secret="local-signature", stripe_connect_webhook_secret="connect-signature",
                            stripe_platform_account_id=SCOPE.platform_account, stripe_ledger_id=SCOPE.ledger_id)
        scope = replace(SCOPE, ledger_id=str(uuid4())) if fault == "wrong_ledger" else SCOPE
        if fault == "wrong_ledger": settings = replace(settings, stripe_ledger_id=scope.ledger_id)
        if fault == "wrong_provider": provider.account["id"] = "acct_wrong"
        async def no_mail(*args): pytest.fail("Scope checking must not send mail")
        service = StripeWebhookService(settings, store, no_mail, journal=backend, provider=integration(backend, scope))
        async with backend.request(store):
            await backend.bind_empty_financial_scope(SCOPE)
            store.processed_webhook_events.add(event["id"])
            if fault in {"none", "connected"}:
                secret = "connect-signature" if fault == "connected" else "local-signature"
                assert (await service.handle(webhook_request(event, secret)))["duplicate"] is True
                event_request = next(r for r in provider.requests if r.url.path == "/v1/events/evt_scoped")
                assert event_request.headers.get("stripe-account") == event.get("account")
            else:
                with pytest.raises(HTTPException) as exc:
                    await service.handle(webhook_request(event, "local-signature"))
                assert exc.value.status_code == 503
        assert not provider.posts
    asyncio.run(run())
