import json
import re
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main

TOKEN = "a" * 56


@pytest.fixture
def confirmation(monkeypatch):
    monkeypatch.setattr(main, "settings", replace(main.settings, supabase_url="https://project.supabase.co",
                                                supabase_anon_key="test-anon", demo_mode=False))
    calls = []
    responses = [httpx.Response(200, json={"user": {"id": "verified-test-user"}, "access_token": "test-access-" + "x" * 32})]

    class ProviderClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            reply = responses.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

    monkeypatch.setattr(main.httpx, "AsyncClient", ProviderClient)
    return TestClient(main.app), calls, responses


def form_data(client, action="signup"):
    response = client.get("/auth/confirm")
    csrf = re.search(r'name="confirmation_csrf" value="([^"]+)"', response.text).group(1)
    return {"token_hash": TOKEN, "confirmation_type": action, "confirmation_csrf": csrf}


def test_password_validation_keeps_private_recovery_form_without_provider_call(confirmation):
    client, calls, _ = confirmation
    token = "test-recovery-" + "x" * 40
    response = client.post("/auth/recovery", data={"access_token": token, "password": "short",
                                                "password_confirmation": "different"})
    assert response.status_code == 422 and not calls
    assert f'value="{token}"' in response.text
    assert 'action="/auth/recovery"' in response.text
    assert token not in str(response.headers)
    assert "no-store" in response.headers["cache-control"]
    assert response.headers["referrer-policy"] == "no-referrer"


def test_prefetch_does_not_consume_token_or_create_login(confirmation):
    client, calls, _ = confirmation
    before = dict(main.store.account_sessions)
    response = client.get(f"/auth/confirm?token_hash={TOKEN}&type=signup")
    assert response.status_code == 200 and not calls
    assert TOKEN not in response.text and TOKEN not in response.headers.get("set-cookie", "")
    assert main.store.account_sessions == before
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "no-store" in response.headers["cache-control"]
    assert 'data-email-confirmation-form hidden' in response.text


def test_confirmation_uses_provider_then_requires_password_login(confirmation):
    client, calls, _ = confirmation
    before = dict(main.store.account_sessions)
    response = client.post("/auth/confirm", data=form_data(client), follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login?email_confirmed=1"
    assert len(calls) == 1
    assert calls[0] == ("https://project.supabase.co/auth/v1/verify", {
        "headers": {"apikey": "test-anon"}, "json": {"token_hash": TOKEN, "type": "email"}})
    assert main.store.account_sessions == before
    assert TOKEN not in str(response.headers) and TOKEN not in response.text


def test_recovery_keeps_access_token_in_private_form_not_redirect_or_cookie(confirmation):
    client, calls, _ = confirmation
    response = client.post("/auth/confirm", data=form_data(client, "recovery"), follow_redirects=False)
    assert response.status_code == 200 and calls[0][1]["json"]["type"] == "recovery"
    assert 'action="/auth/recovery"' in response.text
    assert 'value="test-access-' in response.text
    assert "test-access-" not in str(response.headers)
    assert "no-store" in response.headers["cache-control"]
    assert response.headers["referrer-policy"] == "no-referrer"
    assert TOKEN not in response.text


@pytest.mark.parametrize("replacement", ["", "wrong-csrf", "不正な値"])
def test_missing_or_forged_csrf_cannot_consume_token(confirmation, replacement):
    client, calls, _ = confirmation
    data = form_data(client)
    data["confirmation_csrf"] = replacement
    assert client.post("/auth/confirm", data=data).status_code == 403
    assert not calls


def test_csrf_is_bound_to_browser_and_consumed_once(confirmation):
    client, calls, _ = confirmation
    data = form_data(client)
    assert TestClient(main.app).post("/auth/confirm", data=data).status_code == 403
    assert not calls
    assert client.post("/auth/confirm", data=data, follow_redirects=False).status_code == 303
    assert client.post("/auth/confirm", data=data).status_code == 403 and len(calls) == 1


@pytest.mark.parametrize("changes", [
    {"confirmation_type": "invite"}, {"confirmation_type": "email_change"},
    {"confirmation_type": "https://attacker.invalid"}, {"token_hash": "short"},
    {"token_hash": "x" * 257}, {"token_hash": "a" * 55 + "<"},
])
def test_bad_actions_and_tokens_are_rejected_without_provider_calls(confirmation, changes):
    client, calls, _ = confirmation
    data = {**form_data(client), **changes}
    response = client.post("/auth/confirm", data=data, follow_redirects=False)
    assert response.status_code == 400 and not calls
    assert "location" not in response.headers


def test_caller_cannot_choose_return_url(confirmation):
    client, calls, _ = confirmation
    data = {**form_data(client), "redirect_to": "https://attacker.invalid", "next": "//attacker.invalid"}
    response = client.post("/auth/confirm?redirect_to=https://attacker.invalid", data=data, follow_redirects=False)
    assert response.headers["location"] == "/login?email_confirmed=1"
    assert "attacker" not in json.dumps(calls)


@pytest.mark.parametrize("provider_reply", [
    httpx.Response(403, json={"error_description": "secret-provider-detail"}),
    httpx.Response(200, json=[]), httpx.Response(200, json={"user": None}),
    httpx.Response(200, content="not json"), httpx.ConnectError("secret-provider-detail"),
])
def test_expired_bad_and_unavailable_provider_responses_fail_closed(confirmation, provider_reply, caplog):
    client, calls, responses = confirmation
    responses[:] = [provider_reply]
    response = client.post("/auth/confirm", data=form_data(client), follow_redirects=False)
    assert response.status_code == 400 and len(calls) == 1
    assert "location" not in response.headers
    assert TOKEN not in response.text and "secret-provider-detail" not in response.text + caplog.text


def test_unconfigured_auth_is_rejected_before_provider_call(confirmation, monkeypatch):
    client, calls, _ = confirmation
    monkeypatch.setattr(main, "settings", replace(main.settings, supabase_url="", supabase_anon_key=""))
    assert client.post("/auth/confirm", data=form_data(client)).status_code == 503
    assert not calls


def test_recovery_without_access_token_fails_closed(confirmation):
    client, calls, responses = confirmation
    responses[:] = [httpx.Response(200, json={"user": {"id": "verified-test-user"}})]
    response = client.post("/auth/confirm", data=form_data(client, "recovery"))
    assert response.status_code == 400 and 'data-recovery-form' not in response.text


def test_email_templates_use_site_domain_and_fragment_only():
    root = Path(__file__).resolve().parents[1]
    for filename, action in [("confirm-sign-up.html", "signup"), ("reset-password.html", "recovery")]:
        text = (root / "deploy/auth-email" / filename).read_text(encoding="utf-8")
        assert f'{{{{ .SiteURL }}}}/auth/confirm#token_hash={{{{ .TokenHash }}}}&amp;type={action}' in text
        assert ".ConfirmationURL" not in text and "supabase.co" not in text
