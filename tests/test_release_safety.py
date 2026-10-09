import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone
from threading import RLock
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.config import Settings
from app.data import DemoStore
from app.integrations import StripeIntegration, StripeOutcomeUnknown
from app.mfa import SupabaseMFA, is_privileged_path
from app.middleware import ProductionGuardMiddleware
from app.payout_policy import JST, payout_fee, scheduled_payout_date
from app.production import readiness_summary
from app.security import SessionService


FACTOR = "05c96b3b-aa7e-4f9e-b3d8-b5bce5acfe2b"


@pytest.fixture
def mfa_session():
    user = {"id": "user-1", "username": "member"}
    store = SimpleNamespace(_lock=RLock(), registered_users={"member": user}, account_sessions={}, identity_applications={}, execute_due_account_deletions=lambda at: None)
    sessions = SessionService(store, 3600)
    request = Request({"type": "http", "session": {}, "headers": [], "path": "/security/mfa", "method": "POST"})
    sessions.establish(request, user)
    mfa = SupabaseMFA(Settings(supabase_url="https://project.supabase.co", supabase_anon_key="test-public-key"), sessions)
    mfa.attach_tokens(request, {"access_token": "initial-token", "refresh_token": "refresh-token"})
    return mfa, request


def jwt_response(**overrides):
    claims = {"sub": "user-1", "aal": "aal2", "exp": (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp(), **overrides}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return {"access_token": f"header.{payload}.signature", "refresh_token": "new-refresh-token"}


def test_identity_webhook_result_refreshes_existing_session(mfa_session):
    mfa, request = mfa_session
    assert not mfa.sessions.current(request)["is_verified"]
    mfa.sessions.store.identity_applications["user-1"] = {"status": "verified"}
    assert mfa.sessions.current(request)["is_verified"]
    mfa.sessions.store.identity_applications["user-1"]["status"] = "rejected"
    assert not mfa.sessions.current(request)["is_verified"]


def fake_auth_api(monkeypatch, response=None, *, fail_verify=False):
    calls = []
    def handle(request):
        calls.append((request.method, request.url.path))
        path = request.url.path
        if path.endswith("/user"):
            return httpx.Response(200, json={"id": "user-1", "factors": [{"id": FACTOR, "status": "verified", "factor_type": "totp"}]})
        if path.endswith("/challenge"):
            return httpx.Response(200, json={"id": "challenge-1"})
        if path.endswith("/verify"):
            assert json.loads(request.content) == {"challenge_id": "challenge-1", "code": "123456"}
            return httpx.Response(400 if fail_verify else 200, json=response or jwt_response())
        raise AssertionError(f"unexpected provider operation {request.method} {path}")
    cls = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: cls(transport=httpx.MockTransport(handle), **kw))
    return calls


def test_mfa_elevates_only_after_provider_verification_and_rotates_cookie(mfa_session, monkeypatch):
    mfa, request = mfa_session
    calls = fake_auth_api(monkeypatch)
    old_sid = request.session["sid"]
    assert not mfa.recent(request)
    asyncio.run(mfa.verify(request, FACTOR, "123456"))
    assert mfa.recent(request)
    assert request.session["sid"] != old_sid
    assert old_sid not in mfa.sessions.store.account_sessions
    assert list(request.session) == ["sid"]
    assert "provider_tokens" not in mfa.sessions.current(request)
    assert len(calls) == 3


@pytest.mark.parametrize("claims", [{"aal": "aal1"}, {"sub": "another-user"}, {"exp": 0}])
def test_mfa_rejects_wrong_assurance_subject_or_expiry(mfa_session, monkeypatch, claims):
    mfa, request = mfa_session
    fake_auth_api(monkeypatch, jwt_response(**claims))
    with pytest.raises(HTTPException) as err:
        asyncio.run(mfa.verify(request, FACTOR, "123456"))
    assert err.value.status_code == 401
    assert not mfa.recent(request)


def test_mfa_rejects_provider_failure(mfa_session, monkeypatch):
    mfa, request = mfa_session
    fake_auth_api(monkeypatch, fail_verify=True)
    with pytest.raises(HTTPException):
        asyncio.run(mfa.verify(request, FACTOR, "123456"))
    assert not mfa.recent(request)


def test_mfa_rejects_another_accounts_factor(mfa_session, monkeypatch):
    mfa, request = mfa_session
    calls = fake_auth_api(monkeypatch)
    with pytest.raises(HTTPException) as err:
        asyncio.run(mfa.verify(request, "f8a75eb6-1913-4223-953a-3612d720f501", "123456"))
    assert err.value.status_code == 403
    assert len(calls) == 1


@pytest.mark.parametrize("code", ["", "12345", "1234567", "１２３４５６", "abcdef"])
def test_mfa_validates_code_before_provider_call(mfa_session, code):
    mfa, request = mfa_session
    with pytest.raises(HTTPException) as err:
        asyncio.run(mfa.verify(request, FACTOR, code))
    assert err.value.status_code == 422


def test_mfa_freshness_expires_and_is_session_specific(mfa_session):
    mfa, request = mfa_session
    record = mfa.record(request)
    record["mfa_verified_at"] = datetime.now(timezone.utc) - timedelta(minutes=11)
    record["mfa_token_expires_at"] = datetime.now(timezone.utc) + timedelta(hours=1)
    assert not mfa.recent(request)
    record["mfa_verified_at"] = datetime.now(timezone.utc)
    assert mfa.recent(request)
    user = mfa.sessions.current(request)
    mfa.sessions.establish(request, user)
    assert not mfa.recent(request)


@pytest.mark.parametrize("path", ["/admin", "/admin/", "/admin/users/u/ban", "/payouts", "/seller/payments", "/account/export"])
def test_sensitive_paths_are_protected(path):
    assert is_privileged_path(path, "GET")


def test_mfa_flow_does_not_protect_itself():
    assert not is_privileged_path("/security/mfa/verify", "POST")
    assert not is_privileged_path("/tools", "GET")
    assert is_privileged_path("/settings", "POST")


@pytest.mark.parametrize("key", ["sk_live_example", "rk_live_example"])
def test_live_stripe_writes_are_blocked_until_ledger_is_ready(key):
    stripe = StripeIntegration(key, "https://example.com")
    with pytest.raises(RuntimeError, match="本番決済"):
        asyncio.run(stripe._post("transfers", {"amount": "1000"}))


def test_subscription_does_not_send_payment_intent_parameters(monkeypatch):
    stripe = StripeIntegration("sk_test_example", "https://example.com", charge_mode="destination")
    async def capture(path, data):
        return data
    monkeypatch.setattr(stripe, "_post", capture)
    order = {"id": "one", "tool_slug": "tool", "amount": 1000, "platform_fee": 180, "platform_fee_rate": .18, "tool_name": "test", "billing_type": "subscription"}
    data = asyncio.run(stripe.create_checkout(order, "acct_test"))
    assert not any(key.startswith("payment_intent_data") for key in data)
    assert data["subscription_data[application_fee_percent]"] == "18.0"
    stripe.charge_mode = "separate"
    with pytest.raises(RuntimeError, match="月額契約"):
        asyncio.run(stripe.create_checkout(order, "acct_test"))


@pytest.mark.parametrize("amount,fee", [(161, 160), (2999, 160), (3000, 0), (10000, 0)])
def test_payout_fee_boundaries(amount, fee):
    assert payout_fee(amount, Settings()) == fee


@pytest.mark.parametrize("amount", [0, -1, 159, 160, True, 161.0])
def test_zero_net_and_invalid_payouts_are_rejected(amount):
    with pytest.raises(ValueError):
        payout_fee(amount, Settings(payout_minimum=160))


@pytest.mark.parametrize("requested,expected", [
    ("2026-09-06T23:59:59+09:00", "2026-09-10"),
    ("2026-09-07T00:00:00+09:00", "2026-09-17"),
    ("2026-12-31T12:00:00+09:00", "2027-01-07"),
    ("2026-09-06T15:00:00+00:00", "2026-09-17"),
])
def test_schedule_uses_japanese_week_boundary(requested, expected):
    assert scheduled_payout_date(datetime.fromisoformat(requested)).date().isoformat() == expected


def earnings_store():
    fresh = DemoStore()
    fresh.orders = []
    fresh.payouts = []
    fresh.connected_accounts = {"seller": {"payouts_enabled": True, "details_submitted": True}}
    return fresh


def earning(id, at):
    return {"id": id, "seller_id": "seller", "amount": 1000, "platform_fee": 100, "payment_status": "paid", "status": "completed", "created_at": at, "completed_at": at, "updated_at": at}


def test_expired_payouts_repeat_safely_and_do_not_sweep_new_earnings():
    fresh = earnings_store()
    now = datetime.now(timezone.utc)
    fresh.orders = [earning("old", now - timedelta(days=121)), earning("new", now)]
    first = fresh.process_expired_payouts("seller", at=now)
    assert len(first) == 1 and first[0]["amount"] == 900
    assert first[0]["net_amount"] == 740
    assert fresh.process_expired_payouts("seller", at=now) == []
    assert fresh.available_balance("seller") == 900
    second = fresh.process_expired_payouts("seller", at=now + timedelta(days=121))
    assert len(second) == 1 and second[0]["amount"] == 900
    assert fresh.available_balance("seller") == 0


def test_held_payouts_cannot_be_requested_or_automatically_reserved():
    fresh = earnings_store()
    now = datetime.now(timezone.utc)
    fresh.orders = [earning("old", now - timedelta(days=121))]
    fresh.connected_accounts["seller"]["payouts_paused"] = True
    assert fresh.process_expired_payouts("seller", at=now) == []
    with pytest.raises(ValueError, match="paused"):
        fresh.create_payout("seller", 900)


def test_paid_status_is_reserved():
    fresh = earnings_store()
    fresh.orders = [earning("one", datetime.now(timezone.utc))]
    fresh.payouts = [{"seller_id": "seller", "amount": 900, "status": "paid"}]
    assert fresh.available_balance("seller") == 0


def test_readiness_never_marks_unimplemented_money_or_audit_complete():
    summary = readiness_summary(Settings(environment="production", supabase_url="https://project.supabase.co", supabase_anon_key="public", supabase_service_role_key="server", stripe_secret_key="sk_live_test", stripe_webhook_secret="whsec_test", stripe_connect_webhook_secret="whsec_connect_test", stripe_connect_client_id="ca_test", state_db_path="/tmp/demo.db"))
    checks = {c["key"]: c["ok"] for c in summary["checks"]}
    assert not checks["transaction_database"]
    assert not checks["payments"]
    assert not checks["audit"]
    assert not summary["ready"]


def guard_app(settings):
    app = FastAPI()
    app.add_middleware(ProductionGuardMiddleware, settings=settings)
    @app.get("/test")
    @app.post("/test")
    def endpoint():
        return {"ok": True}
    @app.get("/healthz")
    def health():
        return {"status": "ok"}
    return app


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_redis_outage_fails_closed_but_health_check_survives(monkeypatch, environment):
    async def unavailable(*args):
        return None
    monkeypatch.setattr(ProductionGuardMiddleware, "_distributed_allowed", unavailable)
    client = TestClient(guard_app(Settings(environment=environment, redis_url="redis://localhost:6379")))
    assert client.get("/test").status_code == 503
    assert client.get("/healthz").status_code == 200


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_origin_scheme_must_match(environment):
    client = TestClient(guard_app(Settings(environment=environment, site_base_url="https://toolbako.example", redis_url="")))
    assert client.post("/test").status_code == 403
    assert client.post("/test", headers={"origin": "http://toolbako.example"}).status_code == 403
    assert client.post("/test", headers={"origin": "https://toolbako.example"}).status_code == 200


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_persistence_failure_does_not_return_success_or_leak_secret(environment, caplog):
    app = guard_app(Settings(environment=environment, site_base_url="https://toolbako.example", redis_url=""))
    async def failed():
        raise RuntimeError("storage unavailable provider-private-password")
    app.state.persist = failed
    response = TestClient(app).post("/test", headers={"origin": "https://toolbako.example"})
    assert response.status_code == 500
    assert response.json()["error"] == "internal_server_error"
    assert "provider-private-password" not in response.text
    assert "provider-private-password" not in caplog.text
    logged = [r for r in caplog.records if r.name == "toolbako.requests"]
    assert len(logged) == 1 and logged[0].exc_info is None
    assert response.headers["x-request-id"] in logged[0].getMessage()
    assert "error_type=RuntimeError" in logged[0].getMessage()


def test_actual_admin_routes_require_recent_mfa():
    from app.main import app, settings, store
    client = TestClient(app)
    client.get('/auth/demo', follow_redirects=False)
    original = settings.privileged_mfa_required
    object.__setattr__(settings, 'privileged_mfa_required', True)
    try:
        for path in ("/admin", "/seller", "/payouts"):
            response = client.get(path, follow_redirects=False)
            assert response.status_code == 303
            assert response.headers["location"].startswith("/security/mfa?")
        record = next(r for r in reversed(store.account_sessions.values()) if r["user"].get("username") == "demo_creator")
        record["mfa_verified_at"] = datetime.now(timezone.utc)
        record["mfa_token_expires_at"] = datetime.now(timezone.utc) + timedelta(minutes=30)
        assert client.get("/admin").status_code == 200
    finally:
        object.__setattr__(settings, 'privileged_mfa_required', original)


def test_payout_page_never_runs_automatic_payout_job(monkeypatch):
    from app.main import app, store
    client = TestClient(app)
    client.get('/auth/demo', follow_redirects=False)
    def forbidden(*args):
        raise AssertionError("GET must not reserve funds")
    monkeypatch.setattr(store, "process_expired_payouts", forbidden)
    assert client.get('/payouts').status_code == 200


def test_redis_counter_and_expiry_use_one_atomic_operation():
    settings = Settings(redis_url="")
    guard = ProductionGuardMiddleware(FastAPI(), settings)
    class RedisStub:
        async def eval(self, script, count, key):
            assert "INCR" in script and "EXPIRE" in script and "TTL" in script
            assert count == 1 and key == "toolbako:ratelimit:ip:auth"
            return 11
    guard._redis = RedisStub()
    assert asyncio.run(guard._distributed_allowed("ip:auth", 10)) is False


def test_mfa_enroll_restarts_only_unverified_factors(mfa_session, monkeypatch):
    mfa, request = mfa_session
    calls = []
    def handle(req):
        calls.append((req.method, req.url.path))
        if req.url.path.endswith("/user"):
            return httpx.Response(200, json={"id": "user-1", "factors": [{"id": FACTOR, "status": "unverified", "factor_type": "totp"}]})
        if req.method == "DELETE":
            assert req.url.path.endswith(FACTOR)
            return httpx.Response(204)
        assert req.method == "POST" and req.url.path.endswith("/factors")
        return httpx.Response(200, json={"id": FACTOR, "totp": {"secret": "test-secret", "qr_code": "<svg></svg>"}})
    cls = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: cls(transport=httpx.MockTransport(handle), **kw))
    data = asyncio.run(mfa.enroll(request))
    assert data["id"] == FACTOR
    assert [method for method, _ in calls] == ["GET", "DELETE", "POST"]
    assert "test-secret" not in json.dumps(request.session)
    assert not mfa.recent(request)


def test_enrollment_cannot_remove_verified_factor(mfa_session, monkeypatch):
    mfa, request = mfa_session
    calls = fake_auth_api(monkeypatch)
    with pytest.raises(HTTPException) as err:
        asyncio.run(mfa.enroll(request))
    assert err.value.status_code == 409
    assert len(calls) == 1


def test_mfa_refreshes_expired_access_token_without_browser_exposure(mfa_session, monkeypatch):
    mfa, request = mfa_session
    calls = []
    def handle(req):
        calls.append(req.url.path)
        if req.url.path.endswith("/token"):
            assert json.loads(req.content)["refresh_token"] == "refresh-token"
            return httpx.Response(200, json={"access_token": "refreshed-token", "refresh_token": "rotated-refresh", "user": {"id": "user-1"}})
        if req.headers["Authorization"] == "Bearer initial-token":
            return httpx.Response(401, json={})
        assert req.headers["Authorization"] == "Bearer refreshed-token"
        return httpx.Response(200, json={"id": "user-1", "factors": []})
    cls = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: cls(transport=httpx.MockTransport(handle), **kw))
    assert asyncio.run(mfa.factors(request)) == []
    assert len(calls) == 3
    assert mfa.record(request)["provider_tokens"]["refresh_token"] == "rotated-refresh"
    assert not mfa.recent(request)
    assert list(request.session) == ["sid"]


@pytest.mark.parametrize("outcome", ["timeout", "malformed", "conflict", "server_error"])
def test_uncertain_stripe_outcome_is_not_a_definite_failure(monkeypatch, outcome):
    def handle(req):
        if outcome == "timeout":
            raise httpx.ReadTimeout("read timed out")
        if outcome == "malformed":
            return httpx.Response(200, content="not json")
        return httpx.Response(409 if outcome == "conflict" else 500, json={"error": {}})
    cls = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: cls(transport=httpx.MockTransport(handle), **kw))
    with pytest.raises(StripeOutcomeUnknown) as err:
        asyncio.run(StripeIntegration("sk_test_example", "https://example.com")._post("checkout/sessions", {"_idempotency_key": "checkout-stable-id"}))
    assert err.value.operation_id == "checkout-stable-id"
    assert not isinstance(err.value, RuntimeError)


def test_checkout_timeout_preserves_order_and_prevents_second_charge(monkeypatch):
    from uuid import uuid4
    from app.main import app, settings, store, stripe
    username = "uncertain_" + uuid4().hex[:10]
    client = TestClient(app)
    response = client.post("/signup", data={"display_name": "決済テスト", "username": username, "email": f"{username}@example.com", "password": "test-password-123", "terms_agreement": "yes"}, follow_redirects=False)
    assert response.status_code == 303
    buyer_id = store.registered_users[username]["id"]
    seller_id = store.get("minutes-magic")["author_id"]
    monkeypatch.setitem(store.connected_accounts, seller_id, {"charges_enabled": True, "account_id": "acct_test_seller"})
    calls = []
    async def uncertain(order, destination):
        calls.append(order["id"])
        raise StripeOutcomeUnknown(f"checkout-{order['id']}")
    monkeypatch.setattr(stripe, "create_checkout", uncertain)
    overrides = {"demo_mode": False, "stripe_secret_key": "sk_test_only", "stripe_webhook_secret": "whsec_test", "stripe_connect_webhook_secret": "whsec_connect", "stripe_connect_client_id": "ca_test"}
    original = {key: getattr(settings, key) for key in overrides}
    try:
        for key, value in overrides.items():
            object.__setattr__(settings, key, value)
        first = client.post("/checkout/minutes-magic", data={"purchase_agreement": "yes"}, follow_redirects=False)
        assert first.status_code == 502
        assert "再購入せず" in first.text
        pending = [o for o in store.orders if o["buyer_id"] == buyer_id]
        assert len(pending) == 1
        assert pending[0]["payment_status"] == "pending"
        assert pending[0]["payment_reconciliation_required"] is True
        again = client.post("/checkout/minutes-magic", data={"purchase_agreement": "yes"}, follow_redirects=False)
        assert again.status_code == 409
        assert len(calls) == 1
        assert any(log["action"] == "stripe.reconciliation_required" and log["target"] == f"checkout-{calls[0]}" for log in store.audit_logs)
    finally:
        for key, value in original.items():
            object.__setattr__(settings, key, value)
        store.orders[:] = [o for o in store.orders if o["buyer_id"] != buyer_id]
        store.registered_users.pop(username, None)
