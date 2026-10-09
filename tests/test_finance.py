import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi import HTTPException

from app.config import Settings
from app.data import DemoStore
from app.database import PostgresStateStore, StorageUnavailable
from app.finance import FinanceConflict, balance, request_payout
from app.integrations import StripeIntegration
from app.payout_worker import run_step, sandbox_payouts_ready
from app.webhooks import StripeWebhookService
from test_database import database, query, webhook_request

NOW = datetime(2026, 9, 22, tzinfo=timezone.utc)


def order(**changes):
    return {"id": "order_finance", "seller_id": "seller", "buyer_id": "buyer", "tool_name": "Test",
            "amount": 1000, "primary_payment_amount": 1000, "platform_fee": 150,
            "platform_fee_rate": 0.15, "status": "completed", "payment_status": "paid",
            "payment_reference": "pi_finance", "billing_type": "one_time", "created_at": NOW, **changes}


async def seed(backend, dsn):
    settings = Settings(environment="staging", database_url=dsn, stripe_secret_key="sk_test_finance",
                        stripe_charge_mode="separate", stripe_webhook_secret="whsec_local")
    store = DemoStore(seed=False)
    async with backend.request(store):
        store.orders.append(order())
        store.connected_accounts["seller"] = {"account_id": "acct_seller", "payouts_enabled": True, "details_submitted": True}
        payout = request_payout(store, settings, "seller", 850, at=datetime(2026, 9, 1, tzinfo=timezone.utc))
        await backend.save()
        payout_id = payout["id"]
    stripe = StripeIntegration(settings.stripe_secret_key, "https://example.test", charge_mode="separate", journal=backend)
    return store, settings, stripe, payout_id


class Provider:
    def __init__(self):
        self.transfers = 0
        self.payouts = 0
        self.timeout_transfer = False
        self.timeout_payout = False
        self.refunded = False
        self.balance = 10000
        self.payout_status = "pending"
        self.payout_metadata = {}
        self.bank_headers = []
        self.keys = []
        self.interval = "manual"
        self.reversals = 0

    def __call__(self, request):
        path = request.url.path
        data = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        if request.method == "POST":
            self.keys.append(request.headers.get("Idempotency-Key"))
        if path == "/v1/accounts/acct_seller":
            return httpx.Response(200, json={"id": "acct_seller", "country": "JP", "default_currency": "jpy", "payouts_enabled": True,
                "details_submitted": True, "capabilities": {"transfers": "active"}, "settings": {"payouts": {"schedule": {"interval": self.interval}}}})
        if path == "/v1/payment_intents/pi_finance":
            return httpx.Response(200, json={"id": "pi_finance", "status": "succeeded", "currency": "jpy", "amount_received": 1000,
                "transfer_group": "order-order_finance", "latest_charge": {"id": "ch_finance", "payment_intent": "pi_finance", "paid": True, "amount_refunded": 1000 if self.refunded else 0, "disputed": False}})
        if path == "/v1/transfers":
            self.transfers += 1
            if self.timeout_transfer:
                raise httpx.ReadTimeout("test-only simulated timeout", request=request)
            return httpx.Response(200, json={"id": "tr_finance", "amount": int(data["amount"]), "currency": "jpy", "destination": data["destination"], "source_transaction": data["source_transaction"]})
        if path == "/v1/balance":
            assert request.headers["Stripe-Account"] == "acct_seller"
            return httpx.Response(200, json={"object": "balance", "available": [{"currency": "jpy", "amount": self.balance}]})
        if path == "/v1/payouts":
            self.payouts += 1
            self.bank_headers.append(request.headers.get("Stripe-Account"))
            if self.timeout_payout:
                raise httpx.ReadTimeout("test-only simulated timeout", request=request)
            self.payout_metadata = {"payout_id": data["metadata[payout_id]"]}
            return httpx.Response(200, json=self.bank_object())
        if path == "/v1/payouts/po_finance":
            assert request.headers["Stripe-Account"] == "acct_seller"
            return httpx.Response(200, json=self.bank_object())
        if path == "/v1/transfers/tr_finance/reversals":
            self.reversals += 1
            return httpx.Response(200, json={"id": "trr_finance", "transfer": "tr_finance", "currency": "jpy", "amount": int(data["amount"])})
        raise AssertionError(f"Unexpected provider request: {request.method} {path}")

    def bank_object(self):
        return {"id": "po_finance", "currency": "jpy", "amount": 690, "metadata": self.payout_metadata, "status": self.payout_status}


@pytest.fixture
def provider(monkeypatch):
    provider = Provider()
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(provider), **kwargs))
    return provider


def test_reservation_and_fee_boundaries():
    settings = Settings()
    store = DemoStore(seed=False)
    store.orders.append(order())
    store.connected_accounts["seller"] = {"account_id": "acct_seller", "payouts_enabled": True, "details_submitted": True}
    assert balance(store, "seller") == 850
    payout = request_payout(store, settings, "seller", 850)
    assert payout["net_amount"] == 690 and payout["fee"] == 160
    assert sum(a["transfer_amount"] for a in payout["allocations"]) == 690
    assert balance(store, "seller") == 0
    with pytest.raises(FinanceConflict): request_payout(store, settings, "seller", 161)
    for value in (True, 1.5, 160, -100, "850"):
        with pytest.raises(ValueError): request_payout(store, settings, "seller", value)


def test_repeated_partial_request_uses_same_reservation_and_admin_balance():
    store = DemoStore(seed=False)
    store.orders.append(order())
    store.connected_accounts["seller"] = {"account_id": "acct_seller", "payouts_enabled": True, "details_submitted": True}
    first = request_payout(store, Settings(), "seller", 200, request_key="form-unique")
    second = request_payout(store, Settings(), "seller", 200, request_key="form-unique")
    assert first["id"] == second["id"] and len(store.payouts) == 1
    assert balance(store, "seller") == 650
    assert store.finance_snapshot(ledger=True)["available_to_payout"] == 650
    assert store.finance_snapshot(ledger=True)["payout_reserved"] == 200
    assert store.finance_snapshot(ledger=True)["paid_out"] == 0
    with pytest.raises(FinanceConflict): request_payout(store, Settings(), "seller", 201, request_key="form-unique")


@pytest.mark.parametrize("changes", [{"refund_status": "pending"}, {"refund_status": "failed"}, {"dispute_status": "provider_dispute"}, {"status": "in_progress"}, {"billing_type": "subscription"}])
def test_ineligible_receipts_are_never_payable(changes):
    store = DemoStore(seed=False)
    store.orders.append(order(**changes))
    assert balance(store, "seller") == 0


def test_financial_schema_atomic_immutable_and_no_duplicates(database):
    backend, dsn = database
    async def run():
        store, settings, stripe, payout_id = await seed(backend, dsn)
        async with backend.request(store):
            await backend.save()
        assert query(dsn, "select amount,fee from toolbako_runtime.finance_receipts") == [(1000, 150)]
        assert query(dsn, "select count(*) from toolbako_runtime.finance_entries") == [(2,)]
        with pytest.raises(StorageUnavailable):
            async with backend.request(store):
                store.orders[0]["platform_fee"] = 100
                await backend.save()
        async with backend.request(store):
            assert store.orders[0]["platform_fee"] == 150
        import psycopg
        for table in ("finance_receipts", "finance_entries", "finance_allocations"):
            with pytest.raises(psycopg.errors.RaiseException):
                query(dsn, f"delete from toolbako_runtime.{table}")
    asyncio.run(run())


def test_worker_transfer_bank_and_settlement_are_distinct(database, provider):
    backend, dsn = database
    async def run():
        store, settings, stripe, payout_id = await seed(backend, dsn)
        assert await run_step(backend, store, stripe, settings, at=NOW) == "transferring"
        assert await run_step(backend, store, stripe, settings, at=NOW) == "bank_pending"
        assert await run_step(backend, store, stripe, settings, at=NOW) == "bank_pending"
        provider.payout_status = "paid"
        assert await run_step(backend, store, stripe, settings, at=NOW) == "paid"
        assert await run_step(backend, store, stripe, settings, at=NOW) == "idle"
        assert provider.transfers == provider.payouts == 1
        assert provider.bank_headers == ["acct_seller"]
        assert len(set(provider.keys)) == 2
        assert query(dsn, "select amount from toolbako_runtime.finance_entries where kind='bank_paid'") == [(690,)]
        assert query(dsn, "select amount from toolbako_runtime.finance_entries where kind='withdrawal_fee'") == [(160,)]
    asyncio.run(run())


@pytest.mark.parametrize("phase", ["transfer", "bank"])
def test_unknown_outcome_keeps_reservation_and_never_reissues(database, provider, phase):
    backend, dsn = database
    async def run():
        store, settings, stripe, payout_id = await seed(backend, dsn)
        if phase == "bank":
            await run_step(backend, store, stripe, settings, at=NOW)
            provider.timeout_payout = True
        else:
            provider.timeout_transfer = True
        assert await run_step(backend, store, stripe, settings, at=NOW) == "review"
        for _ in range(2):
            assert await run_step(backend, store, stripe, settings, at=NOW) == "idle"
        assert provider.transfers == 1 and provider.payouts == (1 if phase == "bank" else 0)
        async with backend.request(store):
            assert balance(store, "seller") == 0
        assert query(dsn, "select count(*) from toolbako_runtime.stripe_operations where status='unknown'") == [(1,)]
    asyncio.run(run())


@pytest.mark.parametrize("phase", ["transfer", "bank"])
def test_crash_after_provider_success_is_recovered_without_resend(database, provider, monkeypatch, phase):
    backend, dsn = database
    async def run():
        store, settings, stripe, payout_id = await seed(backend, dsn)
        if phase == "bank":
            await run_step(backend, store, stripe, settings, at=NOW)
        original = backend.save
        async def crash(): raise StorageUnavailable("simulated process termination")
        monkeypatch.setattr(backend, "save", crash)
        with pytest.raises(StorageUnavailable):
            await run_step(backend, store, stripe, settings, at=NOW)
        monkeypatch.setattr(backend, "save", original)
        if phase == "bank": provider.balance = 0
        assert await run_step(backend, store, stripe, settings, at=NOW) == "bank_pending"
        assert provider.transfers == 1 and provider.payouts == 1
    asyncio.run(run())


def test_refund_after_transfer_blocks_bank_and_does_not_release_money(database, provider):
    backend, dsn = database
    async def run():
        store, settings, stripe, payout_id = await seed(backend, dsn)
        await run_step(backend, store, stripe, settings, at=NOW)
        provider.refunded = True  # Including refunds made outside this app.
        assert await run_step(backend, store, stripe, settings, at=NOW) == "review"
        assert provider.payouts == 0
        async with backend.request(store):
            assert balance(store, "seller") == 0
    asyncio.run(run())


def test_waiting_balance_does_not_create_bank_payout(database, provider):
    backend, dsn = database
    async def run():
        store, settings, stripe, payout_id = await seed(backend, dsn)
        await run_step(backend, store, stripe, settings, at=NOW)
        provider.balance = 689
        assert await run_step(backend, store, stripe, settings, at=NOW) == "awaiting_funds"
        assert await run_step(backend, store, stripe, settings, at=NOW) == "awaiting_funds"
        assert provider.transfers == 1 and provider.payouts == 0
    asyncio.run(run())


def test_admin_hold_and_nonmanual_account_stop_transfers(database, provider):
    backend, dsn = database
    async def run():
        store, settings, stripe, payout_id = await seed(backend, dsn)
        async with backend.request(store):
            store.connected_accounts["seller"]["payouts_paused"] = True
            await backend.save()
        assert await run_step(backend, store, stripe, settings, at=NOW) == "held"
        assert provider.transfers == 0
        async with backend.request(store):
            store.connected_accounts["seller"]["payouts_paused"] = False
            store.payouts[0]["status"] = "requested"
            await backend.save()
        provider.interval = "daily"
        assert await run_step(backend, store, stripe, settings, at=NOW) == "review"
        assert provider.transfers == provider.payouts == 0
    asyncio.run(run())


def test_live_or_production_payout_worker_cannot_move_money(database, provider):
    backend, dsn = database
    async def run():
        store, settings, stripe, payout_id = await seed(backend, dsn)
        for unsafe in (replace(settings, environment="production"), replace(settings, stripe_secret_key="sk_live_forbidden"), replace(settings, stripe_charge_mode="destination")):
            assert not sandbox_payouts_ready(unsafe)
            with pytest.raises(FinanceConflict): await run_step(backend, store, stripe, unsafe, at=NOW)
        assert not provider.keys
    asyncio.run(run())


def test_signed_payout_event_checks_connected_account_amount_and_duplicate(database, provider):
    backend, dsn = database
    async def no_email(*args): return True
    async def run():
        store, settings, stripe, payout_id = await seed(backend, dsn)
        await run_step(backend, store, stripe, settings, at=NOW)
        await run_step(backend, store, stripe, settings, at=NOW)
        provider.payout_status = "paid"
        event = {"id": "evt_bank_paid", "type": "payout.paid", "livemode": False, "account": "acct_wrong", "data": {"object": provider.bank_object()}}
        async with backend.request(store):
            service = StripeWebhookService(settings, store, no_email, journal=backend)
            with pytest.raises(HTTPException): await service.handle(webhook_request(event, settings.stripe_webhook_secret))
            assert "evt_bank_paid" not in store.processed_webhook_events
            event["account"] = "acct_seller"
            event["data"]["object"]["amount"] = 691
            with pytest.raises(HTTPException): await service.handle(webhook_request(event, settings.stripe_webhook_secret))
            event["data"]["object"]["amount"] = 690
            assert await service.handle(webhook_request(event, settings.stripe_webhook_secret)) == {"received": True}
            await backend.save()
        restarted = PostgresStateStore(dsn)
        async with restarted.request(store):
            service = StripeWebhookService(settings, store, no_email, journal=restarted)
            assert (await service.handle(webhook_request(event, settings.stripe_webhook_secret)))["duplicate"]
            assert store.payouts[0]["status"] == "paid"
            await restarted.save()
        assert query(dsn, "select count(*) from toolbako_runtime.finance_entries where kind='bank_paid'") == [(1,)]
    asyncio.run(run())


def test_transfer_reversal_returns_funds_before_reservation_is_released(database, provider):
    backend, dsn = database
    async def run():
        store, settings, stripe, payout_id = await seed(backend, dsn)
        await run_step(backend, store, stripe, settings, at=NOW)
        async with backend.request(store):
            store.payouts[0]["status"] = "reversing"
            await backend.save()
        assert await run_step(backend, store, stripe, settings, at=NOW) == "reversing"
        async with backend.request(store):
            assert balance(store, "seller") == 0
        assert await run_step(backend, store, stripe, settings, at=NOW) == "cancelled"
        async with backend.request(store):
            assert balance(store, "seller") == 850
        assert provider.reversals == provider.transfers == 1 and provider.payouts == 0
        assert query(dsn, "select amount from toolbako_runtime.finance_entries where kind='transfer_reversal'") == [(-690,)]
    asyncio.run(run())


def test_bank_failure_keeps_reservation_even_after_stale_paid_event(database, provider):
    from app.payout_worker import apply_bank_status
    backend, dsn = database
    async def run():
        store, settings, stripe, payout_id = await seed(backend, dsn)
        await run_step(backend, store, stripe, settings, at=NOW)
        await run_step(backend, store, stripe, settings, at=NOW)
        provider.payout_status = "failed"
        assert await run_step(backend, store, stripe, settings, at=NOW) == "bank_failed"
        async with backend.request(store):
            provider.payout_status = "paid"
            apply_bank_status(store.payouts[0], provider.bank_object())
            assert store.payouts[0]["status"] == "bank_failed"
            assert balance(store, "seller") == 0
    asyncio.run(run())


def test_financial_history_cannot_disappear_from_snapshot(database):
    backend, dsn = database
    async def run():
        store, *_ = await seed(backend, dsn)
        with pytest.raises(StorageUnavailable):
            async with backend.request(store):
                store.payouts.clear()
                await backend.save()
        with pytest.raises(StorageUnavailable):
            async with backend.request(store):
                store.orders.clear()
                await backend.save()
    asyncio.run(run())


def test_navigation_does_not_rewrite_unchanged_financial_ledger(database, monkeypatch):
    import app.finance as finance
    backend, dsn = database
    async def run():
        store, *_ = await seed(backend, dsn)
        calls = []
        original = finance.sync_finance
        async def capture(conn, state):
            calls.append(True)
            await original(conn, state)
        monkeypatch.setattr(finance, "sync_finance", capture)
        async with backend.request(store):
            store.notifications.append({"id": "nonfinancial"})
            await backend.save()
        assert not calls
        async with backend.request(store):
            store.payouts[0]["status"] = "held"
            await backend.save()
        assert len(calls) == 1
    asyncio.run(run())


def test_primary_and_extra_refunds_must_both_complete():
    from app.refunds import apply_refund_result, request_order_refunds
    store = DemoStore(seed=False)
    original = order(amount=1500, platform_fee=225, status="cancel_pending")
    extra = {"id": "extra", "status": "paid", "amount": 500, "payment_reference": "pi_extra", "platform_fee": 75, "created_at": NOW}
    original["pending_extras"] = [extra]
    store.orders.append(original)
    calls = []
    class Refunds:
        async def refund_payment(self, payment_intent, order_id, **kwargs):
            calls.append((payment_intent, kwargs))
            return {"id": "re_" + payment_intent}
    asyncio.run(request_order_refunds(store, original, Refunds()))
    assert calls == [("pi_finance", {}), ("pi_extra", {"extra_id": "extra"})]
    assert not apply_refund_result(original, None, {"amount_refunded": 1000}, "charge.refunded")
    assert original["refund_status"] == "partial"
    assert apply_refund_result(original, extra, {"amount_refunded": 500}, "charge.refunded")
    assert original["refund_status"] == "completed" and original["refund_confirmed_amount"] == 1500
    # A delayed "pending" response cannot undo the confirmed refund.
    assert apply_refund_result(original, extra, {"status": "pending"}, "refund.updated")


def test_refund_before_payout_cancellation_is_blocked(database):
    from app.refunds import request_order_refunds
    backend, dsn = database
    async def run():
        store, settings, stripe, payout_id = await seed(backend, dsn)
        async with backend.request(store):
            with pytest.raises(FinanceConflict): await request_order_refunds(store, store.orders[0], stripe)
        assert query(dsn, "select count(*) from toolbako_runtime.stripe_operations") == [(0,)]
    asyncio.run(run())


def test_unresolved_payout_delays_account_erasure():
    from datetime import timedelta
    store = DemoStore(seed=False)
    store.registered_users["seller"] = {"id": "seller"}
    store.connected_accounts["seller"] = {"account_id": "acct_seller"}
    store.payouts.append({"seller_id": "seller", "status": "review"})
    store.account_deletions.append({"user_id": "seller", "status": "scheduled", "delete_after": NOW - timedelta(days=1)})
    assert store.execute_due_account_deletions(NOW) == 0
    assert store.connected_accounts["seller"]["account_id"] == "acct_seller"
    assert store.account_deletions[0]["hold_reason"]
