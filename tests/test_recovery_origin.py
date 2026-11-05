"""Native form origin regression with real auth handlers and deployed guards.

The header model is the Fetch no-cors POST/referrer-policy contract, not an
iPhone/WebKit trace. Provider I/O, users and sessions are entirely artificial.
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import base64
import re
from types import SimpleNamespace
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest
from starlette.middleware.sessions import SessionMiddleware

import app.main as main
from app.data import DemoStore
from app.middleware import ProductionGuardMiddleware
from app.security import SessionService


SITE = "https://toolbako.example"
TOKEN_HASH = "a" * 56
ACCESS_TOKEN = "synthetic-recovery-" + "x" * 40
PASSWORD = "synthetic-password-only"
USER_ID = "synthetic-recovery-user"


@pytest.fixture
def recovery_browser(monkeypatch):
    settings = replace(main.settings, environment="production", demo_mode=False,
                       site_base_url=SITE, canonical_redirect_hosts=(), redis_url="",
                       auth_rate_limit_per_minute=1000, supabase_url="https://auth.invalid",
                       supabase_anon_key="synthetic-public-key")
    store = DemoStore(seed=False)
    sessions = SessionService(store, 3600)
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "session_service", sessions)
    monkeypatch.setattr(main, "revoke_user_sessions", sessions.revoke_user)
    calls = []
    replies = SimpleNamespace(verify=httpx.Response(200, json={
        "user": {"id": USER_ID}, "access_token": ACCESS_TOKEN}),
        update=httpx.Response(200, json={"id": USER_ID, "email": "synthetic@example.invalid"}))

    class Provider:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):
            assert url == "https://auth.invalid/auth/v1/verify"
            calls.append(("POST", url, kwargs))
            return replies.verify

        async def put(self, url, **kwargs):
            assert url == "https://auth.invalid/auth/v1/user"
            calls.append(("PUT", url, kwargs))
            return replies.update

    monkeypatch.setattr(main.httpx, "AsyncClient", Provider)
    isolated = FastAPI()
    isolated.add_api_route("/auth/confirm", main.email_confirmation, methods=["GET"])
    isolated.add_api_route("/auth/confirm", main.email_confirmation_submit, methods=["POST"])
    isolated.add_api_route("/auth/recovery", main.password_recovery, methods=["GET"])
    isolated.add_api_route("/auth/recovery", main.password_recovery_submit, methods=["POST"])
    isolated.add_middleware(SessionMiddleware, secret_key="synthetic-session-secret-only",
                            session_cookie="toolbako_session", same_site="lax", https_only=True)
    isolated.add_middleware(ProductionGuardMiddleware, settings=settings)
    with TestClient(isolated, base_url=SITE) as client:
        yield SimpleNamespace(client=client, calls=calls, replies=replies,
                              store=store, app=isolated)


def hidden(response, name):
    match = re.search(rf'name="{name}"[^>]*value="([^"]*)"', response.text)
    assert match is not None
    return match.group(1)


def native_post_headers(document, target):
    """Model same-origin native form POST; no browser/network is started.

    Fetch appends Origin to non-GET/HEAD no-cors requests. no-referrer makes
    that origin opaque (serialized null); strict-origin retains it for our
    same-origin HTTPS action and sends only an origin as Referer.
    """
    source = urlsplit(str(document.request.url))
    destination = urlsplit(target)
    origin = f"{source.scheme}://{source.netloc}"
    assert (source.scheme, source.netloc) == (destination.scheme, destination.netloc)
    policy = document.headers["referrer-policy"]
    if policy == "no-referrer":
        return {"origin": "null"}
    assert policy == "strict-origin"
    return {"origin": origin, "referer": origin + "/"}


def confirmation_data(document, kind="recovery"):
    # JS obtains the token from the fragment, removes the fragment and sets
    # these hidden fields. Neither fragment nor token is put in the HTTP URL.
    return {"token_hash": TOKEN_HASH, "confirmation_type": kind,
            "confirmation_csrf": hidden(document, "confirmation_csrf")}


def private_surface(response, client):
    assert "no-store" in response.headers["cache-control"]
    cookie = client.cookies.get("toolbako_session")
    cookie_payload = base64.b64decode(cookie.split(".")[0]).decode() if cookie else ""
    for secret in (TOKEN_HASH, ACCESS_TOKEN, PASSWORD):
        assert secret not in str(response.headers)
        assert secret not in str(response.request.url)
        assert secret not in str(client.cookies)
        assert secret not in cookie_payload
    assert PASSWORD not in response.text


def open_recovery(browser):
    document = browser.client.get("/auth/confirm")
    assert document.headers["referrer-policy"] == "strict-origin"
    assert not browser.calls  # Mail scanner / external Gmail entry is only GET.
    private_surface(document, browser.client)
    response = browser.client.post("/auth/confirm", data=confirmation_data(document),
                                   headers=native_post_headers(document, SITE + "/auth/confirm"))
    assert response.status_code == 200
    assert response.headers["referrer-policy"] == "strict-origin"
    assert hidden(response, "access_token") == ACCESS_TOKEN
    assert 'action="/auth/recovery"' in response.text
    private_surface(response, browser.client)
    return response


def test_native_recovery_flow_preserves_origin_through_validation_and_revokes_sessions(recovery_browser):
    browser = recovery_browser
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    browser.store.account_sessions.update({
        "old-phone": {"user": {"id": USER_ID}, "expires_at": expires},
        "old-desktop": {"user": {"id": USER_ID}, "expires_at": expires},
        "unrelated": {"user": {"id": "unrelated-user"}, "expires_at": expires},
    })
    recovery = open_recovery(browser)
    invalid = browser.client.post("/auth/recovery", data={"access_token": ACCESS_TOKEN,
        "password": "short", "password_confirmation": "different"},
        headers=native_post_headers(recovery, SITE + "/auth/recovery"))
    assert invalid.status_code == 422
    assert invalid.headers["referrer-policy"] == "strict-origin"
    assert hidden(invalid, "access_token") == ACCESS_TOKEN
    assert len(browser.calls) == 1
    private_surface(invalid, browser.client)
    saved = browser.client.post("/auth/recovery", data={"access_token": ACCESS_TOKEN,
        "password": PASSWORD, "password_confirmation": PASSWORD},
        headers=native_post_headers(invalid, SITE + "/auth/recovery"), follow_redirects=False)
    assert saved.status_code == 303
    assert saved.headers["location"] == "/login?password_reset=1"
    assert [call[0] for call in browser.calls] == ["POST", "PUT"]
    assert browser.calls[-1][2]["headers"]["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert browser.calls[-1][2]["json"] == {"password": PASSWORD}
    assert set(browser.store.account_sessions) == {"unrelated"}
    assert not browser.client.cookies.get("toolbako_session")
    private_surface(saved, browser.client)


def test_signup_confirmation_still_requires_separate_login(recovery_browser):
    browser = recovery_browser
    document = browser.client.get("/auth/confirm")
    response = browser.client.post("/auth/confirm", data=confirmation_data(document, "signup"),
        headers=native_post_headers(document, SITE + "/auth/confirm"), follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?email_confirmed=1"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert browser.store.account_sessions == {}
    assert len(browser.calls) == 1
    assert browser.calls[0][2]["json"]["type"] == "email"
    private_surface(response, browser.client)


@pytest.mark.parametrize("path", ["/auth/confirm", "/auth/recovery"])
@pytest.mark.parametrize("headers,reason", [
    ({"origin": "null"}, "invalid_origin"),
    ({"origin": "null", "referer": SITE + "/auth/confirm"}, "invalid_origin"),
    ({"origin": "https://foreign.invalid", "referer": SITE + "/auth/confirm"}, "invalid_origin"),
    ({}, "origin_required"),
])
def test_untrusted_or_missing_origin_is_rejected_before_provider(recovery_browser, path, headers, reason):
    browser = recovery_browser
    document = browser.client.get("/auth/confirm")
    data = confirmation_data(document) if path == "/auth/confirm" else {
        "access_token": ACCESS_TOKEN, "password": PASSWORD, "password_confirmation": PASSWORD}
    response = browser.client.post(path, data=data, headers=headers)
    assert response.status_code == 403 and response.json()["error"] == reason
    assert not browser.calls and not browser.store.account_sessions
    private_surface(response, browser.client)


@pytest.mark.parametrize("csrf", ["", "forged", "b" * 43])
def test_same_origin_does_not_replace_confirmation_csrf(recovery_browser, csrf):
    browser = recovery_browser
    document = browser.client.get("/auth/confirm")
    data = {**confirmation_data(document), "confirmation_csrf": csrf}
    response = browser.client.post("/auth/confirm", data=data, headers={"origin": SITE})
    assert response.status_code == 403
    assert not browser.calls
    private_surface(response, browser.client)


def test_csrf_remains_bound_to_browser_and_one_use_with_deployed_guard(recovery_browser):
    browser = recovery_browser
    document = browser.client.get("/auth/confirm")
    data = confirmation_data(document)
    with TestClient(browser.app, base_url=SITE) as other:
        assert other.post("/auth/confirm", data=data, headers={"origin": SITE}).status_code == 403
    assert not browser.calls
    assert browser.client.post("/auth/confirm", data=data, headers={"origin": SITE}).status_code == 200
    assert browser.client.post("/auth/confirm", data=data, headers={"origin": SITE}).status_code == 403
    assert len(browser.calls) == 1


def test_direct_recovery_page_and_provider_error_keep_form_policy(recovery_browser):
    browser = recovery_browser
    initial = browser.client.get("/auth/recovery")
    assert initial.headers["referrer-policy"] == "strict-origin"
    assert not browser.calls
    recovery = open_recovery(browser)
    browser.replies.update = httpx.Response(401, json={"error": "synthetic-provider-private-detail"})
    response = browser.client.post("/auth/recovery", data={"access_token": ACCESS_TOKEN,
        "password": PASSWORD, "password_confirmation": PASSWORD},
        headers=native_post_headers(recovery, SITE + "/auth/recovery"))
    assert response.status_code == 400
    assert response.headers["referrer-policy"] == "strict-origin"
    assert ACCESS_TOKEN not in response.text
    assert "synthetic-provider-private-detail" not in response.text
    assert browser.store.account_sessions == {}
    private_surface(response, browser.client)


def test_expired_confirmation_still_fails_without_token_disclosure(recovery_browser):
    browser = recovery_browser
    document = browser.client.get("/auth/confirm")
    browser.replies.verify = httpx.Response(403, json={"error": "synthetic-provider-private-detail"})
    response = browser.client.post("/auth/confirm", data=confirmation_data(document),
        headers=native_post_headers(document, SITE + "/auth/confirm"))
    assert response.status_code == 400
    assert response.headers["referrer-policy"] == "strict-origin"
    assert TOKEN_HASH not in response.text
    assert "synthetic-provider-private-detail" not in response.text
    assert browser.store.account_sessions == {}
    private_surface(response, browser.client)


def test_malformed_query_token_is_not_forwarded_in_native_form_referrer(recovery_browser):
    browser = recovery_browser
    document = browser.client.get(f"/auth/confirm?token_hash={TOKEN_HASH}&type=recovery")
    assert document.headers["referrer-policy"] == "strict-origin"
    headers = native_post_headers(document, SITE + "/auth/confirm")
    assert headers == {"origin": SITE, "referer": SITE + "/"}
    assert TOKEN_HASH not in document.text
    assert TOKEN_HASH not in str(headers)
    assert not browser.calls
