import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from starlette.exceptions import HTTPException
from starlette.requests import Request

from app.account_lifecycle import deletion_hold
from app.account_worker import AuthErasure, ErasureUnavailable, inspect, run_once
from app.config import Settings
from app.data import DemoStore
from app.security import SessionService
from test_database import database, query

USER = "404e4ce3-9c0c-4850-84d8-ce9b3664e600"
AT = datetime.now(timezone.utc)


def pending_store():
    store = DemoStore(seed=False)
    store.registered_users["member"] = {"id": USER, "username": "member", "email": "private@example.invalid"}
    store.account_deletions.append({"id": "delete-synthetic", "user_id": USER, "status": "scheduled", "delete_after": AT-timedelta(days=1)})
    return store


@pytest.mark.parametrize("kind", ["order", "subscription", "payout", "support", "transfer", "dispute", "refund"])
def test_changed_activity_after_request_holds_erasure(kind):
    store = pending_store()
    order = {"id": "o", "buyer_id": USER, "seller_id": "seller", "status": "completed"}
    if kind == "order": store.orders.append({**order, "status": "in_progress"})
    if kind == "subscription": store.subscriptions.append({"buyer_id": USER, "status": "past_due"})
    if kind == "payout": store.payouts.append({"seller_id": USER, "status": "review"})
    if kind == "support": store.support_cases.append({"user_id": USER, "status": "open"})
    if kind == "transfer": store.transfer_inquiries.append({"buyer_id": USER, "status": "negotiating"})
    if kind == "dispute": store.orders.append({**order, "dispute_status": "provider_dispute"})
    if kind == "refund": store.orders.append({**order, "refund_status": "pending"})
    assert deletion_hold(store, USER)
    assert store.execute_due_account_deletions(AT) == 0
    assert "member" in store.registered_users and store.account_deletions[0]["status"] == "scheduled"


def test_web_session_never_erases_non_demo_accounts_and_completed_user_cannot_return():
    store = pending_store()
    sessions = SessionService(store, 3600)
    request = Request({"type": "http", "session": {}, "headers": []})
    sessions.establish(request, store.registered_users["member"])
    assert sessions.current(request)
    assert store.account_deletions[0]["status"] == "scheduled"
    store.account_deletions[0]["status"] = "erasing"
    assert sessions.current(request) is None and not store.account_sessions
    with pytest.raises(HTTPException): sessions.establish(request, store.registered_users["member"])


def test_dry_run_is_read_only_and_does_not_initialize_state(database):
    backend, dsn = database
    before = query(dsn, "select count(*) from toolbako_runtime.marketplace_state")
    assert asyncio.run(inspect(backend))["due"] == 0
    assert query(dsn, "select count(*) from toolbako_runtime.marketplace_state") == before


def test_worker_requires_policy_even_with_apply(database):
    backend, _ = database
    with pytest.raises(ErasureUnavailable):
        asyncio.run(run_once(backend, Settings(demo_mode=False)))


def test_provider_uncertainty_survives_restart_and_is_resolved_before_completion(database):
    backend, dsn = database
    store = pending_store()
    settings = Settings(demo_mode=False, account_erasure_policy_revision="synthetic-policy-v1")
    class Provider:
        fail = True
        calls = 0
        async def erase(self, user_id):
            assert user_id == USER
            # The durable access barrier must precede the external request.
            payload = query(dsn, "select payload from toolbako_runtime.marketplace_state")[0][0]
            assert payload["account_deletions"][0]["status"] == "erasing"
            self.calls += 1
            if self.fail: raise ErasureUnavailable("uncertain")
    provider = Provider()
    async def run():
        async with backend.request(store):
            source = pending_store()
            store.registered_users = source.registered_users
            store.account_deletions = source.account_deletions
            await backend.save()
        assert (await run_once(backend, settings, provider=provider, at=AT))["provider_review"] == 1
        assert (await inspect(backend, at=AT))["provider_review"] == 1
        provider.fail = False
        assert (await run_once(backend, settings, provider=provider, at=AT))["completed"] == 1
        assert (await run_once(backend, settings, provider=provider, at=AT))["completed"] == 0
        async with backend.request(store):
            assert not store.registered_users
            assert store.account_deletions[0]["status"] == "completed"
            assert any(a["action"] == "account.erasure_completed" for a in store.audit_logs)
        assert provider.calls == 2
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["normal", "already_absent", "timeout", "wrong_user", "blocked", "false_404"])
def test_auth_erasure_confirms_exact_user_and_absence_without_exposing_details(monkeypatch, mode):
    present = mode != "already_absent"
    calls = []
    def handle(request):
        nonlocal present
        calls.append(request.method)
        assert request.url.path == f"/auth/v1/admin/users/{USER}"
        assert request.headers["apikey"] == "sb_secret_fixture"
        if request.method == "DELETE":
            if mode == "timeout": raise httpx.ReadTimeout("private provider response", request=request)
            if mode == "blocked": return httpx.Response(400, json={"message": "owns objects"})
            present = False
            return httpx.Response(200, json={"id": USER})
        if mode == "false_404": return httpx.Response(404, json={"message": "unknown path"})
        if present: return httpx.Response(200, json={"id": "other" if mode == "wrong_user" else USER})
        return httpx.Response(404, json={"code": "user_not_found"})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    auth = AuthErasure(Settings(supabase_url="https://project.supabase.co", supabase_service_role_key="sb_secret_fixture"))
    if mode in {"normal", "already_absent"}:
        asyncio.run(auth.erase(USER))
        assert calls == (["GET", "DELETE", "GET"] if mode == "normal" else ["GET", "GET"])
    else:
        with pytest.raises(ErasureUnavailable) as error: asyncio.run(auth.erase(USER))
        assert "private" not in str(error.value)
        if mode in {"wrong_user", "false_404"}: assert calls == ["GET"]
