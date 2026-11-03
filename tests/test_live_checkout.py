"""Disposable local PG, synthetic Stripe transport; never a real payment."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest
from starlette.requests import Request

from app import live_checkout as execution
from app.config import Settings
from app.data import DemoStore
from app.database import content_hash, StorageUnavailable
from app.finance import FinanceConflict
from app.financial_scope import FinancialScope
from app.integrations import StripeIntegration, StripeOutcomeUnknown
from app.live_seller_onboarding import _operation_id, _parameters, _metadata
from app.live_webhooks import build_live_webhook_handler
from app.mfa import SupabaseMFA
from app.payment_reconciliation import propose_payment_recovery, approve_payment_recovery
from app.security import SessionService
from test_database import database, query
from test_finance import order


@pytest.mark.parametrize("fault", ["flag", "mode", "invalid_configuration", "dsn", "host", "user", "port", "dbname", "tls", None])
def test_real_target_gate_rejects_wrong_connection_without_io(monkeypatch, fault):
    settings = Settings(stripe_connection_mode="live_financial", stripe_live_checkout_enabled=True, demo_mode=False,
                        database_url="postgresql://synthetic/target")
    monkeypatch.setattr(execution, "_live_configuration_verified", lambda s: fault != "invalid_configuration")
    monkeypatch.setattr(execution, "_live_database_config", lambda s: {"host": "expected.pooler", "user": "expected.user"})
    info = SimpleNamespace(host="expected.pooler", user="expected.user", port=5432, dbname="postgres")
    conn = SimpleNamespace(info=info, pgconn=SimpleNamespace(ssl_in_use=True))
    backend = SimpleNamespace(dsn=settings.database_url, _connection=conn)
    if fault == "flag": object.__setattr__(settings, "stripe_live_checkout_enabled", False)
    elif fault == "mode": object.__setattr__(settings, "stripe_connection_mode", "live_setup_only")
    elif fault == "dsn": backend.dsn = "postgresql://synthetic/other"
    elif fault == "tls": conn.pgconn.ssl_in_use = False
    elif fault in {"host", "user", "port", "dbname"}: setattr(info, fault, "wrong")
    if fault:
        with pytest.raises(FinanceConflict): execution._target_bound_connection(backend, settings)
    else:
        execution._target_bound_connection(backend, settings)


@pytest.fixture
def live(database, monkeypatch):
    backend, dsn = database
    scope = FinancialScope("live", "acct_1Ty4WnIogL39waUY", str(uuid4()))
    seller, buyer = str(uuid4()), str(uuid4())
    settings = Settings(environment="staging", demo_mode=False, database_url=dsn,
        stripe_secret_key="rk_live_SYNTHETIC", stripe_platform_account_id=scope.platform_account,
        stripe_ledger_id=scope.ledger_id, stripe_charge_mode="separate", stripe_connection_mode="live_financial",
        stripe_live_checkout_enabled=True, stripe_webhook_secret="whsec_platformSynthetic",
        stripe_connect_webhook_secret="whsec_connectSynthetic", stripe_connect_accounts_v2=False,
        site_base_url="https://toolbako.example", admin_user_ids=(seller, buyer))
    store = DemoStore(seed=False)
    stripe = StripeIntegration(settings.stripe_secret_key, settings.site_base_url, journal=backend,
        financial_scope=scope, charge_mode="separate", api_version=settings.stripe_api_version)
    sessions = SessionService(store, 3600)
    mfa = SupabaseMFA(settings, sessions)
    one, two = [Request({"type": "http", "method": "POST", "headers": [], "session": {}}) for _ in range(2)]
    at = datetime.now(timezone.utc)
    account_key = _operation_id(scope, seller)
    e = SimpleNamespace(backend=backend, dsn=dsn, scope=scope, settings=settings, store=store,
        stripe=stripe, sessions=sessions, mfa=mfa, one=one, two=two, seller=seller, buyer=buyer,
        at=at, calls=[], posts=[], remotes=[], payments={}, events={}, fault=None, get_hook=None,
        target_check_calls=0)
    e.account = {"object": "account", "id": "acct_seller", "type": "express", "country": "JP",
        "default_currency": "jpy", "charges_enabled": True, "payouts_enabled": True, "details_submitted": True,
        "metadata": _metadata(scope, seller, account_key), "settings": {"payouts": {"schedule": {"interval": "manual"}}},
        "capabilities": {"card_payments": "active", "transfers": "active"}}

    async def initialize():
        async with backend.request(store):
            await backend.bind_empty_financial_scope(scope)
            for name, uid, req in (("seller", seller, one), ("buyer", buyer, two)):
                user = {"id": uid, "username": name, "email": name + "@example.invalid", "email_verified": True,
                        "is_banned": False, "identity_status": "unverified", "is_verified": False}
                store.registered_users[name] = user
                sessions.establish(req, user)
                store.account_sessions[req.session["sid"]].update(mfa_verified_at=at, mfa_token_expires_at=at + timedelta(hours=1))
            await backend.begin_operation(account_key, "accounts", _parameters(scope, seller, account_key))
            store.connected_accounts[seller] = {"account_id": "acct_seller", "scope": scope.payload(),
                "onboarding_operation_id": account_key, "payouts_enabled": True, "details_submitted": True}
            await backend.reconcile_operation(account_key, endpoint="accounts",
                request_hash=content_hash(_parameters(scope, seller, account_key)), response={"id": "acct_seller", "object": "account"})
            store.orders.append(order(seller_id=seller, buyer_id=buyer, created_at=at, tool_slug="tool",
                status="in_progress", payment_status="pending", payment_reference=None,
                buyer_email="PRIVATE_BUYER@example.invalid", seller_email="PRIVATE_SELLER@example.invalid"))
            store.tools = [{"id": "tool", "slug": "tool", "sales_count": 0}]
            await backend.save()
    asyncio.run(initialize())
    # Only deployed TLS/host config is substituted for this disposable server.
    # Actual scope, locked request, sessions, intents and commits remain real.
    monkeypatch.setattr(execution, "_live_configuration_verified", lambda s: True)
    def target(*args): e.target_check_calls += 1
    monkeypatch.setattr(execution, "_target_bound_connection", target)

    def provider(req):
        e.calls.append(req)
        path = req.url.path.removeprefix("/v1/")
        if req.method == "POST":
            assert req.url.host == "api.stripe.com" and path == "checkout/sessions"
            assert "stripe-account" not in req.headers and req.headers["stripe-version"] == settings.stripe_api_version
            data = {k: v[0] for k, v in parse_qs(req.content.decode(), keep_blank_values=True).items()}
            key = req.headers["idempotency-key"]
            assert query(dsn, "select endpoint,request_hash,status from toolbako_runtime.stripe_operations where operation_id=%s", (key,)) == [
                ("checkout/sessions", content_hash(data), "pending")]
            payload = query(dsn, "select payload from toolbako_runtime.marketplace_state where singleton")[0][0]
            saved = payload["orders"][0]
            part = saved["pending_extras"][0] if key.startswith("extra-") else saved
            assert part[execution.ACCOUNT_FIELD] == "acct_seller"
            assert saved["payment_status"] == ("paid" if key.startswith("extra-") else "pending")
            assert "customer_email" not in data
            assert data["payment_method_types[0]"] == "card"
            assert not any("destination" in k or "application_fee" in k for k in data)
            metadata = {k[9:-1]: v for k, v in data.items() if k.startswith("metadata[")}
            assert all(data[f"payment_intent_data[metadata][{k}]"] == v for k, v in metadata.items())
            e.posts.append((key, data))
            if e.fault == "timeout_before": raise httpx.ReadTimeout("PRIVATE_ERROR", request=req)
            if e.fault == "rejected": return httpx.Response(403, text="PRIVATE_PROVIDER_BODY")
            now = int(time.time())
            remote = {"id": "cs_live_fixture", "object": "checkout.session", "livemode": True,
                "mode": "payment", "currency": "jpy", "amount_total": int(data["line_items[0][price_data][unit_amount]"]),
                "metadata": metadata, "status": "open", "payment_status": "unpaid", "payment_intent": None,
                "created": now, "expires_at": now + 3600, "success_url": data["success_url"], "cancel_url": data["cancel_url"],
                "url": "https://checkout.stripe.com/c/pay/cs_live_fixture#PRIVATE_HANDOFF",
                "customer_details": {"email": "PRIVATE_PAYER@example.invalid"}, "client_secret": "PRIVATE_SECRET"}
            e.remotes.append(remote)
            if e.fault == "timeout_after": raise httpx.ReadTimeout("PRIVATE_ERROR", request=req)
            if e.fault == "bad_response": return httpx.Response(200, json={"id": "cs_test_wrong"})
            if e.fault == "bad_currency": remote["currency"] = "usd"
            return httpx.Response(200, json=remote)
        assert req.method == "GET"
        if e.get_hook: e.get_hook(req)
        if path == "account": return httpx.Response(200, json={"object": "account", "id": scope.platform_account})
        if path == "balance": return httpx.Response(200, json={"object": "balance", "livemode": True})
        if path == "accounts/acct_seller": return httpx.Response(200, json=e.account)
        if path.startswith("accounts/acct_seller/capabilities/"):
            name = path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={"object": "capability", "id": name, "account": "acct_seller", "requested": True,
                "status": e.account["capabilities"][name]})
        if path == "checkout/sessions": return httpx.Response(200, json={"object": "list", "data": e.remotes, "has_more": False})
        if path.startswith("checkout/sessions/"):
            found = next((r for r in e.remotes if r["id"] == path.rsplit("/", 1)[-1]), None)
            return httpx.Response(200 if found else 404, json=found or {})
        if path.startswith("payment_intents/"): return httpx.Response(200, json=e.payments[path.rsplit("/", 1)[-1]])
        if path.startswith("events/"): return httpx.Response(200, json=e.events[path.rsplit("/", 1)[-1]])
        raise AssertionError("Unexpected synthetic endpoint")
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(provider), **kw))
    return e


def run(e, *, extra=False, mutate=None):
    async def call():
        async with e.backend.request(e.store):
            current = e.store.orders[0]
            if mutate: mutate(current)
            part = current["pending_extras"][0] if extra else None
            return await execution.execute_live_checkout(e.backend, e.store, e.stripe, e.settings,
                e.sessions, e.two, current, extra=part)
    return asyncio.run(call())


def result(e, key="checkout-order_finance"):
    return query(e.dsn, "select endpoint,status,response from toolbako_runtime.stripe_operations where operation_id=%s", (key,))[0]


def modify(e, callback):
    async def call():
        async with e.backend.request(e.store):
            callback()
            await e.backend.save()
    asyncio.run(call())


def paid(e):
    remote = e.remotes[0]
    remote.update(status="complete", payment_status="paid", payment_intent="pi_source", url=None)
    metadata = deepcopy(remote["metadata"])
    amount = remote["amount_total"]
    e.payments["pi_source"] = {"id": "pi_source", "object": "payment_intent", "livemode": True,
        "status": "succeeded", "currency": "jpy", "amount": amount, "amount_received": amount,
        "transfer_group": "order-order_finance", "metadata": metadata,
        "latest_charge": {"id": "ch_source", "object": "charge", "livemode": True,
            "payment_intent": "pi_source", "amount": amount, "amount_captured": amount, "currency": "jpy",
            "paid": True, "captured": True, "status": "succeeded", "disputed": False,
            "amount_refunded": 0, "transfer_group": "order-order_finance", "metadata": deepcopy(metadata)}}


def prepare_extra(e):
    def seed():
        current = e.store.orders[0]
        current.update(payment_status="paid", payment_reference="pi_primary", live_checkout_account_id="acct_seller")
        metadata = execution.checkout_metadata(e.scope, current)
        e.payments["pi_primary"] = {"id": "pi_primary", "object": "payment_intent", "livemode": True,
            "status": "succeeded", "currency": "jpy", "amount": 1000, "amount_received": 1000,
            "transfer_group": "order-order_finance", "metadata": metadata,
            "latest_charge": {"id": "ch_primary", "object": "charge", "livemode": True,
                "payment_intent": "pi_primary", "amount": 1000, "amount_captured": 1000, "currency": "jpy",
                "paid": True, "captured": True, "status": "succeeded", "disputed": False,
                "amount_refunded": 0, "transfer_group": "order-order_finance", "metadata": deepcopy(metadata)}}
        e.store.reserve_extra_payment(current, 500, "Additional")
    modify(e, seed)


def notification(e, *, identifier="evt_paid"):
    ev = {"id": identifier, "object": "event", "type": "checkout.session.completed", "livemode": True,
          "api_version": e.settings.stripe_webhook_api_version, "created": int(time.time()),
          "data": {"object": deepcopy(e.remotes[0])}}
    e.events[identifier] = ev
    return ev


def deliver(e, ev):
    async def call():
        handler = build_live_webhook_handler(e.settings, e.store, e.backend, e.stripe)
        assert handler is not None
        raw = json.dumps(ev).encode()
        now = str(int(time.time()))
        sig = hmac.new(e.settings.stripe_webhook_secret.encode(), now.encode() + b"." + raw, hashlib.sha256).hexdigest()
        scope = {"type": "http", "method": "POST", "path": "/webhooks/stripe/live", "headers": [
            (b"stripe-signature", f"t={now},v1={sig}".encode())]}
        consumed = False
        async def receive():
            nonlocal consumed
            if consumed: return {"type": "http.disconnect"}
            consumed = True
            return {"type": "http.request", "body": raw, "more_body": False}
        messages = []
        async def send(message): messages.append(message)
        await handler(scope, receive, send)
        return messages[0]["status"]
    return asyncio.run(call())


def recover(e, *, approve=True, extra=False, change=None):
    async def mail(recipient, subject, body):
        e.backend.enqueue_email(recipient, "synthetic@example.invalid", subject, body)
    async def call():
        kind = "extra" if extra else "checkout"
        async with e.backend.request(e.store):
            current = e.store.orders[0]
            extra_id = current["pending_extras"][0]["id"] if extra else ""
            proposal = await propose_payment_recovery(e.backend, e.store, e.stripe, e.settings,
                current, kind, e.seller, extra_id=extra_id, mfa=e.mfa, request=e.one)
        if change: change()
        if not approve: return proposal
        async with e.backend.request(e.store):
            return await approve_payment_recovery(e.backend, e.store, e.stripe, e.settings,
                e.store.orders[0], kind, e.buyer, proposal["id"], mail, extra_id=extra_id, mfa=e.mfa, request=e.two)
    return asyncio.run(call())


def test_durable_intent_receipt_then_fresh_cached_handoff_without_payment(live):
    e = live
    first = run(e)
    assert first["id"] == "cs_live_fixture" and first["url"].startswith("https://checkout.stripe.com/")
    assert len(e.posts) == 1 and result(e)[1] == "succeeded"
    saved = query(e.dsn, "select payload from toolbako_runtime.marketplace_state where singleton")[0][0]
    assert saved["orders"][0]["checkout_session_id"] == first["id"]
    assert saved["orders"][0]["payment_status"] == "pending" and "delivery" not in saved["orders"][0]
    assert saved["tools"][0]["sales_count"] == 0
    assert "PRIVATE_SECRET" not in json.dumps(result(e)) and "PRIVATE_PAYER" not in json.dumps(result(e))
    before = len(e.calls)
    assert run(e)["id"] == first["id"] and len(e.posts) == 1
    assert any(r.url.path.endswith("/checkout/sessions/cs_live_fixture") for r in e.calls[before:])
    assert any(r.url.path.endswith("/accounts/acct_seller") for r in e.calls[before:])


@pytest.mark.parametrize("flag", [False, "true", 1, None])
def test_flag_is_explicit_bool_and_default_off(live, flag):
    e = live
    # Forged post-construction values must still never bypass the execution gate.
    object.__setattr__(e.settings, "stripe_live_checkout_enabled", flag)
    with pytest.raises(FinanceConflict): run(e)
    assert not e.posts and not query(e.dsn, "select operation_id from toolbako_runtime.stripe_operations where endpoint='checkout/sessions'")


@pytest.mark.parametrize("fault", ["timeout_before", "timeout_after", "rejected", "bad_response", "bad_currency"])
def test_every_post_uncertainty_survives_restart_and_never_reposts(live, fault):
    e = live
    e.fault = fault
    with pytest.raises(StripeOutcomeUnknown): run(e)
    assert result(e)[1] == "unknown"
    e.fault = None
    for _ in range(2):
        with pytest.raises(StripeOutcomeUnknown): run(e)
    assert len(e.posts) == 1 and e.store.orders[0]["payment_reconciliation_required"] is True


@pytest.mark.parametrize("fault", ["seller_disabled", "capability_pending", "wrong_seller_ledger", "wrong_mapping",
    "buyer_banned", "buyer_wrong", "order_copy", "subscription", "negative_amount", "unproven_cached_url"])
def test_pre_intent_proof_failures_never_send(live, fault):
    e = live
    change = None
    if fault == "seller_disabled": e.account["charges_enabled"] = False
    elif fault == "capability_pending": e.account["capabilities"]["transfers"] = "pending"
    elif fault == "wrong_seller_ledger": e.account["metadata"]["ledger_id"] = str(uuid4())
    elif fault == "wrong_mapping": modify(e, lambda: e.store.connected_accounts[e.seller].update(scope={}))
    elif fault == "buyer_banned": modify(e, lambda: e.store.registered_users["buyer"].update(is_banned=True))
    elif fault == "buyer_wrong": e.two = e.one
    elif fault == "order_copy": change = lambda o: e.store.orders.append(deepcopy(o))
    elif fault == "subscription": change = lambda o: o.update(billing_type="monthly")
    elif fault == "negative_amount": change = lambda o: o.update(primary_payment_amount=-1)
    else: change = lambda o: o.update(checkout_session_url="https://checkout.stripe.com/c/forged")
    with pytest.raises(FinanceConflict): run(e, mutate=change)
    assert not e.posts


@pytest.mark.parametrize("fault", ["expired", "foreign_url", "changed_amount", "test_mode", "missing_ledger", "seller_disabled"])
def test_existing_handoff_is_not_a_bypass_and_failure_preserves_intent(live, fault):
    e = live
    run(e)
    remote = e.remotes[0]
    if fault == "expired": remote["expires_at"] = int(time.time()) - 1
    elif fault == "foreign_url": remote["url"] = "https://checkout.stripe.com.evil.invalid/c/pay"
    elif fault == "changed_amount": remote["amount_total"] += 1
    elif fault == "test_mode": remote["livemode"] = False
    elif fault == "missing_ledger": remote["metadata"].pop("ledger_id")
    else: e.account["charges_enabled"] = False
    with pytest.raises(StripeOutcomeUnknown): run(e)
    assert len(e.posts) == 1 and result(e)[1] == "succeeded"
    assert not e.store.orders[0].get("payment_reconciliation_required")


def test_receipt_commit_failure_keeps_unknown_reservation_without_receipt(live, monkeypatch):
    e = live
    async def fail(*a, **kw): raise StorageUnavailable("PRIVATE_DATABASE_FAILURE")
    monkeypatch.setattr(e.backend, "reconcile_operation", fail)
    with pytest.raises(StripeOutcomeUnknown): run(e)
    assert len(e.posts) == 1 and result(e)[1:] == ("unknown", None)
    assert not e.store.orders[0].get("checkout_session_id") and e.store.orders[0][execution.ACCOUNT_FIELD] == "acct_seller"


def test_intent_commit_ack_loss_never_sends_or_recreates(live, monkeypatch):
    e = live
    original = e.backend.begin_operation
    async def lost(*a, **kw):
        await original(*a, **kw)
        raise StorageUnavailable("PRIVATE_ACK_LOSS")
    monkeypatch.setattr(e.backend, "begin_operation", lost)
    with pytest.raises(StripeOutcomeUnknown): run(e)
    assert not e.posts and result(e)[1] == "unknown"


def test_unknown_zero_or_multiple_results_stay_held_without_new_post(live):
    e = live
    e.fault = "timeout_before"
    with pytest.raises(StripeOutcomeUnknown): run(e)
    with pytest.raises(FinanceConflict): recover(e)
    assert len(e.posts) == 1 and result(e)[1] == "unknown"


@pytest.mark.parametrize("outcome", ["open", "paid", "expired"])
def test_two_admin_get_recovery_commits_exact_receipt_and_state(live, outcome):
    e = live
    e.fault = "timeout_after"
    with pytest.raises(StripeOutcomeUnknown): run(e)
    if outcome == "paid": paid(e)
    elif outcome == "expired": e.remotes[0].update(status="expired", payment_status="unpaid", url=None)
    assert recover(e) == ("complete" if outcome == "paid" else outcome)
    assert len(e.posts) == 1 and result(e)[1] == "succeeded"
    current = e.store.orders[0]
    assert current["payment_status"] == {"open": "pending", "paid": "paid", "expired": "expired"}[outcome]
    assert not current["payment_reconciliation_required"]
    if outcome == "open": assert run(e)["id"] == "cs_live_fixture" and len(e.posts) == 1


@pytest.mark.parametrize("fault", ["second_same_admin", "changed_remote", "wrong_ledger", "multiple_results", "expired_mfa"])
def test_get_recovery_rejects_stale_or_unproven_approval(live, fault):
    e = live
    e.fault = "timeout_after"
    with pytest.raises(StripeOutcomeUnknown): run(e)
    change = None
    if fault == "second_same_admin": e.two = e.one
    elif fault == "changed_remote": change = lambda: e.remotes[0].update(expires_at=e.remotes[0]["expires_at"] + 1)
    elif fault == "wrong_ledger": e.remotes[0]["metadata"]["ledger_id"] = str(uuid4())
    elif fault == "multiple_results": e.remotes.append({**e.remotes[0], "id": "cs_live_duplicate"})
    else: modify(e, lambda: e.store.account_sessions[e.two.session["sid"]].update(mfa_verified_at=e.at - timedelta(hours=1)))
    with pytest.raises(FinanceConflict): recover(e, change=change)
    assert result(e)[1] == "unknown" and len(e.posts) == 1


def test_extra_uses_separate_canonical_intent_and_recovery_preserves_primary_amount(live):
    e = live
    prepare_extra(e)
    e.fault = "timeout_after"
    with pytest.raises(StripeOutcomeUnknown): run(e, extra=True)
    paid(e)
    assert recover(e, extra=True) == "complete"
    current = e.store.orders[0]
    assert current["amount"] == 1500 and current["primary_payment_amount"] == 1000
    assert current["payment_reference"] == "pi_primary" and current["pending_extras"][0]["payment_reference"] == "pi_source"
    assert len(e.posts) == 1


@pytest.mark.parametrize("fault", ["refunded", "disputed", "ledger", "amount", "mode"])
def test_extra_requires_fresh_unrefunded_parent_even_before_delayed_notification(live, fault):
    e = live
    prepare_extra(e)
    parent = e.payments["pi_primary"]
    if fault == "refunded": parent["latest_charge"]["amount_refunded"] = 1000
    elif fault == "disputed": parent["latest_charge"]["disputed"] = True
    elif fault == "ledger": parent["latest_charge"]["metadata"].pop("ledger_id")
    elif fault == "amount": parent["amount_received"] = 999
    else: parent["livemode"] = False
    assert e.store.orders[0]["payment_status"] == "paid"
    with pytest.raises(FinanceConflict): run(e, extra=True)
    assert not e.posts


@pytest.mark.parametrize("expiry", ["mfa", "session", "proposer"])
def test_final_scope_get_cannot_outlive_checkout_recovery_authorization(live, monkeypatch, expiry):
    from app import live_payout_execution
    e = live
    e.fault = "timeout_after"
    with pytest.raises(StripeOutcomeUnknown): run(e)
    proposal = recover(e, approve=False)
    original = live_payout_execution.service_context
    contexts = 0
    def hook(req):
        if contexts != 2 or req.url.path != "/v1/balance": return
        if expiry == "mfa": e.store.account_sessions[e.two.session["sid"]]["mfa_verified_at"] = e.at - timedelta(hours=1)
        elif expiry == "session": e.store.account_sessions.pop(e.two.session["sid"], None)
        else: e.store.registered_users["seller"]["is_banned"] = True
    async def context(*args):
        nonlocal contexts
        contexts += 1
        return await original(*args)
    monkeypatch.setattr(live_payout_execution, "service_context", context)
    e.get_hook = hook
    async def call():
        async with e.backend.request(e.store):
            async def mail(*args): raise AssertionError("Must not queue before final auth proof")
            await approve_payment_recovery(e.backend, e.store, e.stripe, e.settings,
                e.store.orders[0], "checkout", e.buyer, proposal["id"], mail, mfa=e.mfa, request=e.two)
    with pytest.raises(FinanceConflict): asyncio.run(call())
    assert contexts == 2 and result(e)[1] == "unknown" and len(e.posts) == 1
    assert e.store.orders[0]["payment_reconciliations"]["checkout"]["status"] == "proposed"


def test_paid_webhook_receipt_state_finance_and_dedup_commit_together(live):
    e = live
    run(e)
    paid(e)
    ev = notification(e)
    assert deliver(e, ev) == 200
    assert e.store.orders[0]["payment_status"] == "paid" and e.store.tools[0]["sales_count"] == 1
    assert e.store.orders[0]["buyer_email"] == "PRIVATE_BUYER@example.invalid"
    assert ev["id"] in e.store.processed_webhook_events
    assert deliver(e, ev) == 200 and e.store.tools[0]["sales_count"] == 1 and len(e.posts) == 1


@pytest.mark.parametrize("fault", ["signed_ledger", "current_ledger", "parent_ledger", "charge_ledger", "journal_hash", "unknown_journal"])
def test_live_paid_notification_requires_scoped_canonical_receipt(live, fault):
    e = live
    run(e)
    paid(e)
    ev = notification(e)
    if fault == "signed_ledger": ev["data"]["object"]["metadata"].pop("ledger_id")
    elif fault == "current_ledger": e.remotes[0]["metadata"].pop("ledger_id")
    elif fault == "parent_ledger": e.payments["pi_source"]["metadata"].pop("ledger_id")
    elif fault == "charge_ledger": e.payments["pi_source"]["latest_charge"]["metadata"].pop("ledger_id")
    else:
        statement = ("update toolbako_runtime.stripe_operations set request_hash='wrong' "
                     if fault == "journal_hash" else
                     "update toolbako_runtime.stripe_operations set status='unknown',response=NULL ")
        query(e.dsn, statement + "where operation_id=%s returning operation_id", ("checkout-order_finance",))
    assert deliver(e, ev) == 409
    assert e.store.orders[0]["payment_status"] == "pending" and ev["id"] not in e.store.processed_webhook_events


def test_failed_webhook_commit_does_not_unlock_or_claim_event(live, monkeypatch):
    e = live
    run(e)
    paid(e)
    ev = notification(e)
    original = e.backend.save
    async def fail(): raise StorageUnavailable("PRIVATE_COMMIT_FAILURE")
    monkeypatch.setattr(e.backend, "save", fail)
    assert deliver(e, ev) == 503
    assert e.store.orders[0]["payment_status"] == "pending" and ev["id"] not in e.store.processed_webhook_events
    monkeypatch.setattr(e.backend, "save", original)
    assert deliver(e, ev) == 200


def test_generic_live_post_remains_forbidden(live):
    e = live
    async def call():
        async with e.backend.request(e.store):
            await e.stripe.create_checkout(e.store.orders[0], "acct_seller")
    with pytest.raises(RuntimeError): asyncio.run(call())
    assert not e.posts
