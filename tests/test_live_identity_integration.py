"""Synthetic shared-Auth/live policy integration; no external calls."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.data import DemoStore
from app.live_identity import LiveClaimPolicy
from app.security import SessionService
from app.public_read import PublicView
from app.member_read import MemberView
from test_live_identity import USER, REVIEWER, LIVE, NOW, account, event
from test_database import database


def request(path="/nda"):
    return Request({"type": "http", "method": "GET", "headers": [], "session": {"sid": "synthetic-session"},
                    "path": path, "query_string": b"", "scheme": "http", "server": ("testserver", 80)})


@pytest.fixture
def live(monkeypatch):
    import app.main as main
    configured = replace(main.settings, environment="development", demo_mode=False,
        stripe_secret_key="rk_live_synthetic", stripe_platform_account_id=LIVE.platform_account,
        stripe_ledger_id=LIVE.ledger_id, stripe_webhook_secret="whsec_synthetic", admin_user_ids=(REVIEWER,),
        email_api_key="", email_from="", supabase_url="", supabase_anon_key="",
        supabase_publishable_key="", supabase_secret_key="", supabase_service_role_key="")
    monkeypatch.setattr(main, "settings", configured)
    store = DemoStore(seed=False)
    store.registered_users["synthetic"] = account()
    store.account_sessions["synthetic-session"] = {"user": deepcopy(account()),
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1), "mfa_verified_at": NOW}
    store.identity_applications[USER] = {"status": "verified", "provider_reference": "vs_unclassified"}
    monkeypatch.setattr(main, "store", store)
    sessions = SessionService(store, 3600, claim_policy=main.claim_policy)
    monkeypatch.setattr(main, "session_service", sessions)
    monkeypatch.setattr(main, "establish_session", sessions.establish)
    monkeypatch.setattr(main, "update_current_session", sessions.update)
    async def no_persist(): pass
    monkeypatch.setattr(main, "persist_state", no_persist)
    return main, store


def test_old_session_and_unscoped_identity_application_do_not_grant_live_access(live):
    main, store = live
    before = deepcopy((store.registered_users, store.account_sessions, store.identity_applications))
    user = main.current_user(request())
    assert user["is_verified"] is True and not user["live_identity_eligible"]
    assert main.identity_verified(user) is False and main.identity_verified_by_id(USER) is False
    assert (store.registered_users, store.account_sessions, store.identity_applications) == before
    store.audit_logs = [event()]
    assert main.current_user(request())["live_identity_eligible"] is True
    assert main.identity_verified(user) is True


@pytest.mark.parametrize("operation", ["nda", "payout"])
def test_direct_identity_required_operations_cannot_bypass_live_gate(live, operation):
    main, store = live
    req = request("/" + operation)
    with pytest.raises(HTTPException) as denied:
        if operation == "nda": asyncio.run(main.sign_nda(req, agree="yes"))
        else: asyncio.run(main.payout_request(req, amount=1000, request_key=""))
    assert denied.value.status_code == 403
    assert not store.nda_records and not store.nda_signatures and not store.payouts


def test_nda_acceptance_after_matching_review_preserves_original_identity(live):
    main, store = live
    store.audit_logs = [event()]
    result = asyncio.run(main.sign_nda(request(), agree="yes"))
    assert result.status_code == 303 and USER in store.nda_signatures
    assert store.registered_users["synthetic"]["is_verified"] is True


def test_unclassified_verified_application_does_not_create_duplicate_identity_request(live, monkeypatch):
    main, store = live
    async def forbidden(*args, **kwargs): raise AssertionError("No provider call")
    monkeypatch.setattr(main.stripe, "create_identity_session", forbidden)
    monkeypatch.setattr(main.stripe, "retrieve_identity_session", forbidden)
    response = asyncio.run(main.start_identity_verification(request("/verification/start")))
    assert response.headers["location"] == "/verification?review_required=1"
    assert store.identity_applications[USER]["provider_reference"] == "vs_unclassified"


@pytest.mark.parametrize("flow", ["password", "oauth"])
def test_shared_profile_login_does_not_replace_runtime_claims_or_mfa(live, monkeypatch, flow):
    main, store = live
    original = deepcopy(store.registered_users["synthetic"])
    # A mismatching shared profile must neither replace reviewed credentials
    # nor mint live permission. It is still preserved as raw session input.
    profile = {"id": USER, "username": "synthetic", "display_name": "Synthetic login",
        "identity_status": "verified", "creator_badges": {
            "is_founding_member": True, "founding_member_since": NOW.isoformat(),
            "is_certified_creator": True, "certified_creator_since": NOW.isoformat()}}
    before = deepcopy(profile)
    responses = []
    def transport(req):
        responses.append(req.method)
        if req.method == "POST":
            return httpx.Response(200, json={"access_token": "synthetic-token", "user": {"id": USER, "email": "synthetic@example.invalid"}})
        return httpx.Response(200, json=[profile])
    client = httpx.AsyncClient
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(transport), **kw))
    monkeypatch.setattr(main, "settings", replace(main.settings, supabase_url="https://synthetic.invalid", supabase_anon_key="synthetic-public"))
    monkeypatch.setattr(main.mfa, "attach_tokens", lambda req, token_data: None)
    req = request("/login" if flow == "password" else "/auth/callback")
    if flow == "password":
        response = asyncio.run(main.login_submit(req, email="synthetic@example.invalid", password="synthetic", next_path="/"))
    else:
        req.session.update(oauth_state="synthetic-state", oauth_code_verifier="synthetic-verifier")
        response = asyncio.run(main.auth_callback(req, code="synthetic", state="synthetic-state", next="/"))
    assert response.status_code == 303 and responses == ["POST", "GET"]
    current = main.current_user(req)
    assert current["is_verified"] is True and current["live_identity_eligible"] is False
    for field in ("is_verified", "identity_status", "is_founding_member", "founding_member_since", "is_certified_creator", "certified_creator_since", "mfa_enrolled"):
        assert store.registered_users["synthetic"][field] == original[field]
    assert profile == before
    # Existing verified runtime claims can still be authorized by real review;
    # the incoming shared profile's different badge dates are not the source.
    store.audit_logs = [event(claim) for claim in ("identity", "founding", "certified")]
    assert main.current_user(req)["live_identity_eligible"] is True
    assert main.creator_badges_for("synthetic")["is_certified_creator"] is True


def test_badge_views_hold_unreviewed_claims_but_do_not_revoke_original_flags(live):
    main, store = live
    row = store.registered_users["synthetic"]
    before = deepcopy(row)
    public = PublicView([], {"synthetic": deepcopy(row)}, [], main.claim_policy)
    member = MemberView({"public_badges": {"synthetic": deepcopy(row)}, "audit_logs": []})
    member.claim_policy = main.claim_policy
    for read in (lambda: main.creator_badges_for("synthetic"), lambda: public.badges_for("synthetic"), lambda: member.badges_for("synthetic")):
        assert read()["is_certified_creator"] is False and "id" not in read()
    for view in (store, public, member): view.audit_logs = [event("certified")]
    assert public.badges_for("synthetic")["is_certified_creator"] is True
    assert member.badges_for("synthetic")["is_certified_creator"] is True
    assert row == before


def test_live_creator_snapshot_does_not_treat_demo_username_as_verification(live):
    main, store = live
    store.registered_users["demo_creator"] = store.registered_users.pop("synthetic")
    snapshot = main.creator_snapshot("demo_creator")
    assert snapshot[0]["verified"] is False


def test_badge_grant_uses_original_other_badge_not_held_display(live, monkeypatch):
    main, store = live
    sent = []
    monkeypatch.setattr(main, "current_user", lambda request: {"id": REVIEWER})
    async def capture(account, badge_state): sent.append(deepcopy(badge_state))
    monkeypatch.setattr(main, "sync_creator_badges_to_supabase", capture)
    response = asyncio.run(main.admin_creator_badge(request(), "synthetic", "certified", "revoke"))
    assert response.status_code == 303
    assert sent[0]["is_founding_member"] is True
    assert sent[0]["founding_member_since"] == account()["founding_member_since"]


def test_legacy_test_session_still_accepts_provider_status(live, monkeypatch):
    main, store = live
    monkeypatch.setattr(main, "settings", replace(main.settings, stripe_secret_key="rk_test_synthetic", stripe_platform_account_id="", stripe_ledger_id=""))
    store.account_sessions["synthetic-session"]["user"]["is_verified"] = False
    user = main.current_user(request())
    assert user["is_verified"] is True and "live_identity_eligible" not in user
    assert main.identity_verified(user) is True


def test_live_fast_views_project_evidence_and_identity_without_exposing_it(database, live):
    backend, dsn = database
    main, source = live
    source.audit_logs = [event(claim) for claim in ("identity", "founding", "certified")]
    source.audit(None, "unrelated.private", "must-not-project")
    # Only the synthetic records are copied into a factory-created local DB.
    async def run():
        saved = deepcopy({key: getattr(source, key) for key in ("registered_users", "account_sessions", "identity_applications", "audit_logs")})
        local = DemoStore(seed=False)
        async with backend.request(local):
            for key, value in saved.items(): setattr(local, key, value)
            await backend.save()
        public = await backend.public_view(catalog=True, live_claims=True)
        public.claim_policy = main.claim_policy
        member = await backend.member_view("synthetic-session", "/favorites", live_claims=True)
        member.claim_policy = main.claim_policy
        assert public.registered_users["synthetic"]["id"] == USER
        assert member.public_badges["synthetic"]["id"] == USER
        assert all(row["action"] == "live.claims.reviewed" for row in public.audit_logs + member.audit_logs)
        for view in (public, member):
            values = view.badges_for("synthetic")
            assert values["is_certified_creator"] is True and "id" not in values
            assert "email" not in values and "evidence" not in repr(values)
        sessions = SessionService(member, 3600, claim_policy=main.claim_policy)
        assert sessions.current(request())["live_identity_eligible"] is True
        await backend.close()
    asyncio.run(run())
