from copy import deepcopy
from contextlib import contextmanager
import json
import os
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

from app import live_dataset_transfer as transfer
from app import live_dataset_import as local
from app.live_dataset import value_hash
from app.live_dataset_review import prepare_reviewed_candidate
from tools.live_dataset_dry_run import artificial_fixture


@pytest.fixture
def target():
    dsn = os.environ.get("TEST_POSTGRES_ADMIN_DSN")
    if not dsn:
        pytest.skip("Local PostgreSQL required")
    with local.new_local_target(dsn) as result:
        yield result


def prepared(target):
    state, manifest = artificial_fixture()
    manifest["target"]["database_fingerprint"] = target.fingerprint
    return manifest, prepare_reviewed_candidate(state, manifest)


def stage(target, values=None):
    manifest, candidate = values or prepared(target)
    with local._connect(target._config, target.database_name) as conn:
        return transfer._stage_transaction(conn, manifest, candidate, target.schema_signature)


def rows(target, query, params=()):
    with local._connect(target._config, target.database_name) as conn:
        result = conn.execute(query, params)
        return result.fetchall() if result.description else []


def test_stage_is_atomic_exact_replay_and_does_not_bind_or_switch(target):
    values = prepared(target)
    first = stage(target, values)
    state = rows(target, "select revision,payload,updated_at from toolbako_runtime.marketplace_state")
    audits = rows(target, "select * from toolbako_runtime.audit_events")
    second = stage(target, values)
    assert (first["status"], second["status"]) == ("staged", "already_staged")
    assert state == rows(target, "select revision,payload,updated_at from toolbako_runtime.marketplace_state")
    assert audits == rows(target, "select * from toolbako_runtime.audit_events")
    for flag in ("source_written", "binding_created", "site_switched", "live_activated", "migration_complete"):
        assert first[flag] is False
    for table in transfer.NORMALIZED_EMPTY_TABLES:
        assert rows(target, f"select count(*) from toolbako_runtime.{table}") == [(0,)]


def test_different_manifest_cannot_overwrite_staged_data(target):
    values = prepared(target)
    stage(target, values)
    original = rows(target, "select * from toolbako_runtime.marketplace_state")
    values[0]["manifest_id"] = str(uuid4())
    with pytest.raises(transfer.TransferError):
        stage(target, values)
    assert rows(target, "select * from toolbako_runtime.marketplace_state") == original


def test_application_change_after_staging_blocks_replay(target):
    values = prepared(target)
    stage(target, values)
    rows(target, "update toolbako_runtime.marketplace_state set revision=revision+1")
    with pytest.raises(transfer.TransferError, match="stored_snapshot_mismatch"):
        stage(target, values)
    assert rows(target, "select revision from toolbako_runtime.marketplace_state") == [(2,)]


def test_late_verification_error_rolls_back_snapshot_and_audit(target, monkeypatch):
    def fail(*args):
        raise transfer.TransferError("synthetic_late_error")
    monkeypatch.setattr(transfer, "_verify", fail)
    with pytest.raises(transfer.TransferError, match="synthetic_late_error"):
        stage(target)
    assert rows(target, "select count(*) from toolbako_runtime.marketplace_state") == [(0,)]
    assert rows(target, "select count(*) from toolbako_runtime.audit_events") == [(0,)]


@pytest.mark.parametrize("fault", ["outbox", "journal", "schema", "policy", "version", "audit", "existing_state"])
def test_target_drift_stops_before_any_data_write(target, fault):
    if fault == "outbox":
        rows(target, "insert into toolbako_runtime.email_outbox(id,recipient,sender,subject,body) values(%s,'a@example.invalid','b@example.invalid','synthetic','do not send')", (str(uuid4()),))
    elif fault == "journal":
        rows(target, "insert into toolbako_runtime.stripe_operations(operation_id,endpoint,request_hash,status) values('existing','internal','test','unknown')")
    elif fault == "schema":
        rows(target, "alter table toolbako_runtime.marketplace_state disable row level security")
    elif fault == "policy":
        rows(target, "create policy synthetic_policy on toolbako_runtime.marketplace_state using (true) with check (true)")
    elif fault == "version":
        rows(target, "update toolbako_runtime.schema_version set version=999")
    elif fault == "audit":
        rows(target, "insert into toolbako_runtime.audit_events(id,action,target,request_id,occurred_at,content_hash) values(%s,'existing','fixed','fixed',now(),'fixed')", (str(uuid4()),))
    else:
        rows(target, "insert into toolbako_runtime.marketplace_state(singleton,revision,payload) values(true,100,'{}')")
    before = rows(target, "select * from toolbako_runtime.marketplace_state")
    with pytest.raises((transfer.TransferError, local.LocalImportError)):
        stage(target)
    assert rows(target, "select * from toolbako_runtime.marketplace_state") == before


@pytest.fixture
def restricted_role(target):
    # Newly created disposable local role only; never accept a role name from
    # a DSN or touch grants on a user-provided/server-side role.
    role = "toolbako_stage_" + uuid4().hex
    with local._connect(target._config, target.database_name) as conn:
        conn.execute(sql.SQL("create role {} nologin nosuperuser nobypassrls").format(sql.Identifier(role)))
        conn.execute(sql.SQL("grant usage on schema toolbako_runtime to {}").format(sql.Identifier(role)))
        conn.execute(sql.SQL("grant all on all tables in schema toolbako_runtime to {}").format(sql.Identifier(role)))
    try:
        yield role
    finally:
        with local._connect(target._config, target.database_name) as conn:
            conn.execute(sql.SQL("drop owned by {}").format(sql.Identifier(role)))
            conn.execute(sql.SQL("drop role {}").format(sql.Identifier(role)))


def test_rls_invisible_rows_are_never_treated_as_an_empty_stage_target(target, restricted_role):
    rows(target, "insert into toolbako_runtime.stripe_operations(operation_id,endpoint,request_hash,status) values('existing','internal','test','unknown')")
    with local._connect(target._config, target.database_name) as conn:
        signature = transfer._schema_signature(conn)
        conn.execute(sql.SQL("set local role {}").format(sql.Identifier(restricted_role)))
        assert conn.execute("select count(*) from toolbako_runtime.stripe_operations").fetchone() == (0,)
    manifest, candidate = prepared(target)
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level security"):
        with local._connect(target._config, target.database_name) as conn:
            conn.execute(sql.SQL("set local role {}").format(sql.Identifier(restricted_role)))
            transfer._stage_transaction(conn, manifest, candidate, signature)
    assert rows(target, "select count(*) from toolbako_runtime.stripe_operations") == [(1,)]
    assert rows(target, "select count(*) from toolbako_runtime.marketplace_state") == [(0,)]
    assert rows(target, "select count(*) from toolbako_runtime.audit_events") == [(0,)]


def test_inspection_fails_closed_when_rls_would_hide_rows(target, restricted_role, monkeypatch):
    original_connect = psycopg.connect

    @contextmanager
    def restricted_connect(*args, **kwargs):
        with original_connect(psycopg.conninfo.make_conninfo(**(target._config | {"dbname": target.database_name}))) as conn:
            conn.execute(sql.SQL("set local role {}").format(sql.Identifier(restricted_role)))
            yield conn

    with monkeypatch.context() as patched:
        # Exercise the actual inspection transaction against only the owned
        # localhost DB; external target checks have independent tests below.
        patched.setattr(psycopg, "connect", restricted_connect)
        patched.setattr(transfer, "target_config", lambda *_: {})
        patched.setattr(transfer, "_connection_identity", lambda _: target.fingerprint)
        with pytest.raises(transfer.TransferError, match="target_inspection_failed"):
            transfer.inspect_target("synthetic-local-only", "synthetic-local-only")
    assert rows(target, "select count(*) from toolbako_runtime.marketplace_state") == [(0,)]


def test_policy_definition_changes_modify_schema_signature(target):
    rows(target, "create policy synthetic_policy on toolbako_runtime.marketplace_state using (true) with check (true)")
    with local._connect(target._config, target.database_name) as conn:
        original = transfer._schema_signature(conn)
    rows(target, "alter policy synthetic_policy on toolbako_runtime.marketplace_state using (false) with check (false)")
    with local._connect(target._config, target.database_name) as conn:
        assert transfer._schema_signature(conn) != original


def safe_dsn(**updates):
    config = dict(host=transfer.TARGET_HOST, port="5432", dbname="postgres", user="postgres", sslmode="verify-full")
    config.update(updates)
    return psycopg.conninfo.make_conninfo(**config)


@pytest.mark.parametrize("updates", [
    {"host": "db.xnjzgfaasbemffgorvnj.supabase.co"}, {"host": "127.0.0.1"},
    {"host": "db.xijdnqnsuhuciqyygnkg.supabase.co,evil.example"}, {"port": "6543"},
    {"dbname": "other"}, {"sslmode": "require"}, {"sslmode": "disable"},
    {"hostaddr": "127.0.0.1"}, {"service": "hidden"}, {"options": "-c search_path=public"},
    {"user": "anonymous"}, {"password": "private-sentinel"},
    {"host": transfer.TARGET_SESSION_HOST, "user": "postgres"},
    {"host": transfer.TARGET_SESSION_HOST, "user": "toolbako_migrator"},
    {"host": transfer.TARGET_SESSION_HOST, "user": "postgres.xnjzgfaasbemffgorvnj"},
    {"host": transfer.TARGET_SESSION_HOST, "user": transfer.TARGET_SESSION_USER, "port": "6543"},
    {"host": "aws-0-ap-northeast-2.pooler.supabase.com", "user": transfer.TARGET_SESSION_USER},
    {"user": transfer.TARGET_SESSION_USER},
])
def test_connection_rejects_wrong_target_and_credential_routing(updates):
    with pytest.raises(transfer.TransferError) as caught:
        transfer.target_config(safe_dsn(**updates), "not-a-real-secret")
    assert "private-sentinel" not in str(caught.value)


@pytest.mark.parametrize("host,user", [
    (transfer.TARGET_HOST, "postgres"),
    (transfer.TARGET_HOST, "toolbako_migrator"),
    (transfer.TARGET_SESSION_HOST, transfer.TARGET_SESSION_USER),
])
def test_only_exact_reviewed_endpoint_role_pairs_are_accepted(host, user):
    config = transfer.target_config(safe_dsn(host=host, user=user), "synthetic-secret")
    assert (config["host"], config["port"], config["user"], config["dbname"]) == (host, "5432", user, "postgres")
    assert config["sslmode"] == "verify-full"
    assert config["passfile"] == os.devnull
    assert config["options"] == "" and config["gssencmode"] == "disable"


class IdentityConnection:
    def __init__(self, **updates):
        ssl_in_use = updates.pop("ssl_in_use", True)
        values = dict(host=transfer.TARGET_SESSION_HOST, user=transfer.TARGET_SESSION_USER,
                      port=5432, dbname="postgres")
        values.update(updates)
        self.info = SimpleNamespace(**values)
        self.pgconn = SimpleNamespace(ssl_in_use=ssl_in_use)
        self.queries = []

    def execute(self, query):
        self.queries.append(query)
        return SimpleNamespace(fetchone=lambda: ("postgres", 5))


def test_connection_identity_pins_endpoint_role_and_project_even_with_same_database_oid():
    session = IdentityConnection()
    direct = IdentityConnection(host=transfer.TARGET_HOST, user="postgres")
    migrator = IdentityConnection(host=transfer.TARGET_HOST, user="toolbako_migrator")
    observed = {transfer._connection_identity(conn) for conn in (session, direct, migrator)}
    assert len(observed) == 3
    assert transfer._connection_identity(session) == value_hash((
        transfer.TARGET_PROJECT, transfer.TARGET_SESSION_HOST, transfer.TARGET_SESSION_USER,
        5432, "postgres", 5))


@pytest.mark.parametrize("updates", [
    {"user": "postgres.xnjzgfaasbemffgorvnj"}, {"user": "postgres"},
    {"host": transfer.TARGET_HOST}, {"host": "aws-0-ap-northeast-2.pooler.supabase.com"},
    {"port": 6543}, {"dbname": "other"}, {"ssl_in_use": False},
])
def test_actual_connection_identity_rejects_endpoint_or_project_drift_before_sql(updates):
    conn = IdentityConnection(**updates)
    with pytest.raises(transfer.TransferError, match="target_connection_mismatch"):
        transfer._connection_identity(conn)
    assert not conn.queries


def test_real_psycopg_transport_api_rejects_unencrypted_connection_before_identity_sql(target):
    with local._connect(target._config, target.database_name) as conn:
        # Exercise the installed psycopg/libpq object, not a made-up SSL field
        # on ConnectionInfo. The owned localhost test connection disables TLS.
        assert isinstance(conn, psycopg.Connection)
        assert isinstance(conn.info, psycopg.ConnectionInfo)
        assert isinstance(conn.pgconn, psycopg.pq.PGconn)
        assert type(conn.pgconn.ssl_in_use) is bool
        assert conn.pgconn.ssl_in_use is False

        # Supply only reviewed endpoint metadata to reach the transport guard;
        # the transport remains the actual unencrypted local PG connection.
        probe = IdentityConnection()
        probe.pgconn = conn.pgconn
        with pytest.raises(transfer.TransferError, match="target_connection_mismatch"):
            transfer._connection_identity(probe)
        assert not probe.queries


def test_session_pooler_identity_cannot_reuse_old_direct_fingerprint():
    conn = IdentityConnection()
    old_direct = value_hash((transfer.TARGET_PROJECT, transfer.TARGET_HOST, 5432, "postgres", 5))
    assert transfer._connection_identity(conn) != old_direct


def test_config_requires_explicit_secret_and_rejects_environment(monkeypatch):
    with pytest.raises(transfer.TransferError):
        transfer.target_config(safe_dsn(), "")
    monkeypatch.setenv("PGSERVICE", "never-disclose")
    with pytest.raises(transfer.TransferError, match="inherited_libpq_options"):
        transfer.target_config(safe_dsn(), "not-real")


def test_public_adapter_checks_approval_before_opening_connection(monkeypatch):
    state, manifest = artificial_fixture()
    candidate = prepare_reviewed_candidate(state, manifest)
    def forbidden(*args, **kwargs):
        raise AssertionError("No connection before exact manifest approval")
    monkeypatch.setattr(psycopg, "connect", forbidden)
    with pytest.raises(transfer.TransferError, match="manifest_not_approved"):
        transfer.stage_reviewed_snapshot(safe_dsn(), "not-real", state, manifest, candidate,
            expected_schema_sha256="a" * 64, approved_manifest_sha256="b" * 64)


def test_ambiguous_connect_or_commit_error_is_sanitized(monkeypatch):
    state, manifest = artificial_fixture()
    candidate = prepare_reviewed_candidate(state, manifest)
    def fail(*args, **kwargs):
        raise psycopg.OperationalError("private-secret-and-data")
    monkeypatch.setattr(psycopg, "connect", fail)
    with pytest.raises(transfer.TransferError, match="staging_outcome_unconfirmed") as caught:
        transfer.stage_reviewed_snapshot(safe_dsn(), "not-real", state, manifest, candidate,
            expected_schema_sha256="a" * 64, approved_manifest_sha256=value_hash(manifest))
    assert "private-secret" not in str(caught.value)


def test_commit_acknowledgment_loss_is_resolved_by_identical_replay_only(target, monkeypatch):
    state, manifest = artificial_fixture()
    manifest["target"]["database_fingerprint"] = target.fingerprint
    candidate = prepare_reviewed_candidate(state, manifest)
    config = target._config | {"dbname": target.database_name}
    original_connect = psycopg.connect

    @contextmanager
    def commit_then_disconnect(*args, **kwargs):
        with original_connect(psycopg.conninfo.make_conninfo(**config)) as conn:
            yield conn
        # The DB has committed, but the caller did not receive confirmation.
        raise psycopg.OperationalError("synthetic-private-connection-text")

    def perform():
        return transfer.stage_reviewed_snapshot("synthetic-local-only", "synthetic-local-only",
            state, manifest, candidate, expected_schema_sha256=target.schema_signature,
            approved_manifest_sha256=value_hash(manifest))

    with monkeypatch.context() as patched:
        patched.setattr(transfer, "target_config", lambda *_: config)
        patched.setattr(transfer, "_connection_identity", lambda _: target.fingerprint)
        patched.setattr(psycopg, "connect", commit_then_disconnect)
        with pytest.raises(transfer.TransferError, match="staging_outcome_unconfirmed") as caught:
            perform()
        assert "private-connection" not in str(caught.value)
        patched.setattr(psycopg, "connect", original_connect)
        stored = rows(target, "select revision,payload,updated_at from toolbako_runtime.marketplace_state")
        audits = rows(target, "select * from toolbako_runtime.audit_events")
        assert perform()["status"] == "already_staged"
        assert rows(target, "select revision,payload,updated_at from toolbako_runtime.marketplace_state") == stored
        assert rows(target, "select * from toolbako_runtime.audit_events") == audits
        # Even after an unknown result, changing the manifest cannot repair or
        # replace the staged dataset. It requires independent investigation.
        manifest["manifest_id"] = str(uuid4())
        candidate = prepare_reviewed_candidate(state, manifest)
        with pytest.raises(transfer.TransferError, match="stored_snapshot_mismatch"):
            perform()
        assert rows(target, "select * from toolbako_runtime.audit_events") == audits
