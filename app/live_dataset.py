"""Pure, fail-closed preparation of a *candidate* live dataset.

No database, environment, HTTP, filesystem, binding or activation operations.
Provenance in the manifest is an operator assertion, not independently verified
Auth/Storage evidence. A successful result is NOT permission to import it.
Input is the decoded persistence format (datetime/set/tuple preserved).
"""
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
import re
from uuid import UUID

from .listing_review import REVIEW_CHECKS, content_fingerprint
from .persistence import STATE_FIELDS


LIST_FIELDS = frozenset("tools reports orders conversations transfer_inquiries reviews buyer_reviews notifications requests payouts subscriptions coupons support_cases audit_logs account_deletions legal_consents".split())
DICT_FIELDS = frozenset("favorite_folders favorite_assignments notification_preferences identity_applications nda_records security_settings registered_users connected_accounts account_sessions".split())
SET_FIELDS = frozenset("likes transfer_nda_acceptances blocks update_followers creator_follows reopen_waiters nda_signatures coupon_redemptions processed_webhook_events".split())
CARRY_FIELDS = frozenset("tools registered_users likes favorite_folders favorite_assignments notification_preferences blocks update_followers creator_follows reopen_waiters security_settings legal_consents".split())
MANUAL_FIELDS = frozenset("reports conversations requests support_cases account_deletions coupons nda_signatures nda_records".split())
ARCHIVE_FIELDS = frozenset("orders payouts subscriptions connected_accounts identity_applications processed_webhook_events coupon_redemptions reviews buyer_reviews transfer_inquiries transfer_nda_acceptances notifications account_sessions audit_logs".split())
NORMALIZED_EMPTY_TABLES = ("finance_receipts", "finance_payouts", "finance_allocations", "finance_entries", "stripe_operations", "email_outbox")
SUPPORTED_RUNTIME_SCHEMA_VERSION = 2


class DatasetValidationError(ValueError):
    """Only fixed reason codes: never interpolate records, paths or tokens."""
    def __init__(self, code):
        self.code = code
        super().__init__(f"live_dataset:{code}")


def _require(ok, code):
    if not ok:
        raise DatasetValidationError(code)


def _keys(value, allowed, required=()):
    _require(type(value) is dict and set(value) <= set(allowed) and set(required) <= set(value), "unknown_or_missing_field")


def _uuid(value):
    try:
        valid = isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        valid = False
    _require(valid, "invalid_uuid")


def _hash(value):
    _require(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None, "invalid_hash")


def _text(value):
    _require(type(value) is str, "invalid_type")


def _bool(value):
    _require(type(value) is bool, "invalid_type")


def _time(value):
    if type(value) is datetime:
        parsed = value
    elif type(value) is str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise DatasetValidationError("invalid_time") from None
    else:
        raise DatasetValidationError("invalid_time")
    _require(parsed.tzinfo is not None and parsed.utcoffset() is not None, "invalid_time")


def _canonical(value, ancestors=None):
    """Typed deterministic representation; rejects cycles/non-JSON extras."""
    ancestors = set() if ancestors is None else ancestors
    if value is None or type(value) in (str, bool, int):
        return [type(value).__name__, value]
    if type(value) is float:
        _require(math.isfinite(value), "invalid_type")
        return ["float", value]
    if type(value) is datetime:
        _time(value)
        return ["datetime", value.isoformat()]
    _require(type(value) in (dict, list, tuple, set) and id(value) not in ancestors, "invalid_type")
    ancestors.add(id(value))
    try:
        if type(value) is dict:
            _require(all(type(k) is str for k in value), "invalid_type")
            return ["dict", [[k, _canonical(value[k], ancestors)] for k in sorted(value)]]
        rows = [_canonical(item, ancestors) for item in value]
        if type(value) is set:
            rows.sort(key=lambda item: json.dumps(item, ensure_ascii=False, separators=(",", ":")))
        return [type(value).__name__, rows]
    finally:
        ancestors.remove(id(value))


def value_hash(value):
    """Hash decoded values without losing tuple/set/datetime distinctions."""
    payload = json.dumps(_canonical(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def empty_state():
    """No DemoStore/config import and therefore no demo seeding or DB access."""
    _require(set(STATE_FIELDS) == LIST_FIELDS | DICT_FIELDS | SET_FIELDS, "unclassified_state_field")
    _require(set(STATE_FIELDS) == CARRY_FIELDS | MANUAL_FIELDS | ARCHIVE_FIELDS, "unclassified_state_field")
    return {field: [] if field in LIST_FIELDS else {} if field in DICT_FIELDS else set() for field in STATE_FIELDS}


def _state_shape(state):
    template = empty_state()
    _keys(state, STATE_FIELDS, STATE_FIELDS)
    for field, empty in template.items():
        _require(type(state[field]) is type(empty), "invalid_collection_type")
    value_hash(state)  # Also rejects cycles, non-finite values and opaque objects.


def _identity(identity, *, target):
    fields = {"database_fingerprint", "auth_project", "storage_project", "mode", "platform_account", "ledger_id"}
    _keys(identity, fields, fields)
    _hash(identity["database_fingerprint"])
    for field in ("auth_project", "storage_project"):
        _require(type(identity[field]) is str and re.fullmatch(r"[a-z0-9_-]{1,64}", identity[field]) is not None, "invalid_identity")
    _require(identity["mode"] == ("live" if target else "test"), "mode_mismatch")
    _require(type(identity["platform_account"]) is str and re.fullmatch(r"acct_[A-Za-z0-9]+", identity["platform_account"]) is not None, "invalid_identity")
    if target or identity["ledger_id"] is not None:
        _uuid(identity["ledger_id"])


USER_TEXT = frozenset("id username email display_name avatar_url headline bio availability response_time pricing_note x_url website_url identity_status ban_reason".split())
USER_BOOL = frozenset("is_verified is_founding_member is_certified_creator is_banned email_verified phone_verified".split())
USER_TIMES = frozenset("created_at updated_at founding_member_since certified_creator_since banned_at".split())
USER_FIELDS = USER_TEXT | USER_BOOL | USER_TIMES | {"skills", "experience", "portfolio"}


def _strings(rows):
    _require(type(rows) is list and all(type(v) is str for v in rows), "invalid_type")


def _simple_rows(rows, fields):
    _require(type(rows) is list, "invalid_type")
    for row in rows:
        _keys(row, fields)
        for value in row.values():
            _text(value)


def _user(username, row, evidence, target):
    _keys(row, USER_FIELDS, ("id", "email", "display_name"))
    _uuid(row["id"])
    _require(type(username) is str and re.fullmatch(r"[a-z0-9_]{3,30}", username) is not None, "invalid_username")
    _require(row.get("username", username) == username, "owner_mismatch")
    for key, value in row.items():
        if key in USER_TEXT:
            if key == "avatar_url" and value is None:
                continue
            _text(value)
        elif key in USER_BOOL:
            _bool(value)
        elif key in USER_TIMES and value is not None:
            _time(value)
    if "skills" in row:
        _strings(row["skills"])
    if "experience" in row:
        _simple_rows(row["experience"], {"title", "period", "detail"})
    if "portfolio" in row:
        _simple_rows(row["portfolio"], {"title", "url", "summary"})
    _require(not row.get("phone_verified", False), "phone_provenance_required")
    fields = {"record_sha256", "auth_user_id", "identity_evidence", "badge_evidence"}
    _keys(evidence, fields, fields)
    _require(evidence["record_sha256"] == value_hash(row) and evidence["auth_user_id"] == row["id"], "provenance_mismatch")
    verified = row.get("is_verified", False)
    status = row.get("identity_status", "verified" if verified else "not_started")
    _require(status in {"verified", "not_started"} and verified == (status == "verified"), "identity_provenance_required")
    proof = evidence["identity_evidence"]
    if verified:
        _keys(proof, {"mode", "platform_account", "evidence_sha256"}, {"mode", "platform_account", "evidence_sha256"})
        _require(proof["mode"] == "live" and proof["platform_account"] == target["platform_account"], "identity_provenance_required")
        _hash(proof["evidence_sha256"])
    else:
        _require(proof is None, "identity_provenance_required")
    badges = evidence["badge_evidence"]
    active_badges = {key for key in ("is_founding_member", "is_certified_creator") if row.get(key, False)}
    _keys(badges, active_badges, active_badges)
    for digest in badges.values():
        _hash(digest)
    # Never silently reset legitimate identity/MFA/ban data to make a plan pass.
    return deepcopy(row)


TOOL_TEXT = frozenset("id slug name tagline description_md category price_type distribution author_name author_username author_id author_email accent status purchase_notes demo_url external_pay_url thumbnail_url fulfillment_type transfer_review_status tech_stack exclusive_summary".split())
TOOL_INTS = frozenset("price like_count view_count sales_count support_days estimated_delivery_days capacity subscription_price exclusive_price_min monthly_revenue monthly_profit monthly_cost handover_days".split())
TOOL_BOOLS = frozenset("is_published is_removed supports_subscription customization_available exclusive_available".split())
TOOL_FIELDS = TOOL_TEXT | TOOL_INTS | TOOL_BOOLS | frozenset("rating weekly_ops_hours created_at updated_at ai_used tags transfer_assets options passport versions faq safety_scan purchase_disclosures rights_confirmation instant_artifact".split())
DISCLOSURE_KEYS = frozenset("audience deliverables requirements additional_costs license data_handling support_scope commercial_use resale".split())
PASSPORT_KEYS = frozenset("data_handling execution license source_included commercial_use skill_level verified_on security quality_score update_policy requirements".split())


def _artifact(artifact):
    fields = {"version", "storage_scope", "file", "guide", "uploaded_at"}
    _keys(artifact, fields, fields)
    version = artifact["version"]
    _require(type(version) is str and re.fullmatch(r"[a-f0-9]{32}", version) is not None, "invalid_artifact")
    _require(artifact["storage_scope"] == "product-" + version, "invalid_artifact")
    _require(type(artifact["guide"]) is str and bool(artifact["guide"].strip()), "invalid_artifact")
    _time(artifact["uploaded_at"])
    file = artifact["file"]
    keys = {"id", "sha256", "name", "size", "scan_status", "scan_engine", "storage_backend", "bucket", "object_key"}
    _keys(file, keys, keys)
    _hash(file["sha256"])
    _require(file["id"] == file["sha256"][:24] and type(file["size"]) is int and file["size"] > 0, "invalid_artifact")
    _require(file["scan_status"] == "passed" and file["scan_engine"] == "clamav" and file["storage_backend"] == "supabase", "invalid_artifact")
    for key in ("name", "bucket", "object_key"):
        _text(file[key])
    _require(file["bucket"] == "toolbako-deliveries", "invalid_artifact")
    _require(re.fullmatch(r"deliveries/product-" + version + r"/[a-f0-9]{32}\.[a-z0-9]+", file["object_key"]) is not None, "invalid_artifact")


def _tool(row, evidence, users):
    _keys(row, TOOL_FIELDS, ("id", "slug", "name", "author_id", "author_username", "price", "price_type", "fulfillment_type", "safety_scan", "rights_confirmation"))
    _uuid(row["id"])
    for key, value in row.items():
        if key in TOOL_TEXT:
            _text(value)
        elif key in TOOL_INTS:
            _require(type(value) is int and value >= 0, "invalid_type")
        elif key in TOOL_BOOLS:
            _bool(value)
        elif key in {"created_at", "updated_at"}:
            _time(value)
    for key in ("rating", "weekly_ops_hours"):
        value = row.get(key)
        _require(value is None or (type(value) in (int, float) and math.isfinite(value) and value >= 0), "invalid_type")
    _require(type(row["slug"]) is str and re.fullmatch(r"[a-z0-9_-]+", row["slug"]) is not None, "invalid_slug")
    owner = users.get(row["author_username"])
    _require(owner is not None and owner["id"] == row["author_id"], "orphan_reference")
    _require(row.get("author_email", owner["email"]) == owner["email"], "owner_mismatch")
    _require(row["price_type"] in {"free", "paid", "consultation"} and row["fulfillment_type"] in {"instant", "custom"}, "invalid_type")
    _require(not row.get("external_pay_url"), "external_payment_review_required")
    _require(not row.get("supports_subscription", False) and not row.get("exclusive_available", False), "unsupported_sales_mode")
    for key in ("ai_used", "tags", "transfer_assets"):
        if key in row:
            _strings(row[key])
    for key, fields in (("faq", {"question", "answer"}), ("versions", {"version", "date", "title"})):
        if key in row:
            _simple_rows(row[key], fields)
    _require(type(row.get("options", [])) is list, "invalid_type")
    for option in row.get("options", []):
        _keys(option, {"id", "name", "price"}, {"id", "name", "price"})
        _text(option["id"]); _text(option["name"])
        _require(type(option["price"]) is int and option["price"] >= 0, "invalid_type")
    passport = row.get("passport", {})
    _keys(passport, PASSPORT_KEYS)
    for key, value in passport.items():
        if key in {"commercial_use", "source_included"}:
            _bool(value)
        elif key == "quality_score":
            _require(type(value) in (int, float) and 0 <= value <= 100, "invalid_type")
        else:
            _text(value)
    disclosures = row.get("purchase_disclosures", {})
    _keys(disclosures, DISCLOSURE_KEYS)
    for value in disclosures.values():
        _text(value)
    if "rights_confirmation" in row:
        rights = row["rights_confirmation"]
        _keys(rights, {"version", "confirmed_at", "user_id"}, {"version", "confirmed_at", "user_id"})
        _text(rights["version"]); _time(rights["confirmed_at"])
        _require(rights["user_id"] == row["author_id"], "owner_mismatch")
    fields = {"record_sha256", "artifact_sha256", "review_evidence_sha256"}
    _keys(evidence, fields, fields)
    _require(evidence["record_sha256"] == value_hash(row), "provenance_mismatch")
    _hash(evidence["review_evidence_sha256"])
    artifact = row.get("instant_artifact")
    if artifact is not None:
        _artifact(artifact)
        _require(evidence["artifact_sha256"] == artifact["file"]["sha256"], "artifact_proof_mismatch")
    else:
        _require(row["fulfillment_type"] != "instant" and evidence["artifact_sha256"] is None, "invalid_artifact")
    review = row["safety_scan"]
    keys = {"status", "label", "checked_at", "method", "reviewer_id", "reviewed_at", "note", "checks", "content_fingerprint"}
    _keys(review, keys, keys)
    _require(review["status"] == "passed" and review["method"] == "manual", "review_required")
    _require(type(review["checks"]) is list and all(type(item) is str for item in review["checks"]) and len(review["checks"]) == len(REVIEW_CHECKS) and set(review["checks"]) == set(REVIEW_CHECKS), "review_required")
    _uuid(review["reviewer_id"])
    _require(review["reviewer_id"] in {user["id"] for user in users.values()}, "orphan_reference")
    _time(review["checked_at"]); _time(review["reviewed_at"])
    _text(review["label"]); _text(review["note"])
    _require(review["content_fingerprint"] == content_fingerprint(row), "stale_review")
    result = deepcopy(row)
    result.update(sales_count=0, rating=None, like_count=0)
    return result


@dataclass(frozen=True)
class DryRunResult:
    """Candidate contains private data: never print it. report is count/hash only."""
    candidate: dict
    report: dict


def _prepare_candidate(state, manifest):
    """Validate a decoded source snapshot and explicit selection; do not import.

    Nonempty manual-review collections intentionally stop this first version.
    Archived collections must be hash-acknowledged even when empty. The caller
    must retain the immutable source/export; this function does not archive it.
    """
    _state_shape(state)
    value_hash(manifest)
    required = {"version", "manifest_id", "source", "target", "source_revision", "source_schema_version", "source_commit", "target_commit", "snapshot_sha256", "users", "tools", "archive_sha256"}
    _keys(manifest, required, required)
    _require(type(manifest["version"]) is int and manifest["version"] == 1, "manifest_version")
    _uuid(manifest["manifest_id"])
    _require(type(manifest["source_schema_version"]) is int and manifest["source_schema_version"] == SUPPORTED_RUNTIME_SCHEMA_VERSION, "schema_version_mismatch")
    for field in ("source_commit", "target_commit"):
        _require(type(manifest[field]) is str and re.fullmatch(r"[a-f0-9]{40}", manifest[field]) is not None, "invalid_commit")
    _require(type(manifest["source_revision"]) is int and manifest["source_revision"] >= 0, "invalid_revision")
    _identity(manifest["source"], target=False); _identity(manifest["target"], target=True)
    source, target = manifest["source"], manifest["target"]
    _require(source["database_fingerprint"] != target["database_fingerprint"], "same_database")
    _require(source["ledger_id"] != target["ledger_id"] and source["platform_account"] != target["platform_account"], "scope_collision")
    _require(source["auth_project"] == target["auth_project"] and source["storage_project"] == target["storage_project"], "auth_storage_migration_not_supported")
    _require(manifest["snapshot_sha256"] == value_hash(state), "snapshot_mismatch")
    _require(not any(state[field] for field in MANUAL_FIELDS), "manual_review_required")
    _keys(manifest["archive_sha256"], ARCHIVE_FIELDS, ARCHIVE_FIELDS)
    for field in ARCHIVE_FIELDS:
        _require(manifest["archive_sha256"][field] == value_hash(state[field]), "archive_acknowledgment_required")
    selected_users, selected_tools = manifest["users"], manifest["tools"]
    _require(type(selected_users) is dict and type(selected_tools) is dict, "invalid_selection")
    _require(set(selected_users) <= set(state["registered_users"]), "missing_selection")
    candidate = empty_state()
    all_users = state["registered_users"]
    _require(all(type(row) is dict and type(row.get("id")) is str for row in all_users.values()), "invalid_type")
    for username, row in all_users.items():
        # Legacy password fields may exist in an excluded source account only.
        _keys(row, USER_FIELDS | {"password_hash", "password_salt"})
        # Auth stays shared. A locally suspended account could log in again and
        # recreate its runtime profile without this ban if omitted here.
        _require(not row.get("is_banned") or username in selected_users,
                 "banned_user_requires_selection")
    all_user_ids = {row["id"] for row in all_users.values()}
    _require(len(all_user_ids) == len(all_users), "duplicate_user")
    for username, evidence in selected_users.items():
        candidate["registered_users"][username] = _user(username, all_users[username], evidence, target)
    users = candidate["registered_users"]
    user_ids = {row["id"] for row in users.values()}
    _require(len({row["email"].casefold() for row in users.values()}) == len(users), "duplicate_user")
    _require(all(type(row) is dict and type(row.get("slug")) is str for row in state["tools"]), "invalid_type")
    for row in state["tools"]:
        _keys(row, TOOL_FIELDS)
    by_slug = {row["slug"]: row for row in state["tools"]}
    _require(len(by_slug) == len(state["tools"]), "duplicate_tool")
    _require(set(selected_tools) <= set(by_slug), "missing_selection")
    for row in state["tools"]:
        if row["slug"] in selected_tools:
            candidate["tools"].append(_tool(row, selected_tools[row["slug"]], users))
    _require(len({row["id"] for row in candidate["tools"]}) == len(candidate["tools"]), "duplicate_tool")
    slugs = set(selected_tools)
    for field in ("likes", "update_followers", "reopen_waiters", "blocks", "creator_follows"):
        targets = set(all_users) if field in {"blocks", "creator_follows"} else set(by_slug)
        selected_targets = set(users) if field in {"blocks", "creator_follows"} else slugs
        for pair in state[field]:
            _require(type(pair) is tuple and len(pair) == 2 and all(type(item) is str for item in pair), "invalid_reference")
            uid, reference = pair
            _require(uid in all_user_ids and reference in targets, "orphan_reference")
            if field == "blocks":
                # Auth stays shared: an omitted account can log in later. Do
                # not silently remove a block involving a retained account.
                _require((uid in user_ids) == (reference in selected_targets), "security_reference_requires_selection")
            if uid in user_ids and reference in selected_targets:
                candidate[field].add(pair)
    folder_ids = {}
    for uid, folders in state["favorite_folders"].items():
        _require(uid in all_user_ids and type(folders) is list, "orphan_reference")
        ids = set()
        for folder in folders:
            _keys(folder, {"id", "name", "created_at"}, {"id", "name", "created_at"})
            _uuid(folder["id"]); _text(folder["name"]); _time(folder["created_at"])
            _require(folder["id"] not in ids, "duplicate_folder")
            ids.add(folder["id"])
        folder_ids[uid] = ids
        if uid in user_ids:
            candidate["favorite_folders"][uid] = deepcopy(folders)
    for key, folder in state["favorite_assignments"].items():
        _require(type(key) is str and key.count("\u241f") == 1, "invalid_reference")
        uid, slug = key.split("\u241f")
        _require(type(folder) is str and folder in folder_ids.get(uid, set()) and (uid, slug) in state["likes"], "orphan_reference")
        if uid in user_ids and slug in slugs:
            candidate["favorite_assignments"][key] = folder
    for field in ("notification_preferences", "security_settings"):
        allowed = {"messages", "transactions", "requests", "updates"} if field == "notification_preferences" else {"two_factor", "login_alerts", "sessions"}
        for uid, settings in state[field].items():
            _require(uid in all_user_ids, "orphan_reference")
            _keys(settings, allowed)
            for key, value in settings.items():
                if key == "sessions":
                    _require(type(value) is int and value >= 0, "invalid_type")
                else:
                    _bool(value)
            if uid in user_ids:
                candidate[field][uid] = deepcopy(settings)
                if field == "security_settings" and "sessions" in settings:
                    candidate[field][uid]["sessions"] = 0
    for row in state["legal_consents"]:
        _keys(row, {"user_id", "document", "document_type", "version", "accepted_at", "created_at", "statement", "tool_id", "tool_slug"}, {"user_id", "version"})
        _require(row["user_id"] in all_user_ids, "orphan_reference")
        _require(("document" in row) != ("document_type" in row) and ("created_at" in row) != ("accepted_at" in row), "invalid_consent")
        _text(row["version"]); _time(row.get("created_at", row.get("accepted_at")))
        _require(row.get("document", row.get("document_type")) in {"terms", "privacy", "nda", "listing_rights"}, "invalid_consent")
        for key in ("statement", "document", "document_type"):
            if key in row:
                _text(row[key])
        slug = row.get("tool_slug")
        if "tool_id" in row or slug is not None:
            _require(type(slug) is str and slug in by_slug and row.get("tool_id") == by_slug[slug].get("id") and row["user_id"] == by_slug[slug].get("author_id"), "orphan_reference")
        if row["user_id"] in user_ids and (slug is None or slug in slugs):
            candidate["legal_consents"].append(deepcopy(row))
    for tool in candidate["tools"]:
        rights = tool["rights_confirmation"]
        _require(any(row.get("document_type") == "listing_rights" and row.get("tool_slug") == tool["slug"]
                     and row.get("tool_id") == tool["id"] and row["user_id"] == tool["author_id"]
                     and row["version"] == rights["version"] and row.get("created_at") == rights["confirmed_at"]
                     for row in candidate["legal_consents"]), "rights_consent_required")
        tool["like_count"] = sum(slug == tool["slug"] for _, slug in candidate["likes"])
    _require(not any(candidate[field] for field in ARCHIVE_FIELDS | MANUAL_FIELDS), "prohibited_target_data")
    _state_shape(candidate)
    return DryRunResult(candidate, {
        "status": "candidate_only", "manifest_sha256": value_hash(manifest),
        "source_sha256": value_hash(state), "candidate_sha256": value_hash(candidate),
        "counts": {field: {"source": len(state[field]), "candidate": len(candidate[field]), "excluded": len(state[field]) - len(candidate[field])} for field in STATE_FIELDS},
        "normalized_tables_must_be_empty": list(NORMALIZED_EMPTY_TABLES),
        "source_target_identity_independently_verified": False,
        "auth_storage_provenance_independently_verified": False,
        "database_read": False, "database_written": False, "binding_created": False,
        "live_activated": False, "migration_complete": False,
    })


def prepare_candidate(state, manifest):
    """Public pure API. Malformed structures produce only fixed error codes."""
    try:
        return _prepare_candidate(state, manifest)
    except DatasetValidationError:
        raise
    except (TypeError, KeyError, ValueError, RecursionError, OverflowError):
        raise DatasetValidationError("invalid_structure") from None
