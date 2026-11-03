"""Real disposable localhost PG + synthetic provider, never real transfers."""
import asyncio
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest
from starlette.requests import Request

from app import live_payout_execution as execution
from app.config import Settings
from app.data import DemoStore
from app.database import StorageUnavailable, content_hash
from app.finance import FinanceConflict, balance, request_payout
from app.financial_scope import FinancialScope
from app.integrations import StripeIntegration, StripeOutcomeUnknown
from app.live_seller_onboarding import _operation_id, _parameters, _metadata
from app.live_setup_plan import STRIPE_API_VERSION
from app.mfa import SupabaseMFA
from app.payout_worker import run_step
from app.reconciliation import propose_recovery, approve_recovery
from app.security import SessionService
from test_database import database, query
from test_finance import order


@dataclass(frozen=True)
class FutureSettings(Settings):
    stripe_live_payouts_enabled: bool = False


@pytest.fixture
def live(database, monkeypatch):
    backend, dsn = database
    scope = FinancialScope("live", "acct_platform", str(uuid4()))
    seller, buyer = str(uuid4()), str(uuid4())
    settings = FutureSettings(environment="staging", demo_mode=False, database_url=dsn,
        stripe_secret_key="rk_live_SYNTHETIC", stripe_platform_account_id=scope.platform_account,
        stripe_ledger_id=scope.ledger_id, stripe_charge_mode="separate", stripe_connection_mode="live_financial",
        stripe_api_version=STRIPE_API_VERSION, stripe_live_payouts_enabled=True, admin_user_ids=(seller, buyer))
    store = DemoStore(seed=False)
    stripe = StripeIntegration(settings.stripe_secret_key, "https://synthetic.example", journal=backend,
        financial_scope=scope, charge_mode="separate", api_version=STRIPE_API_VERSION)
    sessions = SessionService(store, 3600)
    mfa = SupabaseMFA(settings, sessions)
    one, two = [Request({"type": "http", "method": "POST", "headers": [], "session": {}}) for _ in range(2)]
    at = datetime.now(timezone.utc)
    key = _operation_id(scope, seller)
    e = SimpleNamespace(backend=backend, dsn=dsn, scope=scope, settings=settings, store=store,
        stripe=stripe, mfa=mfa, one=one, two=two, seller=seller, buyer=buyer, at=at,
        calls=[], posts=[], transfers=[], payouts=[], events={}, fault=None, before_post=None, get_hook=None,
        account_fault=None, payment_fault=None, available=10000, bank_status="pending")
    e.account = {"object": "account", "id": "acct_seller", "type": "express", "country": "JP",
        "default_currency": "jpy", "charges_enabled": True, "payouts_enabled": True, "details_submitted": True,
        "metadata": _metadata(scope, seller, key), "settings": {"payouts": {"schedule": {"interval": "manual"}}},
        "capabilities": {"card_payments": "active", "transfers": "active"}}
    e.payment = {"id": "pi_finance", "object": "payment_intent", "livemode": True, "status": "succeeded",
        "currency": "jpy", "amount": 1000, "amount_received": 1000, "transfer_group": "order-order_finance",
        "metadata": {"order_id": "order_finance", "ledger_id": scope.ledger_id}, "latest_charge": {
            "id": "ch_finance", "object": "charge", "livemode": True, "payment_intent": "pi_finance",
            "paid": True, "captured": True, "status": "succeeded", "currency": "jpy", "amount": 1000,
            "amount_captured": 1000, "amount_refunded": 0, "disputed": False,
            "transfer_group": "order-order_finance", "metadata": {"order_id": "order_finance", "ledger_id": scope.ledger_id}}}
    async def initialize():
        async with backend.request(store):
            await backend.bind_empty_financial_scope(scope)
            for name, uid, req in (("seller", seller, one), ("buyer", buyer, two)):
                u = {"id": uid, "username": name, "email": name + "@example.invalid", "email_verified": True,
                     "is_banned": False, "identity_status": "unverified", "is_verified": False}
                store.registered_users[name] = u
                sessions.establish(req, u)
                store.account_sessions[req.session["sid"]].update(mfa_verified_at=at, mfa_token_expires_at=at + timedelta(hours=1))
            await backend.begin_operation(key, "accounts", _parameters(scope, seller, key))
            store.connected_accounts[seller] = {"account_id": "acct_seller", "scope": scope.payload(),
                "onboarding_operation_id": key, "payouts_enabled": True, "details_submitted": True}
            await backend.reconcile_operation(key, endpoint="accounts", request_hash=content_hash(_parameters(scope, seller, key)),
                response={"id": "acct_seller", "object": "account"})
            store.orders.append(order(seller_id=seller, buyer_id=buyer, created_at=at - timedelta(days=40)))
            payout = request_payout(store, settings, seller, 850, at=at - timedelta(days=40))
            payout["scheduled_for"] = at - timedelta(days=1)
            await backend.save()
            e.payout_id = payout["id"]
    asyncio.run(initialize())

    def provider(req):
        e.calls.append(req)
        path = req.url.path
        if req.method == "POST":
            assert path in {"/v1/transfers", "/v1/payouts"} and req.headers["stripe-version"] == STRIPE_API_VERSION
            data = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
            key = req.headers["idempotency-key"]
            journal_data = {**data, "_stripe_account": "acct_seller"} if path.endswith("payouts") else data
            assert query(dsn, "select endpoint,request_hash,status from toolbako_runtime.stripe_operations where operation_id=%s", (key,)) == [
                (path[4:], content_hash(journal_data), "pending")]
            p = query(dsn, "select payload from toolbako_runtime.marketplace_state where singleton")[0][0]["payouts"][0]
            assert p[execution.FIELD]["status"] == "executing"
            assert p[execution.FIELD]["proposed_by"] == seller and p[execution.FIELD]["approved_by"] == buyer
            e.posts.append(req)
            if e.before_post:
                e.before_post(req, data)
            if e.fault == "rejected": return httpx.Response(403, text="PRIVATE_PROVIDER_MESSAGE")
            if e.fault == "timeout_before": raise httpx.ReadTimeout("PRIVATE_PROVIDER_MESSAGE", request=req)
            metadata = {k[9:-1]: v for k, v in data.items() if k.startswith("metadata[")}
            if path.endswith("transfers"):
                assert "stripe-account" not in req.headers
                remote = {"id": "tr_live", "object": "transfer", "livemode": True, "amount": int(data["amount"]),
                    "currency": "jpy", "metadata": metadata, "destination": data["destination"],
                    "source_transaction": data["source_transaction"], "transfer_group": data["transfer_group"],
                    "reversed": False, "amount_reversed": 0, "created": int(datetime.now(timezone.utc).timestamp())}
                e.transfers.append(remote)
            else:
                assert req.headers["stripe-account"] == "acct_seller"
                remote = {"id": "po_live", "object": "payout", "livemode": True, "amount": int(data["amount"]),
                    "currency": "jpy", "metadata": metadata, "automatic": False, "method": "standard",
                    "type": "bank_account", "status": e.bank_status, "created": int(datetime.now(timezone.utc).timestamp())}
                e.payouts.append(remote)
            if e.fault == "timeout_after": raise httpx.ReadTimeout("PRIVATE_PROVIDER_MESSAGE", request=req)
            if e.fault == "malformed": return httpx.Response(200, text="PRIVATE_INVALID")
            if e.fault == "wrong_response": return httpx.Response(200, json={**remote, "amount": remote["amount"] - 1})
            return httpx.Response(200, json=remote)
        assert req.method == "GET"
        if e.get_hook:
            e.get_hook(req)
        if path == "/v1/account": return httpx.Response(200, json={"object": "account", "id": scope.platform_account})
        if path == "/v1/balance": return httpx.Response(200, json={"object": "balance", "livemode": True,
            "available": [{"currency": "jpy", "amount": e.available}]})
        if path == "/v1/accounts/acct_seller": return httpx.Response(200, json=e.account)
        if path.startswith("/v1/accounts/acct_seller/capabilities/"):
            name = path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={"object": "capability", "id": name, "account": "acct_seller", "requested": True,
                                           "status": e.account["capabilities"][name]})
        if path == "/v1/payment_intents/pi_finance": return httpx.Response(200, json=e.payment)
        if path.startswith("/v1/events/") and path.rsplit("/", 1)[-1] in e.events:
            assert req.headers["stripe-account"] == "acct_seller"
            return httpx.Response(200, json=e.events[path.rsplit("/", 1)[-1]])
        if path == "/v1/transfers": return httpx.Response(200, json={"object": "list", "data": e.transfers, "has_more": False})
        if path == "/v1/payouts":
            assert req.headers["stripe-account"] == "acct_seller"
            return httpx.Response(200, json={"object": "list", "data": e.payouts, "has_more": False})
        if path == "/v1/payouts/po_live":
            assert req.headers["stripe-account"] == "acct_seller"
            return httpx.Response(200, json={**e.payouts[0], "status": e.bank_status})
        raise AssertionError("Unexpected synthetic request")
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(provider), **kw))
    return e


async def propose(e):
    return await execution.propose_live_payout(e.backend, e.store, e.stripe, e.settings, e.store.payouts[0], e.seller,
                                               mfa=e.mfa, request=e.one)


async def approve(e, proposal):
    return await execution.approve_live_payout(e.backend, e.store, e.stripe, e.settings, e.store.payouts[0], e.buyer,
                                               proposal["id"], mfa=e.mfa, request=e.two)


async def transfer(e):
    async with e.backend.request(e.store):
        return await approve(e, await propose(e))


def test_live_transfer_and_bank_each_require_fresh_distinct_approval_and_atomic_intent(live):
    e = live
    async def run():
        assert await run_step(e.backend, e.store, e.stripe, e.settings, payout_id=e.payout_id) == "approval_required"
        assert not e.posts
        assert await transfer(e) == "transferring"
        assert e.store.payouts[0][execution.FIELD]["amount"] == 690
        assert e.store.payouts[0][execution.FIELD]["amount"] != e.store.payouts[0]["amount"]
        assert e.store.payouts[0][execution.FIELD]["currency"] == "jpy"
        assert len(e.posts) == 1
        async with e.backend.request(e.store):
            old = e.store.payouts[0][execution.FIELD]
            with pytest.raises(FinanceConflict): await approve(e, old)
            bank = await propose(e)
            assert bank["amount"] == 690 and bank["currency"] == "jpy"
            assert await approve(e, bank) == "bank_pending"
            assert e.store.payouts[0].get("paid_at") is None
        assert len(e.posts) == 2
        e.bank_status = "paid"
        assert await run_step(e.backend, e.store, e.stripe, e.settings, payout_id=e.payout_id) == "paid"
        assert len(e.posts) == 2
        assert query(e.dsn, "select count(*) from toolbako_runtime.stripe_operations where status in ('unknown','pending')") == [(0,)]
        assert query(e.dsn, "select count(*) from toolbako_runtime.email_outbox") == [(0,)]
        with pytest.raises(RuntimeError): await e.stripe.create_bank_payout(e.store.payouts[0])
    asyncio.run(run())


def test_bank_notification_requires_scoped_succeeded_intent_and_receipt_without_writes(live):
    e = live
    async def run():
        await transfer(e)
        async with e.backend.request(e.store):
            await approve(e, await propose(e))
            payout = e.store.payouts[0]
            remote = deepcopy(e.payouts[0])
            key, _, data = execution.request_data(e.scope, payout)
            before = deepcopy(payout)
            revision = query(e.dsn, "select revision from toolbako_runtime.marketplace_state where singleton")
            calls = len(e.calls)
            proof = await execution.validate_live_bank_notification(e.backend, e.stripe, e.scope, payout, remote)
            assert proof["id"] == payout["provider_payout_id"] and proof["amount"] == 690
            for fault in ("missing_ledger", "wrong_ledger", "wrong_platform", "wrong_id", "wrong_mode"):
                bad = deepcopy(remote)
                if fault == "missing_ledger": bad["metadata"].pop("ledger_id")
                if fault == "wrong_ledger": bad["metadata"]["ledger_id"] = str(uuid4())
                if fault == "wrong_platform": bad["metadata"]["platform_account_id"] = "acct_other"
                if fault == "wrong_id": bad["id"] = "po_other"
                if fault == "wrong_mode": bad["livemode"] = False
                with pytest.raises(FinanceConflict):
                    await execution.validate_live_bank_notification(e.backend, e.stripe, e.scope, payout, bad)
            for fault in ("legacy_request", "legacy_receipt", "pending", "wrong_receipt_id"):
                async with e.backend._connection.transaction(force_rollback=True):
                    if fault == "legacy_request":
                        old = {k: v for k, v in data.items() if k not in {"metadata[ledger_id]", "metadata[platform_account_id]"}}
                        await e.backend._connection.execute("update toolbako_runtime.stripe_operations set request_hash=%s where operation_id=%s", (content_hash(old), key))
                    elif fault == "pending":
                        await e.backend._connection.execute("update toolbako_runtime.stripe_operations set status='pending',response=null where operation_id=%s", (key,))
                    else:
                        receipt = deepcopy(remote)
                        if fault == "legacy_receipt": receipt["metadata"].pop("ledger_id")
                        else: receipt["id"] = "po_other"
                        await e.backend._connection.execute("update toolbako_runtime.stripe_operations set response=%s::jsonb where operation_id=%s", (json.dumps(receipt), key))
                    with pytest.raises(FinanceConflict):
                        await execution.validate_live_bank_notification(e.backend, e.stripe, e.scope, payout, remote)
            assert payout == before and len(e.calls) == calls
            assert query(e.dsn, "select revision from toolbako_runtime.marketplace_state where singleton") == revision
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["off", "read_only", "setup_only", "api_version", "actor", "get", "session", "mfa", "banned",
    "unbound", "scope", "mapping", "mapping_duplicate", "ownership", "manual", "capability", "source", "source_ledger", "charge_ledger", "unknown_other",
    "paid_out_reserve", "pending_refund", "partial_extra"])
def test_unverified_context_or_facts_cannot_create_a_live_intent(live, fault):
    e = live
    async def run():
        if fault in {"off", "read_only", "setup_only", "api_version"}:
            e.settings = replace(e.settings, **({"stripe_live_payouts_enabled": False} if fault == "off" else
                {"stripe_api_version": "bad"} if fault == "api_version" else {"stripe_connection_mode": "live_" + fault}))
            e.mfa = SupabaseMFA(e.settings, e.mfa.sessions)
        async with e.backend.request(e.store):
            if fault == "get": e.one.scope["method"] = "GET"
            if fault == "session": e.one.session.clear()
            if fault == "mfa": e.store.account_sessions[e.one.session["sid"]]["mfa_verified_at"] = e.at - timedelta(hours=1)
            if fault == "banned": e.store.registered_users["buyer"]["is_banned"] = True
            if fault == "unbound":
                await e.backend._connection.execute("delete from toolbako_runtime.stripe_operations where operation_id='toolbako-financial-scope-v1'")
            if fault == "scope": e.stripe.financial_scope = FinancialScope("live", "acct_other", e.scope.ledger_id)
            if fault == "mapping": e.store.connected_accounts[e.seller]["scope"]["ledger_id"] = str(uuid4())
            if fault == "mapping_duplicate": e.store.connected_accounts[e.buyer] = deepcopy(e.store.connected_accounts[e.seller])
            if fault == "ownership": e.account["metadata"]["user_id"] = e.buyer
            if fault == "manual": e.account["settings"]["payouts"]["schedule"]["interval"] = "daily"
            if fault == "capability": e.account["capabilities"]["transfers"] = "pending"
            if fault == "source": e.payment["amount"] = 999
            if fault == "source_ledger": e.payment["metadata"]["ledger_id"] = str(uuid4())
            if fault == "charge_ledger": e.payment["latest_charge"]["metadata"]["ledger_id"] = str(uuid4())
            if fault == "unknown_other": await e.backend.begin_operation("synthetic-other", "refunds", {"amount": "1"})
            if fault == "paid_out_reserve": e.store.orders.append({"paid_out_refund_case": {"buyer_refund": {"state": "pending"}}})
            if fault == "pending_refund": e.store.orders.append({"primary_refund_status": "pending"})
            if fault == "partial_extra": e.store.orders.append({"pending_extras": [{"refund_status": "partial"}]})
            with pytest.raises((FinanceConflict, RuntimeError)):
                await execution.propose_live_payout(e.backend, e.store, e.stripe, e.settings, e.store.payouts[0],
                    e.buyer if fault == "actor" else e.seller, mfa=e.mfa, request=e.one)
        assert not e.posts
        assert query(e.dsn, "select count(*) from toolbako_runtime.stripe_operations where endpoint in ('transfers','payouts')") == [(0,)]
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["same_admin", "expired", "tamper", "display_amount", "display_currency", "invented", "source_changed", "mapping_changed", "proposer_banned"])
def test_consumption_requires_durable_proposal_and_current_independent_review(live, fault):
    e = live
    async def run():
        async with e.backend.request(e.store):
            p = await propose(e)
            if fault == "same_admin": e.two = e.one
            if fault == "expired": p["proposed_at"] -= timedelta(hours=1)
            if fault == "tamper": p["request_hash"] = "f" * 64
            if fault == "display_amount": p["amount"] -= 1
            if fault == "display_currency": p["currency"] = "usd"
            if fault == "invented": p["id"] = str(uuid4())
            if fault == "source_changed": e.payment["latest_charge"]["disputed"] = True
            if fault == "mapping_changed": e.store.connected_accounts[e.seller]["account_id"] = "acct_elsewhere"
            if fault == "proposer_banned": e.store.registered_users["seller"]["is_banned"] = True
            with pytest.raises((FinanceConflict, RuntimeError)): await approve(e, p)
        assert not e.posts
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["timeout_after", "malformed", "wrong_response"])
@pytest.mark.parametrize("phase", ["transfer", "bank"])
def test_unknown_creation_is_get_only_recoverable_with_new_two_person_review(live, fault, phase):
    e = live
    async def run():
        if phase == "bank": await transfer(e)
        e.fault = fault
        async with e.backend.request(e.store):
            with pytest.raises(StripeOutcomeUnknown): await approve(e, await propose(e))
        post_count = len(e.posts)
        e.fault = None
        # Turning off new money movement must not disable GET-only recovery.
        e.settings = replace(e.settings, stripe_live_payouts_enabled=False, stripe_connection_mode="live_read_only")
        e.mfa = SupabaseMFA(e.settings, e.mfa.sessions)
        async with e.backend.request(e.store):
            p = e.store.payouts[0]
            before = balance(e.store, e.seller)
            r = await propose_recovery(e.backend, e.store, e.stripe, e.settings, p, e.seller, mfa=e.mfa, request=e.one)
            assert r["amount"] == 690 and r["currency"] == "jpy"
            assert p["status"] == "review"
            with pytest.raises(FinanceConflict):
                await approve_recovery(e.backend, e.store, e.stripe, e.settings, p, e.seller, r["id"], mfa=e.mfa, request=e.one)
            result = await approve_recovery(e.backend, e.store, e.stripe, e.settings, p, e.buyer, r["id"], mfa=e.mfa, request=e.two)
            assert result == ("transferring" if phase == "transfer" else "bank_pending")
            assert balance(e.store, e.seller) == before == 0
        assert len(e.posts) == post_count
        assert query(e.dsn, "select count(*) from toolbako_runtime.stripe_operations where status in ('pending','unknown')") == [(0,)]
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["absent", "duplicate", "wrong_ledger", "wrong_amount", "wrong_mode"])
def test_absent_or_ambiguous_unknown_stays_reserved_without_reposting(live, fault):
    e = live
    async def run():
        e.fault = "timeout_after"
        async with e.backend.request(e.store):
            with pytest.raises(StripeOutcomeUnknown): await approve(e, await propose(e))
        if fault == "absent": e.transfers.clear()
        elif fault == "duplicate": e.transfers.append({**e.transfers[0], "id": "tr_duplicate"})
        elif fault == "wrong_ledger": e.transfers[0]["metadata"]["ledger_id"] = str(uuid4())
        elif fault == "wrong_amount": e.transfers[0]["amount"] -= 1
        else: e.transfers[0]["livemode"] = False
        async with e.backend.request(e.store):
            with pytest.raises(FinanceConflict):
                await propose_recovery(e.backend, e.store, e.stripe, e.settings, e.store.payouts[0], e.seller, mfa=e.mfa, request=e.one)
            with pytest.raises(FinanceConflict): await propose(e)
            assert e.store.payouts[0]["status"] == "review" and balance(e.store, e.seller) == 0
        assert len(e.posts) == 1
        assert query(e.dsn, "select count(*) from toolbako_runtime.stripe_operations where status='unknown'") == [(1,)]
    asyncio.run(run())


def test_bank_requires_all_sources_and_fresh_available_yen(live):
    e = live
    async def run():
        await transfer(e)
        async with e.backend.request(e.store):
            for amount in (0, True):
                e.available = amount
                with pytest.raises(FinanceConflict): await propose(e)
            e.available = 10000
            e.payment["latest_charge"]["amount_refunded"] = 1
            with pytest.raises(FinanceConflict): await propose(e)
        assert len(e.posts) == 1
    asyncio.run(run())


def test_failed_intent_commit_never_sends_and_failed_final_commit_keeps_original_intent(live, monkeypatch):
    e = live
    async def run():
        async with e.backend.request(e.store):
            p = await propose(e)
            original = e.backend.begin_operation
            async def failed(*args): raise StorageUnavailable("synthetic commit failed")
            monkeypatch.setattr(e.backend, "begin_operation", failed)
            with pytest.raises(StorageUnavailable): await approve(e, p)
            assert not e.posts
            monkeypatch.setattr(e.backend, "begin_operation", original)
        async with e.backend.request(e.store):
            p = await propose(e)
            original_reconcile = e.backend.reconcile_operation
            async def failed_reconcile(*args, **kwargs): raise StorageUnavailable("synthetic response commit failed")
            monkeypatch.setattr(e.backend, "reconcile_operation", failed_reconcile)
            with pytest.raises(StorageUnavailable): await approve(e, p)
            monkeypatch.setattr(e.backend, "reconcile_operation", original_reconcile)
        assert len(e.posts) == 1
        assert query(e.dsn, "select status from toolbako_runtime.stripe_operations where endpoint='transfers'") == [("pending",)]
        async with e.backend.request(e.store):
            # A crash before outcome recording preserves executing + pending.
            # Recovery requires no manual status mutation or replacement POST.
            assert e.store.payouts[0]["status"] == "requested"
            r = await propose_recovery(e.backend, e.store, e.stripe, e.settings, e.store.payouts[0], e.seller, mfa=e.mfa, request=e.one)
            await approve_recovery(e.backend, e.store, e.stripe, e.settings, e.store.payouts[0], e.buyer, r["id"], mfa=e.mfa, request=e.two)
        assert len(e.posts) == 1
    asyncio.run(run())


def test_rejected_request_cannot_get_a_replacement_key_or_leak_provider_text(live):
    e = live
    async def run():
        e.fault = "rejected"
        async with e.backend.request(e.store):
            with pytest.raises(FinanceConflict) as error: await approve(e, await propose(e))
            assert "PRIVATE" not in str(error.value)
        async with e.backend.request(e.store):
            with pytest.raises(FinanceConflict): await propose(e)
        assert len(e.posts) == 1
        assert query(e.dsn, "select status from toolbako_runtime.stripe_operations where endpoint='transfers'") == [("rejected",)]
    asyncio.run(run())


@pytest.mark.parametrize("expiry", ["mfa", "review"])
def test_last_external_get_cannot_outlive_mfa_or_review_authority(live, expiry):
    e = live
    async def run():
        async with e.backend.request(e.store):
            p = await propose(e)
            def hook(req):
                if req.url.path != "/v1/transfers" or not query(e.dsn,
                    "select operation_id from toolbako_runtime.stripe_operations where endpoint='transfers' and status='pending'"):
                    return
                if expiry == "mfa":
                    e.store.account_sessions[e.two.session["sid"]]["mfa_verified_at"] = e.at - timedelta(hours=1)
                else:
                    p["proposed_at"] -= timedelta(hours=1)
            e.get_hook = hook
            with pytest.raises(StripeOutcomeUnknown): await approve(e, p)
            assert not e.posts
        assert query(e.dsn, "select status from toolbako_runtime.stripe_operations where endpoint='transfers'") == [("unknown",)]
    asyncio.run(run())


@pytest.mark.parametrize("change", ["paused", "capability", "schedule", "other_unknown", "refunded"])
def test_readonly_recovery_does_not_require_new_payment_readiness(live, change):
    e = live
    async def run():
        e.fault = "timeout_after"
        async with e.backend.request(e.store):
            with pytest.raises(StripeOutcomeUnknown): await approve(e, await propose(e))
        e.fault = None
        async with e.backend.request(e.store):
            if change == "paused": e.store.connected_accounts[e.seller]["payouts_paused"] = True
            if change == "capability":
                e.account["capabilities"]["transfers"] = "inactive"
                e.account["payouts_enabled"] = False
            if change == "schedule": e.account["settings"]["payouts"]["schedule"]["interval"] = "daily"
            if change == "other_unknown": await e.backend.begin_operation("another-original-intent", "refunds", {"amount": "1"})
            if change == "refunded":
                e.payment["latest_charge"]["amount_refunded"] = 1000
                e.store.orders[0]["refund_status"] = "completed"
            r = await propose_recovery(e.backend, e.store, e.stripe, e.settings, e.store.payouts[0], e.seller, mfa=e.mfa, request=e.one)
            assert await approve_recovery(e.backend, e.store, e.stripe, e.settings, e.store.payouts[0], e.buyer,
                r["id"], mfa=e.mfa, request=e.two) == "transferring"
            with pytest.raises((FinanceConflict, RuntimeError)): await propose(e)
        assert len(e.posts) == 1
        assert query(e.dsn, "select status from toolbako_runtime.stripe_operations where endpoint='transfers'") == [("succeeded",)]
    asyncio.run(run())
