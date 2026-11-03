"""Future default-off refund domain with real isolated PG and mock Stripe only."""
import asyncio
from copy import deepcopy
from dataclasses import dataclass, fields, replace
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest
from starlette.requests import Request

from app.config import Settings
from app.database import StorageUnavailable, content_hash, snapshot
from app.finance import FinanceConflict
from app.integrations import StripeOutcomeUnknown
from app.live_setup_plan import STRIPE_API_VERSION
from app.mfa import SupabaseMFA
from app.paid_out_refund_execution import _commit_opening
from app.payment_reconciliation import propose_payment_recovery, approve_payment_recovery
from app.refund_resumption import propose_refund_resumption, approve_refund_resumption
from test_database import database, query
from test_finance import order as make_order
from test_payment_reconciliation import sender
from test_refund_resumption import ResumeProvider, seed_live


@dataclass(frozen=True)
class FutureRefundSettings(Settings):
    # Production Settings/runtime is root-owned and deliberately still OFF.
    stripe_live_refunds_enabled: bool = False


class LiveProvider(ResumeProvider):
    def __init__(self):
        super().__init__()
        self.available = 10000
        self.failure = None
        self.before_post = None
        self.on_balance = None
        self.refund_status = "succeeded"

    def __call__(self, req):
        if req.method == "GET" and req.url.path == "/v1/balance":
            self.requests.append(req)
            if self.on_balance: self.on_balance()
            return httpx.Response(200, json={"object": "balance", "livemode": not self.wrong_balance_mode,
                                            "available": [{"currency": "jpy", "amount": self.available}]})
        if req.method != "POST":
            return super().__call__(req)
        assert req.url.path == "/v1/refunds" and "stripe-account" not in req.headers
        assert req.headers["stripe-version"] == STRIPE_API_VERSION
        self.requests.append(req)
        data = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
        if self.before_post: self.before_post(req, data)
        if self.failure == "rejected": return httpx.Response(400, json={"error": "private provider text"})
        if self.failure == "zero_timeout": raise httpx.ReadTimeout("private network text", request=req)
        payment = self.payments[data["payment_intent"]]
        assert data["amount"] == str(payment["amount"])
        assert data["metadata[ledger_id]"] == self.scope.ledger_id
        metadata = {k[len("metadata["):-1]: v for k, v in data.items() if k.startswith("metadata[")}
        record = {"id": f"re_live_{len(self.records)}", "object": "refund", "amount": int(data["amount"]),
                  "currency": "jpy", "metadata": metadata, "payment_intent": data["payment_intent"],
                  "charge": payment["latest_charge"]["id"], "status": self.refund_status,
                  "created": int(datetime.now(timezone.utc).timestamp())}
        if self.refund_status == "succeeded":
            payment["latest_charge"].update(amount_refunded=payment["amount"], refunded=True)
        self.records.append(record)
        if self.failure == "timeout": raise httpx.ReadTimeout("private network text", request=req)
        if self.failure == "malformed": return httpx.Response(200, text="not json")
        if self.failure == "wrong_response": return httpx.Response(200, json={**record, "amount": record["amount"] - 1})
        return httpx.Response(200, json=record)


@pytest.fixture
def live_provider(monkeypatch):
    provider = LiveProvider()
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(provider), **kwargs))
    return provider


async def setup(backend, dsn, provider):
    store, original, stripe, mfa, one, scope, at = await seed_live(backend, dsn, provider)
    values = {field.name: getattr(original, field.name) for field in fields(Settings)}
    values.update(stripe_connection_mode="live_financial", stripe_api_version=STRIPE_API_VERSION)
    settings = FutureRefundSettings(**values, stripe_live_refunds_enabled=True)
    stripe.api_version = STRIPE_API_VERSION
    mfa = SupabaseMFA(settings, mfa.sessions)
    one.scope.update(method="POST", path="/admin/orders/live_order/refund-resumption/propose")
    two = Request({"type": "http", "method": "POST", "headers": [], "session": {}})
    async with backend.request(store):
        store.orders[0]["buyer_email"] = "buyer@example.test"
        mfa.sessions.establish(two, store.registered_users["admin_two"])
        store.account_sessions[two.session["sid"]].update(mfa_verified_at=at, mfa_token_expires_at=at + timedelta(hours=1))
        await backend.save()
    return store, settings, stripe, mfa, one, two, scope, at


async def propose(backend, store, settings, stripe, mfa, req):
    return await propose_refund_resumption(backend, store, stripe, settings, store.orders[0], "admin_one", mfa=mfa, request=req)


async def approve(backend, store, settings, stripe, mfa, req, proposal):
    return await approve_refund_resumption(backend, store, stripe, settings, store.orders[0], "admin_two",
                                           proposal["id"], sender(backend), mfa=mfa, request=req)


def posts(provider): return [r for r in provider.requests if r.method == "POST"]


def test_live_refund_consumes_review_and_exact_intent_before_one_post_then_commits_facts(database, live_provider):
    backend, dsn = database
    async def run():
        store, settings, stripe, mfa, one, two, scope, at = await setup(backend, dsn, live_provider)
        def before_post(req, data):
            assert req.headers["idempotency-key"] == "refund-live_order"
            assert data == {"payment_intent": "pi_live", "amount": "1000", "metadata[order_id]": "live_order", "metadata[ledger_id]": scope.ledger_id}
            assert query(dsn, "select endpoint,status,request_hash from toolbako_runtime.stripe_operations where operation_id='refund-live_order'") == [("refunds", "pending", content_hash(data))]
            persisted = query(dsn, "select payload from toolbako_runtime.marketplace_state where singleton")[0][0]
            p = persisted["orders"][0]["refund_resumption"]
            assert p["status"] == "executing" and p["proposed_by"] == "admin_one" and p["approved_by"] == "admin_two"
        live_provider.before_post = before_post
        async with backend.request(store):
            p = await propose(backend, store, settings, stripe, mfa, one)
            assert p["scope"] == scope.payload() and not posts(live_provider)
            assert await approve(backend, store, settings, stripe, mfa, two, p) == "completed"
            with pytest.raises(FinanceConflict): await approve(backend, store, settings, stripe, mfa, two, p)
            with pytest.raises(RuntimeError): await stripe.refund_payment("pi_live", "live_order", amount=1000)
        assert len(posts(live_provider)) == 1
        assert query(dsn, "select status from toolbako_runtime.stripe_operations where operation_id='refund-live_order'") == [("succeeded",)]
        assert query(dsn, "select count(*) from toolbako_runtime.email_outbox") == [(1,)]
        async with backend.request(store):
            assert store.orders[0]["refund_status"] == "completed"
            assert store.orders[0]["refund_resumption"]["status"] == "executed"
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["off", "read_only", "setup_only", "wrong_version", "spoof_actor", "missing_session", "stale_mfa",
                                    "wrong_account", "wrong_balance_mode", "insufficient", "boolean_balance", "unknown_other", "paid_out_case"])
def test_live_refund_rejects_unproved_execution_before_new_intent(database, live_provider, failure):
    backend, dsn = database
    async def run():
        store, settings, stripe, mfa, one, two, scope, at = await setup(backend, dsn, live_provider)
        if failure in {"off", "read_only", "setup_only", "wrong_version"}:
            settings = replace(settings, **({"stripe_live_refunds_enabled": False} if failure == "off" else
                {"stripe_api_version": "bad"} if failure == "wrong_version" else {"stripe_connection_mode": "live_" + failure}))
            mfa = SupabaseMFA(settings, mfa.sessions)
        async with backend.request(store):
            if failure == "missing_session": one.session.clear()
            if failure == "stale_mfa": store.account_sessions[one.session["sid"]]["mfa_verified_at"] = at - timedelta(hours=1)
            if failure == "wrong_account": live_provider.wrong_platform = True
            if failure == "wrong_balance_mode": live_provider.wrong_balance_mode = True
            if failure == "insufficient": live_provider.available = 999
            if failure == "boolean_balance": live_provider.available = True
            if failure == "unknown_other": await backend.begin_operation("another", "refunds", {"amount": "1"})
            if failure == "paid_out_case": store.orders[0]["paid_out_refund_case"] = {}
            with pytest.raises((FinanceConflict, RuntimeError)):
                await propose_refund_resumption(backend, store, stripe, settings, store.orders[0],
                    "admin_two" if failure == "spoof_actor" else "admin_one", mfa=mfa, request=one)
        assert not posts(live_provider)
        assert query(dsn, "select count(*) from toolbako_runtime.stripe_operations where operation_id='refund-live_order'") == [(0,)]
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["same_admin", "banned_proposer", "expired", "tamper_amount", "tamper_scope", "tamper_hash", "source_changed", "liquidity_changed"])
def test_live_approval_requires_fresh_distinct_review_and_immutable_payload(database, live_provider, failure):
    backend, dsn = database
    async def run():
        store, settings, stripe, mfa, one, two, scope, at = await setup(backend, dsn, live_provider)
        async with backend.request(store):
            p = await propose(backend, store, settings, stripe, mfa, one)
            if failure == "banned_proposer": store.registered_users["admin_one"]["is_banned"] = True
            if failure == "expired": p["proposed_at"] = at - timedelta(hours=1)
            if failure == "tamper_amount": p["next_amount"] = 999
            if failure == "tamper_scope": p["scope"]["ledger_id"] = str(uuid4())
            if failure == "tamper_hash": p["next_request_hash"] = "0" * 64
            if failure == "source_changed": live_provider.payments["pi_live"]["latest_charge"]["amount"] = 999
            if failure == "liquidity_changed": live_provider.available = 999
            with pytest.raises((FinanceConflict, RuntimeError)):
                await approve(backend, store, settings, stripe, mfa, one if failure == "same_admin" else two, p)
        assert not posts(live_provider)
        assert query(dsn, "select count(*) from toolbako_runtime.stripe_operations where operation_id='refund-live_order'") == [(0,)]
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["timeout", "malformed", "wrong_response"])
def test_unknown_refund_recovers_same_scoped_intent_get_only_with_flag_off(database, live_provider, failure):
    backend, dsn = database
    async def run():
        store, settings, stripe, mfa, one, two, scope, at = await setup(backend, dsn, live_provider)
        live_provider.failure = failure
        async with backend.request(store):
            p = await propose(backend, store, settings, stripe, mfa, one)
            with pytest.raises(StripeOutcomeUnknown): await approve(backend, store, settings, stripe, mfa, two, p)
        assert len(posts(live_provider)) == 1
        assert query(dsn, "select status from toolbako_runtime.stripe_operations where operation_id='refund-live_order'") == [("unknown",)]
        settings = replace(settings, stripe_live_refunds_enabled=False, stripe_connection_mode="live_read_only")
        mfa = SupabaseMFA(settings, mfa.sessions)
        async with backend.request(store):
            p = await propose_payment_recovery(backend, store, stripe, settings, store.orders[0], "refund", "admin_one", mfa=mfa, request=one)
            assert p["scope"] == scope.payload()
            assert await approve_payment_recovery(backend, store, stripe, settings, store.orders[0], "refund", "admin_two", p["id"], sender(backend), mfa=mfa, request=two) == "completed"
            with pytest.raises(FinanceConflict):
                await approve_payment_recovery(backend, store, stripe, settings, store.orders[0], "refund", "admin_two", p["id"], sender(backend), mfa=mfa, request=two)
        assert len(posts(live_provider)) == 1
        assert query(dsn, "select status from toolbako_runtime.stripe_operations where operation_id='refund-live_order'") == [("succeeded",)]
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["zero_timeout", "rejected"])
def test_zero_or_rejected_provider_outcome_never_authorizes_another_key(database, live_provider, failure):
    backend, dsn = database
    async def run():
        store, settings, stripe, mfa, one, two, scope, at = await setup(backend, dsn, live_provider)
        live_provider.failure = failure
        async with backend.request(store):
            p = await propose(backend, store, settings, stripe, mfa, one)
            with pytest.raises((StripeOutcomeUnknown, FinanceConflict)): await approve(backend, store, settings, stripe, mfa, two, p)
        async with backend.request(store):
            with pytest.raises(FinanceConflict): await propose(backend, store, settings, stripe, mfa, one)
            with pytest.raises(FinanceConflict):
                await propose_payment_recovery(backend, store, stripe, settings, store.orders[0], "refund", "admin_one", mfa=mfa, request=one)
        assert len(posts(live_provider)) == 1
        assert query(dsn, "select count(*) from toolbako_runtime.stripe_operations where endpoint='refunds'") == [(1,)]
    asyncio.run(run())


def test_paid_out_ONE_reserve_is_not_available_to_ordinary_refund(database, live_provider):
    backend, dsn = database
    async def run():
        store, settings, stripe, mfa, one, two, scope, at = await setup(backend, dsn, live_provider)
        async with backend.request(store):
            case = {"case": {"id": str(uuid4()), "refund_amount": 9500}, "scope": scope.payload(),
                    "funding": {"reserve_id": "ONE-existing-reserve"}, "eligibility": {},
                    "paid_identity_json": "synthetic immutable paid fact", "buyer_refund": {"state": "none"}}
            store.orders.append(make_order(id="reserved_order", payment_status="pending", paid_out_refund_case=case))
            await _commit_opening(backend, store, case, "admin_one")
            with pytest.raises(FinanceConflict): await propose(backend, store, settings, stripe, mfa, one)
        assert not posts(live_provider)
    asyncio.run(run())


@pytest.mark.parametrize("after_commit", [False, True])
def test_financial_intent_commit_failure_never_sends_and_preserves_safe_checkpoint(database, live_provider, monkeypatch, after_commit):
    backend, dsn = database
    async def run():
        store, settings, stripe, mfa, one, two, scope, at = await setup(backend, dsn, live_provider)
        async with backend.request(store):
            p = await propose(backend, store, settings, stripe, mfa, one)
            original = backend.begin_operation
            async def fail(*args, **kwargs):
                if after_commit: await original(*args, **kwargs)
                raise StorageUnavailable("synthetic commit response lost")
            monkeypatch.setattr(backend, "begin_operation", fail)
            with pytest.raises(StorageUnavailable): await approve(backend, store, settings, stripe, mfa, two, p)
        assert not posts(live_provider)
        assert query(dsn, "select status from toolbako_runtime.stripe_operations where operation_id='refund-live_order'") == ([("pending",)] if after_commit else [])
        async with backend.request(store):
            assert store.orders[0]["refund_resumption"]["status"] == ("executing" if after_commit else "proposed")
    asyncio.run(run())


@pytest.mark.parametrize("after_commit", [False, True])
def test_provider_success_then_atomic_result_commit_loss_has_no_second_post(database, live_provider, monkeypatch, after_commit):
    backend, dsn = database
    async def run():
        store, settings, stripe, mfa, one, two, scope, at = await setup(backend, dsn, live_provider)
        original = backend.reconcile_operation
        async def fail(*args, **kwargs):
            if after_commit: await original(*args, **kwargs)
            raise StorageUnavailable("synthetic result commit response lost")
        async with backend.request(store):
            p = await propose(backend, store, settings, stripe, mfa, one)
            monkeypatch.setattr(backend, "reconcile_operation", fail)
            with pytest.raises(StorageUnavailable): await approve(backend, store, settings, stripe, mfa, two, p)
        monkeypatch.setattr(backend, "reconcile_operation", original)
        async with backend.request(store):
            if not after_commit:
                p = await propose_payment_recovery(backend, store, stripe, settings, store.orders[0], "refund", "admin_one", mfa=mfa, request=one)
                await approve_payment_recovery(backend, store, stripe, settings, store.orders[0], "refund", "admin_two", p["id"], sender(backend), mfa=mfa, request=two)
            assert store.orders[0]["refund_status"] == "completed"
        assert len(posts(live_provider)) == 1
        assert query(dsn, "select status from toolbako_runtime.stripe_operations where operation_id='refund-live_order'") == [("succeeded",)]
    asyncio.run(run())


@pytest.mark.parametrize("expiry", ["mfa", "approval", "admin"])
def test_final_get_cannot_outlive_auth_or_approval_and_then_send(database, live_provider, expiry):
    backend, dsn = database
    async def run():
        store, settings, stripe, mfa, one, two, scope, at = await setup(backend, dsn, live_provider)
        async with backend.request(store):
            p = await propose(backend, store, settings, stripe, mfa, one)
            def expires_during_get():
                if p["status"] == "executing":
                    if expiry == "mfa":
                        store.account_sessions[two.session["sid"]]["mfa_verified_at"] = at - timedelta(hours=1)
                    elif expiry == "approval": p["proposed_at"] = at - timedelta(hours=1)
                    else: store.registered_users["admin_one"]["is_banned"] = True
            live_provider.on_balance = expires_during_get
            with pytest.raises(StripeOutcomeUnknown): await approve(backend, store, settings, stripe, mfa, two, p)
        assert not posts(live_provider)
        assert query(dsn, "select status from toolbako_runtime.stripe_operations where operation_id='refund-live_order'") == [("unknown",)]
    asyncio.run(run())


def test_each_extra_needs_new_review_and_pending_success_can_resume_without_waiting_for_webhook(database, live_provider):
    backend, dsn = database
    async def run():
        store, settings, stripe, mfa, one, two, scope, at = await setup(backend, dsn, live_provider)
        async with backend.request(store):
            order = store.orders[0]
            order.update(amount=1400, platform_fee=210, primary_platform_fee=150,
                pending_extras=[{"id": "e1", "amount": 400, "platform_fee": 60, "status": "paid",
                                 "payment_reference": "pi_extra", "created_at": at, "note": "Extra"}])
            metadata = {"order_id": order["id"], "extra_id": "e1", "ledger_id": scope.ledger_id}
            live_provider.payment("pi_extra", 400, metadata)
            payment = live_provider.payments["pi_extra"]
            payment["livemode"] = payment["latest_charge"]["livemode"] = True
            payment["latest_charge"]["metadata"] = dict(metadata)
            await backend.save()
            live_provider.refund_status = "pending"
            p = await propose(backend, store, settings, stripe, mfa, one)
            assert await approve(backend, store, settings, stripe, mfa, two, p) == "pending"
            assert len(posts(live_provider)) == 1
        live_provider.records[0]["status"] = "succeeded"
        live_provider.payments["pi_live"]["latest_charge"].update(amount_refunded=1000, refunded=True)
        live_provider.refund_status = "succeeded"
        async with backend.request(store):
            assert store.orders[0]["primary_refund_status"] == "pending"
            p = await propose(backend, store, settings, stripe, mfa, one)
            assert p["next_amount"] == 400 and p["next_operation"] == "refund-extra-live_order-e1"
            assert await approve(backend, store, settings, stripe, mfa, two, p) == "completed"
        assert [r.headers["idempotency-key"] for r in posts(live_provider)] == ["refund-live_order", "refund-extra-live_order-e1"]
        assert query(dsn, "select count(*) from toolbako_runtime.email_outbox") == [(1,)]
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["refund_ledger", "parent_ledger", "amount", "extra_refund", "unscoped_hash", "same_admin", "review_tamper"])
def test_live_get_recovery_refuses_other_payloads_and_bad_second_review(database, live_provider, failure):
    backend, dsn = database
    async def run():
        store, settings, stripe, mfa, one, two, scope, at = await setup(backend, dsn, live_provider)
        live_provider.failure = "timeout"
        async with backend.request(store):
            p = await propose(backend, store, settings, stripe, mfa, one)
            with pytest.raises(StripeOutcomeUnknown): await approve(backend, store, settings, stripe, mfa, two, p)
        async with backend.request(store):
            if failure == "refund_ledger": live_provider.records[0]["metadata"]["ledger_id"] = str(uuid4())
            if failure == "parent_ledger": live_provider.payments["pi_live"]["latest_charge"]["metadata"]["ledger_id"] = str(uuid4())
            if failure == "amount": live_provider.records[0]["amount"] = 999
            if failure == "extra_refund": live_provider.records.append({**live_provider.records[0], "id": "re_other", "metadata": {}})
            if failure == "unscoped_hash":
                data = stripe.refund_data("pi_live", "live_order", amount=1000)
                data.pop("_idempotency_key")
                await backend._connection.execute("update toolbako_runtime.stripe_operations set request_hash=%s where operation_id='refund-live_order'", (content_hash(data),))
            if failure in {"same_admin", "review_tamper"}:
                p = await propose_payment_recovery(backend, store, stripe, settings, store.orders[0], "refund", "admin_one", mfa=mfa, request=one)
                if failure == "review_tamper": p["fingerprint"] = "0" * 64
                with pytest.raises(FinanceConflict):
                    await approve_payment_recovery(backend, store, stripe, settings, store.orders[0], "refund", "admin_two", p["id"], sender(backend), mfa=mfa, request=one if failure == "same_admin" else two)
            else:
                with pytest.raises(FinanceConflict):
                    await propose_payment_recovery(backend, store, stripe, settings, store.orders[0], "refund", "admin_one", mfa=mfa, request=one)
        assert len(posts(live_provider)) == 1
        assert query(dsn, "select status from toolbako_runtime.stripe_operations where operation_id='refund-live_order'") == [("unknown",)]
    asyncio.run(run())


def test_caught_queue_error_cannot_checkpoint_completed_refund_without_journal_result(database, live_provider):
    backend, dsn = database
    async def run():
        store, settings, stripe, mfa, one, two, scope, at = await setup(backend, dsn, live_provider)
        async def broken_sender(*args):
            await sender(backend)(*args)
            raise RuntimeError("synthetic queue failure")
        async with backend.request(store):
            p = await propose(backend, store, settings, stripe, mfa, one)
            with pytest.raises(RuntimeError):
                await approve_refund_resumption(backend, store, stripe, settings, store.orders[0], "admin_two", p["id"], broken_sender, mfa=mfa, request=two)
            # Model a route that catches a domain RuntimeError as HTTP 409.
            await backend.save()
        assert query(dsn, "select status from toolbako_runtime.stripe_operations where operation_id='refund-live_order'") == [("pending",)]
        assert query(dsn, "select count(*) from toolbako_runtime.email_outbox") == [(0,)]
        async with backend.request(store):
            assert store.orders[0]["refund_resumption"]["status"] == "executing"
            assert store.orders[0]["refund_status"] != "completed"
            p = await propose_payment_recovery(backend, store, stripe, settings, store.orders[0], "refund", "admin_one", mfa=mfa, request=one)
            with pytest.raises(RuntimeError):
                await approve_payment_recovery(backend, store, stripe, settings, store.orders[0], "refund", "admin_two", p["id"], broken_sender, mfa=mfa, request=two)
            await backend.save()
        assert query(dsn, "select status from toolbako_runtime.stripe_operations where operation_id='refund-live_order'") == [("pending",)]
        assert query(dsn, "select count(*) from toolbako_runtime.email_outbox") == [(0,)]
        assert len(posts(live_provider)) == 1
    asyncio.run(run())
