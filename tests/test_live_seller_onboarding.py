"""Synthetic journal + real server-session code + HTTP MockTransport only.

No database connection, real Stripe request, account/link creation or migration.
The journal fixture models the persisted binding contract; it does not prove a
Postgres commit. The new preparation module has no mutation executor.
"""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from starlette.requests import Request

from app.config import Settings
from app.data import DemoStore
from app.database import PostgresStateStore, content_hash
from app.financial_scope import FinancialScope, SCOPE_ENDPOINT, validate_binding
from app.integrations import StripeIntegration
from app.live_seller_onboarding import OnboardingPreparationError, prepare_live_seller_onboarding
from app.mfa import SupabaseMFA
from app.security import SessionService


class SyntheticJournal(PostgresStateStore):
    def __init__(self, store, scope):
        super().__init__("postgresql://synthetic.invalid/never_connected")
        self._store, self._connection = store, object()
        self.binding = (SCOPE_ENDPOINT, content_hash(scope.payload()), "succeeded", scope.payload())
        self.details, self.result, self.reads = None, None, []

    async def assert_financial_scope(self, expected):
        self.reads.append("binding")
        validate_binding(self.binding, expected)

    async def operation_details(self, operation_id):
        self.reads.append(("details", operation_id))
        return deepcopy(self.details)

    async def operation_result(self, operation_id):
        self.reads.append(("result", operation_id))
        return deepcopy(self.result)

    async def begin_operation(self, *args, **kwargs):
        raise AssertionError("Read-only preparation cannot create an intent")

    async def finish_operation(self, *args, **kwargs):
        raise AssertionError("Read-only preparation cannot finish an intent")

    async def reconcile_operation(self, *args, **kwargs):
        raise AssertionError("Read-only preparation cannot reconcile an intent")

    async def save(self, *args, **kwargs):
        raise AssertionError("Read-only preparation cannot save a mapping")


@pytest.fixture
def env(monkeypatch):
    scope = FinancialScope("live", "acct_platform", str(uuid4()))
    settings = Settings(environment="staging", demo_mode=False,
                        stripe_secret_key="sk_live_SYNTHETIC_NOT_A_KEY", stripe_charge_mode="separate",
                        stripe_platform_account_id=scope.platform_account, stripe_ledger_id=scope.ledger_id,
                        stripe_connect_accounts_v2=False, site_base_url="https://example.test")
    store = DemoStore(seed=False)
    user = {"id": str(uuid4()), "username": "seller", "email": "private@example.invalid",
            "identity_status": "unverified", "is_verified": False, "email_verified": True}
    store.registered_users[user["username"]] = user
    sessions = SessionService(store, 3600)
    request = Request({"type": "http", "session": {}, "headers": []})
    sessions.establish(request, user)
    at = datetime.now(timezone.utc)
    store.account_sessions[request.session["sid"]].update(
        mfa_verified_at=at, mfa_token_expires_at=at + timedelta(hours=1))
    mfa = SupabaseMFA(settings, sessions)
    journal = SyntheticJournal(store, scope)
    stripe = StripeIntegration(settings.stripe_secret_key, settings.site_base_url, charge_mode="separate",
                               journal=journal, financial_scope=scope)
    e = SimpleNamespace(scope=scope, settings=settings, store=store, user=user, request=request,
                        mfa=mfa, backend=journal, stripe=stripe, requests=[], accounts=[],
                        account_by_id={}, capabilities={}, connected_balance={"object": "balance", "livemode": True},
                        platform_id=scope.platform_account, platform_live=True, incomplete=False)

    def provider(req):
        e.requests.append(req)
        assert req.method == "GET", "No POST is permitted in preparation"
        path = req.url.path
        if path == "/v1/account":
            assert "Stripe-Account" not in req.headers
            return httpx.Response(200, json={"object": "account", "id": e.platform_id})
        if path == "/v1/balance":
            if "Stripe-Account" in req.headers:
                assert req.headers["Stripe-Account"] in e.account_by_id
                return httpx.Response(200, json=e.connected_balance)
            return httpx.Response(200, json={"object": "balance", "livemode": e.platform_live})
        if path == "/v1/accounts":
            return httpx.Response(200, json={"object": "list", "data": e.accounts, "has_more": e.incomplete})
        if "/capabilities/" in path:
            account_id, name = path.split("/")[-3], path.split("/")[-1]
            assert "Stripe-Account" not in req.headers
            return httpx.Response(200, json=e.capabilities[(account_id, name)])
        if path.startswith("/v1/accounts/"):
            assert "Stripe-Account" not in req.headers
            return httpx.Response(200, json=e.account_by_id[path.split("/")[-1]])
        raise AssertionError("Unexpected endpoint")

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(provider), **kwargs))
    return e


def prepare(e, **kwargs):
    args = dict(backend=e.backend, store=e.store, stripe=e.stripe, settings=e.settings,
                mfa=e.mfa, request=e.request, seller_id=e.user["id"])
    args.update(kwargs)
    return asyncio.run(prepare_live_seller_onboarding(**args))


def attach_intent(e, status="unknown", *, mapping=False, ready=False):
    plan = prepare(e)
    e.backend.details = {"endpoint": "accounts", "request_hash": plan.request_hash, "status": status}
    e.backend.result = {"status": status, "response": None}
    metadata = {k[9:-1]: v for k, v in plan.create_parameters if k.startswith("metadata[")}
    account_id = "acct_seller"
    account = {"object": "account", "id": account_id, "country": "JP", "type": "express",
               "default_currency": "jpy", "metadata": metadata,
               "charges_enabled": ready, "payouts_enabled": ready, "details_submitted": ready,
               "settings": {"payouts": {"schedule": {"interval": "manual"}}},
               "capabilities": {"card_payments": "active" if ready else "pending", "transfers": "active" if ready else "inactive"},
               "business_profile": {"name": "SECRET_COMPANY"}, "email": "PRIVATE_EMAIL"}
    e.accounts, e.account_by_id = [account], {account_id: account}
    for name, capability_status in account["capabilities"].items():
        e.capabilities[(account_id, name)] = {"object": "capability", "id": name, "account": account_id,
                                            "requested": True, "status": capability_status}
    if status == "succeeded":
        e.backend.result["response"] = {"id": account_id, "object": "account"}
    if mapping:
        e.store.connected_accounts[e.user["id"]] = {
            "account_id": account_id, "scope": e.scope.payload(), "onboarding_operation_id": plan.operation_id,
            "charges_enabled": False, "payouts_enabled": False, "details_submitted": False}
    return plan, account


def test_server_session_preparation_is_get_only_and_has_deterministic_nonpersonal_parameters(env):
    first = prepare(env)
    env.user["email"] = "changed_private@example.invalid"
    second = prepare(env)
    assert first == second and first.action == "create_intent_needed"
    params = dict(first.create_parameters)
    assert params["type"] == "express" and params["country"] == "JP"
    assert params["settings[payouts][schedule][interval]"] == "manual"
    assert params["metadata[user_id]"] == env.user["id"]
    assert params["metadata[ledger_id]"] == env.scope.ledger_id
    assert params["metadata[platform_account_id]"] == env.scope.platform_account
    assert params["metadata[onboarding_operation_id]"] == first.operation_id
    assert first.request_hash == content_hash(params)
    assert not any(k in params for k in ("email", "business_type", "tos_acceptance", "external_account", "_idempotency_key"))
    assert env.store.connected_accounts == {} and env.backend.details is None
    assert "private" not in repr(first) and "sk_live" not in repr(first)
    assert env.requests and all(req.method == "GET" for req in env.requests)


@pytest.mark.parametrize("bad", ["seller_id", "mfa_claim", "backend_claim", "stripe_claim", "session_store", "settings_identity", "closed_boundary", "wrong_journal", "demo", "destination", "v2"])
def test_untrusted_or_disconnected_context_cannot_authorize(env, bad):
    kwargs = {}
    if bad == "seller_id": kwargs["seller_id"] = str(uuid4())
    elif bad == "mfa_claim": kwargs["mfa"] = {"recent": True, "user_id": env.user["id"]}
    elif bad == "backend_claim": kwargs["backend"] = {"bound": env.scope.payload()}
    elif bad == "stripe_claim": kwargs["stripe"] = {"verified": True}
    elif bad == "session_store": env.mfa.sessions.store = DemoStore(seed=False)
    elif bad == "settings_identity": kwargs["settings"] = replace(env.settings)
    elif bad == "closed_boundary": env.backend._connection = None
    elif bad == "wrong_journal": env.stripe.journal = SyntheticJournal(env.store, env.scope)
    else:
        changes = {"demo": {"demo_mode": True}, "destination": {"stripe_charge_mode": "destination"}, "v2": {"stripe_connect_accounts_v2": True}}[bad]
        env.settings = replace(env.settings, **changes)
        env.mfa.settings = env.settings
    with pytest.raises(OnboardingPreparationError): prepare(env, **kwargs)
    assert not env.requests


@pytest.mark.parametrize("bad", ["missing", "expired", "future", "cookie_claim", "banned", "revoked", "duplicate_user"])
def test_only_current_server_mfa_and_registered_seller_accepted(env, bad):
    record = env.store.account_sessions[env.request.session["sid"]]
    now = datetime.now(timezone.utc)
    if bad == "missing": record.pop("mfa_verified_at")
    elif bad == "expired": record["mfa_verified_at"] = now - timedelta(minutes=11)
    elif bad == "future": record["mfa_verified_at"] = now + timedelta(minutes=1)
    elif bad == "cookie_claim":
        env.request.session.clear()
        env.request.session.update(user=env.user, mfa_verified=True)
    elif bad == "banned": env.user["is_banned"] = True
    elif bad == "revoked": env.store.account_sessions.clear()
    elif bad == "duplicate_user": env.store.registered_users["duplicate"] = dict(env.user, username="duplicate")
    with pytest.raises(OnboardingPreparationError): prepare(env)
    assert not env.requests


@pytest.mark.parametrize("bad", ["missing_binding", "wrong_binding", "test_scope", "platform", "balance_mode", "provider_scope"])
def test_exact_live_private_binding_and_fresh_provider_proof_required(env, bad):
    if bad == "missing_binding": env.backend.binding = None
    elif bad == "wrong_binding":
        other = FinancialScope("live", env.scope.platform_account, str(uuid4()))
        env.backend.binding = (SCOPE_ENDPOINT, content_hash(other.payload()), "succeeded", other.payload())
    elif bad == "test_scope":
        env.settings = replace(env.settings, stripe_secret_key="sk_test_SYNTHETIC")
        env.mfa.settings = env.settings
        env.stripe.secret_key = env.settings.stripe_secret_key
        env.stripe.financial_scope = FinancialScope("test", env.scope.platform_account, env.scope.ledger_id)
    elif bad == "platform": env.platform_id = "acct_other"
    elif bad == "balance_mode": env.platform_live = False
    elif bad == "provider_scope": env.stripe.financial_scope = FinancialScope("live", "acct_other", env.scope.ledger_id)
    with pytest.raises(RuntimeError): prepare(env)
    assert not any(req.url.path == "/v1/accounts" for req in env.requests)


@pytest.mark.parametrize("phase", ["pending", "unknown"])
def test_uncertain_zero_match_never_becomes_a_new_create(env, phase):
    original, _ = attach_intent(env, phase)
    env.accounts = []
    before = deepcopy(env.backend.details)
    for _ in range(2):
        plan = prepare(env)
        assert plan.action == "outcome_unknown" and plan.operation_id == original.operation_id
        assert plan.create_parameters == ()
    assert env.backend.details == before and env.store.connected_accounts == {}


@pytest.mark.parametrize("phase,expected", [("pending", "reconcile_intent_needed"), ("unknown", "reconcile_intent_needed"), ("succeeded", "reconcile_mapping_needed")])
def test_valid_get_match_only_prepares_atomic_recovery(env, phase, expected):
    _, account = attach_intent(env, phase)
    before = deepcopy((env.backend.details, env.backend.result, env.store.connected_accounts))
    plan = prepare(env)
    assert plan.action == expected and plan.account_id == account["id"]
    assert not plan.capabilities_active and not plan.create_parameters
    assert (env.backend.details, env.backend.result, env.store.connected_accounts) == before
    assert "PRIVATE_EMAIL" not in repr(plan) and "SECRET_COMPANY" not in repr(plan)
    connected = [r for r in env.requests if "Stripe-Account" in r.headers]
    assert connected and all(r.headers["Stripe-Account"] == account["id"] for r in connected)


@pytest.mark.parametrize("ready,expected", [(False, "onboarding_link_needed"), (True, "account_ready")])
def test_owned_mapped_account_readiness_is_not_taken_from_local_flags(env, ready, expected):
    attach_intent(env, "succeeded", mapping=True, ready=ready)
    local = env.store.connected_accounts[env.user["id"]]
    local.update(charges_enabled=not ready, payouts_enabled=not ready, details_submitted=not ready)
    before = deepcopy(local)
    plan = prepare(env)
    assert plan.action == expected and plan.capabilities_active is ready
    assert local == before


@pytest.mark.parametrize("bad", ["foreign_owner", "ledger", "platform", "intent", "country", "type", "currency", "livemode", "balance", "schedule", "capability_account", "capability_requested", "capability_missing", "capability_mismatch", "status_type"])
def test_provider_ownership_mode_and_account_contract_fail_closed(env, bad):
    _, account = attach_intent(env)
    if bad == "foreign_owner": account["metadata"]["user_id"] = str(uuid4())
    elif bad == "ledger": account["metadata"]["ledger_id"] = str(uuid4())
    elif bad == "platform": account["metadata"]["platform_account_id"] = "acct_other"
    elif bad == "intent": account["metadata"]["onboarding_operation_id"] = "other"
    elif bad == "country": account["country"] = "US"
    elif bad == "type": account["type"] = "standard"
    elif bad == "currency": account["default_currency"] = "usd"
    elif bad == "livemode": account["livemode"] = False
    elif bad == "balance": env.connected_balance["livemode"] = False
    elif bad == "schedule": account["settings"]["payouts"]["schedule"]["interval"] = "daily"
    elif bad == "capability_account": env.capabilities[(account["id"], "transfers")]["account"] = "acct_other"
    elif bad == "capability_requested": env.capabilities[(account["id"], "transfers")]["requested"] = False
    elif bad == "capability_missing": account["capabilities"].pop("transfers")
    elif bad == "capability_mismatch": env.capabilities[(account["id"], "transfers")]["status"] = "active"
    elif bad == "status_type": account["charges_enabled"] = 1
    with pytest.raises(RuntimeError): prepare(env)
    assert not env.store.connected_accounts


@pytest.mark.parametrize("bad", ["unattributed", "duplicates", "incomplete", "journal_hash", "journal_endpoint", "journal_inconsistent", "receipt_other", "mapping_other", "mapping_unscoped", "duplicate_mapping", "orphan_mapping"])
def test_ambiguous_or_conflicting_history_never_creates_or_adopts(env, bad):
    plan, account = attach_intent(env, "succeeded", mapping=True)
    if bad == "unattributed":
        env.backend.details = env.backend.result = None
        env.store.connected_accounts.clear()
    elif bad == "duplicates": env.accounts.append(deepcopy(account))
    elif bad == "incomplete": env.incomplete = True
    elif bad == "journal_hash": env.backend.details["request_hash"] = "changed"
    elif bad == "journal_endpoint": env.backend.details["endpoint"] = "payouts"
    elif bad == "journal_inconsistent": env.backend.result["status"] = "unknown"
    elif bad == "receipt_other": env.backend.result["response"]["id"] = "acct_other"
    elif bad == "mapping_other": env.store.connected_accounts[env.user["id"]]["account_id"] = "acct_other"
    elif bad == "mapping_unscoped": env.store.connected_accounts[env.user["id"]].pop("scope")
    else:
        other = str(uuid4())
        if bad == "duplicate_mapping": env.store.registered_users["other"] = {"id": other, "username": "other"}
        # Use another server-generated operation key, not the current seller's.
        from app.live_seller_onboarding import _operation_id
        env.store.connected_accounts[other] = {"account_id": account["id"], "scope": env.scope.payload(),
                                               "onboarding_operation_id": _operation_id(env.scope, other)}
    with pytest.raises(RuntimeError): prepare(env)
    assert all(r.method == "GET" for r in env.requests)


def test_rejected_intent_does_not_follow_legacy_new_key_retry(env):
    original, _ = attach_intent(env, "rejected")
    env.accounts = []
    count = len(env.requests)
    for _ in range(2):
        plan = prepare(env)
        assert plan.action == "operator_review_required" and plan.operation_id == original.operation_id
        assert not plan.create_parameters
    assert not any(r.url.path == "/v1/accounts" for r in env.requests[count:])


def test_existing_global_live_post_guard_remains_in_force(env):
    with pytest.raises(RuntimeError):
        asyncio.run(env.stripe._post("accounts", {"_idempotency_key": "synthetic", "country": "JP"}))
    assert not env.requests
