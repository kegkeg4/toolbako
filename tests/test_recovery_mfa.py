"""Recovery-only MFA: isolated provider, real handlers/guards, no real account."""
import base64
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import time
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest
from starlette.exceptions import HTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request

import app.main as main
from app.data import DemoStore
from app.middleware import ProductionGuardMiddleware
from app.security import SessionService
from test_recovery_origin import hidden


SITE = "https://toolbako.example"
USER = "11111111-1111-4111-8111-111111111111"
FACTOR = "22222222-2222-4222-8222-222222222222"
OTHER = "33333333-3333-4333-8333-333333333333"
CHALLENGE = "44444444-4444-4444-8444-444444444444"
TOKEN = "synthetic-recovery-only-" + "x" * 40
CODE = "123456"
PASSWORD = "synthetic-new-password-only"


def jwt(**overrides):
    claims = {"sub": USER, "aal": "aal2", "exp": int(time.time()) + 3600, **overrides}
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "eyJhbGciOiJSUzI1NiJ9." + encoded + ".synthetic-provider-signature"


@pytest.fixture
def recovery_mfa(monkeypatch):
    settings = replace(main.settings, environment="production", demo_mode=False, site_base_url=SITE,
        canonical_redirect_hosts=(), redis_url="", auth_rate_limit_per_minute=1000,
        rate_limit_per_minute=1000, supabase_url="https://auth.invalid", supabase_anon_key="synthetic-public")
    store = DemoStore(seed=False)
    sessions = SessionService(store, 3600)
    for key, value in {"settings": settings, "store": store, "session_service": sessions,
                       "database_store": None, "revoke_user_sessions": sessions.revoke_user}.items():
        monkeypatch.setattr(main, key, value)
    monkeypatch.setattr(main, "establish_session", lambda *a, **k: pytest.fail("Recovery cannot log in"))
    monkeypatch.setattr(main.mfa, "attach_tokens", lambda *a, **k: pytest.fail("Recovery cannot save provider tokens"))
    state = SimpleNamespace(user={"id": USER, "factors": [
        {"id": FACTOR, "factor_type": "totp", "status": "verified", "friendly_name": "Synthetic"}]},
        upgraded=jwt(), verified_user=USER, challenge={"id": CHALLENGE}, verify_failure=None,
        user_failure=None, verify_payload=None, after_verify=None, calls=[])

    class Provider:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def put(self, url, **kwargs):
            assert url == "https://auth.invalid/auth/v1/user"
            state.calls.append(("PUT", "user"))
            if kwargs["headers"]["Authorization"] == f"Bearer {state.upgraded}":
                return httpx.Response(200, json={"id": USER, "email": "synthetic@example.invalid"})
            return httpx.Response(401, json={"error_code": "insufficient_aal", "message": "PRIVATE_PROVIDER"})
        async def request(self, method, url, *, headers, json=None):
            assert url.startswith("https://auth.invalid/auth/v1/")
            path = url.removeprefix("https://auth.invalid/auth/v1/")
            state.calls.append((method, path))
            if method == "GET" and path == "user":
                if state.user_failure is not None: return state.user_failure
                if headers["Authorization"] == f"Bearer {state.upgraded}":
                    return httpx.Response(200, json={"id": state.verified_user, "factors": []})
                return httpx.Response(200, json=state.user)
            assert headers["Authorization"] == f"Bearer {TOKEN}"
            if method == "POST" and path == f"factors/{FACTOR}/challenge":
                assert json == {}
                return httpx.Response(200, json=state.challenge)
            if method == "POST" and path == f"factors/{FACTOR}/verify":
                assert json == {"challenge_id": CHALLENGE, "code": CODE}
                if state.after_verify: state.after_verify()
                if isinstance(state.verify_failure, Exception): raise state.verify_failure
                if state.verify_failure is not None: return state.verify_failure
                return httpx.Response(200, json=state.verify_payload if state.verify_payload is not None
                                      else {"access_token": state.upgraded, "refresh_token": "NEVER_SAVE_THIS_REFRESH"})
            pytest.fail("Unexpected provider operation")

    monkeypatch.setattr(main.httpx, "AsyncClient", Provider)
    app = FastAPI()
    app.add_exception_handler(HTTPException, main.friendly_http_error)
    app.add_api_route("/auth/recovery", main.password_recovery_submit, methods=["POST"])
    app.add_api_route("/auth/recovery/mfa", main.password_recovery_mfa_submit, methods=["POST"])

    @app.get("/fixture/context")
    async def adjust(request: Request, age: int = 0):
        request.session["recovery_mfa"] = {**request.session["recovery_mfa"],
                                           "started_at": int(time.time()) - age}
        return {"synthetic": True}

    app.add_middleware(SessionMiddleware, secret_key="synthetic-recovery-mfa-test",
                       session_cookie="toolbako_session", same_site="lax", https_only=True)
    app.add_middleware(ProductionGuardMiddleware, settings=settings)
    with TestClient(app, base_url=SITE) as client:
        yield SimpleNamespace(client=client, state=state, store=store, app=app)


def start(b):
    return b.client.post("/auth/recovery", data={"access_token": TOKEN, "password": PASSWORD,
        "password_confirmation": PASSWORD}, headers={"Origin": SITE}, follow_redirects=False)


def verify(b, page, **changes):
    data = {"access_token": TOKEN, "recovery_mfa_csrf": hidden(page, "recovery_mfa_csrf"),
            "factor_id": FACTOR, "code": CODE, **changes}
    return b.client.post("/auth/recovery/mfa", data=data, headers={"Origin": SITE}, follow_redirects=False)


def cookie_state(b):
    cookie = b.client.cookies.get("toolbako_session")
    return json.loads(base64.b64decode(cookie.split(".")[0])) if cookie else {}


def private(response, b, caplog):
    assert "no-store" in response.headers["cache-control"]
    assert response.headers["referrer-policy"] == "strict-origin"
    for secret in (TOKEN, CODE, PASSWORD, b.state.upgraded, "NEVER_SAVE_THIS_REFRESH", "PRIVATE_PROVIDER"):
        assert secret not in str(response.headers) + str(b.client.cookies) + caplog.text + repr(b.store.account_sessions)
    assert PASSWORD not in response.text and 'value="123456"' not in response.text
    assert "NEVER_SAVE_THIS_REFRESH" not in response.text and "PRIVATE_PROVIDER" not in response.text


def test_recovery_mfa_then_manual_password_update_never_logs_user_in(recovery_mfa, caplog):
    b = recovery_mfa
    page = start(b)
    assert page.status_code == 200 and 'action="/auth/recovery/mfa"' in page.text
    assert 'data-recovery-form' not in page.text
    assert b.state.calls == [("PUT", "user"), ("GET", "user")]
    assert set(cookie_state(b)) == {"recovery_mfa"}
    assert set(cookie_state(b)["recovery_mfa"]) == {"csrf", "started_at"}
    private(page, b, caplog)
    upgraded = verify(b, page)
    assert upgraded.status_code == 200 and hidden(upgraded, "access_token") == b.state.upgraded
    assert 'action="/auth/recovery"' in upgraded.text
    assert b.state.calls == [("PUT", "user"), ("GET", "user"), ("GET", "user"),
        ("POST", f"factors/{FACTOR}/challenge"), ("POST", f"factors/{FACTOR}/verify"), ("GET", "user")]
    assert b.store.account_sessions == {} and cookie_state(b) == {}
    private(upgraded, b, caplog)
    before = list(b.state.calls)
    replay = verify(b, page)
    assert replay.status_code == 403 and b.state.calls == before
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    b.store.account_sessions["old-device"] = {"user": {"id": USER}, "expires_at": expires}
    success = b.client.post("/auth/recovery", data={"access_token": b.state.upgraded,
        "password": PASSWORD, "password_confirmation": PASSWORD}, headers={"Origin": SITE}, follow_redirects=False)
    assert success.status_code == 303 and success.headers["location"] == "/login?password_reset=1"
    assert b.store.account_sessions == {} and b.state.calls[-1] == ("PUT", "user")


def test_invalid_nonce_does_not_consume_current_flow(recovery_mfa):
    b = recovery_mfa
    page = start(b)
    before = list(b.state.calls)
    for csrf in ("", "wrong", "x" * 43):
        assert verify(b, page, recovery_mfa_csrf=csrf).status_code == 403
        assert b.state.calls == before
    assert verify(b, page).status_code == 200


@pytest.mark.parametrize("age", [600, 601, -1])
def test_expired_or_future_flow_does_not_call_provider(recovery_mfa, age):
    b = recovery_mfa
    page = start(b)
    b.client.get(f"/fixture/context?age={age}")
    before = list(b.state.calls)
    assert verify(b, page).status_code == 403
    assert b.state.calls == before and cookie_state(b) == {}


@pytest.mark.parametrize("headers", [{}, {"Origin": "null"}, {"Origin": "https://foreign.invalid"}])
def test_origin_guard_rejects_before_mfa_provider(recovery_mfa, headers):
    b = recovery_mfa
    page = start(b)
    before = list(b.state.calls)
    response = b.client.post("/auth/recovery/mfa", data={"access_token": TOKEN,
        "recovery_mfa_csrf": hidden(page, "recovery_mfa_csrf"), "factor_id": FACTOR, "code": CODE}, headers=headers)
    assert response.status_code == 403 and b.state.calls == before


@pytest.mark.parametrize("change", [{"factor_id": "../../admin"}, {"code": "12"}, {"code": "１２３４５６"}])
def test_bad_form_format_cannot_create_challenge(recovery_mfa, change):
    b = recovery_mfa
    page = start(b)
    response = verify(b, page, **change)
    assert response.status_code == 422
    assert not any(method == "POST" for method, path in b.state.calls)


@pytest.mark.parametrize("fault", ["foreign", "unverified", "phone", "no_factors"])
def test_factor_is_fresh_owned_and_verified_before_challenge(recovery_mfa, fault):
    b = recovery_mfa
    page = start(b)
    factor = b.state.user["factors"][0]
    if fault == "foreign": factor["id"] = OTHER
    elif fault == "unverified": factor["status"] = "unverified"
    elif fault == "phone": factor["factor_type"] = "phone"
    else: b.state.user["factors"] = []
    response = verify(b, page)
    assert response.status_code == 403 and TOKEN not in response.text
    assert not any(method == "POST" for method, path in b.state.calls)


@pytest.mark.parametrize("fault", ["aal1", "subject", "expired", "bool_exp", "bad_token", "no_token", "challenge", "fresh_user"])
def test_invalid_provider_assurance_never_returns_password_form(recovery_mfa, caplog, fault):
    b = recovery_mfa
    page = start(b)
    if fault == "aal1": b.state.upgraded = jwt(aal="aal1")
    elif fault == "subject": b.state.upgraded = jwt(sub=OTHER)
    elif fault == "expired": b.state.upgraded = jwt(exp=1)
    elif fault == "bool_exp": b.state.upgraded = jwt(exp=True)
    elif fault == "bad_token": b.state.upgraded = "synthetic-malformed-provider-token"
    elif fault == "no_token": b.state.verify_payload = {"refresh_token": "NEVER_SAVE_THIS_REFRESH"}
    elif fault == "challenge": b.state.challenge = {"id": "foreign-path"}
    else: b.state.verified_user = OTHER
    response = verify(b, page)
    assert response.status_code == 403 and 'data-recovery-form' not in response.text
    assert TOKEN not in response.text and b.state.upgraded not in response.text
    assert b.store.account_sessions == {} and cookie_state(b) == {}
    private(response, b, caplog)


@pytest.mark.parametrize("status, code", [(422, "mfa_verification_failed"), (429, "over_request_rate_limit")])
def test_code_or_rate_rejection_allows_manual_retry_without_extending_flow(recovery_mfa, caplog, status, code):
    b = recovery_mfa
    page = start(b)
    original_start = cookie_state(b)["recovery_mfa"]["started_at"]
    b.state.verify_failure = httpx.Response(status, json={"error_code": code, "message": "PRIVATE_PROVIDER"})
    rejected = verify(b, page)
    assert rejected.status_code == (429 if status == 429 else 400)
    assert hidden(rejected, "access_token") == TOKEN
    assert cookie_state(b)["recovery_mfa"]["started_at"] == original_start
    assert sum(method == "POST" and path.endswith("/verify") for method, path in b.state.calls) == 1
    private(rejected, b, caplog)
    b.state.verify_failure = None
    assert verify(b, rejected).status_code == 200


def test_provider_timeout_does_not_retry_mfa_or_show_exception(recovery_mfa, caplog):
    b = recovery_mfa
    page = start(b)
    b.state.verify_failure = httpx.ReadTimeout("PRIVATE_PROVIDER" + TOKEN + CODE)
    response = verify(b, page)
    assert response.status_code == 403 and TOKEN not in response.text
    assert sum(method == "POST" and path.endswith("/verify") for method, path in b.state.calls) == 1
    private(response, b, caplog)


def test_flow_expiring_during_provider_calls_discards_upgraded_token(recovery_mfa, monkeypatch):
    b = recovery_mfa
    page = start(b)
    now = time.time()
    b.state.after_verify = lambda: monkeypatch.setattr(main.time, "time", lambda: now + 601)
    response = verify(b, page)
    assert response.status_code == 403
    assert b.state.upgraded not in response.text and TOKEN not in response.text
    assert cookie_state(b) == {} and b.store.account_sessions == {}


@pytest.mark.parametrize("user", [{}, {"id": 123}, {"id": USER, "factors": {}},
    {"id": USER, "factors": [{"id": "bad", "factor_type": "totp", "status": "verified"}]},
    {"id": USER, "factors": []}])
def test_unverified_provider_user_cannot_open_mfa_form(recovery_mfa, user):
    b = recovery_mfa
    b.state.user = deepcopy(user)
    response = start(b)
    assert response.status_code == 403 and 'data-recovery-mfa-form' not in response.text
    assert TOKEN not in response.text and cookie_state(b) == {}
