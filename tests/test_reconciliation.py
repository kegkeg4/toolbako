import asyncio
from dataclasses import replace
from datetime import timedelta
from urllib.parse import parse_qs

import httpx
import pytest

from app.database import StorageUnavailable
from app.finance import FinanceConflict, balance
from app.payout_worker import run_step
from app.reconciliation import approve_recovery, propose_recovery
from test_database import database, query
from test_finance import NOW, Provider, seed


class RecoveryProvider(Provider):
    def __init__(self):
        super().__init__()
        self.records = []
        self.list_requests = []
        self.duplicate = False

    def __call__(self, request):
        path = request.url.path
        data = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        if request.method == "GET" and path in {"/v1/transfers", "/v1/payouts"}:
            self.list_requests.append(request)
            objects = [r for r in self.records if r["object"] == ("transfer" if path.endswith("transfers") else "payout")]
            if self.duplicate and objects:
                objects.append({**objects[0], "id": objects[0]["id"] + "duplicate"})
            return httpx.Response(200, json={"object": "list", "data": objects, "has_more": False})
        if request.method == "POST" and path in {"/v1/transfers", "/v1/payouts"}:
            if path.endswith("transfers"):
                remote = {"id": "tr_finance", "object": "transfer", "livemode": False,
                    "amount": int(data["amount"]), "currency": "jpy", "destination": data["destination"],
                    "source_transaction": data["source_transaction"], "transfer_group": data["transfer_group"],
                    "metadata": {"allocation_id": data["metadata[allocation_id]"], "payout_id": data["metadata[payout_id]"]},
                    "reversed": False, "amount_reversed": 0}
            else:
                self.payout_metadata = {"payout_id": data["metadata[payout_id]"]}
                remote = {**self.bank_object(), "object": "payout", "livemode": False,
                          "automatic": False, "method": "standard", "destination": "ba_fixture"}
            self.records.append(remote)
            try:
                super().__call__(request)
            except httpx.ReadTimeout:
                raise
            return httpx.Response(200, json=remote)
        return super().__call__(request)


@pytest.fixture
def recovery_provider(monkeypatch):
    provider = RecoveryProvider()
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(provider), **kwargs))
    return provider


async def uncertain(backend, dsn, provider, phase):
    store, settings, stripe, payout_id = await seed(backend, dsn)
    settings = replace(settings, admin_user_ids=("admin_one", "admin_two"))
    if phase == "bank":
        assert await run_step(backend, store, stripe, settings, at=NOW) == "transferring"
        provider.timeout_payout = True
    else:
        provider.timeout_transfer = True
    assert await run_step(backend, store, stripe, settings, at=NOW) == "review"
    return store, settings, stripe, payout_id


@pytest.mark.parametrize("phase", ["transfer", "bank"])
def test_two_person_recovery_uses_get_only_and_never_resends(database, recovery_provider, phase):
    backend, dsn = database
    async def run():
        store, settings, stripe, payout_id = await uncertain(backend, dsn, recovery_provider, phase)
        async with backend.request(store):
            payout = store.payouts[0]
            proposal = await propose_recovery(backend, store, stripe, settings, payout, "admin_one", at=NOW)
            assert payout["status"] == "review" and balance(store, "seller") == 0
            for actor in ("admin_one", "outsider"):
                with pytest.raises(FinanceConflict):
                    await approve_recovery(backend, store, stripe, settings, payout, actor, proposal["id"], at=NOW)
            assert await approve_recovery(backend, store, stripe, settings, payout, "admin_two", proposal["id"], at=NOW) == ("bank_pending" if phase == "bank" else "transferring")
            assert balance(store, "seller") == 0
        assert recovery_provider.transfers == 1
        assert recovery_provider.payouts == (1 if phase == "bank" else 0)
        assert len(recovery_provider.list_requests) == 2
        if phase == "bank":
            assert all(r.headers["Stripe-Account"] == "acct_seller" for r in recovery_provider.list_requests)
        assert query(dsn, "select count(*) from toolbako_runtime.stripe_operations where status='unknown'") == [(0,)]
        assert query(dsn, "select count(*) from toolbako_runtime.audit_events where action in ('payout.reconciliation_proposed','payout.reconciliation_approved')") == [(2,)]
        recovery_provider.timeout_payout = False
        recovery_provider.payout_status = "paid"
        for _ in range(2):
            await run_step(backend, store, stripe, settings, at=NOW)
        async with backend.request(store):
            assert store.payouts[0]["status"] == "paid"
        assert recovery_provider.transfers == recovery_provider.payouts == 1
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["absent", "duplicate", "amount", "account", "live", "source", "metadata", "malformed_metadata", "reversed"])
def test_invalid_or_ambiguous_provider_result_keeps_unknown_and_reservation(database, recovery_provider, failure):
    backend, dsn = database
    async def run():
        store, settings, stripe, payout_id = await uncertain(backend, dsn, recovery_provider, "transfer")
        record = recovery_provider.records[0]
        if failure == "absent": recovery_provider.records = []
        elif failure == "duplicate": recovery_provider.duplicate = True
        elif failure == "amount": record["amount"] += 1
        elif failure == "account": record["destination"] = "acct_wrong"
        elif failure == "live": record["livemode"] = True
        elif failure == "source": record["source_transaction"] = "ch_wrong"
        elif failure == "metadata": record["metadata"]["payout_id"] = "another-payout"
        elif failure == "malformed_metadata": record["metadata"] = ["invalid"]
        elif failure == "reversed": record["amount_reversed"] = 1
        async with backend.request(store):
            with pytest.raises(FinanceConflict):
                await propose_recovery(backend, store, stripe, settings, store.payouts[0], "admin_one", at=NOW)
            assert balance(store, "seller") == 0 and store.payouts[0]["status"] == "review"
        assert query(dsn, "select count(*) from toolbako_runtime.stripe_operations where status='unknown'") == [(1,)]
        assert recovery_provider.transfers == 1
    asyncio.run(run())


def test_approval_rechecks_remote_facts_and_rejects_expired_or_stale_proposals(database, recovery_provider):
    backend, dsn = database
    async def run():
        store, settings, stripe, payout_id = await uncertain(backend, dsn, recovery_provider, "transfer")
        async with backend.request(store):
            payout = store.payouts[0]
            proposal = await propose_recovery(backend, store, stripe, settings, payout, "admin_one", at=NOW)
            for proposal_id, at in (("stale-form", NOW), (proposal["id"], NOW + timedelta(minutes=31))):
                with pytest.raises(FinanceConflict):
                    await approve_recovery(backend, store, stripe, settings, payout, "admin_two", proposal_id, at=at)
            recovery_provider.records[0]["source_transaction"] = "ch_changed"
            with pytest.raises(FinanceConflict):
                await approve_recovery(backend, store, stripe, settings, payout, "admin_two", proposal["id"], at=NOW)
        assert query(dsn, "select count(*) from toolbako_runtime.stripe_operations where status='unknown'") == [(1,)]
    asyncio.run(run())


def test_journal_and_state_recovery_roll_back_together_on_commit_failure(database, recovery_provider, monkeypatch):
    backend, dsn = database
    async def run():
        store, settings, stripe, payout_id = await uncertain(backend, dsn, recovery_provider, "transfer")
        async with backend.request(store):
            proposal = await propose_recovery(backend, store, stripe, settings, store.payouts[0], "admin_one", at=NOW)
        async def fail(conn):
            raise RuntimeError("simulated commit failure")
        monkeypatch.setattr(backend, "_save", fail)
        with pytest.raises(StorageUnavailable):
            async with backend.request(store):
                await approve_recovery(backend, store, stripe, settings, store.payouts[0], "admin_two", proposal["id"], at=NOW)
        assert query(dsn, "select count(*) from toolbako_runtime.stripe_operations where status='unknown'") == [(1,)]
        async with backend.request(store):
            assert store.payouts[0]["status"] == "review"
            assert store.payouts[0]["reconciliation"]["status"] == "proposed"
            assert balance(store, "seller") == 0
    asyncio.run(run())


def test_incomplete_pagination_cannot_authorize_recovery(monkeypatch):
    from app.integrations import StripeIntegration
    stripe = StripeIntegration("sk_test_fixture", "https://example.test")
    async def repeated(path, **kwargs):
        return {"object": "list", "data": [{"id": "tr_repeat"}], "has_more": True}
    monkeypatch.setattr(stripe, "_get", repeated)
    with pytest.raises(RuntimeError):
        asyncio.run(stripe.list_financial_objects("transfers"))
