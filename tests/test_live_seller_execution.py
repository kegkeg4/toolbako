"""Disposable localhost PG atomicity + synthetic Stripe only. No real POST."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import pickle
import time
from types import SimpleNamespace
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest
from starlette.requests import Request

from app import live_seller_execution as execution
from app.config import Settings
from app.data import DemoStore
from app.database import content_hash
from app.financial_scope import FinancialScope
from app.integrations import StripeIntegration
from app.mfa import SupabaseMFA
from app.security import SessionService
from test_database import database, query


@pytest.fixture
def live(database, monkeypatch):
    backend, dsn = database
    scope = FinancialScope("live", "acct_1Ty4WnIogL39waUY", str(uuid4()))
    settings = Settings(environment="staging", demo_mode=False, database_url=dsn,
                        stripe_secret_key="rk_live_SYNTHETIC", stripe_platform_account_id=scope.platform_account,
                        stripe_ledger_id=scope.ledger_id, stripe_charge_mode="separate",
                        stripe_connection_mode="live_setup_only", stripe_live_seller_setup_enabled=True,
                        stripe_webhook_secret="whsec_syntheticplatform", stripe_connect_webhook_secret="whsec_syntheticconnect",
                        stripe_connect_accounts_v2=False, site_base_url="https://toolbako.example",
                        allowed_hosts=("toolbako.example",))
    store = DemoStore(seed=False)
    user = {"id": str(uuid4()), "username": "seller", "email": "PRIVATE_EMAIL@example.invalid",
            "is_banned": False, "identity_status": "unverified", "email_verified": True}
    request = Request({"type": "http", "method": "POST", "session": {}, "headers": []})
    sessions = SessionService(store, 3600)
    mfa = SupabaseMFA(settings, sessions)
    async def initialize():
        async with backend.request(store):
            await backend.bind_empty_financial_scope(scope)
            store.registered_users[user["username"]] = user
            sessions.establish(request, user)
            now = datetime.now(timezone.utc)
            store.account_sessions[request.session["sid"]].update(mfa_verified_at=now, mfa_token_expires_at=now + timedelta(hours=1))
            await backend.save()
    asyncio.run(initialize())
    stripe = StripeIntegration(settings.stripe_secret_key, settings.site_base_url, charge_mode="separate", api_version=settings.stripe_api_version,
                               journal=backend, financial_scope=scope)
    e = SimpleNamespace(backend=backend, dsn=dsn, scope=scope, settings=settings, store=store,
                        user=user, request=request, mfa=mfa, stripe=stripe, requests=[], posts=[], accounts=[],
                        account_fault=None, link_fault=None)
    # Only deployed target TLS/host identity is simulated for local PG. Binding,
    # server-session MFA, journal commits, rollback and independent reads are real.
    monkeypatch.setattr(execution, "_target_bound_connection", lambda *args: None)

    def provider(req):
        e.requests.append(req)
        path = req.url.path
        if req.method == "POST":
            assert req.url.host == "api.stripe.com" and path in {"/v1/accounts", "/v1/account_links"}
            assert "stripe-account" not in req.headers
            assert req.headers["stripe-version"] == settings.stripe_api_version
            values = {k: v[0] for k, v in parse_qs(req.content.decode(), keep_blank_values=True).items()}
            key = req.headers["idempotency-key"]
            # An independent PG connection sees the intent AND pre-I/O snapshot.
            intent = query(dsn, "select endpoint,request_hash,status from toolbako_runtime.stripe_operations where operation_id=%s", (key,))
            assert intent == [(path.removeprefix("/v1/"), content_hash(values), "pending")]
            e.posts.append((path, key, values))
            fault = e.account_fault if path == "/v1/accounts" else e.link_fault
            if fault == "timeout_before": raise httpx.ReadTimeout("PRIVATE_PROVIDER_FAILURE", request=req)
            if fault == "rejected": return httpx.Response(403, text="PRIVATE_PROVIDER_BODY")
            if path == "/v1/accounts":
                assert not e.accounts
                metadata = {k[9:-1]: v for k, v in values.items() if k.startswith("metadata[")}
                account = {"object": "account", "id": "acct_seller", "metadata": metadata,
                           "type": "express", "country": "JP", "default_currency": "jpy",
                           "settings": {"payouts": {"schedule": {"interval": "manual"}}},
                           "charges_enabled": False, "payouts_enabled": False, "details_submitted": False,
                           "capabilities": {"card_payments": "pending", "transfers": "pending"}}
                e.accounts.append(account)
                if fault == "timeout_after": raise httpx.ReadTimeout("PRIVATE_PROVIDER_FAILURE", request=req)
                if fault == "invalid": return httpx.Response(200, json={"object": "account", "id": "acct_wrong"})
                return httpx.Response(200, json=account)
            now = int(time.time())
            link = {"object": "account_link", "created": now, "expires_at": now + 300,
                    "url": "https://connect.stripe.com/setup/e/acct_seller/PRIVATE_LINK"}
            if fault == "timeout_after": raise httpx.ReadTimeout("PRIVATE_PROVIDER_FAILURE", request=req)
            if fault == "url": link["url"] = "https://connect.stripe.com.attacker.invalid/PRIVATE_LINK"
            if fault == "expired": link["expires_at"] = now - 1
            return httpx.Response(200, json=link)
        assert req.method == "GET"
        if path == "/v1/account": return httpx.Response(200, json={"object": "account", "id": scope.platform_account})
        if path == "/v1/balance": return httpx.Response(200, json={"object": "balance", "livemode": True})
        if path == "/v1/accounts": return httpx.Response(200, json={"object": "list", "data": e.accounts, "has_more": False})
        if path == "/v1/accounts/acct_seller": return httpx.Response(200, json=e.accounts[0])
        if path.startswith("/v1/accounts/acct_seller/capabilities/"):
            name = path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={"object": "capability", "id": name, "account": "acct_seller",
                                            "requested": True, "status": "pending"})
        raise AssertionError("Unexpected synthetic provider request")
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(provider), **kwargs))
    return e


def run(e, *, link=False):
    async def call():
        async with e.backend.request(e.store):
            function = execution.execute_live_seller_account_link if link else execution.execute_live_seller_account
            return await function(e.backend, e.store, e.stripe, e.settings, e.mfa, e.request, seller_id=e.user["id"])
    return asyncio.run(call())


def journal(e, operation_id):
    return query(e.dsn, "select endpoint,status,response from toolbako_runtime.stripe_operations where operation_id=%s", (operation_id,))[0]


def test_account_intent_precedes_post_and_receipt_mapping_audit_are_durable(live):
    e = live
    first = run(e)
    assert first.action == "account_mapped" and first.account_id == "acct_seller"
    assert len(e.posts) == 1
    assert journal(e, first.operation_id)[1] == "succeeded"
    saved = query(e.dsn, "select payload from toolbako_runtime.marketplace_state where singleton")[0][0]
    assert saved["connected_accounts"][e.user["id"]]["account_id"] == "acct_seller"
    assert query(e.dsn, "select count(*) from toolbako_runtime.audit_events where action='connect.live_account_mapped'") == [(1,)]
    assert run(e).action == "onboarding_link_needed" and len(e.posts) == 1
    assert "PRIVATE_EMAIL" not in json.dumps(journal(e, first.operation_id))


def test_financial_stage_keeps_explicit_seller_setup_lane(live):
    e = live
    e.settings = replace(e.settings, stripe_connection_mode='live_financial')
    e.mfa.settings = e.settings
    first = run(e)
    assert first.action == 'account_mapped' and len(e.posts) == 1
    assert all(path == '/v1/accounts' for path, *_ in e.posts)


@pytest.mark.parametrize('mode,flag',[('live_read_only',True),('live_financial',False)])
def test_seller_setup_stage_and_flag_still_guard_posts(live,mode,flag):
    e=live
    e.settings=replace(e.settings,stripe_connection_mode=mode,stripe_live_seller_setup_enabled=flag)
    with pytest.raises(execution.SellerSetupError): run(e)
    assert e.requests == []


@pytest.mark.parametrize("fault", ["timeout_before", "timeout_after", "invalid"])
def test_unknown_creation_is_get_only_after_restart_even_when_no_result(live, fault):
    e = live
    e.account_fault = fault
    with pytest.raises(execution.SellerSetupUncertain): run(e)
    key = e.posts[0][1]
    assert journal(e, key)[1] == "unknown"
    e.account_fault = None
    result = run(e)
    assert result.action == ("outcome_unknown" if fault == "timeout_before" else "account_mapped")
    assert len(e.posts) == 1


def test_rejected_create_does_not_get_a_fresh_operation_id(live):
    e = live
    e.account_fault = "rejected"
    assert run(e).action == run(e).action == "operator_review_required"
    assert len(e.posts) == 1 and journal(e, e.posts[0][1])[1] == "rejected"


@pytest.mark.parametrize("after_commit", [False, True])
def test_intent_commit_failure_never_sends_post(live, monkeypatch, after_commit):
    e = live
    original = e.backend.begin_operation
    async def failed(*args, **kwargs):
        if after_commit: await original(*args, **kwargs)
        raise RuntimeError("PRIVATE_DB_FAILURE")
    monkeypatch.setattr(e.backend, "begin_operation", failed)
    with pytest.raises(execution.SellerSetupUncertain): run(e)
    assert not e.posts
    monkeypatch.setattr(e.backend, "begin_operation", original)
    result = run(e)
    assert result.action == ("outcome_unknown" if after_commit else "account_mapped")
    assert len(e.posts) == (0 if after_commit else 1)


@pytest.mark.parametrize("after_commit", [False, True])
def test_final_commit_unknown_recovers_without_second_create(live, monkeypatch, after_commit):
    e = live
    original = e.backend.reconcile_operation
    async def failed(*args, **kwargs):
        if after_commit: await original(*args, **kwargs)
        raise RuntimeError("PRIVATE_DB_FAILURE")
    monkeypatch.setattr(e.backend, "reconcile_operation", failed)
    with pytest.raises(execution.SellerSetupUncertain): run(e)
    assert len(e.posts) == 1
    assert bool(e.store.connected_accounts) is after_commit
    monkeypatch.setattr(e.backend, "reconcile_operation", original)
    assert run(e).action == ("onboarding_link_needed" if after_commit else "account_mapped")
    assert len(e.posts) == 1


def test_real_reconcile_rollback_cannot_leak_dirty_mapping_into_generic_save(live, monkeypatch):
    e = live
    original = e.backend._save
    calls = 0
    async def failing(conn, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2: raise RuntimeError("synthetic failure after journal UPDATE")
        return await original(conn, **kwargs)
    monkeypatch.setattr(e.backend, "_save", failing)
    async def attempt():
        async with e.backend.request(e.store):
            with pytest.raises(execution.SellerSetupUncertain):
                await execution.execute_live_seller_account(e.backend, e.store, e.stripe, e.settings,
                                                           e.mfa, e.request, seller_id=e.user["id"])
            assert not e.store.connected_accounts
            assert not any(a["action"] == "connect.live_account_mapped" for a in e.store.audit_logs)
            await e.backend.save()  # model a route converting the exception to 409
    asyncio.run(attempt())
    assert journal(e, e.posts[0][1])[1] == "pending"
    assert query(e.dsn, "select count(*) from toolbako_runtime.audit_events where action='connect.live_account_mapped'") == [(0,)]
    monkeypatch.setattr(e.backend, "_save", original)
    assert run(e).action == "account_mapped" and len(e.posts) == 1


def test_link_intent_is_separate_and_handoff_url_never_persisted(live):
    e = live
    account = run(e)
    link = run(e, link=True)
    assert link.action == "link_created" and link.operation_id != account.operation_id
    assert link.handoff.for_redirect().startswith("https://connect.stripe.com/")
    assert "PRIVATE_LINK" not in repr(link) + repr(link.handoff)
    with pytest.raises(TypeError): pickle.dumps(link.handoff)
    for table, column in (("marketplace_state", "payload"), ("stripe_operations", "response"), ("audit_events", "target")):
        rows = query(e.dsn, f"select {column}::text from toolbako_runtime.{table}")
        assert "PRIVATE_LINK" not in repr(rows)
    assert run(e, link=True).action == "link_unavailable" and len(e.posts) == 2


@pytest.mark.parametrize("fault", ["timeout_before", "timeout_after", "url", "expired", "rejected"])
def test_uncertain_link_cannot_be_bypassed_with_a_second_post(live, fault):
    e = live
    run(e)
    e.link_fault = fault
    if fault == "rejected": assert run(e, link=True).action == "operator_review_required"
    else:
        with pytest.raises(execution.SellerSetupUncertain): run(e, link=True)
    result = run(e, link=True)
    assert result.action == ("operator_review_required" if fault == "rejected" else "link_outcome_unknown")
    assert len(e.posts) == 2


@pytest.mark.parametrize("after_commit", [False, True])
def test_link_commit_failure_does_not_return_url_or_resubmit(live, monkeypatch, after_commit):
    e = live
    run(e)
    original = e.backend.reconcile_operation
    async def failed(*args, **kwargs):
        if after_commit: await original(*args, **kwargs)
        raise RuntimeError("PRIVATE_DB_FAILURE")
    monkeypatch.setattr(e.backend, "reconcile_operation", failed)
    with pytest.raises(execution.SellerSetupUncertain): run(e, link=True)
    monkeypatch.setattr(e.backend, "reconcile_operation", original)
    result = run(e, link=True)
    assert result.action == ("link_unavailable" if after_commit else "link_outcome_unknown")
    assert result.handoff is None and len(e.posts) == 2


@pytest.mark.parametrize("fault", ["off", "mode", "get", "mfa", "other_user", "scope", "api_version"])
def test_server_capability_actor_and_binding_required_before_any_post(live, fault):
    e = live
    if fault == "off": e.settings = replace(e.settings, stripe_live_seller_setup_enabled=False)
    if fault == "mode": e.settings = replace(e.settings, stripe_connection_mode="live_read_only")
    e.mfa.settings = e.settings
    if fault == "get": e.request.scope["method"] = "GET"
    if fault == "other_user": e.user = dict(e.user, id=str(uuid4()))
    if fault == "scope": e.stripe.financial_scope = replace(e.scope, ledger_id=str(uuid4()))
    if fault == "api_version": e.stripe.api_version = "different-version"
    async def call():
        async with e.backend.request(e.store):
            if fault == "mfa": e.store.account_sessions[e.request.session["sid"]].pop("mfa_verified_at")
            with pytest.raises(RuntimeError):
                await execution.execute_live_seller_account(e.backend, e.store, e.stripe, e.settings,
                                                           e.mfa, e.request, seller_id=e.user["id"])
    asyncio.run(call())
    assert not e.posts


@pytest.mark.parametrize("endpoint", ["payment_intents", "checkout/sessions", "refunds", "transfers", "payouts", "accounts/acct_other"])
def test_setup_transport_allowlist_cannot_expand_to_financial_or_arbitrary_posts(endpoint):
    with pytest.raises(execution.SellerSetupError):
        asyncio.run(execution._send_setup(None, None, None, None, None, None,
                                          operation_id="synthetic", endpoint=endpoint, data={}))


@pytest.mark.parametrize("endpoint", ["accounts", "account_links", "checkout/sessions", "refunds", "transfers", "payouts"])
def test_global_live_mutation_guard_remains_unchanged(endpoint, monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: pytest.fail("No generic live POST"))
    with pytest.raises(RuntimeError, match="停止"):
        asyncio.run(StripeIntegration("rk_live_SYNTHETIC", "https://toolbako.example")._post(endpoint, {"_idempotency_key": "synthetic"}))


def test_expired_acknowledged_link_allows_only_next_durable_generation(live, monkeypatch):
    e = live
    run(e)
    first = run(e, link=True)
    expires = journal(e, first.operation_id)[2]["expires_at"]
    monkeypatch.setattr(execution.time, "time", lambda: expires + 1)
    second = run(e, link=True)
    assert second.action == "link_created" and second.operation_id != first.operation_id
    assert len(e.posts) == 3 and journal(e, first.operation_id)[1] == "succeeded"
    assert e.store.connected_accounts[e.user["id"]]["onboarding_link"]["generation"] == 2


@pytest.mark.parametrize("after_commit", [False, True])
def test_link_intent_commit_failure_does_not_send_or_orphan_pointer(live, monkeypatch, after_commit):
    e = live
    run(e)
    original = e.backend.begin_operation
    async def failed(*args, **kwargs):
        if after_commit: await original(*args, **kwargs)
        raise RuntimeError("PRIVATE_DB_FAILURE")
    monkeypatch.setattr(e.backend, "begin_operation", failed)
    with pytest.raises(execution.SellerSetupUncertain): run(e, link=True)
    assert len(e.posts) == 1
    assert ("onboarding_link" in e.store.connected_accounts[e.user["id"]]) is after_commit
    monkeypatch.setattr(e.backend, "begin_operation", original)
    assert run(e, link=True).action == ("link_outcome_unknown" if after_commit else "link_created")
    assert len(e.posts) == (1 if after_commit else 2)


@pytest.mark.parametrize("change", [
    {"url": "http://connect.stripe.com/setup/path"},
    {"url": "https://connect.stripe.com@attacker.invalid/setup/path"},
    {"url": "https://connect.stripe.com:444/setup/path"},
    {"url": "https://connect.stripe.com/setup/path#fragment"},
    {"url": "https://connect.stripe.com/"}, {"url": "https://connect.stripe.com/\\bad"},
    {"created": True}, {"created": 1001}, {"expires_at": True}, {"expires_at": 999},
    {"expires_at": 5001}, {"object": "checkout_session"},
])
def test_link_response_validation_is_exact_and_does_not_render_url(change):
    obj = {"object": "account_link", "url": "https://connect.stripe.com/setup/PRIVATE_LINK", "created": 900, "expires_at": 1100}
    # A slightly future created time is accepted, but cannot exceed expires_at.
    if change == {"created": 1001}: change = {"created": 1200}
    obj.update(change)
    with pytest.raises((execution.SellerSetupError, ValueError)) as error:
        execution._validate_link(obj, 1000)
    assert "PRIVATE_LINK" not in str(error.value)


@pytest.mark.parametrize("fault", [None, "off", "mode", "dsn", "host", "user", "port", "dbname", "tls", "unbound"])
def test_actual_target_identity_guard_rejects_forged_or_wrong_connection(fault, monkeypatch):
    from test_live_connection_stage import stage, HOST, USER
    import os
    for name in tuple(os.environ):
        if name.startswith("PG"): monkeypatch.delenv(name)
    settings = stage(stripe_connection_mode="live_setup_only", stripe_live_seller_setup_enabled=True,
                     stripe_webhook_secret="whsec_syntheticplatform", stripe_connect_webhook_secret="whsec_syntheticconnect")
    conn = SimpleNamespace(info=SimpleNamespace(host=HOST, user=USER, port=5432, dbname="postgres"),
                           pgconn=SimpleNamespace(ssl_in_use=True))
    backend = SimpleNamespace(dsn=settings.database_url, _connection=conn)
    if fault == "off": settings = replace(settings, stripe_live_seller_setup_enabled=False)
    elif fault == "mode": settings = replace(settings, stripe_connection_mode="live_read_only")
    elif fault == "dsn": backend.dsn = "different"
    elif fault in {"host", "user", "port", "dbname"}: setattr(conn.info, fault, "wrong")
    elif fault == "tls": conn.pgconn.ssl_in_use = False
    elif fault == "unbound": backend._connection = None
    if fault:
        with pytest.raises(RuntimeError): execution._target_bound_connection(backend, settings)
    else: execution._target_bound_connection(backend, settings)
