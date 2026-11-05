"""Native logout from real private HTML, with isolated users and deployed guard.

Header derivation models Fetch's native form contract; this is not a WebKit
trace. No provider, database, main lifespan, or external HTTP is used.
"""
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from starlette.exceptions import HTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request

import app.main as main
from app.data import DemoStore
from app.middleware import ProductionGuardMiddleware


SITE = "https://toolbako.example"
INSPECTION = "/admin/orders/synthetic-order/refund-inspection"
PAGES = ("payment", "onboarding", "identity", "inspection")


@pytest.fixture
def handoff_browser(monkeypatch):
    settings = replace(main.settings, environment="production", demo_mode=False,
                       site_base_url=SITE, canonical_redirect_hosts=(), redis_url="",
                       rate_limit_per_minute=1000, auth_rate_limit_per_minute=1000)
    store = DemoStore(seed=False)
    user = {"id": "synthetic-admin", "username": "synthetic", "display_name": "Synthetic"}
    state = SimpleNamespace(inspection_failure=False, inspection_calls=0)
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "database_store", object())
    monkeypatch.setattr(main, "current_user", lambda request: user if request.session.get("sid") else None)
    monkeypatch.setattr(main, "is_admin", lambda account: account is user)
    monkeypatch.setattr(main.mfa, "recent", lambda request: True)

    async def inspect(*args, **kwargs):
        state.inspection_calls += 1
        if state.inspection_failure:
            raise RuntimeError("synthetic failure, never shown")
        return {"order_id": "synthetic-order", "observed_at": datetime.now(timezone.utc),
                "verified_refund_count": 0, "remaining_count": 1, "remaining_amount": 1000}

    monkeypatch.setattr(main, "inspect_refund_resumption_live", inspect)
    app = FastAPI()
    app.add_exception_handler(HTTPException, main.friendly_http_error)

    @app.get("/synthetic-login")
    async def login(request: Request):
        request.session["sid"] = "synthetic-session"
        store.account_sessions["synthetic-session"] = {"user_id": user["id"]}
        return {"synthetic": True}

    @app.get("/checkout/handoff/{kind}")
    async def page(request: Request, kind: str):
        if kind == "identity":
            return main.identity_handoff(request, "https://verify.stripe.com/start/synthetic")
        return main.payment_handoff(request,
            "https://connect.stripe.com/setup/synthetic" if kind == "onboarding"
            else "https://checkout.stripe.com/pay/synthetic", onboarding=kind == "onboarding")

    app.add_api_route("/admin/orders/{order_id}/refund-inspection", main.admin_inspect_live_refund, methods=["POST"])
    app.add_api_route("/logout", main.logout, methods=["POST"])
    app.add_middleware(SessionMiddleware, secret_key="synthetic-handoff-session-only",
                       session_cookie="toolbako_session", same_site="lax", https_only=True)
    app.add_middleware(ProductionGuardMiddleware, settings=settings)
    with TestClient(app, base_url=SITE) as client:
        assert client.get("/synthetic-login").status_code == 200
        yield SimpleNamespace(client=client, store=store, state=state)


def open_page(browser, kind):
    if kind == "inspection":
        return browser.client.post(INSPECTION, headers={"Origin": SITE, "Accept": "text/html"})
    return browser.client.get(f"/checkout/handoff/{kind}?sensitive_context=synthetic")


def native_headers(document, *, old_policy=False):
    origin = urlsplit(str(document.request.url))
    origin = f"{origin.scheme}://{origin.netloc}"
    policy = "no-referrer" if old_policy else document.headers["referrer-policy"]
    if policy == "no-referrer":
        return {"Origin": "null"}
    assert policy == "strict-origin"
    return {"Origin": origin, "Referer": origin + "/"}


@pytest.mark.parametrize("kind", PAGES)
def test_private_html_logout_reaches_handler_and_drops_session(handoff_browser, kind):
    page = open_page(handoff_browser, kind)
    assert page.status_code == 200
    assert 'action="/logout" method="post"' in page.text
    assert "no-store" in page.headers["cache-control"]
    assert "form-action 'self'" in page.headers["content-security-policy"]
    headers = native_headers(page)
    assert headers["Referer"] == SITE + "/"  # no source path or query
    if kind != "inspection":
        assert 'rel="noreferrer"' in page.text
    response = handoff_browser.client.post("/logout", headers=headers, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/"
    assert "synthetic-session" not in handoff_browser.store.account_sessions


@pytest.mark.parametrize("kind", PAGES)
def test_old_document_policy_reproduces_rejection_without_logout(handoff_browser, kind):
    page = open_page(handoff_browser, kind)
    response = handoff_browser.client.post("/logout", headers=native_headers(page, old_policy=True))
    assert response.status_code == 403 and response.json()["error"] == "invalid_origin"
    assert "synthetic-session" in handoff_browser.store.account_sessions


@pytest.mark.parametrize("failure", ["backend", "inspection"])
def test_inspection_error_html_can_still_logout(handoff_browser, monkeypatch, failure):
    if failure == "backend":
        monkeypatch.setattr(main, "database_store", None)
    else:
        handoff_browser.state.inspection_failure = True
    page = open_page(handoff_browser, "inspection")
    assert page.status_code == 409 and 'action="/logout" method="post"' in page.text
    assert "synthetic failure" not in page.text
    response = handoff_browser.client.post("/logout", headers=native_headers(page), follow_redirects=False)
    assert response.status_code == 303
    assert "synthetic-session" not in handoff_browser.store.account_sessions


@pytest.mark.parametrize("headers, error", [
    ({}, "origin_required"),
    ({"Origin": "https://foreign.invalid"}, "invalid_origin"),
    ({"Origin": "null", "Referer": SITE + "/"}, "invalid_origin"),
])
def test_missing_foreign_and_opaque_origins_remain_rejected(handoff_browser, headers, error):
    response = handoff_browser.client.post("/logout", headers=headers)
    assert response.status_code == 403 and response.json()["error"] == error
    assert "synthetic-session" in handoff_browser.store.account_sessions
