"""Run the artificial-only migration rehearsal. Never reads real datasets.

Usage: python tools/live_dataset_dry_run.py --artificial-fixture
The pure app.live_dataset API accepts decoded snapshots for future adapters;
this CLI deliberately has no DB, credential, input-file or import options.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.listing_review import REVIEW_CHECKS, content_fingerprint
from app.live_dataset import ARCHIVE_FIELDS, DatasetValidationError, empty_state, prepare_candidate, value_hash


def artificial_fixture():
    """Deterministic fictional records; not a production seeding function."""
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    uid = "11111111-1111-4111-8111-111111111111"
    buyer = "22222222-2222-4222-8222-222222222222"
    tid = "33333333-3333-4333-8333-333333333333"
    state = empty_state()
    state["registered_users"] = {
        "sample_seller": {"id": uid, "email": "seller@example.invalid", "display_name": "人工販売者", "is_verified": False, "identity_status": "not_started", "is_banned": False},
        "sample_buyer": {"id": buyer, "email": "buyer@example.invalid", "display_name": "人工購入者", "is_verified": False, "identity_status": "not_started", "is_banned": False},
    }
    digest = "a" * 64
    version = "b" * 32
    tool = {"id": tid, "slug": "artificial-tool", "name": "人工商品", "tagline": "検証専用", "description_md": "実際の商品ではありません。",
            "author_id": uid, "author_username": "sample_seller", "author_name": "人工販売者",
            "price": 1000, "price_type": "paid", "distribution": "download", "fulfillment_type": "instant",
            "is_published": True, "status": "published", "created_at": at, "sales_count": 9, "rating": 5.0, "like_count": 10,
            "rights_confirmation": {"version": "artificial-v1", "confirmed_at": at, "user_id": uid},
            "instant_artifact": {"version": version, "storage_scope": "product-" + version, "guide": "人工の導入手順です。", "uploaded_at": at,
                "file": {"id": digest[:24], "sha256": digest, "name": "artificial.zip", "size": 112,
                         "scan_status": "passed", "scan_engine": "clamav", "storage_backend": "supabase", "bucket": "toolbako-deliveries",
                         "object_key": "deliveries/product-" + version + "/" + "c" * 32 + ".zip"}}}
    tool["safety_scan"] = {"status": "passed", "label": "掲載審査承認済み", "checked_at": at.isoformat(), "method": "manual", "reviewer_id": uid,
                           "reviewed_at": at.isoformat(), "note": "人工の審査記録です。", "checks": list(REVIEW_CHECKS), "content_fingerprint": content_fingerprint(tool)}
    state["tools"] = [tool]
    state["likes"] = {(buyer, tool["slug"])}
    state["blocks"] = {(uid, "sample_buyer")}
    folder = "44444444-4444-4444-8444-444444444444"
    state["favorite_folders"] = {buyer: [{"id": folder, "name": "人工お気に入り", "created_at": at}]}
    state["favorite_assignments"] = {buyer + "\u241f" + tool["slug"]: folder}
    state["notification_preferences"] = {buyer: {"transactions": False}}
    state["security_settings"] = {uid: {"two_factor": True, "login_alerts": True, "sessions": 2}}
    state["legal_consents"] = [{"user_id": uid, "document_type": "listing_rights", "version": "artificial-v1", "tool_id": tid, "tool_slug": tool["slug"], "created_at": at, "statement": "人工の同意記録です。"}]
    # These exist in the immutable source but must never enter the candidate.
    state["orders"] = [{"id": "artificial-order", "payment_intent": "pi_artificial", "amount": 1000}]
    state["account_sessions"] = {"artificial-session": {"access_token": "artificial-not-a-real-token"}}
    state["identity_applications"] = {uid: {"status": "verified", "mode": "test"}}
    state["connected_accounts"] = {uid: {"id": "acct_artificialSeller", "livemode": False}}
    identity = {"auth_project": "artificial-auth", "storage_project": "artificial-storage"}
    manifest = {"version": 1, "manifest_id": "66666666-6666-4666-8666-666666666666", "source_revision": 42, "source_schema_version": 2, "source_commit": "a" * 40, "target_commit": "b" * 40,
                "source": {**identity, "database_fingerprint": "1" * 64, "mode": "test", "platform_account": "acct_artificialTest", "ledger_id": None},
                "target": {**identity, "database_fingerprint": "2" * 64, "mode": "live", "platform_account": "acct_artificialLive", "ledger_id": "55555555-5555-4555-8555-555555555555"},
                "snapshot_sha256": value_hash(state),
                "users": {name: {"record_sha256": value_hash(row), "auth_user_id": row["id"], "identity_evidence": None, "badge_evidence": {}} for name, row in state["registered_users"].items()},
                "tools": {tool["slug"]: {"record_sha256": value_hash(tool), "artifact_sha256": digest, "review_evidence_sha256": "3" * 64}},
                "archive_sha256": {field: value_hash(state[field]) for field in ARCHIVE_FIELDS}}
    return state, manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artificial-fixture", action="store_true", required=True)
    parser.parse_args(argv)
    try:
        state, manifest = artificial_fixture()
        result = prepare_candidate(state, manifest)
    except DatasetValidationError as exc:
        print(json.dumps({"status": "blocked", "reason": exc.code, "migration_complete": False}))
        return 1
    print(json.dumps({"artificial_fixture": True, **result.report}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
