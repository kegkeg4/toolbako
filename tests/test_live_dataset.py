"""Migration candidates must preserve real evidence and exclude test authority."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re

import pytest

from app import live_dataset as ld
from app.listing_review import content_fingerprint
from app.persistence import STATE_FIELDS, decode_value, encode_value
from tools.live_dataset_dry_run import artificial_fixture, main


def refresh(state, manifest, *, review=False):
    """A test operator acknowledges changed fixture bytes, not validity."""
    manifest["snapshot_sha256"] = ld.value_hash(state)
    for name in manifest["users"]:
        manifest["users"][name]["record_sha256"] = ld.value_hash(state["registered_users"][name])
    for row in state["tools"]:
        if row["slug"] in manifest["tools"]:
            if review:
                row["safety_scan"]["content_fingerprint"] = content_fingerprint(row)
            manifest["tools"][row["slug"]]["record_sha256"] = ld.value_hash(row)
    manifest["archive_sha256"] = {field: ld.value_hash(state[field]) for field in ld.ARCHIVE_FIELDS}
    manifest["snapshot_sha256"] = ld.value_hash(state)


def test_snapshot_is_not_mutated_and_sensitive_history_is_not_carried():
    state, manifest = artificial_fixture()
    before, before_manifest = deepcopy(state), deepcopy(manifest)
    result = ld.prepare_candidate(state, manifest)
    assert state == before and manifest == before_manifest
    for field in ld.ARCHIVE_FIELDS | ld.MANUAL_FIELDS:
        assert result.candidate[field] == ld.empty_state()[field]
    assert "pi_artificial" not in repr(result.candidate)
    assert "artificial-not-a-real-token" not in repr(result.candidate)
    assert result.candidate["registered_users"] == state["registered_users"]
    tool = result.candidate["tools"][0]
    assert (tool["price"], tool["slug"], tool["instant_artifact"], tool["rights_confirmation"], tool["safety_scan"]) == (
        state["tools"][0]["price"], state["tools"][0]["slug"], state["tools"][0]["instant_artifact"], state["tools"][0]["rights_confirmation"], state["tools"][0]["safety_scan"])
    assert tool["sales_count"] == 0 and tool["rating"] is None and tool["like_count"] == 1
    assert result.candidate["blocks"] == state["blocks"]
    assert next(iter(result.candidate["security_settings"].values())) == {"two_factor": True, "login_alerts": True, "sessions": 0}
    assert all(result.report[key] is False for key in ("database_read", "database_written", "binding_created", "live_activated", "migration_complete", "auth_storage_provenance_independently_verified"))
    # Deep copies cannot change the source after return either.
    tool["instant_artifact"]["file"]["size"] = 999
    assert state == before


def test_all_persistence_fields_classified_and_type_roundtrip_is_preserved():
    state, manifest = artificial_fixture()
    assert set(ld.empty_state()) == set(STATE_FIELDS)
    candidate = ld.prepare_candidate(state, manifest).candidate
    assert decode_value(json.loads(json.dumps(encode_value(candidate)))) == candidate
    assert isinstance(candidate["tools"][0]["created_at"], datetime)
    assert isinstance(candidate["likes"], set) and all(isinstance(pair, tuple) for pair in candidate["likes"])


def test_runtime_store_collection_contract_and_transfer_nda_history_are_preserved():
    from app.data import DemoStore

    runtime = DemoStore(seed=False)
    source = {field: deepcopy(getattr(runtime, field)) for field in STATE_FIELDS}
    expected = ld.empty_state()
    assert {field: type(value) for field, value in source.items()} == {
        field: type(value) for field, value in expected.items()}
    # Use the actual list contract, including a nonempty historical acceptance.
    # It is acknowledged/excluded, never converted or removed from the source.
    source["transfer_nda_acceptances"].append({
        "inquiry_id": "synthetic-inquiry", "user_id": "synthetic-user",
        "version": "synthetic-version", "accepted_at": datetime(2026, 1, 1, tzinfo=timezone.utc)})
    _, manifest = artificial_fixture()
    manifest["users"] = {}
    manifest["tools"] = {}
    refresh(source, manifest)
    original = deepcopy(source)
    result = ld.prepare_candidate(source, manifest)
    assert source == original
    assert decode_value(json.loads(json.dumps(encode_value(source)))) == original
    assert type(result.candidate["transfer_nda_acceptances"]) is list
    assert result.candidate["transfer_nda_acceptances"] == []
    assert result.report["counts"]["transfer_nda_acceptances"] == {
        "source": 1, "candidate": 0, "excluded": 1}


def test_transfer_nda_wrong_collection_type_is_rejected_without_coercion():
    state, manifest = artificial_fixture()
    state["transfer_nda_acceptances"] = set()
    refresh(state, manifest)
    before = deepcopy(state)
    with pytest.raises(ld.DatasetValidationError, match="invalid_collection_type"):
        ld.prepare_candidate(state, manifest)
    assert state == before and type(state["transfer_nda_acceptances"]) is set


def test_runtime_created_product_without_thumbnail_keeps_none_and_defaults():
    from app.data import DemoStore
    from app.product_disclosures import record_rights

    state, manifest = artificial_fixture()
    original_tool = state["tools"][0]
    runtime = DemoStore(seed=False)
    # main.tool_create passes save_thumbnail(None, slug) == None to this
    # existing creation method; let the method supply its actual defaults.
    tool = runtime.create({key: deepcopy(original_tool[key]) for key in (
        "slug", "name", "tagline", "description_md", "author_id", "author_username",
        "author_name", "price", "price_type", "distribution", "fulfillment_type",
        "instant_artifact")} | {"thumbnail_url": None})
    record_rights(runtime, tool, state["registered_users"]["sample_seller"])
    tool["safety_scan"] = deepcopy(original_tool["safety_scan"])
    state["tools"] = runtime.tools
    state["legal_consents"] = runtime.legal_consents
    refresh(state, manifest, review=True)
    before = deepcopy(state)
    result = ld.prepare_candidate(state, manifest)
    assert state == before
    assert result.candidate["tools"][0] == before["tools"][0] | {"like_count": 1}
    assert result.candidate["tools"][0]["thumbnail_url"] is None
    assert result.candidate["tools"][0]["rights_confirmation"] == tool["rights_confirmation"]


@pytest.mark.parametrize("field,value", [
    ("name", None), ("author_email", None), ("demo_url", None),
    ("thumbnail_url", 0), ("thumbnail_url", {}), ("thumbnail_url", []),
])
def test_optional_thumbnail_does_not_relax_other_text_or_thumbnail_types(field, value):
    state, manifest = artificial_fixture()
    state["tools"][0][field] = value
    refresh(state, manifest, review=True)
    before = deepcopy(state)
    with pytest.raises(ld.DatasetValidationError):
        ld.prepare_candidate(state, manifest)
    assert state == before


@pytest.mark.parametrize("field", STATE_FIELDS)
def test_missing_state_field_fails_closed(field):
    state, manifest = artificial_fixture()
    del state[field]
    with pytest.raises(ld.DatasetValidationError):
        ld.prepare_candidate(state, manifest)


@pytest.mark.parametrize("path", ["top", "user", "tool", "artifact", "file", "review", "disclosures", "manifest", "target"])
def test_unknown_fields_cannot_hide_authority_or_private_data(path):
    state, manifest = artificial_fixture()
    tool = state["tools"][0]
    target = {"top": state, "user": state["registered_users"]["sample_seller"], "tool": tool,
              "artifact": tool["instant_artifact"], "file": tool["instant_artifact"]["file"], "review": tool["safety_scan"],
              "disclosures": tool.setdefault("purchase_disclosures", {}), "manifest": manifest, "target": manifest["target"]}[path]
    target["access_token"] = "never-echo-this-private-sentinel"
    refresh(state, manifest)
    with pytest.raises(ld.DatasetValidationError) as caught:
        ld.prepare_candidate(state, manifest)
    assert "sentinel" not in str(caught.value)


@pytest.mark.parametrize("field", ld.ARCHIVE_FIELDS)
def test_every_excluded_collection_requires_exact_hash_acknowledgment(field):
    state, manifest = artificial_fixture()
    del manifest["archive_sha256"][field]
    with pytest.raises(ld.DatasetValidationError):
        ld.prepare_candidate(state, manifest)
    manifest["archive_sha256"][field] = "0" * 64
    with pytest.raises(ld.DatasetValidationError):
        ld.prepare_candidate(state, manifest)


@pytest.mark.parametrize("field", ld.MANUAL_FIELDS)
def test_unreviewed_mixed_business_data_cannot_be_silently_discarded(field):
    state, manifest = artificial_fixture()
    value = state[field]
    if isinstance(value, list): value.append({"unresolved": True})
    elif isinstance(value, dict): value["unresolved"] = True
    else: value.add("unresolved")
    refresh(state, manifest)
    with pytest.raises(ld.DatasetValidationError, match="manual_review_required"):
        ld.prepare_candidate(state, manifest)


@pytest.mark.parametrize("fault", ["same_database", "same_account", "same_ledger", "wrong_source_mode", "wrong_target_mode", "bad_ledger", "different_auth", "different_storage", "credential_url", "revision_bool", "version_bool"])
def test_scope_identity_is_explicit_and_cannot_relabel_current_dataset(fault):
    state, manifest = artificial_fixture()
    source, target = manifest["source"], manifest["target"]
    if fault == "same_database": target["database_fingerprint"] = source["database_fingerprint"]
    elif fault == "same_account": target["platform_account"] = source["platform_account"]
    elif fault == "same_ledger": source["ledger_id"] = target["ledger_id"]
    elif fault == "wrong_source_mode": source["mode"] = "live"
    elif fault == "wrong_target_mode": target["mode"] = "test"
    elif fault == "bad_ledger": target["ledger_id"] = "anything"
    elif fault == "different_auth": target["auth_project"] = "another-auth"
    elif fault == "different_storage": target["storage_project"] = "another-storage"
    elif fault == "credential_url": target["database_fingerprint"] = "postgres://private@host/db"
    elif fault == "revision_bool": manifest["source_revision"] = True
    else: manifest["version"] = True
    with pytest.raises(ld.DatasetValidationError):
        ld.prepare_candidate(state, manifest)


@pytest.mark.parametrize("fault", ["no_evidence", "test_evidence", "other_account", "bad_hash", "inconsistent_flag", "pending"])
def test_identity_provenance_is_not_inferred_from_profile_flag(fault):
    state, manifest = artificial_fixture()
    row = state["registered_users"]["sample_seller"]
    row.update(is_verified=True, identity_status="verified")
    proof = {"mode": "live", "platform_account": manifest["target"]["platform_account"], "evidence_sha256": "a" * 64}
    manifest["users"]["sample_seller"]["identity_evidence"] = proof
    if fault == "no_evidence": manifest["users"]["sample_seller"]["identity_evidence"] = None
    elif fault == "test_evidence": proof["mode"] = "test"
    elif fault == "other_account": proof["platform_account"] = "acct_other"
    elif fault == "bad_hash": proof["evidence_sha256"] = "not-evidence"
    elif fault == "inconsistent_flag": row["is_verified"] = False
    else: row["identity_status"] = "pending"
    refresh(state, manifest)
    before = deepcopy(state)
    with pytest.raises(ld.DatasetValidationError):
        ld.prepare_candidate(state, manifest)
    assert state == before


def test_supabase_profile_default_is_preserved_without_granting_identity():
    root = Path(__file__).resolve().parents[1]
    defaults = set()
    for filename in ("schema.sql", "bootstrap_runtime_auth.sql"):
        schema = (root / "supabase" / filename).read_text(encoding="utf-8")
        match = re.search(r"identity_status\s+text\s+not\s+null\s+default\s+'([^']+)'", schema)
        assert match is not None
        defaults.add(match.group(1))
    assert defaults == {"unverified"}

    state, manifest = artificial_fixture()
    for row in state["registered_users"].values():
        row.update(is_verified=False, identity_status=next(iter(defaults)))
    refresh(state, manifest)
    before, before_manifest = deepcopy(state), deepcopy(manifest)
    result = ld.prepare_candidate(state, manifest)
    assert result.candidate["registered_users"] == before["registered_users"]
    assert all(not row["is_verified"] and row["identity_status"] == "unverified"
               for row in result.candidate["registered_users"].values())
    assert result.candidate["identity_applications"] == {}
    for uid, security in before["security_settings"].items():
        assert result.candidate["security_settings"][uid]["two_factor"] == security["two_factor"]
    assert state == before and manifest == before_manifest


@pytest.mark.parametrize("status,verified,with_proof", [
    ("unverified", True, False), ("unverified", True, True),
    ("unverified", False, True), ("verified", False, False),
    ("pending", False, False), ("rejected", False, False),
])
def test_unverified_default_does_not_allow_contradictions_or_other_statuses(status, verified, with_proof):
    state, manifest = artificial_fixture()
    state["registered_users"]["sample_seller"].update(identity_status=status, is_verified=verified)
    if with_proof:
        manifest["users"]["sample_seller"]["identity_evidence"] = {
            "mode": "live", "platform_account": manifest["target"]["platform_account"],
            "evidence_sha256": "a" * 64}
    refresh(state, manifest)
    before = deepcopy(state)
    with pytest.raises(ld.DatasetValidationError, match="identity_provenance_required"):
        ld.prepare_candidate(state, manifest)
    assert state == before


def test_explicit_live_provenance_preserves_identity_badge_and_ban_without_claiming_verification():
    state, manifest = artificial_fixture()
    row = state["registered_users"]["sample_seller"]
    row.update(is_verified=True, identity_status="verified", is_certified_creator=True, certified_creator_since=datetime(2026, 1, 1, tzinfo=timezone.utc), is_banned=True)
    evidence = manifest["users"]["sample_seller"]
    evidence["identity_evidence"] = {"mode": "live", "platform_account": manifest["target"]["platform_account"], "evidence_sha256": "a" * 64}
    evidence["badge_evidence"] = {"is_certified_creator": "b" * 64}
    refresh(state, manifest)
    result = ld.prepare_candidate(state, manifest)
    assert result.candidate["registered_users"]["sample_seller"] == row
    assert result.report["auth_storage_provenance_independently_verified"] is False
    evidence["badge_evidence"] = {}
    with pytest.raises(ld.DatasetValidationError): ld.prepare_candidate(state, manifest)


@pytest.mark.parametrize("fault", ["owner", "reviewer", "like_user", "like_tool", "block", "folder_owner", "folder_assignment", "consent_owner", "consent_tool"])
def test_orphan_references_never_make_a_candidate(fault):
    state, manifest = artificial_fixture()
    tool = state["tools"][0]
    uid = tool["author_id"]
    other = "99999999-9999-4999-8999-999999999999"
    if fault == "owner": tool["author_id"] = other
    elif fault == "reviewer": tool["safety_scan"]["reviewer_id"] = other
    elif fault == "like_user": state["likes"].add((other, tool["slug"]))
    elif fault == "like_tool": state["likes"].add((uid, "missing"))
    elif fault == "block": state["blocks"].add((uid, "missing"))
    elif fault == "folder_owner": state["favorite_folders"][other] = []
    elif fault == "folder_assignment": state["favorite_assignments"][next(iter(state["favorite_assignments"]))] = "missing"
    elif fault == "consent_owner": state["legal_consents"][0]["user_id"] = other
    else: state["legal_consents"][0]["tool_id"] = other
    refresh(state, manifest)
    with pytest.raises(ld.DatasetValidationError): ld.prepare_candidate(state, manifest)


@pytest.mark.parametrize("fault", ["fingerprint", "scan_failed", "backend", "object_key", "file_id", "file_hash", "manifest_hash", "rights_consent"])
def test_exact_review_file_and_rights_evidence_required(fault):
    state, manifest = artificial_fixture()
    tool = state["tools"][0]
    file = tool["instant_artifact"]["file"]
    if fault == "fingerprint": tool["name"] = "Changed after review"
    elif fault == "scan_failed": file["scan_status"] = "failed"
    elif fault == "backend": file["storage_backend"] = "local"
    elif fault == "object_key": file["object_key"] = "deliveries/other/file.zip"
    elif fault == "file_id": file["id"] = "wrong"
    elif fault == "file_hash": file["sha256"] = "invalid"
    elif fault == "manifest_hash": manifest["tools"][tool["slug"]]["artifact_sha256"] = "d" * 64
    else: state["legal_consents"] = []
    refresh(state, manifest)
    with pytest.raises(ld.DatasetValidationError): ld.prepare_candidate(state, manifest)


def test_excluded_user_selection_prunes_only_owned_social_and_preference_rows():
    state, manifest = artificial_fixture()
    state["blocks"].clear()
    refresh(state, manifest)
    del manifest["users"]["sample_buyer"]
    result = ld.prepare_candidate(state, manifest)
    for field in ("likes", "blocks", "favorite_folders", "favorite_assignments", "notification_preferences"):
        assert not result.candidate[field]
    assert result.candidate["tools"][0]["like_count"] == 0
    assert result.candidate["tools"][0]["slug"] == state["tools"][0]["slug"]


def test_block_of_omitted_user_is_not_silently_removed_with_shared_auth():
    state, manifest = artificial_fixture()
    del manifest["users"]["sample_buyer"]
    with pytest.raises(ld.DatasetValidationError, match="security_reference_requires_selection"):
        ld.prepare_candidate(state, manifest)


def test_locally_banned_user_cannot_be_omitted_with_shared_auth():
    state, manifest = artificial_fixture()
    state["blocks"].clear()  # The ban must be protected without a block reference.
    state["registered_users"]["sample_buyer"].update(
        is_banned=True, ban_reason="operator suspension")
    refresh(state, manifest)
    del manifest["users"]["sample_buyer"]
    before = deepcopy(state)
    with pytest.raises(ld.DatasetValidationError, match="banned_user_requires_selection"):
        ld.prepare_candidate(state, manifest)
    assert state == before


def test_selected_locally_banned_user_keeps_the_suspension():
    state, manifest = artificial_fixture()
    state["blocks"].clear()
    banned = state["registered_users"]["sample_buyer"]
    banned.update(is_banned=True, ban_reason="operator suspension",
                  banned_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    refresh(state, manifest)
    result = ld.prepare_candidate(state, manifest)
    assert result.candidate["registered_users"]["sample_buyer"] == banned
    assert result.candidate["registered_users"]["sample_buyer"]["is_banned"] is True
    assert result.report["auth_storage_provenance_independently_verified"] is False


@pytest.mark.parametrize("kind", ["unknown_user", "unknown_tool", "selected_password", "phone_verified"])
def test_unclassified_source_records_and_unproven_authority_are_stopped(kind):
    state, manifest = artificial_fixture()
    if kind == "unknown_user":
        state["registered_users"]["ignored_user"] = {"id": "legacy", "unknown_future_authority": True}
    elif kind == "unknown_tool":
        state["tools"].append({"slug": "ignored-tool", "new_financial_setting": 10})
    elif kind == "selected_password":
        state["registered_users"]["sample_seller"]["password_hash"] = "private-sentinel"
    else:
        state["registered_users"]["sample_seller"]["phone_verified"] = True
    refresh(state, manifest)
    with pytest.raises(ld.DatasetValidationError): ld.prepare_candidate(state, manifest)


def test_new_persistence_field_requires_explicit_classification(monkeypatch):
    monkeypatch.setattr(ld, "STATE_FIELDS", (*STATE_FIELDS, "future_financial_field"))
    with pytest.raises(ld.DatasetValidationError, match="unclassified_state_field"):
        ld.empty_state()


def test_encoded_json_cannot_be_mistaken_for_decoded_snapshot():
    state, manifest = artificial_fixture()
    with pytest.raises(ld.DatasetValidationError, match="invalid_collection_type"):
        ld.prepare_candidate(encode_value(state), manifest)


@pytest.mark.parametrize("field,value", [("manifest_id", "not-uuid"), ("source_schema_version", True), ("source_schema_version", 999), ("source_commit", "main"), ("target_commit", "release")])
def test_unpinned_or_unknown_schema_revision_is_rejected(field, value):
    state, manifest = artificial_fixture()
    manifest[field] = value
    with pytest.raises(ld.DatasetValidationError): ld.prepare_candidate(state, manifest)


def test_pure_prepare_does_not_use_network_database_or_files(monkeypatch):
    import builtins
    import socket
    import sqlite3
    state, manifest = artificial_fixture()
    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected IO")
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    assert ld.prepare_candidate(state, manifest).report["migration_complete"] is False


def test_stale_source_and_stale_provenance_stop_without_exposing_details():
    state, manifest = artificial_fixture()
    state["registered_users"]["sample_seller"]["display_name"] = "private-sentinel"
    with pytest.raises(ld.DatasetValidationError, match="snapshot_mismatch") as caught: ld.prepare_candidate(state, manifest)
    assert "private" not in str(caught.value)
    manifest["snapshot_sha256"] = ld.value_hash(state)
    with pytest.raises(ld.DatasetValidationError, match="provenance_mismatch"): ld.prepare_candidate(state, manifest)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), object(), datetime(2026, 1, 1)])
def test_non_portable_types_cannot_be_hashed(value):
    with pytest.raises(ld.DatasetValidationError): ld.value_hash(value)


def test_typed_hash_is_deterministic_and_distinguishes_structure():
    assert ld.value_hash({"b": {("u", "x"), ("v", "y")}, "a": 1}) == ld.value_hash({"a": 1, "b": {("v", "y"), ("u", "x")}})
    assert len({ld.value_hash(value) for value in (True, 1, 1.0, "1", [], (), set(), {})}) == 8
    cyclic = []; cyclic.append(cyclic)
    with pytest.raises(ld.DatasetValidationError): ld.value_hash(cyclic)


def test_cli_artificial_only_outputs_no_personal_or_financial_payload(capsys):
    assert main(["--artificial-fixture"]) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["artificial_fixture"] is True and report["migration_complete"] is False
    assert report["counts"]["orders"] == {"source": 1, "candidate": 0, "excluded": 1}
    for secret in ("example.invalid", "pi_artificial", "access_token", "sample_seller", "acct_artificial"):
        assert secret not in output
    with pytest.raises(SystemExit): main(["--input", "real.json"])
