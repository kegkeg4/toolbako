"""Existing durable case executor; all HTTP is synthetic and PG is local."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest

from app.database import StorageUnavailable, content_hash
from app.finance import FinanceConflict
from app.integrations import StripeOutcomeUnknown
from app.live_setup_plan import STRIPE_API_VERSION
from app.mfa import SupabaseMFA
from app.paid_out_refund_execution import FIELD, _key, _data, _commit_opening, _execute_live
from test_database import database, query
from test_paid_out_refund_execution import Provider, seed, open_case, propose, approve
from test_paid_out_refund import facts


class LiveProvider(Provider):
    def __init__(self):
        super().__init__(facts("live"))
        self.before_post = self.on_balance = None
        self.zero_timeout = False
    def __call__(self, request):
        if request.method == "GET" and request.url.path == "/v1/balance" and self.on_balance:
            self.on_balance()
        if request.method == "POST":
            assert request.headers["Stripe-Version"] == STRIPE_API_VERSION
            if self.before_post: self.before_post(request)
            if self.zero_timeout:
                self.requests.append(request)
                raise httpx.ReadTimeout("synthetic", request=request)
        try:
            return super().__call__(request)
        finally:
            charge = self.bundle["payment"]["latest_charge"]
            charge["refunded"] = charge["amount_refunded"] == charge["amount"]


@pytest.fixture
def provider(monkeypatch):
    result = LiveProvider()
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(result), **kw))
    return result


async def prepared(backend, dsn, provider):
    store, settings, stripe, mfa, requests = await seed(backend, dsn, provider, mode="live")
    settings = replace(settings, stripe_connection_mode="live_financial", stripe_live_refunds_enabled=True)
    stripe.api_version = STRIPE_API_VERSION
    mfa = SupabaseMFA(settings, mfa.sessions)
    for request in requests.values(): request.scope["method"] = "POST"
    args = store, settings, stripe, mfa, requests
    async with backend.request(store):
        await open_case(backend, args)
        proposal = await propose(backend, args)
    return args, proposal


def test_one_refund_commits_consumed_review_before_post_and_never_changes_paid_evidence(database, provider):
    backend, dsn = database
    async def run():
        args, p = await prepared(backend, dsn, provider)
        store = args[0]
        before_payouts = query(dsn, "select * from toolbako_runtime.finance_payouts")
        before_allocations = query(dsn, "select * from toolbako_runtime.finance_allocations")
        case = store.orders[0][FIELD]
        def before(request):
            assert request.headers["idempotency-key"] == _key(case)
            assert query(dsn, "select status,request_hash from toolbako_runtime.stripe_operations where operation_id=%s", (_key(case),)) == [("pending", content_hash(_data(case)))]
            saved = query(dsn, "select payload from toolbako_runtime.marketplace_state where singleton")[0][0]
            assert p["id"] in saved["orders"][0][FIELD]["consumed_proposals"]
        provider.before_post = before
        async with backend.request(store):
            assert await approve(backend, args, p) == "buyer_refunded"
            assert store.orders[0][FIELD]["seller_recovery"]["confirmed"] == 0
            with pytest.raises(FinanceConflict): await approve(backend, args, p)
        assert len(provider.posts) == 1
        # Repository synchronization may update its housekeeping timestamp;
        # every paid financial field and immutable request hash must survive.
        assert before_payouts[0][:-1] == query(dsn, "select * from toolbako_runtime.finance_payouts")[0][:-1]
        assert before_allocations == query(dsn, "select * from toolbako_runtime.finance_allocations")
        assert query(dsn, "select count(*) from toolbako_runtime.email_outbox") == [(0,)]
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["off", "setup_only", "same_admin", "expired", "mfa", "banned", "amount", "scope",
    "payment_ledger", "charge_ledger", "bank_ledger", "transfer_ledger", "parent_hash", "parent_unknown", "liquidity", "other_unknown"])
def test_unproved_live_paid_out_refund_never_consumes_or_posts(database, provider, fault):
    backend, dsn = database
    async def run():
        args, p = await prepared(backend, dsn, provider)
        store, settings, stripe, mfa, requests = args
        if fault in {"off", "setup_only"}:
            settings = replace(settings, **({"stripe_live_refunds_enabled": False} if fault == "off" else {"stripe_connection_mode": "live_setup_only"}))
            args = store, settings, stripe, SupabaseMFA(settings, mfa.sessions), requests
        async with backend.request(store):
            case = store.orders[0][FIELD]
            if fault == "expired": case["proposal"]["proposed_at"] -= timedelta(hours=1)
            if fault == "mfa": store.account_sessions[requests["admin_two"].session["sid"]]["mfa_verified_at"] -= timedelta(hours=1)
            if fault == "banned": store.registered_users["admin_one"]["is_banned"] = True
            if fault == "amount": case["case"]["refund_amount"] -= 1
            if fault == "scope": case["scope"]["platform_account"] = "acct_wrong"
            if fault == "payment_ledger": provider.bundle["payment"]["metadata"]["ledger_id"] = "wrong"
            if fault == "charge_ledger": provider.bundle["payment"]["latest_charge"]["metadata"]["ledger_id"] = "wrong"
            if fault == "bank_ledger": provider.bundle["provider_payout"]["metadata"]["ledger_id"] = "wrong"
            if fault == "transfer_ledger": provider.bundle["transfer"]["metadata"]["ledger_id"] = "wrong"
            if fault in {"parent_hash", "parent_unknown"}:
                await backend._connection.execute("update toolbako_runtime.stripe_operations set " + ("request_hash='bad'" if fault == "parent_hash" else "status='unknown',response=null") + " where endpoint='payouts'")
            if fault == "liquidity": provider.available = 999
            if fault == "other_unknown": await backend.begin_operation("other-refund", "refunds", {"amount": "1"})
            with pytest.raises((FinanceConflict, RuntimeError)):
                await approve(backend, args, p, who="admin_one" if fault == "same_admin" else "admin_two")
            assert not case["consumed_proposals"]
        assert not provider.posts
    asyncio.run(run())


@pytest.mark.parametrize("expiry", ["mfa", "approval", "admin"])
def test_final_get_cannot_expire_auth_and_then_post(database, provider, expiry):
    backend, dsn = database
    async def run():
        args, p = await prepared(backend, dsn, provider)
        store, settings, stripe, mfa, requests = args
        async with backend.request(store):
            case = store.orders[0][FIELD]
            def expire():
                if case["status"] != "submitting": return
                if expiry == "mfa": store.account_sessions[requests["admin_two"].session["sid"]]["mfa_verified_at"] -= timedelta(hours=1)
                elif expiry == "approval": case["proposal"]["proposed_at"] -= timedelta(hours=1)
                else: store.registered_users["admin_one"]["is_banned"] = True
            provider.on_balance = expire
            with pytest.raises(StripeOutcomeUnknown): await approve(backend, args, p)
        assert not provider.posts
        assert query(dsn, "select status from toolbako_runtime.stripe_operations where operation_id=%s", (_key(case),)) == [("unknown",)]
    asyncio.run(run())


@pytest.mark.parametrize("zero", [False, True])
def test_unknown_preserves_same_intent_and_only_get_recovery_without_flag(database, provider, zero):
    backend, dsn = database
    async def run():
        args, p = await prepared(backend, dsn, provider)
        store, settings, stripe, mfa, requests = args
        provider.timeout, provider.zero_timeout = not zero, zero
        async with backend.request(store):
            with pytest.raises(StripeOutcomeUnknown): await approve(backend, args, p)
        settings = replace(settings, stripe_connection_mode="live_read_only", stripe_live_refunds_enabled=False)
        args = store, settings, stripe, SupabaseMFA(settings, mfa.sessions), requests
        async with backend.request(store):
            with pytest.raises(FinanceConflict): await propose(backend, args)
            if zero:
                with pytest.raises(FinanceConflict): await propose(backend, args, action="reconcile")
            else:
                p = await propose(backend, args, action="reconcile")
                assert await approve(backend, args, p) == "buyer_refunded"
        assert len(provider.posts) == 1
    asyncio.run(run())


@pytest.mark.parametrize("after", [False, True])
def test_intent_commit_loss_never_sends(database, provider, monkeypatch, after):
    backend, dsn = database
    async def run():
        args, p = await prepared(backend, dsn, provider)
        original = backend.begin_operation
        async def fail(*a, **kw):
            if after: await original(*a, **kw)
            raise StorageUnavailable("synthetic commit loss")
        async with backend.request(args[0]):
            with monkeypatch.context() as patch:
                patch.setattr(backend, "begin_operation", fail)
                with pytest.raises(StorageUnavailable): await approve(backend, args, p)
        assert not provider.posts
    asyncio.run(run())


@pytest.mark.parametrize("after", [False, True])
def test_result_commit_loss_preserves_same_intent_and_paid_state(database, provider, monkeypatch, after):
    backend, dsn = database
    async def run():
        args, p = await prepared(backend, dsn, provider)
        original = backend.reconcile_operation
        async def fail(*a, **kw):
            if after: await original(*a, **kw)
            raise StorageUnavailable("synthetic response commit loss")
        async with backend.request(args[0]):
            with monkeypatch.context() as patch:
                patch.setattr(backend, "reconcile_operation", fail)
                with pytest.raises(StorageUnavailable): await approve(backend, args, p)
        async with backend.request(args[0]):
            case = args[0].orders[0][FIELD]
            assert (await backend.operation_details(_key(case)))["status"] == ("succeeded" if after else "pending")
            if not after:
                p = await propose(backend, args, action="reconcile")
                assert await approve(backend, args, p) == "buyer_refunded"
            else: assert case["status"] == "buyer_refunded"
            assert args[0].payouts[0]["status"] == "paid"
        assert len(provider.posts) == 1
    asyncio.run(run())


def test_live_pending_provider_response_keeps_reserve_until_fresh_get_confirms(database, provider):
    backend, dsn = database
    async def run():
        args, p = await prepared(backend, dsn, provider)
        provider.outcome = "pending"
        async with backend.request(args[0]):
            assert await approve(backend, args, p) == "awaiting_confirmation"
            assert args[0].orders[0][FIELD]["buyer_refund"]["state"] != "complete"
        provider.records[0]["status"] = "succeeded"
        provider.bundle["payment"]["latest_charge"].update(amount_refunded=1000, refunded=True)
        async with backend.request(args[0]):
            p = await propose(backend, args, action="reconcile")
            assert await approve(backend, args, p) == "buyer_refunded"
        assert len(provider.posts) == 1
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["other_reserve", "duplicate_reserve", "ordinary_pending"])
def test_other_existing_reservations_cannot_be_spent_by_this_refund(database, provider, fault):
    backend, dsn = database
    async def run():
        args, p = await prepared(backend, dsn, provider)
        store = args[0]
        async with backend.request(store):
            other = deepcopy(store.orders[0])
            other.update(id="other_order", payment_reference="pi_other")
            case = other.pop(FIELD)
            if fault == "ordinary_pending":
                other["primary_refund_status"] = "pending"
            else:
                case["case"].update(id=str(uuid4()), order_id=other["id"], payment_id="pi_other")
                case["funding"].update(case_id=case["case"]["id"], reserve_id="one-reserve-1" if fault == "duplicate_reserve" else "other-reserve")
                case["eligibility"]["id"] = other["id"]
                other[FIELD] = case
            store.orders.append(other)
            if fault != "ordinary_pending": await _commit_opening(backend, store, case, "admin_one")
            else: await backend.save()
            provider.available = 1500
            with pytest.raises(FinanceConflict): await approve(backend, args, p)
        assert not provider.posts
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["unconsumed", "duplicate_consumption", "wrong_approval"])
def test_private_live_executor_rechecks_consumed_approval_before_new_intent(database, provider, fault):
    backend, dsn = database
    async def run():
        args, p = await prepared(backend, dsn, provider)
        store, settings, stripe, mfa, requests = args
        async with backend.request(store):
            case = store.orders[0][FIELD]
            case["status"] = "submitting"
            case["last_approval"] = {"id": p["id"], "action": "execute", "approved_by": "admin_two",
                "proposed_by": "admin_one", "fingerprint": p["fingerprint"]}
            if fault == "duplicate_consumption": case["consumed_proposals"] = [p["id"], p["id"]]
            elif fault == "wrong_approval":
                case["consumed_proposals"] = [p["id"]]
                case["last_approval"]["approved_by"] = "admin_one"
            with pytest.raises(FinanceConflict):
                await _execute_live(backend, store, stripe, settings, mfa, requests["admin_two"], store.orders[0], case,
                                    stripe.financial_scope, "admin_two")
            assert await backend.operation_details(_key(case)) is None
        assert not provider.posts
    asyncio.run(run())
