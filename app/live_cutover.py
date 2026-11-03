"""Explicit reviewed copy + empty-ledger binding; never activate the site.

Operator-only API, not an HTTP handler or authorization mechanism. Independently
approved maintenance, old-process retirement, Auth/Storage provenance and retained
mail disposition are prerequisites. Review a preview OUTSIDE the source freeze;
apply reacquires it and requires all nine committed source-table hashes unchanged.
Do not wait for people, deployments or provider I/O inside the short freeze.

Only the designated new target can be written. Stage and bind are separately
committed: interruption is recovered with the identical manifest, never overwrite.
Ambiguous outcomes require readback; no success means no permission to switch.
No source writes, credentials in outputs, file exports, email or provider calls.
"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import json
import re
from uuid import UUID, uuid5

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from . import live_dataset_transfer as transfer
from .database import LOCK_ID, content_hash
from .financial_scope import FinancialScope, SCOPE_OPERATION_ID, SCOPE_ENDPOINT, validate_binding
from .live_dataset import NORMALIZED_EMPTY_TABLES, SUPPORTED_RUNTIME_SCHEMA_VERSION, value_hash
from .live_dataset_import import TABLES, _schema_signature
from .live_dataset_review import prepare_reviewed_candidate
from .live_source_freeze import held_source_freeze
from .persistence import encode_value


_TOKEN = object()
_HASH = re.compile(r"[a-f0-9]{64}")
_BIND_ACTION = "finance.scope_bound"


class CutoverError(RuntimeError):
    def __init__(self, code):
        allowed = {"review_invalid", "preview_required", "approval_mismatch", "source_changed",
                   "target_changed", "target_busy", "target_nonempty", "schema_changed",
                   "candidate_changed", "binding_mismatch", "preview_failed", "apply_unconfirmed"}
        self.code = code if code in allowed else "apply_unconfirmed"
        super().__init__("live_cutover:" + self.code)


def _require(ok, code):
    if not ok:
        raise CutoverError(code)


def _hash(value):
    return type(value) is str and _HASH.fullmatch(value) is not None


class CutoverPreview:
    """Private in-memory review; safe report contains counts/hashes only.

    Deliberately not serializable. The separately reviewed manifest may be rebuilt
    by trusted orchestration, but arbitrary persisted approval dictionaries are
    not a replacement for this live preview. Never log its private attributes.
    """
    __slots__ = ("_manifest", "_report", "_source", "_source_schema", "_target_schema",
                 "_mail_review", "_writer_evidence", "_digest")

    def __init__(self, manifest, report, source, source_schema, target_schema,
                 mail_review, writer_evidence, *, _token=None):
        _require(_token is _TOKEN, "preview_required")
        self._manifest, self._report = deepcopy(manifest), deepcopy(report)
        self._source = source
        self._source_schema, self._target_schema = source_schema, target_schema
        self._mail_review, self._writer_evidence = mail_review, writer_evidence
        self._digest = value_hash(self._proof())

    def _proof(self):
        return {"manifest_sha256": value_hash(self._manifest), "candidate": self._report,
                "source": asdict(self._source), "source_schema": self._source_schema,
                "target_schema": self._target_schema, "mail_review": asdict(self._mail_review),
                "writer_evidence": self._writer_evidence}

    def report(self):
        # Do not return the manifest, source payload, account IDs or filenames.
        return {"preview_sha256": self._digest, "manifest_sha256": self._report["manifest_sha256"],
                "source_sha256": self._report["source_sha256"],
                "candidate_sha256": self._report["candidate_sha256"],
                "source_revision": self._source.revision,
                "source_table_counts": dict(self._source.table_counts),
                "source_table_sha256": dict(self._source.table_sha256),
                "counts": deepcopy(self._report["counts"]), "written": False}

    def __repr__(self):
        return "CutoverPreview(<redacted>)"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("CutoverPreview cannot be serialized")


@contextmanager
def _target_connection(dsn, password):
    config = transfer.target_config(dsn, password)
    with psycopg.connect(make_conninfo(**config)) as conn:
        yield conn


def _target_identity(conn):
    return transfer._connection_identity(conn)


def _target_guard(conn, manifest, schema, *, readonly):
    conn.execute("set transaction isolation level serializable")
    if readonly:
        conn.execute("set transaction read only")
    conn.execute("set local statement_timeout='5s'")
    conn.execute("set local lock_timeout='1s'")
    conn.execute("set local row_security=off")
    conn.execute("set local timezone='UTC'")
    _require(_target_identity(conn) == manifest["target"]["database_fingerprint"], "target_changed")
    _require(manifest["source"]["database_fingerprint"] != manifest["target"]["database_fingerprint"],
             "target_changed")
    if not readonly:
        # No backend.request(): absent state must never be initialized here.
        _require(conn.execute("select pg_try_advisory_xact_lock(%s)", (LOCK_ID,)).fetchone() == (True,),
                 "target_busy")
        conn.execute(sql.SQL("lock table {} in access exclusive mode").format(sql.SQL(",").join(
            sql.Identifier("toolbako_runtime", name) for name in TABLES)))
    _require(_schema_signature(conn) == schema, "schema_changed")
    _require(conn.execute("select singleton,version from toolbako_runtime.schema_version").fetchall()
             == [(True, SUPPORTED_RUNTIME_SCHEMA_VERSION)], "schema_changed")


def _scope(manifest):
    target = manifest["target"]
    _require(target["mode"] == "live", "review_invalid")
    return FinancialScope("live", target["platform_account"], target["ledger_id"])


def _proof(manifest, prepared):
    return {key: prepared.report[key] for key in ("manifest_sha256", "source_sha256", "candidate_sha256")} | {
        "target_database_fingerprint": manifest["target"]["database_fingerprint"],
        "source_revision": manifest["source_revision"]}


def _audit_tuple(event):
    return (event["id"], event.get("user_id"), event["action"], event["target"],
            event.get("request_id", ""), event["created_at"], content_hash(event))


def _kind(conn, manifest, prepared):
    """Read-only classification of exact empty, same stage or same bound state."""
    for name in NORMALIZED_EMPTY_TABLES:
        if name != "stripe_operations":
            count = conn.execute(sql.SQL("select count(*) from {}").format(
                sql.Identifier("toolbako_runtime", name))).fetchone()[0]
            _require(count == 0, "target_nonempty")
    journal = conn.execute("""select operation_id,endpoint,request_hash,status,response
        from toolbako_runtime.stripe_operations order by operation_id""").fetchall()
    snapshot, audit = transfer._snapshot(conn), transfer._audit(conn)
    if snapshot is None and not audit and not journal:
        return "empty"
    _require(snapshot is not None and len(audit) in (1, 2), "target_nonempty")
    import_rows = [row for row in audit if row[2] == transfer.ACTION]
    _require(len(import_rows) == 1, "target_nonempty")
    imported = transfer._event(manifest, _proof(manifest, prepared), import_rows[0][5])
    _require(import_rows[0] == _audit_tuple(imported), "candidate_changed")
    payload = deepcopy(prepared.candidate)
    if not journal:
        payload["audit_logs"] = [imported]
        _require(snapshot[0] == 1 and len(audit) == 1
                 and value_hash(snapshot[1]) == value_hash(payload), "candidate_changed")
        return "staged"
    scope = _scope(manifest)
    _require(len(journal) == 1 and journal[0][0] == SCOPE_OPERATION_ID, "binding_mismatch")
    validate_binding(journal[0][1:], scope)
    binding_rows = [row for row in audit if row[2] == _BIND_ACTION]
    _require(len(binding_rows) == 1 and len(audit) == 2, "binding_mismatch")
    bound = _binding_event(manifest, binding_rows[0][5])
    _require(binding_rows[0] == _audit_tuple(bound), "binding_mismatch")
    payload["audit_logs"] = [bound, imported]
    _require(snapshot[0] == 2 and value_hash(snapshot[1]) == value_hash(payload), "candidate_changed")
    return "bound"


def _binding_event(manifest, at):
    # Deterministic event ID makes only the same reviewed transition replayable.
    # All other fields use the existing DemoStore.audit/backend binding format.
    return {"id": str(uuid5(UUID(manifest["manifest_id"]), _BIND_ACTION)), "user_id": None,
            "action": _BIND_ACTION, "target": "live", "request_id": "",
            "created_at": at.astimezone(timezone.utc)}


def _inspect(dsn, password, manifest, prepared, schema):
    with _target_connection(dsn, password) as conn:
        _target_guard(conn, manifest, schema, readonly=True)
        kind = _kind(conn, manifest, prepared)
        conn.rollback()
        return kind


def _bind(dsn, password, manifest, prepared, schema, source):
    with _target_connection(dsn, password) as conn:
        _target_guard(conn, manifest, schema, readonly=False)
        kind = _kind(conn, manifest, prepared)
        _require(kind in {"staged", "bound"}, "target_nonempty")
        if kind == "staged":
            scope = _scope(manifest)
            conn.execute("""insert into toolbako_runtime.stripe_operations
                (operation_id,endpoint,request_hash,status,response) values(%s,%s,%s,'succeeded',%s::jsonb)""",
                (SCOPE_OPERATION_ID, SCOPE_ENDPOINT, content_hash(scope.payload()), json.dumps(scope.payload())))
            payload = transfer._snapshot(conn)[1]
            event = _binding_event(manifest, datetime.now(timezone.utc))
            payload["audit_logs"].insert(0, event)
            conn.execute("""insert into toolbako_runtime.audit_events
                (id,actor_id,action,target,request_id,occurred_at,content_hash) values(%s,%s,%s,%s,%s,%s,%s)""",
                _audit_tuple(event))
            updated = conn.execute("""update toolbako_runtime.marketplace_state
                set revision=2,payload=%s::jsonb,updated_at=now() where singleton and revision=1 returning revision""",
                (json.dumps(encode_value(payload)),)).fetchall()
            _require(updated == [(2,)], "candidate_changed")
        _require(_kind(conn, manifest, prepared) == "bound", "binding_mismatch")
        # A lost source freeze rolls back this target transaction. If commit or
        # subsequent source verification is uncertain, only exact readback helps.
        source.verify()


def _stage_reviewed(*args, **kwargs):
    return transfer.stage_reviewed_snapshot(*args, **kwargs)


def _freeze(dsn, password, manifest, source_schema, mail_review, writer_evidence):
    return held_source_freeze(dsn, password,
        expected_fingerprint=manifest["source"]["database_fingerprint"],
        expected_schema_sha256=source_schema, mail_review=mail_review,
        external_writer_evidence_sha256=writer_evidence)


def _prepare(source, reviewed_manifest):
    proof = source.verify()
    manifest = deepcopy(reviewed_manifest)
    _require(type(manifest) is dict and type(manifest.get("version")) is int
             and manifest["version"] == 2, "review_invalid")
    # All selections, provenance, manual dispositions and archive hashes remain
    # exactly reviewed. Never auto-select new users/tools or silently rebase.
    _require(manifest.get("source_revision") == proof.revision
             and manifest.get("snapshot_sha256") == proof.snapshot_sha256
             and manifest.get("source_schema_version") == SUPPORTED_RUNTIME_SCHEMA_VERSION, "source_changed")
    manifest.update(source_revision=proof.revision, snapshot_sha256=proof.snapshot_sha256,
                    source_schema_version=SUPPORTED_RUNTIME_SCHEMA_VERSION)
    def prepare(state):
        return prepare_reviewed_candidate(state, manifest)
    prepared = source.export(prepare, manifest=manifest)
    _scope(manifest)
    return proof, manifest, prepared


def preview_cutover(source_dsn, source_password, target_dsn, target_password, *, reviewed_manifest,
                    source_schema_sha256, target_schema_sha256, mail_review, external_writer_evidence_sha256,
                    resume_approved_preview_sha256=None):
    """Read current committed source and EMPTY target; write nothing.

    reviewed_manifest must be a full v2 review, with actual endpoint fingerprints,
    fixed manifest/ledger IDs and exact selection hashes. No placeholder target
    or auto-generated legal/provenance decisions are accepted by preparation.
    """
    try:
        _require(_hash(source_schema_sha256) and _hash(target_schema_sha256), "review_invalid")
        with _freeze(source_dsn, source_password, reviewed_manifest, source_schema_sha256,
                     mail_review, external_writer_evidence_sha256) as source:
            proof, manifest, prepared = _prepare(source, reviewed_manifest)
            kind = _inspect(target_dsn, target_password, manifest, prepared, target_schema_sha256)
            source.verify()
            preview = CutoverPreview(manifest, prepared.report, proof, source_schema_sha256,
                                     target_schema_sha256, mail_review, external_writer_evidence_sha256, _token=_TOKEN)
            # Process-loss recovery rebuilds the proof only from current reads
            # and the previously approved digest. It cannot approve a new stage.
            if resume_approved_preview_sha256 is not None:
                _require(_hash(resume_approved_preview_sha256)
                         and resume_approved_preview_sha256 == preview._digest, "approval_mismatch")
            else:
                _require(kind == "empty", "target_nonempty")
            return preview
    except CutoverError:
        raise
    except Exception:
        raise CutoverError("preview_failed") from None


def apply_cutover(source_dsn, source_password, target_dsn, target_password, *, preview,
                  approved_preview_sha256, approved_manifest_sha256):
    """Explicit copy/bind only, using the exact independently approved preview.

    Scope binding is not live-payment/site activation. Keep maintenance and old
    workers stopped independently after this API returns. Same object/manifest
    retry only; source drift or post-import activity prevents automatic replay.
    """
    try:
        _require(type(preview) is CutoverPreview, "preview_required")
        _require(_hash(approved_preview_sha256) and _hash(approved_manifest_sha256)
                 and approved_preview_sha256 == preview._digest == value_hash(preview._proof())
                 and approved_manifest_sha256 == value_hash(preview._manifest), "approval_mismatch")
        with _freeze(source_dsn, source_password, preview._manifest, preview._source_schema,
                     preview._mail_review, preview._writer_evidence) as source:
            proof, manifest, prepared = _prepare(source, preview._manifest)
            _require(proof == preview._source, "source_changed")
            _require(prepared.report == preview._report, "candidate_changed")
            kind = _inspect(target_dsn, target_password, manifest, prepared, preview._target_schema)
            source.verify()
            if kind != "bound":
                def stage(state):
                    return _stage_reviewed(target_dsn, target_password, state, manifest, prepared,
                        expected_schema_sha256=preview._target_schema,
                        approved_manifest_sha256=approved_manifest_sha256)
                source.export(stage, manifest=manifest)
                _require(_inspect(target_dsn, target_password, manifest, prepared, preview._target_schema) == "staged",
                         "candidate_changed")
                source.verify()
                _bind(target_dsn, target_password, manifest, prepared, preview._target_schema, source)
                source.verify()
            _require(_inspect(target_dsn, target_password, manifest, prepared, preview._target_schema) == "bound",
                     "binding_mismatch")
            source.verify()
            return {"status": "already_bound" if kind == "bound" else "staged_and_bound",
                    "manifest_sha256": approved_manifest_sha256,
                    "candidate_sha256": prepared.report["candidate_sha256"], "source_revision": proof.revision,
                    "snapshot_revision": 2, "binding_created": True, "source_written": False,
                    "mail_sent": False, "mail_copied": False, "site_switched": False,
                    "live_activated": False, "migration_complete": False}
    except CutoverError:
        raise
    except Exception:
        raise CutoverError("apply_unconfirmed") from None
