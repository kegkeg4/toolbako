"""Real local-only PostgreSQL rehearsal, not a production dataset migration."""
from copy import deepcopy
from dataclasses import replace
import json
import os
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest

from app import live_dataset_import as imp
from app.live_dataset import empty_state, prepare_candidate, value_hash
from app.persistence import encode_value
from tools.live_dataset_dry_run import artificial_fixture


@pytest.fixture
def target():
    dsn = os.environ.get("TEST_POSTGRES_ADMIN_DSN")
    if not dsn:
        pytest.skip("Set TEST_POSTGRES_ADMIN_DSN to the disposable local PostgreSQL server")
    with imp.new_local_target(dsn) as allocated:
        yield allocated


def bundle(target):
    state, manifest = artificial_fixture()
    manifest["target"]["database_fingerprint"] = target.fingerprint
    return state, manifest, prepare_candidate(state, manifest)


def read(target, query, params=()):
    with imp._connect(target._config, target.database_name) as conn:
        cursor = conn.execute(query, params)
        return cursor.fetchall() if cursor.description else []


def write(target, query, params=()):
    read(target, query, params)


def assert_empty(target):
    assert read(target, "select count(*) from toolbako_runtime.marketplace_state") == [(0,)]
    assert read(target, "select count(*) from toolbako_runtime.audit_events") == [(0,)]
    assert read(target, "select count(*) from toolbako_runtime.stripe_operations") == [(0,)]


def do_import(target, values=None):
    return imp.import_local_rehearsal(target, *(values or bundle(target)), synthetic_data=True)


def test_loopback_published_docker_backend_identity_is_pinned(monkeypatch):
    config = {"host": "127.0.0.1", "port": "15432"}
    connection = SimpleNamespace(info=SimpleNamespace(hostaddr="127.0.0.1", port=15432, dbname="postgres"))
    monkeypatch.setattr(imp, "_server_details", lambda conn: ("172.18.0.2", 5432, conn.info.dbname))
    identity = imp._server(connection, config, "postgres")
    assert identity == ("172.18.0.2", 5432)
    connection.info.dbname = "toolbako_import_" + "a" * 32
    assert imp._server(connection, config, connection.info.dbname, identity) == identity


@pytest.mark.parametrize("backend", [
    ("172.18.0.3", 5432, "postgres"), ("172.18.0.2", 5433, "postgres"),
    ("172.18.0.2", 5432, "other_database"), (None, 5432, "postgres"),
    ("not-an-address", 5432, "postgres"), ("0.0.0.0", 5432, "postgres"),
    ("224.0.0.1", 5432, "postgres"), ("8.8.8.8", 5432, "postgres"),
    ("172.18.0.2", 0, "postgres"), ("172.18.0.2", True, "postgres"),
])
def test_changed_or_invalid_backend_identity_is_rejected(monkeypatch, backend):
    connection = SimpleNamespace(info=SimpleNamespace(hostaddr="127.0.0.1", port=15432, dbname="postgres"))
    monkeypatch.setattr(imp, "_server_details", lambda conn: backend)
    with pytest.raises(imp.LocalImportError, match="server_identity_mismatch"):
        imp._server(connection, {"host": "127.0.0.1", "port": "15432"}, "postgres", ("172.18.0.2", 5432))


@pytest.mark.parametrize("address", [None, "not-an-address", "0.0.0.0", "224.0.0.1", "8.8.8.8", "::"])
def test_initial_backend_capture_rejects_untrusted_address(monkeypatch, address):
    connection = SimpleNamespace(info=SimpleNamespace(hostaddr="127.0.0.1", port=15432, dbname="postgres"))
    monkeypatch.setattr(imp, "_server_details", lambda conn: (address, 5432, conn.info.dbname))
    with pytest.raises(imp.LocalImportError, match="server_identity_mismatch"):
        imp._server(connection, {"host": "127.0.0.1", "port": "15432"}, "postgres")


@pytest.mark.parametrize("client", [
    ("172.18.0.2", 15432, "postgres"), ("127.0.0.1", 5432, "postgres"),
    ("127.0.0.1", 15432, "other_database"),
])
def test_docker_support_does_not_allow_changed_client_endpoint(monkeypatch, client):
    connection = SimpleNamespace(info=SimpleNamespace(hostaddr=client[0], port=client[1], dbname=client[2]))
    def forbidden(conn): raise AssertionError("Backend query must not be attempted")
    monkeypatch.setattr(imp, "_server_details", forbidden)
    with pytest.raises(imp.LocalImportError, match="client_identity_mismatch"):
        imp._server(connection, {"host": "127.0.0.1", "port": "15432"}, "postgres")


def test_real_pg_lifecycle_with_docker_translated_server_reporting(monkeypatch):
    dsn = os.environ.get("TEST_POSTGRES_ADMIN_DSN")
    if not dsn: pytest.skip("Local PostgreSQL required")
    # Actual CREATE/schema/transaction/replay/drop on local PostgreSQL. Only
    # backend reporting is simulated; actual Docker acceptance belongs to CI.
    monkeypatch.setattr(imp, "_server_details", lambda conn: ("172.18.0.2", 5432, conn.info.dbname))
    config = imp.local_config(dsn)
    with imp._connect(config) as conn:
        before = conn.execute("select datname from pg_database order by datname").fetchall()
    with imp.new_local_target(dsn) as allocated:
        assert allocated._server_identity == ("172.18.0.2", 5432)
        values = bundle(allocated)
        with monkeypatch.context() as scoped:
            scoped.setattr(imp, "_server_details", lambda conn: ("172.18.0.3", 5432, conn.info.dbname))
            with pytest.raises(imp.LocalImportError, match="server_identity_mismatch"):
                do_import(allocated, values)
        assert_empty(allocated)
        def fail(*args, **kwargs): raise imp.LocalImportError("synthetic_failure")
        with monkeypatch.context() as scoped:
            scoped.setattr(imp, "_write_audit", fail)
            with pytest.raises(imp.LocalImportError, match="synthetic_failure"):
                do_import(allocated, values)
        assert_empty(allocated)
        assert do_import(allocated, values)["status"] == "synthetic_imported"
        assert do_import(allocated, values)["status"] == "already_imported"
    with imp._connect(config) as conn:
        assert conn.execute("select datname from pg_database order by datname").fetchall() == before
    assert not allocated._active


def test_actual_transactional_import_and_exact_replay_do_not_bind_or_rewrite(target):
    values = bundle(target)
    before = deepcopy(values)
    first = do_import(target, values)
    rows_before = read(target, "select revision,payload,updated_at from toolbako_runtime.marketplace_state")
    audit_before = read(target, "select * from toolbako_runtime.audit_events")
    again = do_import(target, values)
    assert first["status"] == "synthetic_imported" and again["status"] == "already_imported"
    assert first["stored_payload_sha256"] == again["stored_payload_sha256"]
    assert all(first[key] is False for key in ("binding_created", "live_activated", "production_migration_complete"))
    assert rows_before == read(target, "select revision,payload,updated_at from toolbako_runtime.marketplace_state")
    assert audit_before == read(target, "select * from toolbako_runtime.audit_events")
    assert values == before
    with imp._connect(target._config, target.database_name) as conn:
        revision, actual = imp._read_snapshot(conn)
    assert revision == 1
    expected = deepcopy(values[2].candidate)
    event = actual["audit_logs"].pop()
    assert actual == expected and event["action"] == imp.ACTION
    # Journal stays empty: the import marker cannot obstruct future separately
    # authorized bind-empty merely by occupying stripe_operations. No bind run.
    for table in imp.NORMALIZED_EMPTY_TABLES:
        assert read(target, f"select count(*) from toolbako_runtime.{table}") == [(0,)]


def test_fresh_empty_initialized_snapshot_is_allowed(target):
    write(target, "insert into toolbako_runtime.marketplace_state(singleton,payload) values(true,%s::jsonb)", (json.dumps(encode_value(empty_state())),))
    assert do_import(target)["status"] == "synthetic_imported"


@pytest.mark.parametrize("stage", ["audit", "verification"])
def test_failure_after_snapshot_write_rolls_back_every_row(target, monkeypatch, stage):
    function = "_write_audit" if stage == "audit" else "_verify_persisted"
    def fail(*args, **kwargs):
        raise imp.LocalImportError("synthetic_failure")
    with monkeypatch.context() as scoped:
        scoped.setattr(imp, function, fail)
        with pytest.raises(imp.LocalImportError, match="synthetic_failure"):
            do_import(target)
    assert_empty(target)
    assert do_import(target)["status"] == "synthetic_imported"


def test_database_failure_after_snapshot_write_is_atomic_and_sanitized(target, monkeypatch):
    def duplicate_audit(conn, event):
        # NOT NULL failure after the marketplace_state insert.
        conn.execute("insert into toolbako_runtime.audit_events(id,action,target,request_id,occurred_at,content_hash) values(%s,null,%s,'',now(),'')", (str(uuid4()), "private-sentinel"))
    with monkeypatch.context() as scoped:
        scoped.setattr(imp, "_write_audit", duplicate_audit)
        with pytest.raises(imp.LocalImportError, match="transaction_failed") as caught:
            do_import(target)
        assert "private-sentinel" not in str(caught.value)
    assert_empty(target)


@pytest.mark.parametrize("field", ["orders", "tools", "registered_users", "account_sessions", "identity_applications", "audit_logs"])
def test_existing_runtime_records_are_never_overwritten(target, field):
    state = empty_state()
    if isinstance(state[field], list): state[field].append({"existing": True})
    else: state[field]["existing"] = True
    write(target, "insert into toolbako_runtime.marketplace_state(singleton,payload) values(true,%s::jsonb)", (json.dumps(encode_value(state)),))
    before = read(target, "select * from toolbako_runtime.marketplace_state")
    with pytest.raises(imp.LocalImportError, match="target_nonempty"): do_import(target)
    assert read(target, "select * from toolbako_runtime.marketplace_state") == before
    assert read(target, "select count(*) from toolbako_runtime.audit_events") == [(0,)]


@pytest.mark.parametrize("kind", ["receipt", "payout", "allocation", "entry", "outbox", "journal", "audit"])
def test_existing_financial_mail_or_audit_facts_block_import(target, kind):
    payout = "77777777-7777-4777-8777-777777777777"
    if kind in {"receipt", "allocation"}:
        write(target, "insert into toolbako_runtime.finance_receipts(payment_intent,order_id,seller_id,amount,fee) values('pi_synthetic','synthetic','seller',1000,200)")
    if kind in {"payout", "allocation"}:
        write(target, "insert into toolbako_runtime.finance_payouts(id,seller_id,account_id,amount,fee,net_amount,status,scheduled_for,request_key,content_hash) values(%s,'seller','acct_synthetic',800,0,800,'requested',now(),'synthetic','synthetic')", (payout,))
    if kind == "allocation":
        write(target, "insert into toolbako_runtime.finance_allocations(id,payout_id,receipt_id,amount,transfer_amount) values(%s,%s,'pi_synthetic',800,800)", (str(uuid4()), payout))
    if kind == "entry":
        write(target, "insert into toolbako_runtime.finance_entries(id,kind,amount,content_hash) values('synthetic','synthetic',1,'synthetic')")
    if kind == "outbox":
        write(target, "insert into toolbako_runtime.email_outbox(id,recipient,sender,subject,body) values(%s,'to@example.invalid','from@example.invalid','synthetic','never send')", (str(uuid4()),))
    if kind == "journal":
        write(target, "insert into toolbako_runtime.stripe_operations(operation_id,endpoint,request_hash,status) values('synthetic','synthetic','synthetic','unknown')")
    if kind == "audit":
        write(target, "insert into toolbako_runtime.audit_events(id,action,target,request_id,occurred_at,content_hash) values(%s,'existing','synthetic','',now(),'synthetic')", (str(uuid4()),))
    with pytest.raises(imp.LocalImportError): do_import(target)
    assert read(target, "select count(*) from toolbako_runtime.marketplace_state") == [(0,)]


@pytest.mark.parametrize("kind", ["version", "rls", "immutable", "foreign_key", "unexpected_table"])
def test_actual_schema_drift_blocks_before_any_import(target, kind):
    statements = {
        "version": "update toolbako_runtime.schema_version set version=999",
        "rls": "alter table toolbako_runtime.marketplace_state disable row level security",
        "immutable": "drop trigger runtime_audit_immutable on toolbako_runtime.audit_events",
        "foreign_key": "alter table toolbako_runtime.finance_entries drop constraint finance_entries_receipt_id_fkey",
        "unexpected_table": "create table toolbako_runtime.unexpected(id integer)",
    }
    write(target, statements[kind])
    with pytest.raises(imp.LocalImportError): do_import(target)
    assert_empty(target)


def test_import_audit_keeps_actual_append_only_protection(target):
    do_import(target)
    with pytest.raises(psycopg.errors.RaiseException):
        write(target, "delete from toolbako_runtime.audit_events")
    assert read(target, "select count(*) from toolbako_runtime.audit_events") == [(1,)]


def test_new_schema_actual_foreign_key_rejects_orphan_without_partial_rows(target):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        write(target, "insert into toolbako_runtime.finance_entries(id,kind,amount,receipt_id,content_hash) values('synthetic-orphan','synthetic',1,'missing-receipt','synthetic')")
    assert read(target, "select count(*) from toolbako_runtime.finance_entries") == [(0,)]
    assert do_import(target)["status"] == "synthetic_imported"


@pytest.mark.parametrize("fault", ["changed_manifest", "changed_candidate", "changed_report", "target_fingerprint", "different_source", "missing_declaration"])
def test_prepared_proof_cannot_be_reused_for_changed_inputs(target, fault):
    state, manifest, prepared = bundle(target)
    if fault == "changed_manifest": manifest["manifest_id"] = str(uuid4())
    elif fault == "changed_candidate": prepared.candidate["orders"].append({"private": "sentinel"})
    elif fault == "changed_report": prepared.report["candidate_sha256"] = "0" * 64
    elif fault == "target_fingerprint":
        manifest["target"]["database_fingerprint"] = "0" * 64
        prepared = prepare_candidate(state, manifest)
    elif fault == "different_source": state["registered_users"]["sample_buyer"]["display_name"] = "changed"
    with pytest.raises(imp.LocalImportError):
        imp.import_local_rehearsal(target, state, manifest, prepared, synthetic_data=fault != "missing_declaration")
    assert_empty(target)


def test_different_manifest_after_success_does_not_overwrite_or_add_marker(target):
    values = bundle(target)
    do_import(target, values)
    before = read(target, "select * from toolbako_runtime.marketplace_state")
    state, manifest, _ = values
    manifest["manifest_id"] = str(uuid4())
    with pytest.raises(imp.LocalImportError): do_import(target, (state, manifest, prepare_candidate(state, manifest)))
    assert read(target, "select * from toolbako_runtime.marketplace_state") == before
    assert read(target, "select count(*) from toolbako_runtime.audit_events") == [(1,)]


def test_changed_target_after_success_is_not_repaired_by_replay(target):
    values = bundle(target)
    do_import(target, values)
    write(target, "update toolbako_runtime.marketplace_state set revision=revision+1")
    with pytest.raises(imp.LocalImportError, match="snapshot_verification_failed"): do_import(target, values)
    assert read(target, "select revision from toolbako_runtime.marketplace_state") == [(2,)]


def test_copied_handle_and_wrong_ownership_marker_are_rejected(target):
    values = bundle(target)
    with pytest.raises(imp.LocalImportError, match="target_not_owned"):
        do_import(replace(target), values)
    with imp._connect(target._config, autocommit=True) as conn:
        conn.execute(psycopg.sql.SQL("comment on database {} is 'other-owner'").format(psycopg.sql.Identifier(target.database_name)))
    with pytest.raises(imp.LocalImportError, match="target_not_owned"): do_import(target, values)
    assert_empty(target)


@pytest.mark.parametrize("dsn", [
    "host=database.example.com user=postgres dbname=postgres", "host=localhost user=postgres dbname=postgres",
    "host=127.0.0.1 user=postgres dbname=existing_database", "host=127.0.0.1 user=postgres dbname=postgres hostaddr=203.0.113.5",
    "host=127.0.0.1 user=postgres dbname=postgres service=external", "host=127.0.0.1 user=postgres dbname=postgres options='-c search_path=public'",
    "host=127.0.0.1 user=postgres dbname=postgres port=5432,5433", "user=postgres dbname=postgres",
])
def test_nonlocal_or_ambiguous_connection_rejected_before_connection(monkeypatch, dsn):
    def forbidden(*args, **kwargs): raise AssertionError("Connection must not be attempted")
    monkeypatch.setattr(psycopg, "connect", forbidden)
    with pytest.raises(imp.LocalImportError):
        with imp.new_local_target(dsn): pass


def test_inherited_routing_environment_is_rejected_without_disclosing_value(monkeypatch):
    monkeypatch.setenv("PGHOSTADDR", "private-sentinel")
    with pytest.raises(imp.LocalImportError, match="inherited_libpq_options") as caught:
        imp.local_config("host=127.0.0.1 user=postgres dbname=postgres")
    assert "private-sentinel" not in str(caught.value)


def test_context_only_removes_its_new_database_and_invalidates_handle():
    dsn = os.environ.get("TEST_POSTGRES_ADMIN_DSN")
    if not dsn: pytest.skip("Local PostgreSQL required")
    config = imp.local_config(dsn)
    with imp._connect(config) as conn:
        before = conn.execute("select datname from pg_database order by datname").fetchall()
    with imp.new_local_target(dsn) as target:
        values = bundle(target)
        assert target.database_name not in {name for (name,) in before}
        do_import(target, values)
    with imp._connect(config) as conn:
        assert conn.execute("select datname from pg_database order by datname").fetchall() == before
    with pytest.raises(imp.LocalImportError, match="target_not_owned"): do_import(target, values)
