"""Explicit, transaction-only staging adapter for the designated live DB.

No environment parsing, grants, password generation, source deletion, scope
binding, site cutover, Stripe calls, or email. The source snapshot and manifest
must already have been reviewed. Never print their contents or connection data.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from uuid import UUID, uuid5

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from .live_dataset import (
    ARCHIVE_FIELDS, NORMALIZED_EMPTY_TABLES, SUPPORTED_RUNTIME_SCHEMA_VERSION,
    DatasetValidationError, DryRunResult, empty_state, value_hash,
)
from .live_dataset_import import TABLES, _schema_signature
from .live_dataset_review import prepare_reviewed_candidate
from .persistence import decode_value, encode_value


TARGET_PROJECT = "xijdnqnsuhuciqyygnkg"
TARGET_HOST = "db." + TARGET_PROJECT + ".supabase.co"
TARGET_SESSION_HOST = "aws-0-ap-northeast-1.pooler.supabase.com"
TARGET_SESSION_USER = "postgres." + TARGET_PROJECT
_TARGET_ENDPOINTS = frozenset({
    (TARGET_HOST, "postgres"),
    (TARGET_HOST, "toolbako_migrator"),
    (TARGET_SESSION_HOST, TARGET_SESSION_USER),
})
ACTION = "live.dataset.staged"
_HASH = re.compile(r"[a-f0-9]{64}")


class TransferError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__("live_dataset_transfer:" + code)


def _require(ok, code):
    if not ok:
        raise TransferError(code)


def target_config(dsn, password):
    """Only the named direct or session-pooler TLS endpoint; no fallbacks.

    Password is supplied by the approved caller, never read from browser logs.
    No implicit PGPASSWORD, service/passfile, hostaddr or startup options.
    """
    try:
        parsed = conninfo_to_dict(dsn)
    except (psycopg.Error, TypeError, ValueError):
        raise TransferError("invalid_target_config") from None
    _require(not set(parsed) - {"host", "port", "dbname", "user", "sslmode", "sslrootcert"},
             "invalid_target_config")
    _require(parsed.get("host") in {TARGET_HOST, TARGET_SESSION_HOST}
             and parsed.get("port", "5432") == "5432"
             and parsed.get("dbname") == "postgres" and parsed.get("sslmode") == "verify-full",
             "invalid_target_config")
    _require((parsed.get("host"), parsed.get("user")) in _TARGET_ENDPOINTS,
             "invalid_target_role")
    _require(type(password) is str and bool(password) and "\x00" not in password, "missing_password")
    # Explicit settings override environmental routing/passwords. Reject any
    # remaining inherited libpq settings instead of silently accepting them.
    _require(not any(key.startswith("PG") for key in os.environ), "inherited_libpq_options")
    return parsed | {"password": password, "port": "5432", "passfile": os.devnull,
                     "options": "", "connect_timeout": "10", "gssencmode": "disable",
                     "application_name": "toolbako-live-dataset-stage"}


def _connection_identity(conn):
    _require((conn.info.host, conn.info.user) in _TARGET_ENDPOINTS and conn.info.port == 5432
             and conn.info.dbname == "postgres" and conn.info.ssl_in_use,
             "target_connection_mismatch")
    row = conn.execute("select current_database(),(select oid::bigint from pg_database where datname=current_database())").fetchone()
    _require(row and row[0] == "postgres" and type(row[1]) is int, "target_database_mismatch")
    # A shared pooler host does not identify a project: its exact project user
    # is part of both the allowlist and the reviewed fingerprint. Switching
    # endpoint or role requires a fresh inspection and manifest review.
    return value_hash((TARGET_PROJECT, conn.info.host, conn.info.user, 5432, row[0], row[1]))


def inspect_target(dsn, password):
    """Bounded read-only inventory. No automatic schema initialization."""
    config = target_config(dsn, password)
    try:
        with psycopg.connect(make_conninfo(**config)) as conn:
            conn.execute("set transaction read only")
            # This does not disable RLS. PostgreSQL rejects queries whose rows
            # would be filtered instead of allowing a false empty inventory.
            conn.execute("set local row_security=off")
            conn.execute("set local statement_timeout='10s'")
            fingerprint = _connection_identity(conn)
            exists = conn.execute("select exists(select 1 from pg_namespace where nspname='toolbako_runtime')").fetchone()[0]
            report = {"database_fingerprint": fingerprint, "project": TARGET_PROJECT,
                      "runtime_schema_exists": exists, "written": False}
            if exists:
                report["schema_sha256"] = _schema_signature(conn)
                report["table_counts"] = {name: conn.execute(sql.SQL("select count(*) from {}").format(
                    sql.Identifier("toolbako_runtime", name))).fetchone()[0] for name in TABLES}
            conn.rollback()
            return report
    except TransferError:
        raise
    except Exception:
        raise TransferError("target_inspection_failed") from None


def _audit_digest(event):
    return hashlib.sha256(json.dumps(encode_value(event), ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _event(manifest, proof, at):
    return {"id": str(uuid5(UUID(manifest["manifest_id"]), ACTION)), "user_id": None,
            "action": ACTION, "target": json.dumps(proof, sort_keys=True, separators=(",", ":")),
            "request_id": manifest["manifest_id"], "created_at": at.astimezone(timezone.utc)}


def _snapshot(conn):
    rows = conn.execute("select revision,payload from toolbako_runtime.marketplace_state").fetchall()
    _require(len(rows) <= 1, "target_nonempty")
    return (rows[0][0], decode_value(rows[0][1])) if rows else None


def _audit(conn):
    return conn.execute("select id::text,actor_id,action,target,request_id,occurred_at,content_hash from toolbako_runtime.audit_events order by id").fetchall()


def _verify(conn, payload, event):
    actual = _snapshot(conn)
    _require(actual is not None and actual[0] == 1 and value_hash(actual[1]) == value_hash(payload),
             "stored_snapshot_mismatch")
    expected = (event["id"], None, ACTION, event["target"], event["request_id"], event["created_at"], _audit_digest(event))
    _require(_audit(conn) == [expected], "stored_audit_mismatch")


def _stage_transaction(conn, manifest, prepared, expected_schema_sha256, *, verify_external_identity=False):
    """Internal transaction body, also exercised against synthetic local PG."""
    conn.execute("set transaction isolation level serializable")
    conn.execute("set local lock_timeout='5s'")
    conn.execute("set local statement_timeout='15s'")
    conn.execute("set local row_security=off")
    if verify_external_identity:
        _require(_connection_identity(conn) == manifest["target"]["database_fingerprint"], "target_fingerprint_mismatch")
    conn.execute(sql.SQL("lock table {} in access exclusive mode").format(sql.SQL(",").join(
        sql.Identifier("toolbako_runtime", name) for name in TABLES)))
    _require(_schema_signature(conn) == expected_schema_sha256, "schema_changed")
    _require(conn.execute("select singleton,version from toolbako_runtime.schema_version").fetchall()
             == [(True, SUPPORTED_RUNTIME_SCHEMA_VERSION)], "schema_version_mismatch")
    for table in NORMALIZED_EMPTY_TABLES:
        count = conn.execute(sql.SQL("select count(*) from {}").format(sql.Identifier("toolbako_runtime", table))).fetchone()[0]
        _require(count == 0, "financial_or_outbox_nonempty")
    payload = deepcopy(prepared.candidate)
    proof = {key: prepared.report[key] for key in ("manifest_sha256", "source_sha256", "candidate_sha256")}
    proof["target_database_fingerprint"] = manifest["target"]["database_fingerprint"]
    proof["source_revision"] = manifest["source_revision"]
    current, audit = _snapshot(conn), _audit(conn)
    replay = False
    if audit:
        _require(len(audit) == 1 and current is not None, "target_nonempty")
        event = _event(manifest, proof, audit[0][5])
        payload["audit_logs"] = [event]
        _verify(conn, payload, event)
        replay = True
    else:
        _require(current is None or (current[0] == 0 and value_hash(current[1]) == value_hash(empty_state())), "target_nonempty")
        event = _event(manifest, proof, datetime.now(timezone.utc))
        payload["audit_logs"] = [event]
        conn.execute("""insert into toolbako_runtime.marketplace_state(singleton,revision,payload)
            values(true,1,%s::jsonb) on conflict(singleton) do update set revision=1,payload=excluded.payload,updated_at=now()
            where toolbako_runtime.marketplace_state.revision=0""", (json.dumps(encode_value(payload)),))
        conn.execute("""insert into toolbako_runtime.audit_events(id,actor_id,action,target,request_id,occurred_at,content_hash)
            values(%s,null,%s,%s,%s,%s,%s)""", (event["id"], ACTION, event["target"], event["request_id"], event["created_at"], _audit_digest(event)))
        _verify(conn, payload, event)
    return {"status": "already_staged" if replay else "staged", "stored_payload_sha256": value_hash(payload),
            "manifest_sha256": prepared.report["manifest_sha256"], "candidate_sha256": prepared.report["candidate_sha256"],
            "source_revision": manifest["source_revision"], "snapshot_revision": 1, "source_written": False,
            "binding_created": False, "site_switched": False, "live_activated": False, "migration_complete": False}


def stage_reviewed_snapshot(dsn, password, source_state, manifest, prepared, *,
                            expected_schema_sha256, approved_manifest_sha256):
    """Atomically stage the approved snapshot; never switch the running site.

    Requires the exact reviewed manifest hash. This is technical matching,
    not a substitute for operator/user authorization or final write freeze.
    Existing target data is immutable to this adapter; there is no overwrite,
    truncate, cleanup, delete, upsert-later-revision or automatic re-import.
    """
    config = target_config(dsn, password)
    try:
        manifest = deepcopy(manifest)
        verified = prepare_reviewed_candidate(deepcopy(source_state), manifest)
        _require(type(prepared) is DryRunResult and prepared.report == verified.report
                 and value_hash(prepared.candidate) == value_hash(verified.candidate), "preparation_mismatch")
        _require(type(approved_manifest_sha256) is str and _HASH.fullmatch(approved_manifest_sha256)
                 and approved_manifest_sha256 == verified.report["manifest_sha256"], "manifest_not_approved")
        _require(type(expected_schema_sha256) is str and _HASH.fullmatch(expected_schema_sha256), "schema_proof_required")
        _require(not any(verified.candidate[name] for name in ARCHIVE_FIELDS), "financial_candidate_not_empty")
    except TransferError:
        raise
    except (DatasetValidationError, TypeError, ValueError, KeyError):
        raise TransferError("candidate_invalid") from None
    try:
        with psycopg.connect(make_conninfo(**config)) as conn:
            result = _stage_transaction(conn, manifest, verified, expected_schema_sha256,
                                        verify_external_identity=True)
        return result
    except TransferError:
        raise
    except Exception:
        # Even commit/connection errors must not disclose DSNs or payloads.
        # An ambiguous commit is resolved only by the identical replay check.
        raise TransferError("staging_outcome_unconfirmed") from None
