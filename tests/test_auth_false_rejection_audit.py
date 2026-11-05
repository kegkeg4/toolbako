"""Isolated OAuth state audit: no provider network, email or credential changes."""
import base64
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest
from starlette.middleware.sessions import SessionMiddleware

import app.main as main
from app.data import DemoStore
from app.mfa import SupabaseMFA
from app.middleware import ProductionGuardMiddleware
from app.security import SessionService


SITE = "https://toolbako.example"
ACCESS_TOKEN = "synthetic-access-token-only"
REFRESH_TOKEN = "synthetic-refresh-token-only"


@pytest.fixture
def oauth_browser(monkeypatch):
    settings = replace(main.settings, environment="production", demo_mode=False,
                       site_base_url=SITE, canonical_redirect_hosts=(), redis_url="",
                       auth_rate_limit_per_minute=1000, supabase_url="https://auth.invalid",
                       supabase_anon_key="synthetic-public-key", oauth_providers=("google",))
    store = DemoStore(seed=False)
    sessions = SessionService(store, 3600)
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "session_service", sessions)
    monkeypatch.setattr(main, "establish_session", sessions.establish)
    monkeypatch.setattr(main, "mfa", SupabaseMFA(settings, sessions))

    async def persist_nothing():
        pass

    monkeypatch.setattr(main, "persist_state", persist_nothing)
    calls = []
    provider = SimpleNamespace(token_status=200)

    class Provider:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):
            assert url == "https://auth.invalid/auth/v1/token?grant_type=pkce"
            calls.append(("POST", kwargs))
            if provider.token_status != 200:
                return httpx.Response(provider.token_status, json={"error": "synthetic-denied"})
            return httpx.Response(200, json={"access_token": ACCESS_TOKEN,
                "refresh_token": REFRESH_TOKEN,
                "user": {"id": "synthetic-user", "email": "synthetic@example.invalid"}})

        async def get(self, url, **kwargs):
            assert url.startswith("https://auth.invalid/rest/v1/profiles?")
            assert kwargs["headers"]["Authorization"] == f"Bearer {ACCESS_TOKEN}"
            calls.append(("GET", kwargs))
            return httpx.Response(200, json=[{"id": "synthetic-user", "username": "synthetic",
                "display_name": "Synthetic", "identity_status": "unverified", "is_banned": False}])

    monkeypatch.setattr(main.httpx, "AsyncClient", Provider)
    app = FastAPI()
    app.add_api_route("/auth/oauth/{provider}", main.oauth, methods=["GET"])
    app.add_api_route("/auth/callback", main.auth_callback, methods=["GET"])
    app.add_middleware(SessionMiddleware, secret_key="synthetic-session-secret-only",
                       session_cookie="toolbako_session", same_site="lax", https_only=True)
    app.add_middleware(ProductionGuardMiddleware, settings=settings)
    with TestClient(app, base_url=SITE) as client:
        yield SimpleNamespace(client=client, app=app, calls=calls, store=store, provider=provider)


def begin(browser):
    result = browser.client.get("/auth/oauth/google", params={"next": "/seller?tab=tools"},
                                follow_redirects=False)
    assert result.status_code == 303
    authorize = parse_qs(urlsplit(result.headers["location"]).query)
    assert authorize["code_challenge_method"] == ["s256"]
    assert len(authorize["code_challenge"][0]) == 43
    callback = parse_qs(urlsplit(authorize["redirect_to"][0]).query)
    return callback["state"][0]


def callback(browser, state, code="synthetic-auth-code"):
    result = browser.client.get("/auth/callback", params={"state": state, "code": code,
        "next": "/seller?tab=tools"}, follow_redirects=False)
    assert result.headers["referrer-policy"] == "strict-origin"
    return result


def assert_login_success(browser, result):
    assert result.status_code == 303
    assert result.headers["referrer-policy"] == "strict-origin"
    assert result.headers["location"] == "/seller?tab=tools"
    assert [call[0] for call in browser.calls] == ["POST", "GET"]
    assert len(browser.store.account_sessions) == 1
    cookie = browser.client.cookies.get("toolbako_session")
    cookie_payload = base64.b64decode(cookie.split(".")[0]).decode()
    for secret in (ACCESS_TOKEN, REFRESH_TOKEN):
        assert secret not in str(result.headers) + cookie_payload


def test_single_oauth_flow_succeeds_without_weakening_state_or_pkce(oauth_browser):
    state = begin(oauth_browser)
    assert_login_success(oauth_browser, callback(oauth_browser, state))
    replay = callback(oauth_browser, state)
    assert replay.status_code == 400
    assert len(oauth_browser.calls) == 2


def test_older_tab_callback_does_not_consume_newer_valid_oauth_flow(oauth_browser):
    older = begin(oauth_browser)
    newer = begin(oauth_browser)
    assert older != newer
    stale = callback(oauth_browser, older, "synthetic-older-code")
    assert stale.status_code == 400 and not oauth_browser.calls
    assert_login_success(oauth_browser, callback(oauth_browser, newer, "synthetic-newer-code"))


@pytest.mark.parametrize("unrelated_state", ["", "not-this-browser-state"])
def test_unrelated_callback_cannot_destroy_pending_valid_state(oauth_browser, unrelated_state):
    state = begin(oauth_browser)
    invalid = callback(oauth_browser, unrelated_state)
    assert invalid.status_code == 400 and not oauth_browser.calls
    assert_login_success(oauth_browser, callback(oauth_browser, state))


def test_oauth_callback_still_rejects_a_different_browser(oauth_browser):
    state = begin(oauth_browser)
    with TestClient(oauth_browser.app, base_url=SITE) as other:
        result = other.get("/auth/callback", params={"code": "synthetic-auth-code", "state": state},
                           follow_redirects=False)
    assert result.status_code == 400 and not oauth_browser.calls
    assert result.headers["referrer-policy"] == "strict-origin"
    assert_login_success(oauth_browser, callback(oauth_browser, state))


@pytest.mark.parametrize("terminal", ["cancelled", "missing_code"])
def test_matching_callback_terminal_failure_is_consumed_once(oauth_browser, terminal):
    state = begin(oauth_browser)
    params = {"state": state, "code": "synthetic-auth-code"}
    if terminal == "cancelled":
        params["error"] = "access_denied"
    else:
        params["code"] = ""
    result = oauth_browser.client.get("/auth/callback", params=params, follow_redirects=False)
    assert result.status_code == 400 and not oauth_browser.calls
    assert result.headers["referrer-policy"] == "strict-origin"
    retry = callback(oauth_browser, state)
    assert retry.status_code == 400 and not oauth_browser.calls
    assert not oauth_browser.store.account_sessions


def test_provider_exchange_failure_keeps_matching_callback_one_use(oauth_browser):
    state = begin(oauth_browser)
    oauth_browser.provider.token_status = 403
    result = callback(oauth_browser, state)
    assert result.status_code == 403
    assert [call[0] for call in oauth_browser.calls] == ["POST"]
    retry = callback(oauth_browser, state)
    assert retry.status_code == 400 and len(oauth_browser.calls) == 1
    assert not oauth_browser.store.account_sessions


@pytest.mark.parametrize("bad_state", ["正当なstateではない", "state-\N{SNOWMAN}", "bad\x00state"])
def test_malformed_unrelated_state_is_safe_400_and_preserves_current_flow(oauth_browser, bad_state, caplog):
    state = begin(oauth_browser)
    result = callback(oauth_browser, bad_state)
    assert result.status_code == 400 and not oauth_browser.calls
    assert bad_state not in result.text + caplog.text
    assert_login_success(oauth_browser, callback(oauth_browser, state))
