"""Explicit disposition of mixed non-financial records before live import.

This extension never infers that a record is test data. A reviewed collection
hash and evidence hash are mandatory. Source values are never modified.
"""
from copy import deepcopy

from . import live_dataset as base


SUPPORTED_CARRY = frozenset({"requests", "nda_records", "nda_signatures"})


def prepare_reviewed_candidate(state, manifest):
    """Version 2 adds exact reviewed carry/archive decisions for manual fields.

    Archive retains the record in the immutable source; this function cannot
    remove it there. Evidence hashes are assertions to verify independently.
    """
    if type(manifest) is not dict or manifest.get("version") == 1:
        return base.prepare_candidate(state, manifest)
    try:
        return _prepare(state, manifest)
    except base.DatasetValidationError:
        raise
    except (TypeError, ValueError, KeyError, RecursionError, OverflowError):
        raise base.DatasetValidationError("invalid_structure") from None


def _prepare(state, manifest):
    base._state_shape(state)
    # Auth remains shared. Dropping erasure holds can re-enable sign-in; these
    # records need a dedicated lifecycle migration, even with an archive hash.
    base._require(not state["account_deletions"], "deletion_lifecycle_review_required")
    base._require(type(manifest.get("version")) is int and manifest["version"] == 2,
                  "manifest_version")
    reviews = manifest.get("manual_review")
    base._keys(reviews, base.MANUAL_FIELDS, base.MANUAL_FIELDS)
    original_hash = base.value_hash(state)
    base._require(manifest.get("snapshot_sha256") == original_hash, "snapshot_mismatch")
    stripped, delegated = deepcopy(state), deepcopy(manifest)
    for field, decision in reviews.items():
        base._keys(decision, {"action", "record_sha256", "evidence_sha256"},
                   {"action", "record_sha256", "evidence_sha256"})
        base._require(decision["record_sha256"] == base.value_hash(state[field]),
                      "manual_review_mismatch")
        base._require(decision["action"] in {"empty", "carry", "archive"}, "manual_review_required")
        if state[field]:
            base._hash(decision["evidence_sha256"])
            base._require(decision["action"] != "empty", "manual_review_required")
        else:
            base._require(decision["action"] == "empty" and decision["evidence_sha256"] is None,
                          "manual_review_required")
        if decision["action"] == "carry":
            base._require(field in SUPPORTED_CARRY, "unsupported_manual_carry")
        stripped[field] = base.empty_state()[field]
    delegated.pop("manual_review")
    delegated["version"] = 1
    delegated["snapshot_sha256"] = base.value_hash(stripped)
    prepared = base.prepare_candidate(stripped, delegated)
    candidate = prepared.candidate
    users = candidate["registered_users"]
    ids = {user["id"]: username for username, user in users.items()}
    nda_carry = {field for field in ("nda_records", "nda_signatures")
                 if reviews[field]["action"] == "carry"}
    base._require(not nda_carry or nda_carry == {"nda_records", "nda_signatures"},
                  "nda_pair_required")
    if nda_carry:
        base._require(set(state["nda_records"]) == state["nda_signatures"], "nda_pair_mismatch")
        for uid, record in state["nda_records"].items():
            base._require(uid in ids, "orphan_reference")
            base._keys(record, {"version", "signed_at", "username"}, {"version", "signed_at", "username"})
            base._text(record["version"]); base._time(record["signed_at"])
            base._require(record["username"] == ids[uid], "owner_mismatch")
        for field in nda_carry:
            candidate[field] = deepcopy(state[field])
    if reviews["requests"]["action"] == "carry":
        seen = set()
        for row in state["requests"]:
            required = {"id", "owner_id", "owner_name", "owner_username", "status", "applications",
                        "application_list", "created_at", "title", "category", "detail", "budget_min",
                        "budget_max", "deadline"}
            base._keys(row, required, required)
            base._uuid(row["id"])
            base._require(row["id"] not in seen, "duplicate_request")
            seen.add(row["id"])
            base._require(row["owner_id"] in ids and row["owner_username"] == ids[row["owner_id"]],
                          "orphan_reference")
            base._require(row["status"] == "open", "request_contract_review_required")
            base._time(row["created_at"])
            for field in ("title", "category", "detail", "deadline", "owner_name"):
                base._text(row[field])
            for field in ("budget_min", "budget_max", "applications"):
                base._require(type(row[field]) is int and row[field] >= 0, "invalid_type")
            base._require(row["budget_min"] <= row["budget_max"], "invalid_type")
            base._require(type(row["application_list"]) is list and
                          row["applications"] == len(row["application_list"]), "invalid_application_count")
            applicants, application_ids = set(), set()
            for item in row["application_list"]:
                keys = {"id", "applicant_id", "applicant_name", "applicant_username", "message",
                        "amount", "delivery_days", "status"}
                base._keys(item, keys, keys); base._uuid(item["id"])
                base._require(item["applicant_id"] in ids and item["applicant_id"] != row["owner_id"]
                              and item["applicant_username"] == ids[item["applicant_id"]], "orphan_reference")
                base._require(item["applicant_id"] not in applicants and item["id"] not in application_ids,
                              "duplicate_application")
                applicants.add(item["applicant_id"]); application_ids.add(item["id"])
                base._require(item["status"] == "submitted", "request_contract_review_required")
                for field in ("applicant_name", "message"):
                    base._text(item[field])
                for field in ("amount", "delivery_days"):
                    base._require(type(item[field]) is int and item[field] > 0, "invalid_type")
        candidate["requests"] = deepcopy(state["requests"])
    base._state_shape(candidate)
    report = prepared.report | {
        "manifest_sha256": base.value_hash(manifest), "source_sha256": original_hash,
        "candidate_sha256": base.value_hash(candidate),
        "counts": {field: {"source": len(state[field]), "candidate": len(candidate[field]),
                           "excluded": len(state[field])-len(candidate[field])} for field in base.STATE_FIELDS},
        "manual_dispositions": {field: row["action"] for field, row in reviews.items()},
    }
    return base.DryRunResult(candidate, report)
