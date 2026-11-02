"""Real disposable PostgreSQL + synthetic HTTP only; no real funds/mail."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.config import Settings
from app.data import DemoStore
from app.database import PostgresStateStore, StorageUnavailable
from app.finance import FinanceConflict
from app.financial_scope import FinancialScopeError
from app.integrations import StripeIntegration, StripeOutcomeUnknown
from app.mfa import SupabaseMFA
from app.security import SessionService
from app.webhooks import StripeWebhookService
from app.paid_out_refund_execution import (
    FIELD, _key, _opening_key, open_paid_out_refund_case,
    propose_paid_out_refund_action, approve_paid_out_refund_action,
    reconcile_paid_out_refund_notification,
)
from test_database import database, query, webhook_request
from test_finance import order as make_order
from test_paid_out_refund import facts


class Provider:
    def __init__(self, bundle):
        self.bundle = bundle
        self.records, self.requests, self.events = [], [], {}
        self.available, self.outcome = 5000, "succeeded"
        self.timeout, self.fail_get, self.reject, self.incomplete = False, False, False, False

    def __call__(self, request):
        self.requests.append(request)
        path, b = request.url.path, self.bundle
        if request.method == "POST":
            assert path == "/v1/refunds" and "Stripe-Account" not in request.headers
            if self.reject:
                return httpx.Response(400, json={"error": {"message": "private rejection"}})
            data = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            assert "reverse_transfer" not in data and "refund_application_fee" not in data
            record = {"object": "refund", "id": "re_case", "amount": int(data["amount"]), "currency": "jpy",
                      "payment_intent": data["payment_intent"], "charge": "ch_one", "status": self.outcome,
                      "created": int(datetime.now(timezone.utc).timestamp()),
                      "metadata": {k[9:-1]: v for k, v in data.items() if k.startswith("metadata[")}}
            self.records.append(record)
            if self.outcome == "succeeded":
                b["payment"]["latest_charge"]["amount_refunded"] = record["amount"]
            if self.timeout:
                raise httpx.ReadTimeout("synthetic secret not logged", request=request)
            return httpx.Response(200, json=deepcopy(record))
        if self.fail_get:
            raise httpx.ConnectTimeout("synthetic", request=request)
        if path.startswith("/v1/events/"):
            return httpx.Response(200, json=deepcopy(self.events[path.rsplit("/", 1)[-1]]))
        if path == "/v1/account":
            return httpx.Response(200, json=b["platform_account"])
        if path == "/v1/balance":
            return httpx.Response(200, json={**b["platform_balance"], "available": [{"currency": "jpy", "amount": self.available}]})
        if path == "/v1/payment_intents/pi_one":
            return httpx.Response(200, json=b["payment"])
        if path == "/v1/payouts/po_one":
            assert request.headers["Stripe-Account"] == "acct_seller"
            return httpx.Response(200, json=b["provider_payout"])
        if path == "/v1/transfers/tr_one":
            return httpx.Response(200, json=b["transfer"])
        if path == "/v1/transfers/tr_one/reversals":
            return httpx.Response(200, json={"object": "list", "data": [], "has_more": False})
        if path == "/v1/refunds":
            assert request.url.params["payment_intent"] == "pi_one"
            return httpx.Response(200, json={"object": "list", "data": deepcopy(self.records), "has_more": self.incomplete})
        raise AssertionError("Unexpected synthetic endpoint")

    @property
    def posts(self):
        return [r for r in self.requests if r.method == "POST"]


@pytest.fixture
def provider(monkeypatch):
    result = Provider(facts())
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(result), **kw))
    return result


async def seed(backend, dsn, provider, mode="test"):
    at = datetime.now(timezone.utc)
    b = provider.bundle = facts(mode)
    payout = b["payout"]
    payout.update(id=str(uuid4()), paid_at=at - timedelta(days=1), scheduled_for=at - timedelta(days=2))
    allocation = payout["allocations"][0]
    allocation["id"] = str(uuid4())
    b["provider_payout"]["metadata"]["payout_id"] = payout["id"]
    b["transfer"]["metadata"].update(payout_id=payout["id"], allocation_id=allocation["id"])
    scope = b["scope"]
    settings = Settings(environment="staging", demo_mode=False, database_url=dsn,
                        stripe_secret_key="sk_" + mode + "_synthetic", stripe_charge_mode="separate",
                        stripe_webhook_secret="whsec_synthetic_paid_out",
                        stripe_platform_account_id=scope.platform_account, stripe_ledger_id=scope.ledger_id,
                        site_base_url="https://example.test", admin_user_ids=("admin_one", "admin_two"))
    store = DemoStore(seed=False)
    sessions = SessionService(store, 3600)
    mfa = SupabaseMFA(settings, sessions)
    requests = {who: Request({"type": "http", "session": {}, "headers": []}) for who in settings.admin_user_ids}
    async with backend.request(store):
        await backend.bind_empty_financial_scope(scope)
        for who, request in requests.items():
            user = {"id": who, "username": who, "email_verified": True}
            store.registered_users[who] = user
            sessions.establish(request, user)
            store.account_sessions[request.session["sid"]].update(mfa_verified_at=at, mfa_token_expires_at=at + timedelta(hours=1))
        store.orders.append(make_order(id="order_one", payment_reference="pi_one", seller_id="seller_one",
            amount=1000, primary_payment_amount=1000, platform_fee=200, primary_platform_fee=200,
            status="cancel_pending", refund_status="awaiting_approval", cancel_requested_by="buyer",
            refund_cancel_accepted_by="seller_one", refund_cancel_accepted_at=at - timedelta(minutes=1),
            pending_extras=[]))
        store.payouts.append(deepcopy(payout))
        await backend.save()
    stripe = StripeIntegration(settings.stripe_secret_key, settings.site_base_url, charge_mode="separate", journal=backend, financial_scope=scope)
    return store, settings, stripe, mfa, requests


async def open_case(backend, args, **kwargs):
    store, settings, stripe, mfa, requests = args
    return await open_paid_out_refund_case(backend, store, stripe, settings, mfa, requests["admin_one"], "order_one",
        reserve_id=kwargs.pop("reserve_id", "one-reserve-1"), reserve_amount=kwargs.pop("reserve_amount", 1000),
        seller_recovery_amount=640, **kwargs)


async def propose(backend, args, **kwargs):
    store, settings, stripe, mfa, requests = args
    return await propose_paid_out_refund_action(backend, store, stripe, settings, mfa, requests["admin_one"], "order_one", **kwargs)


async def approve(backend, args, prop, who="admin_two", **kwargs):
    store, settings, stripe, mfa, requests = args
    return await approve_paid_out_refund_action(backend, store, stripe, settings, mfa, requests[who], "order_one", prop["id"], **kwargs)


async def opened(backend, dsn, provider, **kwargs):
    args = await seed(backend, dsn, provider, **kwargs)
    async with backend.request(args[0]):
        await open_case(backend, args)
        prop = await propose(backend, args)
    return args, prop


def test_persistent_case_one_refund_distinct_admins_paid_payout_unchanged(database, provider):
    backend, dsn = database
    async def run():
        args, prop = await opened(backend, dsn, provider)
        original = query(dsn, "select * from toolbako_runtime.finance_payouts")
        allocations = query(dsn, "select * from toolbako_runtime.finance_allocations")
        async with backend.request(args[0]):
            assert await approve(backend, args, prop) == "buyer_refunded"
        # New process-style repository/store read proves durable state.
        fresh = PostgresStateStore(dsn)
        store = DemoStore(seed=False)
        async with fresh.request(store):
            case = store.orders[0][FIELD]
            assert case["buyer_refund"]["confirmed"] == 1000
            assert case["buyer_refund"]["provider_reference"] == "re_case"
            assert case["seller_recovery"]["confirmed"] == 0
            assert case["seller_recovery"]["remaining"] == 640
            assert case["consumed_proposals"] == [prop["id"]]
            assert "primary_refund_status" not in store.orders[0]
            assert store.payouts[0] == provider.bundle["payout"]
            assert "private" not in str(case)
        assert query(dsn, "select * from toolbako_runtime.finance_payouts")[0][:-1] == original[0][:-1]
        assert query(dsn, "select * from toolbako_runtime.finance_allocations") == allocations
        assert query(dsn, "select count(*) from toolbako_runtime.finance_entries where kind in ('seller_refund','withdrawal_released','transfer_reversal')") == [(0,)]
        assert query(dsn, "select count(*) from toolbako_runtime.email_outbox") == [(0,)]
        assert len(provider.posts) == 1
        async with backend.request(args[0]):
            with pytest.raises(FinanceConflict): await approve(backend, args, prop)
        assert len(provider.posts) == 1
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["same_admin", "mfa", "banned", "removed_admin", "expired_proposal", "proposal_actor_mutated", "funding_mutated", "payout_mutated", "wrong_scope", "no_liquidity"])
def test_approval_guards_prevent_post(database, provider, fault):
    backend, dsn = database
    async def run():
        args, prop = await opened(backend, dsn, provider)
        store, settings, stripe, mfa, requests = args
        async with backend.request(store):
            if fault == "mfa": store.account_sessions[requests["admin_two"].session["sid"]].pop("mfa_verified_at")
            if fault == "banned": store.registered_users["admin_two"]["is_banned"] = True
            if fault == "removed_admin": store.registered_users.pop("admin_one")
            if fault == "expired_proposal": store.orders[0][FIELD]["proposal"]["proposed_at"] -= timedelta(hours=1)
            if fault == "proposal_actor_mutated": store.orders[0][FIELD]["proposal"]["proposed_by"] = "admin_two"
            if fault == "funding_mutated": store.orders[0][FIELD]["funding"]["available_amount"] += 1
            if fault == "payout_mutated": store.payouts[0]["paid_at"] -= timedelta(seconds=1)
            if fault == "wrong_scope": stripe.financial_scope = replace(stripe.financial_scope, platform_account="acct_other")
            if fault == "no_liquidity": provider.available = 999
            with pytest.raises((FinanceConflict, FinancialScopeError)):
                await approve(backend, args, prop, who="admin_one" if fault == "same_admin" else "admin_two")
        assert not provider.posts
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["no_agreement", "extra", "dispute", "not_paid", "bad_payment_mode", "insufficient_reserve", "insufficient_liquidity", "wrong_account", "existing_refund"])
def test_case_opening_rejects_unsupported_or_unproved_case(database, provider, fault):
    backend, dsn = database
    async def run():
        args = await seed(backend, dsn, provider)
        async with backend.request(args[0]):
            order = args[0].orders[0]
            if fault == "no_agreement": order["refund_cancel_accepted_by"] = None
            if fault == "extra": order["pending_extras"] = [{"id": "extra", "status": "pending"}]
            if fault == "dispute": order["dispute_status"] = "open"
            if fault == "not_paid": args[0].payouts[0]["status"] = "bank_pending"
            if fault == "bad_payment_mode": provider.bundle["payment"]["livemode"] = True
            if fault == "insufficient_liquidity": provider.available = 999
            if fault == "wrong_account": provider.bundle["platform_account"]["id"] = "acct_other"
            if fault == "existing_refund": provider.records = [{"id": "re_external"}]
            with pytest.raises((FinanceConflict, FinancialScopeError)):
                await open_case(backend, args, reserve_amount=999 if fault == "insufficient_reserve" else 1000)
        assert not provider.posts
        assert query(dsn, "select count(*) from toolbako_runtime.stripe_operations where endpoint='internal/paid-out-refund-case-v1'") == [(0,)]
    asyncio.run(run())


def test_timeout_is_durable_and_get_recovery_never_reposts(database, provider):
    backend, dsn = database
    async def run():
        args, prop = await opened(backend, dsn, provider)
        provider.timeout = True
        async with backend.request(args[0]):
            with pytest.raises(StripeOutcomeUnknown): await approve(backend, args, prop)
        async with backend.request(args[0]):
            case = args[0].orders[0][FIELD]
            assert case["status"] == "unknown" and case["consumed_proposals"] == [prop["id"]]
            assert (await backend.operation_details(_key(case)))["status"] == "unknown"
            with pytest.raises(FinanceConflict): await propose(backend, args)
            recovery = await propose(backend, args, action="reconcile")
        async with backend.request(args[0]):
            assert await approve(backend, args, recovery) == "buyer_refunded"
            assert (await backend.operation_details(_key(args[0].orders[0][FIELD])))["status"] == "succeeded"
        assert len(provider.posts) == 1
    asyncio.run(run())


def test_unknown_with_no_provider_match_never_resends(database, provider):
    backend, dsn = database
    async def run():
        args, prop = await opened(backend, dsn, provider)
        provider.timeout = True
        async with backend.request(args[0]):
            with pytest.raises(StripeOutcomeUnknown): await approve(backend, args, prop)
        provider.records.clear()
        provider.bundle["payment"]["latest_charge"]["amount_refunded"] = 0
        async with backend.request(args[0]):
            for action in ("execute", "reconcile"):
                with pytest.raises(FinanceConflict): await propose(backend, args, action=action)
        assert len(provider.posts) == 1
    asyncio.run(run())


def test_provider_pending_is_not_completion_and_recovery_uses_get(database, provider):
    backend, dsn = database
    async def run():
        args, prop = await opened(backend, dsn, provider)
        provider.outcome = "pending"
        async with backend.request(args[0]):
            assert await approve(backend, args, prop) == "awaiting_confirmation"
            assert args[0].orders[0][FIELD]["buyer_refund"]["confirmed"] == 0
        provider.records[0]["status"] = "succeeded"
        provider.bundle["payment"]["latest_charge"]["amount_refunded"] = 1000
        async with backend.request(args[0]):
            recovery = await propose(backend, args, action="reconcile")
        async with backend.request(args[0]):
            assert await approve(backend, args, recovery) == "buyer_refunded"
        assert len(provider.posts) == 1
    asyncio.run(run())


def test_live_review_does_not_enable_live_post_or_consume_approval(database, provider):
    backend, dsn = database
    async def run():
        args, prop = await opened(backend, dsn, provider, mode="live")
        async with backend.request(args[0]):
            with pytest.raises(FinanceConflict): await approve(backend, args, prop)
            assert not args[0].orders[0][FIELD]["consumed_proposals"]
        assert not provider.posts
    asyncio.run(run())


def test_case_and_opening_evidence_rollback_together(database, provider, monkeypatch):
    backend, dsn = database
    async def run():
        args = await seed(backend, dsn, provider)
        async with backend.request(args[0]):
            async def fail(*args, **kwargs): raise RuntimeError("synthetic commit failure")
            with monkeypatch.context() as patch:
                patch.setattr(backend, "_save", fail)
                with pytest.raises(RuntimeError): await open_case(backend, args)
        assert query(dsn, "select count(*) from toolbako_runtime.stripe_operations where endpoint='internal/paid-out-refund-case-v1'") == [(0,)]
        async with backend.request(args[0]): assert FIELD not in args[0].orders[0]
    asyncio.run(run())


def test_failed_intent_commit_does_not_consume_approval_or_post(database, provider, monkeypatch):
    backend, dsn = database
    async def run():
        args, prop = await opened(backend, dsn, provider)
        async with backend.request(args[0]):
            async def fail(*a, **kw): raise RuntimeError("synthetic failed checkpoint")
            with monkeypatch.context() as patch:
                patch.setattr(backend, "_save", fail)
                with pytest.raises(StorageUnavailable): await approve(backend, args, prop)
        async with backend.request(args[0]):
            case = args[0].orders[0][FIELD]
            assert not case["consumed_proposals"]
            assert await backend.operation_details(_key(case)) is None
        assert not provider.posts
    asyncio.run(run())


def test_process_style_failure_after_durable_intent_blocks_resend(database, provider, monkeypatch):
    backend, dsn = database
    async def run():
        args, prop = await opened(backend, dsn, provider)
        async def crash(path, data):
            data = dict(data)
            key = data.pop("_idempotency_key")
            await backend.begin_operation(key, path, data)
            raise RuntimeError("synthetic process loss before provider")
        async with backend.request(args[0]):
            with monkeypatch.context() as patch:
                patch.setattr(args[2], "_post", crash)
                with pytest.raises(RuntimeError): await approve(backend, args, prop)
        async with backend.request(args[0]):
            case = args[0].orders[0][FIELD]
            assert case["consumed_proposals"] == [prop["id"]]
            assert (await backend.operation_details(_key(case)))["status"] == "pending"
            with pytest.raises(FinanceConflict): await propose(backend, args)
            with pytest.raises(FinanceConflict): await propose(backend, args, action="reconcile")
        assert not provider.posts
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["wrong_parent", "wrong_ledger", "partial", "duplicate", "failed"])
def test_unknown_result_with_conflicting_get_proof_is_held(database, provider, fault):
    backend, dsn = database
    async def run():
        args, prop = await opened(backend, dsn, provider)
        provider.timeout = True
        async with backend.request(args[0]):
            with pytest.raises(StripeOutcomeUnknown): await approve(backend, args, prop)
        remote = provider.records[0]
        if fault == "wrong_parent": remote["payment_intent"] = "pi_other"
        if fault == "wrong_ledger": remote["metadata"]["ledger_id"] = str(uuid4())
        if fault == "partial": remote["amount"] = 400
        if fault == "duplicate": provider.records.append({**remote, "id": "re_duplicate"})
        if fault == "failed": remote["status"] = "failed"
        async with backend.request(args[0]):
            with pytest.raises(FinanceConflict): await propose(backend, args, action="reconcile")
            assert args[0].orders[0][FIELD]["status"] == "unknown"
        assert len(provider.posts) == 1
    asyncio.run(run())


def test_webhook_notice_is_get_only_and_preserves_two_admin_recovery(database, provider):
    backend, dsn = database
    async def run():
        args, prop = await opened(backend, dsn, provider)
        provider.timeout = True
        async with backend.request(args[0]):
            with pytest.raises(StripeOutcomeUnknown): await approve(backend, args, prop)
        event = {"id": "evt_case", "type": "refund.updated", "livemode": False, "data": {"object": deepcopy(provider.records[0])}}
        async with backend.request(args[0]):
            assert await reconcile_paid_out_refund_notification(event, args[0], backend, args[2], args[2].financial_scope)
            assert args[0].orders[0][FIELD]["status"] == "awaiting_review"
            assert args[0].orders[0][FIELD]["buyer_refund"]["confirmed"] == 0
            await backend.save()
        async with backend.request(args[0]):
            case = args[0].orders[0][FIELD]
            assert case["last_provider_notice"]["event_id"] == "evt_case"
            assert (await backend.operation_details(_key(case)))["status"] == "unknown"
            recovery = await propose(backend, args, action="reconcile")
        async with backend.request(args[0]): assert await approve(backend, args, recovery) == "buyer_refunded"
        assert len(provider.posts) == 1
        assert query(dsn, "select count(*) from toolbako_runtime.email_outbox") == [(0,)]
    asyncio.run(run())


def test_webhook_notice_wrong_case_rolls_back_without_provider_post(database, provider):
    backend, dsn = database
    async def run():
        args, prop = await opened(backend, dsn, provider)
        provider.timeout = True
        async with backend.request(args[0]):
            with pytest.raises(StripeOutcomeUnknown): await approve(backend, args, prop)
        obj = deepcopy(provider.records[0]); obj["metadata"]["paid_out_case_id"] = str(uuid4())
        event = {"id": "evt_wrong", "type": "refund.updated", "livemode": False, "data": {"object": obj}}
        async with backend.request(args[0]):
            with pytest.raises(FinanceConflict): await reconcile_paid_out_refund_notification(event, args[0], backend, args[2], args[2].financial_scope)
            assert "last_provider_notice" not in args[0].orders[0][FIELD]
        assert len(provider.posts) == 1
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["charge.refunded", "refund.updated"])
def test_legacy_signed_webhook_preserves_paid_out_case_and_get_only_recovery(database, provider, kind):
    backend, dsn = database
    async def run():
        args, prop = await opened(backend, dsn, provider)
        store, settings, stripe, _, _ = args
        provider.timeout = True
        async with backend.request(store):
            with pytest.raises(StripeOutcomeUnknown): await approve(backend, args, prop)
        obj = deepcopy(provider.bundle["payment"]["latest_charge"] if kind == "charge.refunded" else provider.records[0])
        if kind == "charge.refunded": obj["metadata"] = {"order_id": "order_one"}
        event = {"id": "evt_legacy_case", "type": kind, "livemode": False, "data": {"object": obj}}
        provider.events[event["id"]] = event
        async def no_mail(*args): raise AssertionError("Paid-out notification must not send email")
        service = StripeWebhookService(settings, store, no_mail, journal=backend, provider=stripe,
                                      paid_out_refund_reconciler=reconcile_paid_out_refund_notification)
        payouts_before = query(dsn, "select * from toolbako_runtime.finance_payouts")[0][:-1]
        async with backend.request(store):
            assert await service.handle(webhook_request(event, settings.stripe_webhook_secret)) == {"received": True}
            assert store.orders[0][FIELD]["status"] == "awaiting_review"
            assert store.orders[0]["refund_status"] == "awaiting_approval"
            assert "primary_refund_status" not in store.orders[0]
            await backend.save()
        # Duplicate delivery is acknowledged without another reconciliation or POST.
        async with backend.request(store):
            calls_before = len(provider.requests)
            assert (await service.handle(webhook_request(event, settings.stripe_webhook_secret)))["duplicate"]
            repeated = provider.requests[calls_before:]
            assert all(r.method == "GET" for r in repeated)
            assert {r.url.path for r in repeated} == {"/v1/account", "/v1/balance", "/v1/events/evt_legacy_case"}
            assert (await backend.operation_details(_key(store.orders[0][FIELD])))["status"] == "unknown"
            recovery = await propose(backend, args, action="reconcile")
        async with backend.request(store):
            assert await approve(backend, args, recovery) == "buyer_refunded"
            assert store.orders[0][FIELD]["seller_recovery"]["confirmed"] == 0
        assert len(provider.posts) == 1
        assert query(dsn, "select * from toolbako_runtime.finance_payouts")[0][:-1] == payouts_before
        assert query(dsn, "select count(*) from toolbako_runtime.finance_entries where kind in ('seller_refund','withdrawal_released','transfer_reversal')") == [(0,)]
        assert query(dsn, "select count(*) from toolbako_runtime.email_outbox") == [(0,)]
    asyncio.run(run())


@pytest.mark.parametrize("fault,status", [("wrong_case", 409), ("missing_reconciler", 503), ("not_handled", 503)])
def test_legacy_paid_out_webhook_failure_leaves_event_and_case_retryable(database, provider, fault, status):
    backend, dsn = database
    async def run():
        args, prop = await opened(backend, dsn, provider)
        store, settings, stripe, _, _ = args
        provider.timeout = True
        async with backend.request(store):
            with pytest.raises(StripeOutcomeUnknown): await approve(backend, args, prop)
        obj = deepcopy(provider.records[0])
        if fault == "wrong_case": obj["metadata"]["paid_out_case_id"] = str(uuid4())
        event = {"id": "evt_rejected_case", "type": "refund.updated", "livemode": False, "data": {"object": obj}}
        provider.events[event["id"]] = event
        async def no_mail(*args): raise AssertionError("No mail on rejected notification")
        async def not_handled(*args): return False
        callback = {"wrong_case": reconcile_paid_out_refund_notification,
                    "missing_reconciler": None, "not_handled": not_handled}[fault]
        service = StripeWebhookService(settings, store, no_mail, journal=backend, provider=stripe,
                                      paid_out_refund_reconciler=callback)
        with pytest.raises(HTTPException) as error:
            async with backend.request(store):
                await service.handle(webhook_request(event, settings.stripe_webhook_secret))
                await backend.save()
        assert error.value.status_code == status
        async with backend.request(store):
            assert event["id"] not in store.processed_webhook_events
            assert store.orders[0][FIELD]["status"] == "unknown"
            assert "last_provider_notice" not in store.orders[0][FIELD]
            assert store.orders[0]["refund_status"] == "awaiting_approval"
        assert len(provider.posts) == 1
        assert query(dsn, "select count(*) from toolbako_runtime.finance_entries where kind='seller_refund'") == [(0,)]
    asyncio.run(run())
