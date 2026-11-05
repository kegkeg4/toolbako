"""Recovery failures remain actionable without disclosing provider data.

Actual handlers + deployed Origin guard, synthetic provider only. No email,
external credentials, live account, or database is used.
"""
from datetime import datetime, timedelta, timezone
import logging
import re

import httpx
import pytest

import app.main as main
from test_recovery_origin import (
    ACCESS_TOKEN, PASSWORD, SITE, USER_ID, hidden, native_post_headers,
    open_recovery, recovery_browser,
)


PRIVATE = "PRIVATE_PROVIDER_DETAIL_DO_NOT_EXPOSE"


def submit(browser, page, **values):
    data = {"access_token": ACCESS_TOKEN, "password": PASSWORD,
            "password_confirmation": PASSWORD, **values}
    return browser.client.post("/auth/recovery", data=data,
        headers=native_post_headers(page, SITE + "/auth/recovery"), follow_redirects=False)


def failure_text(response):
    return re.search(r'<div class="form-error"[^>]*>(.*?)</div>', response.text, re.S).group(1)


def assert_private(response, browser, caplog):
    assert PRIVATE not in response.text + caplog.text
    assert PASSWORD not in response.text + caplog.text
    assert ACCESS_TOKEN not in caplog.text + str(response.headers) + str(browser.client.cookies)
    assert response.headers["referrer-policy"] == "strict-origin"
    assert "no-store" in response.headers["cache-control"]


@pytest.mark.parametrize("field", ["code", "error_code"])
@pytest.mark.parametrize("code, word", [("same_password", "現在と異なる"), ("weak_password", "安全性")])
def test_password_policy_rejection_keeps_token_for_manual_correction(recovery_browser, caplog, field, code, word):
    b = recovery_browser
    page = open_recovery(b)
    b.replies.update = httpx.Response(422, json={field: code, "message": PRIVATE,
                                               "weak_password": {"reasons": [PRIVATE]}})
    result = submit(b, page)
    assert result.status_code == 422
    assert word in failure_text(result) and "期限切れ" not in failure_text(result)
    assert hidden(result, "access_token") == ACCESS_TOKEN
    assert [call[0] for call in b.calls] == ["POST", "PUT"]
    assert_private(result, b, caplog)


@pytest.mark.parametrize("body", [{"code": "over_request_rate_limit"}, {"message": PRIVATE}])
def test_rate_limit_is_not_expiry_and_does_not_automatically_retry(recovery_browser, caplog, body):
    b = recovery_browser
    page = open_recovery(b)
    b.replies.update = httpx.Response(429, json=body)
    result = submit(b, page)
    assert result.status_code == 429 and "時間をおいて" in result.text
    assert "期限切れ" not in failure_text(result)
    assert hidden(result, "access_token") == ACCESS_TOKEN
    assert len(b.calls) == 2
    assert_private(result, b, caplog)


@pytest.mark.parametrize("status, code", [(422, "same_password"), (422, "weak_password"), (429, "over_request_rate_limit")])
def test_manual_retry_with_same_token_can_succeed_and_revoke_sessions(recovery_browser, status, code):
    b = recovery_browser
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    b.store.account_sessions.update({
        "previous-device": {"user": {"id": USER_ID}, "expires_at": expires},
        "unrelated-device": {"user": {"id": "unrelated"}, "expires_at": expires},
    })
    page = open_recovery(b)
    b.replies.update = httpx.Response(status, json={"error_code": code})
    failed = submit(b, page)
    assert "previous-device" in b.store.account_sessions
    assert hidden(failed, "access_token") == ACCESS_TOKEN
    b.replies.update = httpx.Response(200, json={"id": USER_ID, "email": "synthetic@example.invalid"})
    succeeded = submit(b, failed, password="a-different-synthetic-password",
                       password_confirmation="a-different-synthetic-password")
    assert succeeded.status_code == 303 and succeeded.headers["location"] == "/login?password_reset=1"
    assert set(b.store.account_sessions) == {"unrelated-device"}
    assert [call[0] for call in b.calls] == ["POST", "PUT", "PUT"]
    assert all(call[2]["headers"]["Authorization"] == f"Bearer {ACCESS_TOKEN}" for call in b.calls[1:])
    assert not b.client.cookies.get("toolbako_session")


@pytest.mark.parametrize("code", ["bad_jwt", "session_expired", "session_not_found"])
@pytest.mark.parametrize("field", ["code", "error_code"])
def test_verified_invalid_session_drops_token(recovery_browser, caplog, code, field):
    b = recovery_browser
    page = open_recovery(b)
    b.replies.update = httpx.Response(401, json={field: code, "message": PRIVATE})
    result = submit(b, page)
    assert result.status_code == 400
    assert "再設定メール" in result.text and ACCESS_TOKEN not in result.text
    assert len(b.calls) == 2
    assert_private(result, b, caplog)


@pytest.mark.parametrize("status, code", [(403, "user_banned"), (403, "reauthentication_needed"),
                                        (403, "reauthentication_not_valid")])
def test_ban_and_reauthentication_are_not_bypassed(recovery_browser, caplog, status, code):
    b = recovery_browser
    page = open_recovery(b)
    b.replies.update = httpx.Response(status, json={"error_code": code, "message": PRIVATE})
    result = submit(b, page)
    assert result.status_code == 403
    assert ACCESS_TOKEN not in result.text and "期限切れ" not in failure_text(result)
    assert len(b.calls) == 2 and b.store.account_sessions == {}
    assert_private(result, b, caplog)


@pytest.mark.parametrize("reply", [
    httpx.Response(500, json={"code": "weak_password", "message": PRIVATE}),
    httpx.Response(503, content=PRIVATE.encode()),
    httpx.Response(302, headers={"location": "https://foreign.invalid/"}),
    httpx.Response(200, content=PRIVATE.encode()),
    httpx.Response(200, json=[]),
    httpx.Response(200, json={}),
    httpx.Response(200, json={"id": ""}),
    httpx.Response(200, json={"id": "  "}),
    httpx.Response(200, json={"id": 123}),
    httpx.Response(200, json={"id": True}),
    httpx.Response(204),
])
def test_uncertain_outcomes_are_not_expiry_or_success(recovery_browser, caplog, reply):
    b = recovery_browser
    page = open_recovery(b)
    b.replies.update = reply
    result = submit(b, page)
    assert result.status_code == 503
    assert "更新結果を確認できません" in result.text and "ログイン" in result.text
    assert ACCESS_TOKEN not in result.text and "期限切れ" not in failure_text(result)
    assert len(b.calls) == 2 and b.store.account_sessions == {}
    assert_private(result, b, caplog)


def test_timeout_is_unknown_without_retry_or_exception_text(recovery_browser, monkeypatch, caplog):
    b = recovery_browser
    page = open_recovery(b)
    attempts = []
    async def timeout(self, *args, **kwargs):
        attempts.append(True)
        raise httpx.ReadTimeout(PRIVATE + ACCESS_TOKEN + PASSWORD)
    monkeypatch.setattr(main.httpx.AsyncClient, "put", timeout)
    result = submit(b, page)
    assert result.status_code == 503 and "更新結果を確認できません" in result.text
    assert "ログイン" in result.text and "期限切れ" not in failure_text(result)
    assert attempts == [True] and ACCESS_TOKEN not in result.text
    assert_private(result, b, caplog)


@pytest.mark.parametrize("payload", [
    {"code": PRIVATE, "message": PRIVATE},
    {"error_code": {"nested": PRIVATE}},
    {"code": "same_password", "error_code": "bad_jwt"},
    {"message": "same_password"},
    [],
])
def test_unknown_rejection_does_not_infer_expiry_from_message(recovery_browser, caplog, payload):
    b = recovery_browser
    page = open_recovery(b)
    b.replies.update = httpx.Response(400, json=payload)
    result = submit(b, page)
    assert result.status_code == 400
    assert "更新を受け付けられませんでした" in result.text
    assert "期限切れ" not in failure_text(result) and ACCESS_TOKEN not in result.text
    assert_private(result, b, caplog)


@pytest.mark.parametrize("token", ["short", "x" * 4097, "x" * 30 + "\n", "あ" * 30],
                         ids=["too_short", "too_long", "newline", "non_ascii"])
def test_bad_token_is_distinct_from_bad_password_and_never_sent(recovery_browser, token):
    b = recovery_browser
    page = open_recovery(b)
    result = submit(b, page, access_token=token)
    assert result.status_code == 400 and "再設定リンク" in result.text
    assert "パスワードは10文字" not in result.text
    assert len(b.calls) == 1


def test_local_password_validation_keeps_valid_token_without_provider_call(recovery_browser):
    b = recovery_browser
    page = open_recovery(b)
    result = submit(b, page, password="short", password_confirmation="different")
    assert result.status_code == 422
    assert hidden(result, "access_token") == ACCESS_TOKEN
    assert "パスワードは10文字" in result.text and len(b.calls) == 1


def test_diagnostics_only_use_fixed_categories_and_numeric_status(recovery_browser, caplog):
    b = recovery_browser
    page = open_recovery(b)
    b.replies.update = httpx.Response(422, json={"error_code": "same_password", "message": PRIVATE + ACCESS_TOKEN})
    with caplog.at_level(logging.WARNING, logger="toolbako.auth"):
        result = submit(b, page)
    records = [record.getMessage() for record in caplog.records if record.name == "toolbako.auth"]
    assert records == ["password_recovery_result category=same_password provider_status=422"]
    assert_private(result, b, caplog)
