"""Artificial provenance decisions only; no Auth, documents or live records."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import pytest

from app.financial_scope import FinancialScope
from app import live_identity as policy


NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
USER = "11111111-1111-4111-8111-111111111111"
REVIEWER = "22222222-2222-4222-8222-222222222222"
LIVE = FinancialScope("live", "acct_LivePlatform", "33333333-3333-4333-8333-333333333333")
TEST = FinancialScope("test", "acct_TestPlatform", "44444444-4444-4444-8444-444444444444")


def account():
    return {"id": USER, "username": "synthetic", "is_verified": True,
            "identity_status": "verified", "is_founding_member": True,
            "founding_member_since": NOW - timedelta(days=10),
            "is_certified_creator": True, "certified_creator_since": NOW - timedelta(days=2),
            "mfa_enrolled": True, "ban_reason": "", "email": "synthetic@example.invalid"}


def event(claim="identity", *, subject=None, decision="approve", at=None, scope=LIVE, record=None):
    row = account() if record is None else record
    return {"id": str(uuid4()), "user_id": REVIEWER, "action": policy.REVIEW_ACTION,
            "created_at": at or NOW - timedelta(minutes=1),
            "target": json.dumps({"version": 1, "scope": scope.payload(),
                "subject_user_id": subject or row["id"], "claim": claim,
                "claim_sha256": policy.claim_digest(row, claim),
                "evidence_sha256": "a" * 64, "source_record_sha256": "b" * 64,
                "source": "reviewed_import", "decision": decision})}


def registry(events):
    return policy.review_registry(events, LIVE, {REVIEWER}, now=NOW)


def change_payload(row, **changes):
    result = deepcopy(row)
    value = json.loads(result["target"])
    value.update(changes)
    result["target"] = json.dumps(value)
    return result


def test_live_without_review_holds_eligibility_and_does_not_revoke_original_claims():
    raw = account()
    session = {**raw, "mfa_verified_at": NOW, "access_token": "synthetic-never-read"}
    before = deepcopy((raw, session))
    result = policy.annotate_live_eligibility(session, raw, LIVE)
    assert result["is_verified"] is True and result["identity_status"] == "verified"
    assert result["is_certified_creator"] is True and result["mfa_verified_at"] == NOW
    assert result["live_claims_required"] is True
    assert result["live_identity_eligible"] is False
    assert result["live_identity_reason"] == "provenance_unavailable"
    assert (raw, session) == before and result is not session


def test_shared_profile_or_session_self_asserted_proof_cannot_become_approval():
    raw = account()
    raw["live_identity_eligible"] = True
    raw["trust_record"] = event()
    raw["app_metadata"] = {"live_identity_eligible": True}
    result = policy.evaluate_claims(raw, LIVE, registry([]))
    assert result.identity.eligible is False and result.identity.reason == "provenance_missing"
    assert result.founding.eligible is False and result.certified.eligible is False


def test_reviewed_matching_claims_preserve_legitimate_live_eligibility():
    raw, events = account(), [event(claim) for claim in policy.CLAIMS]
    before = deepcopy((raw, events))
    trust = registry(events)
    result = policy.evaluate_claims(raw, LIVE, trust)
    assert result.live_required and result.live_identity_eligible
    assert all(getattr(result, claim).eligible for claim in policy.CLAIMS)
    assert (raw, events) == before
    # Registry does not alias mutable audit payloads.
    events[0]["target"] = "{}"
    assert policy.evaluate_claims(raw, LIVE, trust).identity.eligible


def test_identity_and_badge_reviews_are_separate():
    trust = registry([event("certified")])
    result = policy.evaluate_claims(account(), LIVE, trust)
    assert not result.identity.eligible and not result.founding.eligible and result.certified.eligible


@pytest.mark.parametrize("scope", [None, TEST])
def test_legacy_test_claim_behavior_is_preserved(scope):
    raw = account()
    raw["is_verified"] = False
    raw["is_certified_creator"] = "legacy-truthy"
    result = policy.evaluate_claims(raw, scope)
    assert result.live_required is False and result.identity.eligible and result.certified.eligible
    assert result.identity.reason == "legacy_scope"


@pytest.mark.parametrize("scope", [{"mode": "live"}, "live", False])
def test_malformed_live_scope_does_not_fall_back_to_legacy(scope):
    result = policy.evaluate_claims(account(), scope)
    assert result.live_required and not result.identity.eligible
    assert result.identity.reason == "invalid_live_scope"


@pytest.mark.parametrize("fault", ["subject", "platform", "ledger", "test", "digest", "unminted", "copy"])
def test_live_scope_subject_and_evidence_must_all_match(fault):
    reviewed = event()
    scope = LIVE
    if fault == "subject": reviewed = change_payload(reviewed, subject_user_id=str(uuid4()))
    if fault == "platform": reviewed = change_payload(reviewed, scope=replace(LIVE, platform_account="acct_Another").payload())
    if fault == "ledger": reviewed = change_payload(reviewed, scope=replace(LIVE, ledger_id=str(uuid4())).payload())
    if fault == "test": reviewed = change_payload(reviewed, scope=TEST.payload())
    if fault == "digest": reviewed = change_payload(reviewed, claim_sha256="f" * 64)
    trust = registry([reviewed])
    if fault == "unminted": trust = policy.ReviewRegistry(LIVE, trust._reviews)
    if fault == "copy": trust = replace(trust)
    assert not policy.evaluate_claims(account(), scope, trust).identity.eligible


def test_registry_cannot_be_reused_for_another_ledger():
    other = replace(LIVE, ledger_id=str(uuid4()))
    assert policy.evaluate_claims(account(), other, registry([event()])).identity.reason == "provenance_unavailable"


@pytest.mark.parametrize("flag", [True, "true", 1])
def test_existing_ban_is_never_overruled_by_reviewed_identity(flag):
    raw = account()
    raw["is_banned"] = flag
    result = policy.evaluate_claims(raw, LIVE, registry([event(claim) for claim in policy.CLAIMS]))
    assert not result.identity.eligible and result.identity.reason == "account_restricted"
    assert not result.founding.eligible and not result.certified.eligible
    assert raw["is_banned"] == flag


def test_session_claims_do_not_authorize_another_or_missing_runtime_account():
    raw = account()
    session = {**raw, "id": str(uuid4())}
    trust = registry([event()])
    assert not policy.annotate_live_eligibility(session, raw, LIVE, trust)["live_identity_eligible"]
    assert not policy.annotate_live_eligibility(raw, None, LIVE, trust)["live_identity_eligible"]


@pytest.mark.parametrize("value", [False, "true", 1, None])
def test_live_identity_requires_strict_positive_boolean(value):
    raw = {**account(), "is_verified": value}
    assert not policy.evaluate_claims(raw, LIVE, registry([event()])).identity.eligible


@pytest.mark.parametrize("value", ["not_started", "pending", "rejected", None])
def test_live_identity_requires_current_positive_status(value):
    raw = {**account(), "identity_status": value}
    assert not policy.evaluate_claims(raw, LIVE, registry([event()])).identity.eligible


def test_changed_badge_date_cannot_reuse_prior_review():
    raw = account()
    raw["certified_creator_since"] = NOW
    result = policy.evaluate_claims(raw, LIVE, registry([event("certified")]))
    assert not result.certified.eligible and result.certified.reason == "claim_changed"


def test_equivalent_badge_timestamp_representation_matches():
    raw = account()
    raw["certified_creator_since"] = raw["certified_creator_since"].isoformat().replace("+00:00", "Z")
    assert policy.evaluate_claims(raw, LIVE, registry([event("certified")])).certified.eligible


def test_newer_hold_wins_independent_of_audit_input_order():
    approval = event(at=NOW - timedelta(minutes=2))
    hold = event(decision="hold", at=NOW - timedelta(minutes=1))
    for rows in ([approval, hold], [hold, approval]):
        result = policy.evaluate_claims(account(), LIVE, registry(rows))
        assert not result.identity.eligible and result.identity.reason == "review_hold"
    assert account()["is_verified"] is True


def test_exact_duplicate_audit_delivery_is_idempotent():
    approved = event()
    assert policy.evaluate_claims(account(), LIVE, registry([approved, deepcopy(approved)])).identity.eligible


def test_same_time_conflicting_decisions_have_no_arbitrary_winner():
    with pytest.raises(policy.ClaimEvidenceError, match="ambiguous_review_order"):
        registry([event(), event(decision="hold")])


def test_duplicate_id_with_different_subject_is_rejected():
    first = event()
    second = change_payload(first, subject_user_id=str(uuid4()))
    with pytest.raises(policy.ClaimEvidenceError, match="conflicting_review_id"):
        registry([first, second])


@pytest.mark.parametrize("fault", ["actor", "actor_missing", "version", "unknown_field", "subject", "claim",
    "decision", "source", "source_list", "evidence_hash", "source_hash", "scope", "future", "naive", "json", "duplicate_json"])
def test_malformed_review_fails_closed_without_exposing_values(fault):
    reviewed = event()
    if fault == "actor": reviewed["user_id"] = str(uuid4())
    elif fault == "actor_missing": reviewed.pop("user_id")
    elif fault == "version": reviewed = change_payload(reviewed, version=True)
    elif fault == "unknown_field": reviewed = change_payload(reviewed, private_sentinel="private-sentinel")
    elif fault == "subject": reviewed = change_payload(reviewed, subject_user_id="private-sentinel")
    elif fault == "claim": reviewed = change_payload(reviewed, claim="private-sentinel")
    elif fault == "decision": reviewed = change_payload(reviewed, decision=["approve"])
    elif fault == "source": reviewed = change_payload(reviewed, source="unreviewed_profile")
    elif fault == "source_list": reviewed = change_payload(reviewed, source=["reviewed_import"])
    elif fault == "evidence_hash": reviewed = change_payload(reviewed, evidence_sha256="private-sentinel")
    elif fault == "source_hash": reviewed = change_payload(reviewed, source_record_sha256="private-sentinel")
    elif fault == "scope": reviewed = change_payload(reviewed, scope={"mode": "live"})
    elif fault == "future": reviewed["created_at"] = NOW + timedelta(seconds=1)
    elif fault == "naive": reviewed["created_at"] = NOW.replace(tzinfo=None)
    elif fault == "json": reviewed["target"] = "private-sentinel"
    elif fault == "duplicate_json": reviewed["target"] = reviewed["target"][:-1] + ',"decision":"hold"}'
    with pytest.raises(policy.ClaimEvidenceError) as error:
        registry([reviewed])
    assert "private-sentinel" not in str(error.value)


def test_irrelevant_audit_action_does_not_grant_a_claim():
    row = event()
    row["action"] = "profile.updated"
    assert not policy.evaluate_claims(account(), LIVE, registry([row])).identity.eligible


def test_registry_and_evaluation_do_not_use_io_or_read_private_profile_contents(monkeypatch):
    def forbidden(*args, **kwargs): raise AssertionError("No IO allowed")
    monkeypatch.setattr("builtins.open", forbidden)
    monkeypatch.setattr("socket.socket", forbidden)
    raw = {**account(), "documents": object(), "provider_tokens": object()}
    assert policy.evaluate_claims(raw, LIVE, registry([event()])).identity.eligible
