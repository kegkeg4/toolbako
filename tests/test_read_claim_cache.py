"""Request-only evidence reuse; synthetic records and disposable local PG."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json

import pytest

from app import live_identity
from app.config import Settings
from app.data import DemoStore
from app.database import PostgresStateStore
from app.member_read import MemberView
from app.public_read import PublicView
from test_database import database
from test_live_identity import USER, REVIEWER, LIVE, NOW, account, event


@pytest.fixture
def policy_state(monkeypatch):
    state = {"settings": Settings(demo_mode=False, stripe_secret_key="rk_live_synthetic",
        stripe_platform_account_id=LIVE.platform_account, stripe_ledger_id=LIVE.ledger_id,
        admin_user_ids=(REVIEWER,)), "calls": 0}
    original = live_identity.review_registry
    def counted(*args, **kwargs):
        state["calls"] += 1
        return original(*args, **kwargs)
    monkeypatch.setattr(live_identity, "review_registry", counted)
    state["policy"] = live_identity.LiveClaimPolicy(lambda: state["settings"])
    return state


def view(kind, state, events=None):
    events = deepcopy(events if events is not None else [event("certified")])
    if kind == "public":
        result = PublicView([], {"synthetic": account()}, events)
    else:
        result = MemberView({"tools": [], "public_badges": {"synthetic": account()},
                             "registered_users": {"synthetic": account()}, "audit_logs": events})
    result.claim_policy = state["policy"]
    return result


@pytest.mark.parametrize("kind", ["public", "member"])
def test_one_read_validates_registry_once_and_never_caches_returned_badge_dict(policy_state, kind):
    read = view(kind, policy_state)
    before = deepcopy(read.audit_logs)
    for _ in range(50):
        result = read.badges_for("synthetic")
        assert result["is_certified_creator"] is True and "id" not in result
        result.update(is_certified_creator=False, certified_creator_since=None, injected="untrusted")
    assert policy_state["calls"] == 1
    assert read.audit_logs == before
    assert "injected" not in read.badges_for("synthetic")
    assert "evidence_sha256" not in repr(read)


@pytest.mark.parametrize("kind", ["public", "member"])
@pytest.mark.parametrize("mutation", ["in_place_hold", "append_hold", "malformed", "untrusted_reviewer", "remove"])
def test_changed_evidence_invalidates_reuse_and_cannot_retain_approval(policy_state, kind, mutation):
    read = view(kind, policy_state)
    assert read.badges_for("synthetic")["is_certified_creator"] is True
    if mutation == "in_place_hold":
        payload = json.loads(read.audit_logs[0]["target"])
        payload["decision"] = "hold"
        read.audit_logs[0]["target"] = json.dumps(payload)
    elif mutation == "append_hold":
        read.audit_logs.append(event("certified", decision="hold", at=NOW))
    elif mutation == "malformed":
        read.audit_logs[0]["target"] = "{}"
    elif mutation == "untrusted_reviewer":
        read.audit_logs[0]["user_id"] = USER
    else:
        read.audit_logs.clear()
    assert read.badges_for("synthetic")["is_certified_creator"] is False
    assert policy_state["calls"] == 2
    read.audit_logs[:] = [event("certified")]
    assert read.badges_for("synthetic")["is_certified_creator"] is True
    assert policy_state["calls"] == 3


@pytest.mark.parametrize("kind", ["public", "member"])
@pytest.mark.parametrize("changed", ["reviewers", "scope"])
def test_current_policy_configuration_invalidates_read_registry(policy_state, kind, changed):
    read = view(kind, policy_state)
    assert read.badges_for("synthetic")["is_certified_creator"] is True
    changes = {"admin_user_ids": (USER,)} if changed == "reviewers" else {"stripe_ledger_id": "55555555-5555-4555-8555-555555555555"}
    policy_state["settings"] = replace(policy_state["settings"], **changes)
    assert read.badges_for("synthetic")["is_certified_creator"] is False
    assert policy_state["calls"] == 2


@pytest.mark.parametrize("kind", ["public", "member"])
@pytest.mark.parametrize("changed", ["ban", "badge", "badge_date"])
def test_current_account_flags_are_always_reevaluated(policy_state, kind, changed):
    read = view(kind, policy_state)
    assert read.badges_for("synthetic")["is_certified_creator"] is True
    row = (read.registered_users if kind == "public" else read.public_badges)["synthetic"]
    if changed == "ban": row["is_banned"] = True
    elif changed == "badge": row["is_certified_creator"] = False
    else: row["certified_creator_since"] += timedelta(seconds=1)
    assert read.badges_for("synthetic")["is_certified_creator"] is False
    assert policy_state["calls"] == 1


def test_requests_do_not_share_registry_or_negative_cache(policy_state):
    first = view("public", policy_state)
    second = view("member", policy_state, [event("certified", decision="hold")])
    assert first.badges_for("synthetic")["is_certified_creator"] is True
    assert second.badges_for("synthetic")["is_certified_creator"] is False
    third = view("public", policy_state, first.audit_logs)
    assert third.badges_for("synthetic")["is_certified_creator"] is True
    assert policy_state["calls"] == 3
    assert first._claim_registry_cache is not third._claim_registry_cache


def test_shared_mutable_store_financial_policy_remains_uncached(policy_state):
    store = DemoStore(seed=False)
    store.registered_users["synthetic"] = account()
    store.audit_logs = [event()]
    for _ in range(3):
        assert policy_state["policy"].eligibility(store, USER).identity.eligible is True
    assert policy_state["calls"] == 3
    assert not hasattr(store, "_claim_registry_cache")
    store.audit_logs[0] = event(decision="hold")
    assert policy_state["policy"].eligibility(store, USER).identity.eligible is False
    assert policy_state["calls"] == 4


def test_anonymous_information_projection_does_not_reference_audit_history(monkeypatch):
    backend = PostgresStateStore("unused-synthetic-no-connection")
    seen = []
    async def inspect(projection, params, joins, build):
        seen.append(projection)
        return build((2, 9, [], {}))
    monkeypatch.setattr(backend, "_read_view", inspect)
    read = asyncio.run(backend.public_view(catalog=False, live_claims=True))
    assert seen == ["'[]'::jsonb, '{}'::jsonb"]
    assert read.tools == [] and read.registered_users == {} and read.audit_logs == []


def test_fresh_database_reads_revoke_claims_and_keep_anonymous_information_empty(database, policy_state):
    backend, dsn = database
    store = DemoStore(seed=False)
    async def run():
        reader = PostgresStateStore(dsn, reuse_connections=True)
        try:
            async with backend.request(store):
                store.registered_users["synthetic"] = account()
                store.audit_logs = [event("certified")]
                await backend.save()
                first = await reader.public_view(catalog=True, live_claims=True)
                first.claim_policy = policy_state["policy"]
                assert first.badges_for("synthetic")["is_certified_creator"] is True
                information = await reader.public_view(catalog=False, live_claims=True)
                assert information.tools == [] and information.registered_users == {} and information.audit_logs == []
                store.audit_logs.append(event("certified", decision="hold", at=NOW))
                await backend.save()
                second = await reader.public_view(catalog=True, live_claims=True)
                second.claim_policy = policy_state["policy"]
                assert second.badges_for("synthetic")["is_certified_creator"] is False
                assert first.badges_for("synthetic")["is_certified_creator"] is True
                assert policy_state["calls"] == 2
        finally:
            await reader.close()
    asyncio.run(run())
