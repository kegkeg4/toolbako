"""Pure v2 decisions: real business records are never inferred to be tests."""
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.live_dataset import ARCHIVE_FIELDS, MANUAL_FIELDS, DatasetValidationError, prepare_candidate, value_hash
from app.live_dataset_review import prepare_reviewed_candidate
from tools.live_dataset_dry_run import artificial_fixture


def reviewed_fixture():
    state, manifest = artificial_fixture()
    seller, buyer = state["registered_users"]["sample_seller"]["id"], state["registered_users"]["sample_buyer"]["id"]
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    state["nda_records"] = {seller: {"version": "2026-07-16", "signed_at": at, "username": "sample_seller"}}
    state["nda_signatures"] = {seller}
    state["requests"] = [{"id": "77777777-7777-4777-8777-777777777777", "owner_id": buyer,
        "owner_username": "sample_buyer", "owner_name": "人工購入者", "status": "open", "applications": 1,
        "created_at": at, "title": "人工の制作相談", "category": "業務効率化", "detail": "検証用の依頼内容",
        "budget_min": 1000, "budget_max": 5000, "deadline": "2026-12-01",
        "application_list": [{"id": "88888888-8888-4888-8888-888888888888", "applicant_id": seller,
            "applicant_username": "sample_seller", "applicant_name": "人工販売者", "message": "検証用の提案内容",
            "amount": 3000, "delivery_days": 3, "status": "submitted"}]}]
    manifest["version"] = 2
    manifest["manual_review"] = {field: {"action": "carry" if state[field] else "empty",
        "record_sha256": value_hash(state[field]), "evidence_sha256": "d" * 64 if state[field] else None}
        for field in MANUAL_FIELDS}
    rehash(state, manifest)
    return state, manifest


def rehash(state, manifest, *, review=True):
    manifest["snapshot_sha256"] = value_hash(state)
    if review:
        for field in MANUAL_FIELDS:
            manifest["manual_review"][field]["record_sha256"] = value_hash(state[field])


def test_v1_behavior_is_unchanged():
    state, manifest = artificial_fixture()
    assert prepare_reviewed_candidate(state, manifest) == prepare_candidate(state, manifest)


def test_exact_reviewed_nda_and_open_requests_are_preserved_without_source_write():
    state, manifest = reviewed_fixture()
    original = deepcopy((state, manifest))
    result = prepare_reviewed_candidate(state, manifest)
    for field in ("nda_records", "nda_signatures", "requests"):
        assert result.candidate[field] == state[field]
        assert result.report["counts"][field]["excluded"] == 0
    assert result.report["source_sha256"] == value_hash(state)
    assert result.report["manifest_sha256"] == value_hash(manifest)
    assert result.report["candidate_sha256"] == value_hash(result.candidate)
    assert (state, manifest) == original
    assert all(not result.candidate[field] for field in ARCHIVE_FIELDS)
    for flag in ("database_written", "binding_created", "live_activated", "migration_complete",
                 "auth_storage_provenance_independently_verified", "source_target_identity_independently_verified"):
        assert result.report[flag] is False
    # Result mutation cannot mutate source/manual consent records.
    result.candidate["requests"][0]["application_list"][0]["message"] = "changed"
    result.candidate["nda_signatures"].clear()
    assert (state, manifest) == original


@pytest.mark.parametrize("field", sorted(MANUAL_FIELDS - {"nda_records", "nda_signatures", "requests", "account_deletions"}))
def test_other_manual_archives_require_explicit_review_hash_and_preserve_source(field):
    state, manifest = reviewed_fixture()
    state[field] = [{"id": "external-review-required", "private_detail": "not emitted in report"}]
    manifest["manual_review"][field].update(action="archive", evidence_sha256="e" * 64)
    rehash(state, manifest)
    before = deepcopy(state)
    result = prepare_reviewed_candidate(state, manifest)
    assert result.candidate[field] == [] and state == before
    assert result.report["manual_dispositions"][field] == "archive"
    assert result.report["counts"][field] == {"source": 1, "candidate": 0, "excluded": 1}
    assert "private_detail" not in str(result.report)


@pytest.mark.parametrize("fault", ["missing_review", "missing_collection", "extra_collection", "missing_evidence",
    "nonempty_empty", "empty_carry", "empty_evidence", "unknown_action", "unknown_key", "stale_snapshot", "stale_record", "unsupported_carry"])
def test_review_decision_fails_closed(fault):
    state, manifest = reviewed_fixture()
    if fault == "missing_review": manifest.pop("manual_review")
    if fault == "missing_collection": manifest["manual_review"].pop("nda_records")
    if fault == "extra_collection": manifest["manual_review"]["unknown"] = {}
    if fault == "missing_evidence": manifest["manual_review"]["requests"]["evidence_sha256"] = None
    if fault == "nonempty_empty": manifest["manual_review"]["requests"]["action"] = "empty"
    if fault == "empty_carry": manifest["manual_review"]["conversations"]["action"] = "carry"
    if fault == "empty_evidence": manifest["manual_review"]["conversations"]["evidence_sha256"] = "a" * 64
    if fault == "unknown_action": manifest["manual_review"]["requests"]["action"] = "automatically_test"
    if fault == "unknown_key": manifest["manual_review"]["requests"]["unreviewed"] = True
    if fault == "stale_snapshot": state["requests"][0]["title"] = "changed after review"
    if fault == "stale_record":
        state["requests"][0]["title"] = "changed collection"
        rehash(state, manifest, review=False)
    if fault == "unsupported_carry":
        state["conversations"] = [{"id": "conversation"}]
        manifest["manual_review"]["conversations"].update(action="carry", evidence_sha256="a" * 64)
        rehash(state, manifest)
    before = deepcopy((state, manifest))
    with pytest.raises(DatasetValidationError): prepare_reviewed_candidate(state, manifest)
    assert (state, manifest) == before


@pytest.mark.parametrize("fault", ["pair_action", "pair_members", "orphan", "wrong_username", "bad_time", "extra_nda_key"])
def test_nda_carry_requires_exact_pair_owner_and_record_shape(fault):
    state, manifest = reviewed_fixture()
    seller = state["registered_users"]["sample_seller"]["id"]
    row = state["nda_records"][seller]
    if fault == "pair_action": manifest["manual_review"]["nda_records"]["action"] = "archive"
    if fault == "pair_members": state["nda_signatures"].add(state["registered_users"]["sample_buyer"]["id"])
    if fault == "orphan":
        uid = str(uuid4()); state["nda_records"] = {uid: row}; state["nda_signatures"] = {uid}
    if fault == "wrong_username": row["username"] = "sample_buyer"
    if fault == "bad_time": row["signed_at"] = "2026-01-01T00:00:00"
    if fault == "extra_nda_key": row["unvalidated_rights"] = "unknown"
    rehash(state, manifest)
    with pytest.raises(DatasetValidationError): prepare_reviewed_candidate(state, manifest)


@pytest.mark.parametrize("fault", ["contracted", "selected", "unknown_owner", "wrong_owner_username", "owner_not_selected",
    "unknown_applicant", "wrong_applicant_username", "self_application", "duplicate_request", "duplicate_application",
    "bad_request_id", "bad_application_id", "application_count", "negative_budget", "reverse_budget", "boolean_budget",
    "zero_amount", "boolean_days", "bad_time", "unexpected_order_reference", "unexpected_application_key"])
def test_request_carry_requires_uncontracted_exact_ownership_and_shape(fault):
    state, manifest = reviewed_fixture()
    row, seller = state["requests"][0], state["registered_users"]["sample_seller"]["id"]
    application = row["application_list"][0]
    if fault == "contracted": row["status"] = "contracted"
    if fault == "selected": application["status"] = "selected"
    if fault == "unknown_owner": row["owner_id"] = str(uuid4())
    if fault == "wrong_owner_username": row["owner_username"] = "sample_seller"
    if fault == "owner_not_selected": manifest["users"].pop("sample_buyer")
    if fault == "unknown_applicant": application["applicant_id"] = str(uuid4())
    if fault == "wrong_applicant_username": application["applicant_username"] = "sample_buyer"
    if fault == "self_application":
        application["applicant_id"] = row["owner_id"]; application["applicant_username"] = row["owner_username"]
    if fault == "duplicate_request": state["requests"].append(deepcopy(row))
    if fault == "duplicate_application":
        row["application_list"].append(deepcopy(application)); row["applications"] = 2
    if fault == "bad_request_id": row["id"] = "legacy-test-id"
    if fault == "bad_application_id": application["id"] = "legacy-application"
    if fault == "application_count": row["applications"] = 0
    if fault == "negative_budget": row["budget_min"] = -1
    if fault == "reverse_budget": row["budget_min"] = 100000
    if fault == "boolean_budget": row["budget_max"] = True
    if fault == "zero_amount": application["amount"] = 0
    if fault == "boolean_days": application["delivery_days"] = True
    if fault == "bad_time": row["created_at"] = "unknown"
    if fault == "unexpected_order_reference": row["order_id"] = "financial-reference"
    if fault == "unexpected_application_key": application["payment_intent"] = "pi_old_test"
    rehash(state, manifest)
    before = deepcopy(state)
    with pytest.raises(DatasetValidationError): prepare_reviewed_candidate(state, manifest)
    assert state == before


def test_explicit_archive_does_not_infer_or_mutate_contract_history():
    state, manifest = reviewed_fixture()
    state["requests"][0]["status"] = "contracted"
    state["requests"][0]["application_list"][0]["status"] = "selected"
    manifest["manual_review"]["requests"]["action"] = "archive"
    rehash(state, manifest)
    result = prepare_reviewed_candidate(state, manifest)
    assert result.candidate["requests"] == [] and state["requests"][0]["status"] == "contracted"
    assert result.report["manual_dispositions"]["requests"] == "archive"


@pytest.mark.parametrize("status", ["scheduled", "erasing", "completed", "unknown"])
def test_shared_auth_erasure_guards_cannot_be_dropped_as_archive(status):
    state, manifest = reviewed_fixture()
    uid = state["registered_users"]["sample_buyer"]["id"]
    state["account_deletions"] = [{"id": "deletion-one", "user_id": uid, "status": status}]
    manifest["manual_review"]["account_deletions"].update(action="archive", evidence_sha256="a" * 64)
    rehash(state, manifest)
    with pytest.raises(DatasetValidationError): prepare_reviewed_candidate(state, manifest)


def test_error_messages_do_not_disclose_manual_record_contents():
    state, manifest = reviewed_fixture()
    state["requests"][0]["private-sentinel"] = "do not disclose"
    rehash(state, manifest)
    with pytest.raises(DatasetValidationError) as caught: prepare_reviewed_candidate(state, manifest)
    assert "private-sentinel" not in str(caught.value) and "do not disclose" not in str(caught.value)
