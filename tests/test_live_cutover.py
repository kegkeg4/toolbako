"""Actual two-database transaction rehearsals, exclusively fresh localhost DBs."""
import asyncio
from contextlib import contextmanager
from copy import deepcopy
import json
import os
import pickle
from types import SimpleNamespace
from uuid import uuid4

import pytest
from psycopg.conninfo import make_conninfo

from app import live_cutover as cutover
from app import live_dataset_import as local
from app import live_dataset_transfer as transfer
from app.database import PostgresStateStore
from app.data import DemoStore
from app.financial_scope import FinancialScope, SCOPE_OPERATION_ID
from app.live_dataset import MANUAL_FIELDS, value_hash
from app.live_dataset_review import prepare_reviewed_candidate
from app.persistence import encode_value
from test_live_source_drain import source, read
from test_live_source_freeze import mail_source, rows as source_rows


@pytest.fixture
def rehearsal(mail_source, monkeypatch):
    dsn = os.environ.get("TEST_POSTGRES_ADMIN_DSN")
    if not dsn:
        pytest.skip("Disposable local PostgreSQL required")
    with local.new_local_target(dsn) as target:
        manifest = deepcopy(mail_source.manifest)
        manifest["version"] = 2
        manifest["target"]["database_fingerprint"] = target.fingerprint
        manifest["manual_review"] = {field: {"action": "empty", "record_sha256": value_hash(mail_source.state[field]),
                                              "evidence_sha256": None} for field in MANUAL_FIELDS}
        calls = []
        @contextmanager
        def target_connection(dsn, password):
            assert dsn == "TARGET_ONLY" and password == "TARGET_SECRET"
            with local._connect(target._config, target.database_name) as conn:
                yield conn
        def identity(conn):
            local._target_identity(conn, target)
            return target.fingerprint
        def stage(dsn, password, state, given_manifest, prepared, **options):
            assert options["approved_manifest_sha256"] == value_hash(given_manifest)
            assert options["expected_schema_sha256"] == target.schema_signature
            verified = prepare_reviewed_candidate(state, given_manifest)
            assert verified.report == prepared.report
            calls.append("stage")
            with target_connection(dsn, password) as conn:
                identity(conn)
                conn.rollback()
                return transfer._stage_transaction(conn, given_manifest, verified, target.schema_signature)
        monkeypatch.setattr(cutover, "_target_connection", target_connection)
        monkeypatch.setattr(cutover, "_target_identity", identity)
        monkeypatch.setattr(cutover, "_stage_reviewed", stage)
        yield SimpleNamespace(source=mail_source, target=target, manifest=manifest, calls=calls)


def target_rows(rehearsal, query, params=()):
    with local._connect(rehearsal.target._config, rehearsal.target.database_name) as conn:
        cursor = conn.execute(query, params)
        return cursor.fetchall() if cursor.description else []


def preview(r, **changes):
    options = dict(reviewed_manifest=r.manifest, source_schema_sha256=r.source.target.schema_signature,
                   target_schema_sha256=r.target.schema_signature, mail_review=r.source.mail_review,
                   external_writer_evidence_sha256="e"*64)
    options.update(changes)
    return cutover.preview_cutover("SOURCE_ONLY", "SOURCE_SECRET", "TARGET_ONLY", "TARGET_SECRET", **options)


def apply(r, prepared, **changes):
    report = prepared.report()
    options = dict(preview=prepared, approved_preview_sha256=report["preview_sha256"],
                   approved_manifest_sha256=report["manifest_sha256"])
    options.update(changes)
    return cutover.apply_cutover("SOURCE_ONLY", "SOURCE_SECRET", "TARGET_ONLY", "TARGET_SECRET", **options)


def stored(r):
    return target_rows(r, "select * from toolbako_runtime.marketplace_state"), target_rows(r, "select * from toolbako_runtime.audit_events order by id"), target_rows(r, "select * from toolbako_runtime.stripe_operations")


def test_preview_is_readonly_redacted_and_explicit_apply_binds_only_new_target(rehearsal, capsys):
    r = rehearsal
    original = source_rows(r.source)
    p = preview(r)
    assert stored(r) == ([], [], [])
    report = p.report()
    assert report["source_table_counts"]["email_outbox"] == 16
    assert report["counts"]["orders"]["candidate"] == 0
    assert r.calls == [] and report["written"] is False
    assert not any(secret in json.dumps(report) + repr(p) for secret in
                   ("seller@example", "SECRET", "人工", "acct_", "66666666"))
    with pytest.raises(TypeError): pickle.dumps(p)
    first = apply(r, p)
    assert first["status"] == "staged_and_bound" and first["binding_created"] is True
    for flag in ("source_written", "mail_sent", "mail_copied", "site_switched", "live_activated", "migration_complete"):
        assert first[flag] is False
    assert source_rows(r.source) == original
    assert target_rows(r, "select count(*) from toolbako_runtime.email_outbox") == [(0,)]
    assert target_rows(r, "select revision from toolbako_runtime.marketplace_state") == [(2,)]
    assert capsys.readouterr().out == ""


def test_existing_runtime_backend_accepts_binding_and_catalog_without_writing(rehearsal):
    r = rehearsal
    p = preview(r)
    apply(r, p)
    before = stored(r)
    dsn = make_conninfo(**(r.target._config | {"dbname": r.target.database_name}))
    backend = PostgresStateStore(dsn)
    scope = FinancialScope(**{key: r.manifest["target"][key] for key in ("mode", "platform_account", "ledger_id")})
    async def check():
        store = DemoStore(seed=False)
        async with backend.request(store):
            await backend.assert_financial_scope(scope)
            # The existing binder treats our exact same-scope journal as replay.
            await backend.bind_empty_financial_scope(scope)
            assert len(store.tools) == 1 and not store.orders and not store.connected_accounts
            assert [event["action"] for event in store.audit_logs] == ["finance.scope_bound", transfer.ACTION]
    asyncio.run(check())
    assert stored(r) == before


def test_completed_retry_reads_without_restaging_and_process_loss_recovers_same_digest(rehearsal):
    r = rehearsal
    p = preview(r)
    apply(r, p)
    before = stored(r)
    assert apply(r, p)["status"] == "already_bound"
    recovered = preview(r, resume_approved_preview_sha256=p.report()["preview_sha256"])
    assert apply(r, recovered)["status"] == "already_bound"
    assert r.calls == ["stage"] and stored(r) == before
    with pytest.raises(cutover.CutoverError): preview(r)
    with pytest.raises(cutover.CutoverError):
        preview(r, resume_approved_preview_sha256="0"*64)


def test_failure_between_stage_and_bind_keeps_stage_and_same_manifest_resumes(rehearsal, monkeypatch):
    r = rehearsal
    p = preview(r)
    original = source_rows(r.source)
    binder = cutover._bind
    def fail(*args): raise RuntimeError("PRIVATE_CONNECTION_PASSWORD")
    monkeypatch.setattr(cutover, "_bind", fail)
    with pytest.raises(cutover.CutoverError) as error: apply(r, p)
    assert "PRIVATE" not in str(error.value)
    assert target_rows(r, "select revision from toolbako_runtime.marketplace_state") == [(1,)]
    assert target_rows(r, "select count(*) from toolbako_runtime.stripe_operations") == [(0,)]
    imported_audit = target_rows(r, "select * from toolbako_runtime.audit_events")
    monkeypatch.setattr(cutover, "_bind", binder)
    recovered = preview(r, resume_approved_preview_sha256=p.report()["preview_sha256"])
    assert apply(r, recovered)["status"] == "staged_and_bound"
    assert target_rows(r, "select * from toolbako_runtime.audit_events where action=%s", (transfer.ACTION,)) == imported_audit
    assert source_rows(r.source) == original


def test_unknown_success_after_binding_is_readback_only_on_retry(rehearsal, monkeypatch):
    r = rehearsal
    p = preview(r)
    bind = cutover._bind
    def commit_then_fail(*args):
        bind(*args)
        raise ConnectionError("PRIVATE_TARGET_URL")
    monkeypatch.setattr(cutover, "_bind", commit_then_fail)
    with pytest.raises(cutover.CutoverError): apply(r, p)
    before = stored(r)
    assert target_rows(r, "select revision from toolbako_runtime.marketplace_state") == [(2,)]
    assert apply(r, p)["status"] == "already_bound"
    assert stored(r) == before and r.calls == ["stage"]


@pytest.mark.parametrize("field", ["approved_preview_sha256", "approved_manifest_sha256"])
def test_explicit_approval_mismatch_stops_before_any_connection(rehearsal, monkeypatch, field):
    r = rehearsal
    p = preview(r)
    monkeypatch.setattr(cutover, "_freeze", lambda *args: pytest.fail("No source connection"))
    with pytest.raises(cutover.CutoverError, match="approval_mismatch"):
        apply(r, p, **{field: "0"*64})
    assert stored(r) == ([], [], [])


@pytest.mark.parametrize("fault", ["snapshot", "audit", "journal", "ledger", "schema", "outbox", "mail_review"])
def test_stale_source_or_review_never_rebases_silently(rehearsal, fault):
    r = rehearsal
    p = preview(r)
    if fault == "snapshot": read(r.source, "update toolbako_runtime.marketplace_state set revision=43")
    elif fault == "audit": read(r.source, "insert into toolbako_runtime.audit_events values(%s,null,'new','new','new',now(),'new')", (str(uuid4()),))
    elif fault == "journal": read(r.source, "update toolbako_runtime.stripe_operations set request_hash='changed'")
    elif fault == "ledger": read(r.source, "insert into toolbako_runtime.finance_entries(id,kind,amount,content_hash) values('new','new',1,'new')")
    elif fault == "schema": read(r.source, "alter table toolbako_runtime.marketplace_state disable row level security")
    elif fault == "outbox": read(r.source, "update toolbako_runtime.email_outbox set attempts=1")
    else: p._writer_evidence = "a"*64
    with pytest.raises(cutover.CutoverError): apply(r, p)
    assert stored(r) == ([], [], []) and r.calls == []


@pytest.mark.parametrize("fault", ["outbox", "journal", "snapshot", "schema", "fingerprint", "ledger"])
def test_changed_target_or_scope_is_never_overwritten(rehearsal, monkeypatch, fault):
    r = rehearsal
    p = preview(r)
    if fault == "outbox": target_rows(r, "insert into toolbako_runtime.email_outbox(id,recipient,sender,subject,body) values(%s,'x','x','x','x')", (str(uuid4()),))
    elif fault == "journal": target_rows(r, "insert into toolbako_runtime.stripe_operations(operation_id,endpoint,request_hash,status) values('x','x','x','pending')")
    elif fault == "snapshot": target_rows(r, "insert into toolbako_runtime.marketplace_state(singleton,revision,payload) values(true,0,'{}')")
    elif fault == "schema": target_rows(r, "alter table toolbako_runtime.marketplace_state disable row level security")
    elif fault == "fingerprint": monkeypatch.setattr(cutover, "_target_identity", lambda conn: r.source.fingerprint)
    else: p._manifest["target"]["ledger_id"] = str(uuid4())
    before = stored(r)
    with pytest.raises(cutover.CutoverError): apply(r, p)
    assert stored(r) == before and r.calls == []


def test_source_disconnect_before_binding_commit_rolls_back_binding_only(rehearsal, monkeypatch):
    r = rehearsal
    p = preview(r)
    bind = cutover._bind
    def disconnect(*args):
        # Source lock loss just before binding must not produce a bound target.
        for conn in r.source.connections: conn.close()
        return bind(*args)
    monkeypatch.setattr(cutover, "_bind", disconnect)
    with pytest.raises(cutover.CutoverError): apply(r, p)
    assert target_rows(r, "select revision from toolbako_runtime.marketplace_state") == [(1,)]
    assert target_rows(r, "select count(*) from toolbako_runtime.stripe_operations") == [(0,)]
    assert target_rows(r, "select count(*) from toolbako_runtime.audit_events") == [(1,)]


def test_target_writes_between_stage_and_binding_block_binding(rehearsal, monkeypatch):
    r = rehearsal
    p = preview(r)
    bind = cutover._bind
    def corrupt_then_bind(*args):
        target_rows(r, "insert into toolbako_runtime.email_outbox(id,recipient,sender,subject,body) values(%s,'x','x','x','x')", (str(uuid4()),))
        return bind(*args)
    monkeypatch.setattr(cutover, "_bind", corrupt_then_bind)
    with pytest.raises(cutover.CutoverError): apply(r, p)
    assert target_rows(r, "select revision from toolbako_runtime.marketplace_state") == [(1,)]
    assert target_rows(r, "select count(*) from toolbako_runtime.stripe_operations") == [(0,)]


def test_staged_payload_and_bound_audit_tampering_reject_exact_retry(rehearsal):
    r = rehearsal
    p = preview(r)
    apply(r, p)
    target_rows(r, "insert into toolbako_runtime.audit_events values(%s,null,'finance.scope_bound','tampered','',now(),'tampered')", (str(uuid4()),))
    before = stored(r)
    with pytest.raises(cutover.CutoverError): apply(r, p)
    assert stored(r) == before and r.calls == ["stage"]


def test_unreviewed_new_source_selection_is_not_automatically_included(rehearsal):
    r = rehearsal
    changed = deepcopy(r.source.state)
    changed["registered_users"]["sample_seller"]["bio"] = "UNREVIEWED_PRIVATE_TEXT"
    read(r.source, "update toolbako_runtime.marketplace_state set payload=%s::jsonb", (json.dumps(encode_value(changed)),))
    r.manifest["snapshot_sha256"] = value_hash(changed)
    with pytest.raises(cutover.CutoverError): preview(r)
    assert stored(r) == ([], [], [])


def test_public_stage_wrapper_uses_existing_reviewed_adapter(monkeypatch):
    seen = []
    monkeypatch.setattr(transfer, "stage_reviewed_snapshot", lambda *a, **k: seen.append((a, k)) or "called")
    assert cutover._stage_reviewed("opaque", approved_manifest_sha256="a"*64) == "called"
    assert seen == [(("opaque",), {"approved_manifest_sha256": "a"*64})]


def test_forged_preview_and_dynamic_error_details_are_not_accepted():
    with pytest.raises(cutover.CutoverError, match="preview_required"):
        cutover.apply_cutover("SECRET", "SECRET", "SECRET", "SECRET", preview={"approved": True},
                              approved_preview_sha256="a"*64, approved_manifest_sha256="b"*64)
    assert "SECRET" not in str(cutover.CutoverError("SECRET"))
