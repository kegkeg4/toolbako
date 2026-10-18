"""Local disposable Postgres + fake Stripe; no live money or email."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.database import StorageUnavailable
from app.finance import FinanceConflict, balance, request_payout
from app.payout_recovery import propose_failed_payout, approve_failed_payout
from app.payout_worker import apply_bank_status, run_step
from test_database import database, query
from test_finance import seed as seed_payout
from test_reconciliation import RecoveryProvider


class FailedProvider(RecoveryProvider):
    def __init__(self):
        super().__init__()
        self.bank_changes = {}
        self.return_changes = {}
        self.requests = []
        self.reject_reversal = False
        self.incomplete = False
        self.at = int(datetime.now(timezone.utc).timestamp())

    def bank_object(self):
        return {**super().bank_object(), "object": "payout", "livemode": False, "automatic": False,
                "method": "standard", "type": "bank_account", "balance_transaction": "txn_original",
                "failure_balance_transaction": "txn_return", **self.bank_changes}

    def __call__(self, request):
        self.requests.append(request)
        if request.url.path == "/v1/balance_transactions/txn_return":
            assert request.method == "GET" and request.headers["Stripe-Account"] == "acct_seller"
            return httpx.Response(200, json={"id": "txn_return", "object": "balance_transaction", "amount": 690,
                "net": 690, "fee": 0, "currency": "jpy", "source": "po_finance", "status": "available",
                "type": "payout_failure" if self.payout_status == "failed" else "payout_cancel",
                "available_on": self.at, "created": self.at, **self.return_changes})
        if request.url.path == "/v1/transfers/tr_finance/reversals":
            if request.method == "POST" and self.reject_reversal:
                return httpx.Response(400, json={"error": {"message": "private error"}})
            if request.method == "GET" and self.incomplete:
                return httpx.Response(200, json={"object": "list", "data": [], "has_more": True})
        return super().__call__(request)


@pytest.fixture
def provider(monkeypatch):
    result = FailedProvider()
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(result), **kwargs))
    return result


async def seed(backend, dsn, provider):
    store, settings, stripe, _ = await seed_payout(backend, dsn)
    settings = replace(settings, admin_user_ids=("admin_one", "admin_two"))
    at = datetime.now(timezone.utc)
    assert await run_step(backend, store, stripe, settings, at=at) == "transferring"
    assert await run_step(backend, store, stripe, settings, at=at) == "bank_pending"
    provider.payout_status = "failed"
    assert await run_step(backend, store, stripe, settings, at=at) == "bank_failed"
    return store, settings, stripe, at


async def review(backend, store, settings, stripe, at):
    async with backend.request(store):
        payout = store.payouts[0]
        proposal = await propose_failed_payout(backend, store, stripe, settings, payout, "admin_one", at=at)
        return await approve_failed_payout(backend, store, stripe, settings, payout, "admin_two", proposal["id"], at=at)


@pytest.mark.parametrize("remote_status", ["failed", "canceled"])
def test_full_recovery_releases_only_after_new_two_person_confirmation(database, provider, remote_status):
    backend, dsn = database
    async def run():
        store, settings, stripe, at = await seed(backend, dsn, provider)
        provider.payout_status = remote_status
        assert await review(backend, store, settings, stripe, at) == "reverse"
        assert balance(store, "seller") == 0 and store.payouts[0]["status"] == "bank_failed"
        assert provider.reversals == provider.transfers == provider.payouts == 1
        assert await run_step(backend, store, stripe, settings, at=at) == "idle"
        assert await review(backend, store, settings, stripe, at) == "release"
        async with backend.request(store):
            payout = store.payouts[0]
            assert payout["status"] == "cancelled" and balance(store, "seller") == 850
            assert payout["provider_payout_id"] == "po_finance"
            for late in ["failed", "canceled", "pending", "in_transit", "paid"]:
                apply_bank_status(payout, {**provider.bank_object(), "status": late})
                assert payout["status"] == "cancelled" and not payout.get("paid_at")
            next_payout = request_payout(store, settings, "seller", 850, at=at)
            assert next_payout["id"] != payout["id"] and balance(store, "seller") == 0
            await backend.save()
        assert provider.reversals == provider.transfers == provider.payouts == 1
        assert query(dsn, "select count(*) from toolbako_runtime.finance_entries where kind='withdrawal_released'") == [(1,)]
        assert query(dsn, "select count(*) from toolbako_runtime.audit_events where action='payout.failure_recovery_approved'") == [(2,)]
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["no_return", "pending", "paid", "live", "automatic", "card", "bank_amount", "bank_journal",
    "return_source", "return_currency", "return_amount", "return_net", "return_fee", "return_pending", "return_future", "return_type",
    "parent_amount", "parent_account", "parent_charge", "parent_metadata", "partial", "missing_transfer", "paid_history", "pagination"])
def test_uncertain_or_mismatched_funds_never_authorize_reversal(database, provider, fault):
    backend, dsn = database
    async def run():
        store, settings, stripe, at = await seed(backend, dsn, provider)
        async with backend.request(store):
            payout = store.payouts[0]
            if fault == "no_return": provider.bank_changes["failure_balance_transaction"] = None
            elif fault in {"pending", "paid"}: provider.payout_status = fault
            elif fault == "live": provider.bank_changes["livemode"] = True
            elif fault == "automatic": provider.bank_changes["automatic"] = True
            elif fault == "card": provider.bank_changes["type"] = "card"
            elif fault == "bank_amount": provider.bank_changes["amount"] = True
            elif fault == "bank_journal": payout["account_id"] = "acct_changed"
            elif fault == "return_source": provider.return_changes["source"] = "po_other"
            elif fault == "return_currency": provider.return_changes["currency"] = "usd"
            elif fault == "return_amount": provider.return_changes["amount"] = 691
            elif fault == "return_net": provider.return_changes["net"] = 689
            elif fault == "return_fee": provider.return_changes["fee"] = 1
            elif fault == "return_pending": provider.return_changes["status"] = "pending"
            elif fault == "return_future": provider.return_changes["available_on"] = provider.at + 3600
            elif fault == "return_type": provider.return_changes["type"] = "adjustment"
            elif fault == "parent_amount": provider.records[0]["amount"] = 691
            elif fault == "parent_account": provider.records[0]["destination"] = "acct_other"
            elif fault == "parent_charge": provider.records[0]["source_transaction"] = "ch_other"
            elif fault == "parent_metadata": provider.records[0]["metadata"]["payout_id"] = "other"
            elif fault == "partial": provider.records[0].update(reversed=False, amount_reversed=1)
            elif fault == "missing_transfer": payout["allocations"][0].pop("transfer_id")
            elif fault == "paid_history": payout["paid_at"] = at
            elif fault == "pagination": provider.incomplete = True
            with pytest.raises((FinanceConflict, RuntimeError)):
                await propose_failed_payout(backend, store, stripe, settings, payout, "admin_one", at=at)
            assert payout["status"] == "bank_failed" and balance(store, "seller") == 0
        assert provider.reversals == 0
    asyncio.run(run())


def test_two_admin_expiry_stale_proposal_and_changed_provider(database, provider):
    backend, dsn = database
    async def run():
        store, settings, stripe, at = await seed(backend, dsn, provider)
        async with backend.request(store):
            p = store.payouts[0]
            proposal = await propose_failed_payout(backend, store, stripe, settings, p, "admin_one", at=at)
            for actor, proposal_id, when in [("admin_one", proposal["id"], at), ("stranger", proposal["id"], at),
                    ("admin_two", "stale", at), ("admin_two", proposal["id"], at + timedelta(minutes=31)),
                    ("admin_two", proposal["id"], at - timedelta(seconds=1))]:
                with pytest.raises(FinanceConflict):
                    await approve_failed_payout(backend, store, stripe, settings, p, actor, proposal_id, at=when)
            provider.records[0]["description"] = "changed since proposal"
            with pytest.raises(FinanceConflict):
                await approve_failed_payout(backend, store, stripe, settings, p, "admin_two", proposal["id"], at=at)
            assert balance(store, "seller") == 0
        assert provider.reversals == 0
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["live", "production", "single_admin", "wrong_journal", "wrong_key", "destination"])
def test_connection_and_environment_guards(database, provider, fault):
    backend, dsn = database
    async def run():
        store, settings, stripe, at = await seed(backend, dsn, provider)
        if fault == "live": settings = replace(settings, stripe_secret_key="sk_live_blocked")
        elif fault == "production": settings = replace(settings, environment="production")
        elif fault == "single_admin": settings = replace(settings, admin_user_ids=("admin_one",))
        elif fault == "wrong_journal": stripe.journal = object()
        elif fault == "wrong_key": stripe.secret_key = "sk_test_wrong"
        elif fault == "destination": stripe.charge_mode = "destination"
        async with backend.request(store):
            with pytest.raises(FinanceConflict):
                await propose_failed_payout(backend, store, stripe, settings, store.payouts[0], "admin_one", at=at)
        assert provider.reversals == 0
    asyncio.run(run())


def test_timeout_result_is_get_reconciled_without_resubmission(database, provider):
    backend, dsn = database
    async def run():
        store, settings, stripe, at = await seed(backend, dsn, provider)
        provider.timeout_reversal = True
        assert await review(backend, store, settings, stripe, at) == "reverse"
        assert store.payouts[0]["failed_payout_recovery"]["status"] == "review"
        assert balance(store, "seller") == 0
        assert await review(backend, store, settings, stripe, at) == "reconcile"
        assert provider.reversals == 1 and balance(store, "seller") == 0
        assert await review(backend, store, settings, stripe, at) == "release"
        assert provider.reversals == 1 and balance(store, "seller") == 850
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["absent", "duplicate", "partial", "rejected"])
def test_no_retry_for_unknown_rejected_or_ambiguous_reversal(database, provider, fault):
    backend, dsn = database
    async def run():
        store, settings, stripe, at = await seed(backend, dsn, provider)
        provider.timeout_reversal = fault != "rejected"
        provider.reject_reversal = fault == "rejected"
        await review(backend, store, settings, stripe, at)
        previous = len([r for r in provider.requests if r.method == "POST"])
        if fault == "absent": provider.reversal_records = []
        elif fault == "duplicate": provider.duplicate = True
        elif fault == "partial": provider.reversal_records[0]["amount"] -= 1
        async with backend.request(store):
            with pytest.raises(FinanceConflict):
                await propose_failed_payout(backend, store, stripe, settings, store.payouts[0], "admin_one", at=at)
            assert balance(store, "seller") == 0
        assert len([r for r in provider.requests if r.method == "POST"]) == previous
    asyncio.run(run())


def test_release_commit_failure_rolls_back_state_ledger_and_audit(database, provider, monkeypatch):
    backend, dsn = database
    async def run():
        store, settings, stripe, at = await seed(backend, dsn, provider)
        await review(backend, store, settings, stripe, at)
        async with backend.request(store):
            p = store.payouts[0]
            proposal = await propose_failed_payout(backend, store, stripe, settings, p, "admin_one", at=at)
        original = backend._save
        async def broken(*args, **kwargs):
            await original(*args, **kwargs)
            raise RuntimeError("simulated commit failure")
        monkeypatch.setattr(backend, "_save", broken)
        with pytest.raises(StorageUnavailable):
            async with backend.request(store):
                await approve_failed_payout(backend, store, stripe, settings, store.payouts[0], "admin_two", proposal["id"], at=at)
        monkeypatch.setattr(backend, "_save", original)
        async with backend.request(store):
            assert store.payouts[0]["status"] == "bank_failed" and balance(store, "seller") == 0
            assert store.payouts[0]["failed_payout_recovery"]["status"] == "proposed"
        assert query(dsn, "select count(*) from toolbako_runtime.finance_entries where kind='withdrawal_released'") == [(0,)]
        assert provider.reversals == 1
    asyncio.run(run())


def test_database_guard_rejects_unverified_release(database, provider):
    backend, dsn = database
    async def run():
        store, settings, stripe, at = await seed(backend, dsn, provider)
        with pytest.raises(StorageUnavailable):
            async with backend.request(store):
                store.payouts[0]["status"] = "cancelled"
                await backend.save()
        async with backend.request(store):
            assert store.payouts[0]["status"] == "bank_failed" and balance(store, "seller") == 0
    asyncio.run(run())


def test_response_commit_crash_restores_from_get_without_second_reversal(database, provider, monkeypatch):
    backend, dsn = database
    async def run():
        store, settings, stripe, at = await seed(backend, dsn, provider)
        async with backend.request(store):
            proposal = await propose_failed_payout(backend, store, stripe, settings, store.payouts[0], "admin_one", at=at)
        original = backend.save
        async def crash_after_response():
            if provider.reversals:
                raise StorageUnavailable("crash after provider response was journaled")
            await original()
        monkeypatch.setattr(backend, "save", crash_after_response)
        with pytest.raises(StorageUnavailable):
            async with backend.request(store):
                await approve_failed_payout(backend, store, stripe, settings, store.payouts[0], "admin_two", proposal["id"], at=at)
        monkeypatch.setattr(backend, "save", original)
        async with backend.request(store):
            payout = store.payouts[0]
            assert not payout["allocations"][0].get("reversal_id") and balance(store, "seller") == 0
            assert payout["failed_payout_recovery"]["status"] == "approved"
            with pytest.raises(FinanceConflict):
                await approve_failed_payout(backend, store, stripe, settings, payout, "admin_two", proposal["id"], at=at)
        assert await review(backend, store, settings, stripe, at) == "release"
        assert provider.reversals == 1 and balance(store, "seller") == 850
    asyncio.run(run())


def test_recovery_does_not_make_refunded_sale_withdrawable(database, provider):
    backend, dsn = database
    async def run():
        store, settings, stripe, at = await seed(backend, dsn, provider)
        await review(backend, store, settings, stripe, at)
        async with backend.request(store):
            store.orders[0]["refund_status"] = "completed"
            await backend.save()
        assert await review(backend, store, settings, stripe, at) == "release"
        assert balance(store, "seller") == 0
        async with backend.request(store):
            with pytest.raises(FinanceConflict):
                request_payout(store, settings, "seller", 850, at=at)
    asyncio.run(run())


def test_route_requires_admin_fresh_mfa_and_sandbox(monkeypatch):
    from app import main
    request = Request({"type": "http", "method": "POST", "path": "/admin", "headers": [], "session": {}})
    monkeypatch.setattr(main, "current_user", lambda r: {"id": "admin_one"})
    monkeypatch.setattr(main, "is_admin", lambda user: False)
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.admin_recover_failed_payout(request, "missing", "propose", ""))
    assert error.value.status_code == 403
    monkeypatch.setattr(main, "is_admin", lambda user: True)
    monkeypatch.setattr(main, "database_store", object())
    monkeypatch.setattr(main, "sandbox_payouts_ready", lambda settings: False)
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.admin_recover_failed_payout(request, "missing", "propose", ""))
    assert error.value.status_code == 409
    monkeypatch.setattr(main, "sandbox_payouts_ready", lambda settings: True)
    monkeypatch.setattr(main.mfa, "recent", lambda r: False)
    response = asyncio.run(main.admin_recover_failed_payout(request, "missing", "propose", ""))
    assert response.status_code == 303 and response.headers["location"] == "/security/mfa?next=/admin"
