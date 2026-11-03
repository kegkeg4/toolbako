"""Synthetic local PostgreSQL only. Production endpoint checks use fake info."""
from copy import deepcopy
import json
import os
import pickle
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from psycopg.conninfo import make_conninfo
import pytest

from app import live_source_drain as drain
from app import live_dataset_import as local
from app.live_dataset import value_hash
from app.live_dataset_review import prepare_reviewed_candidate
from app.persistence import encode_value
from tools.live_dataset_dry_run import artificial_fixture


def source_dsn(**changes):
    fields = {"host": "aws-0-ap-northeast-1.pooler.supabase.com", "port": "5432", "dbname": "postgres",
              "user": "postgres.xnjzgfaasbemffgorvnj", "sslmode": "verify-full", "sslrootcert": drain._verified_ca_path()}
    fields.update(changes)
    return make_conninfo(**fields)


@pytest.mark.parametrize("change", [
    {"host": "db.xijdnqnsuhuciqyygnkg.supabase.co"}, {"user": "postgres.xijdnqnsuhuciqyygnkg"},
    {"host": "127.0.0.1"}, {"host": "aws-0-ap-northeast-1.pooler.supabase.com.evil.invalid"},
    {"host": "a,b"}, {"port": "6543"}, {"dbname": "other"}, {"sslmode": "require"},
    {"sslmode": "disable"}, {"sslrootcert": "/unreviewed/ca"}, {"password": "SYNTHETIC_SECRET"},
    {"hostaddr": "127.0.0.1"}, {"service": "other"}, {"options": "-c search_path=public"},
])
def test_source_config_rejects_target_or_implicit_redirect(monkeypatch, change):
    monkeypatch.setattr(os, "environ", {})
    with pytest.raises(drain.SourceDrainError, match="configuration_invalid") as error:
        drain._source_config(source_dsn(**change), "SYNTHETIC_SECRET")
    assert "SYNTHETIC_SECRET" not in str(error.value)


@pytest.mark.parametrize("host,user", sorted(drain.SOURCE_ENDPOINTS))
def test_only_exact_source_session_or_direct_pair_is_allowed(monkeypatch, host, user):
    monkeypatch.setattr(os, "environ", {})
    result = drain._source_config(source_dsn(host=host, user=user), "quote' slash\\ SYNTHETIC")
    assert result["host"] == host and result["user"] == user and result["port"] == "5432"
    assert result["sslmode"] == "verify-full" and result["gssencmode"] == "disable"
    assert result["password"] == "quote' slash\\ SYNTHETIC"


@pytest.mark.parametrize("name", ["PGPASSWORD", "PGHOST", "PGOPTIONS", "PGSERVICE", "pgpassword"])
def test_inherited_pg_is_rejected_without_modifying_it(monkeypatch, name):
    monkeypatch.setattr(os, "environ", {name: "SYNTHETIC_SECRET"})
    with pytest.raises(drain.SourceDrainError): drain._source_config(source_dsn(), "supplied")
    assert os.environ == {name: "SYNTHETIC_SECRET"}


def test_bad_input_never_connects_or_prints_dsn_password(monkeypatch, capsys):
    monkeypatch.setattr(drain, "_connect", lambda *_: pytest.fail("Must not connect"))
    with pytest.raises(drain.SourceDrainError) as error:
        with drain.held_source_drain("PASSWORD=SECRET", "SECRET", expected_fingerprint="1"*64,
                                    expected_schema_sha256="2"*64, external_writer_evidence_sha256=""):
            pass
    assert error.value.code == "precondition_required" and "SECRET" not in str(error.value)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("fault", ["host", "user", "port", "dbname", "tls", "database_oid", "role", "pid"])
def test_actual_connection_identity_is_checked_independently_of_dsn(fault):
    config = {"host": "aws-0-ap-northeast-1.pooler.supabase.com", "user": "postgres.xnjzgfaasbemffgorvnj"}
    info = SimpleNamespace(**config, port=5432, dbname="postgres")
    pgconn = SimpleNamespace(ssl_in_use=True)
    row = ["postgres", 100, "postgres", "postgres", 123]
    if fault in {"host", "user", "dbname"}: setattr(info, fault, "different")
    elif fault == "port": info.port = 6543
    elif fault == "tls": pgconn.ssl_in_use = False
    elif fault == "database_oid": row[1] = "100"
    elif fault == "role": row[3] = "other"
    elif fault == "pid": row[4] = "123"
    connection = SimpleNamespace(info=info, pgconn=pgconn,
                                 execute=lambda *a: SimpleNamespace(fetchone=lambda: tuple(row)))
    with pytest.raises(drain.SourceDrainError, match="identity_mismatch"):
        drain._identity(connection, config)


@pytest.fixture
def source(monkeypatch):
    dsn = os.environ.get("TEST_POSTGRES_ADMIN_DSN")
    if not dsn:
        pytest.skip("Only a disposable localhost PostgreSQL admin DSN may be provided")
    with local.new_local_target(dsn) as target:
        state, manifest = artificial_fixture()
        fingerprint = value_hash(("synthetic-source", target.fingerprint))
        manifest["source"]["database_fingerprint"] = fingerprint
        ids = {name: str(uuid4()) for name in ("audit", "mail", "payout", "allocation")}
        with local._connect(target._config, target.database_name) as conn:
            conn.execute("insert into toolbako_runtime.marketplace_state(singleton,revision,payload) values(true,42,%s::jsonb)",
                         (json.dumps(encode_value(state)),))
            conn.execute("insert into toolbako_runtime.audit_events values(%s,null,'synthetic','SECRET_ROW','synthetic',now(),'synthetic')", (ids["audit"],))
            conn.execute("insert into toolbako_runtime.stripe_operations(operation_id,endpoint,request_hash,status) values('synthetic','accounts','synthetic','rejected')")
            conn.execute("""insert into toolbako_runtime.email_outbox(id,recipient,sender,subject,body,status,sent_at,claim_token)
                         values(%s,'synthetic@example.invalid','sender@example.invalid','SECRET_SUBJECT','SECRET_BODY','sent',now(),%s)""",
                         (ids["mail"], str(uuid4())))
            conn.execute("insert into toolbako_runtime.finance_receipts(payment_intent,order_id,seller_id,amount,fee) values('pi_synthetic','order','seller',1000,200)")
            conn.execute("""insert into toolbako_runtime.finance_payouts(id,seller_id,account_id,amount,fee,net_amount,status,scheduled_for,request_key,content_hash)
                         values(%s,'seller','acct_synthetic',800,160,640,'paid',now(),'synthetic','synthetic')""", (ids["payout"],))
            conn.execute("insert into toolbako_runtime.finance_allocations(id,payout_id,receipt_id,amount,transfer_amount) values(%s,%s,'pi_synthetic',800,640)",
                         (ids["allocation"], ids["payout"]))
            conn.execute("insert into toolbako_runtime.finance_entries(id,kind,amount,receipt_id,content_hash) values('entry','synthetic',800,'pi_synthetic','synthetic')")
        connections = []
        def connect(config):
            conn = local._connect(target._config, target.database_name, autocommit=True)
            connections.append(conn)
            return conn
        def identity(conn, config):
            local._target_identity(conn, target)
            return fingerprint, conn.execute("select pg_backend_pid()").fetchone()[0]
        monkeypatch.setattr(drain, "_source_config", lambda *a: {})
        monkeypatch.setattr(drain, "_connect", connect)
        monkeypatch.setattr(drain, "_identity", identity)
        yield SimpleNamespace(target=target, state=state, manifest=manifest, fingerprint=fingerprint,
                              connections=connections, ids=ids)
        for conn in connections:
            conn.close()


def read(source, query, params=()):
    with local._connect(source.target._config, source.target.database_name) as conn:
        cursor = conn.execute(query, params)
        return cursor.fetchall() if cursor.description else []


def inventory(source):
    with local._connect(source.target._config, source.target.database_name) as conn:
        return drain._all_rows(conn)


def held(source, **kwargs):
    args = dict(expected_fingerprint=source.fingerprint, expected_schema_sha256=source.target.schema_signature,
                external_writer_evidence_sha256="e"*64, observation_interval_seconds=0.01)
    args.update(kwargs)
    return drain.held_source_drain("synthetic-local-only", "synthetic-local-only", **args)


def test_held_export_uses_real_lock_and_never_writes_any_of_nine_tables(source, capsys):
    before = inventory(source)
    with held(source) as boundary:
        proof = boundary.verify()
        assert dict(proof.table_counts) == {name: 1 for name in drain.TABLES}
        assert set(dict(proof.table_sha256)) == set(drain.TABLES)
        assert proof.snapshot_sha256 == value_hash(source.state) and proof.revision == 42
        with local._connect(source.target._config, source.target.database_name, autocommit=True) as competitor:
            assert competitor.execute("select pg_try_advisory_lock(%s)", (drain.LOCK_ID,)).fetchone() == (False,)
        result = boundary.export(lambda state: prepare_reviewed_candidate(state, source.manifest), manifest=source.manifest)
        assert result is not None
        assert "SECRET" not in str(boundary) + repr(boundary) + repr(proof)
        with pytest.raises(TypeError): pickle.dumps(boundary)
    assert inventory(source) == before and all(c.closed for c in source.connections)
    with local._connect(source.target._config, source.target.database_name, autocommit=True) as conn:
        assert conn.execute("select pg_try_advisory_lock(%s)", (drain.LOCK_ID,)).fetchone() == (True,)
        conn.execute("select pg_advisory_unlock(%s)", (drain.LOCK_ID,))
    with pytest.raises(drain.SourceDrainError, match="boundary_inactive"): boundary.verify()
    assert capsys.readouterr().out == ""


def test_source_connection_is_read_only_and_export_snapshot_is_not_source_storage(source):
    before = inventory(source)
    with held(source) as boundary:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            source.connections[-1].execute("update toolbako_runtime.marketplace_state set revision=999")
        boundary.verify()
    assert inventory(source) == before


@pytest.mark.parametrize("status", ["pending", "unknown"])
def test_unresolved_provider_journal_is_not_cleared_or_exported(source, status):
    read(source, "update toolbako_runtime.stripe_operations set status=%s", (status,))
    before = inventory(source)
    with pytest.raises(drain.SourceDrainError, match="unsettled_work"):
        with held(source): pytest.fail("Must not expose export handle")
    assert inventory(source) == before


@pytest.mark.parametrize("fault", ["pending", "sending", "review", "lease", "missing_sent_at"])
def test_unsettled_mail_or_lease_fails_without_reset_or_resend(source, fault):
    if fault in {"pending", "sending", "review"}:
        read(source, "update toolbako_runtime.email_outbox set status=%s", (fault,))
    elif fault == "lease": read(source, "update toolbako_runtime.email_outbox set lease_until=now()-interval '1 hour'")
    else: read(source, "update toolbako_runtime.email_outbox set sent_at=null")
    before = inventory(source)
    with pytest.raises(drain.SourceDrainError, match="unsettled_work"):
        with held(source): pytest.fail("Must not expose export handle")
    assert inventory(source) == before


@pytest.mark.parametrize("table", drain.TABLES)
def test_each_runtime_table_is_checked_against_independently_committed_mutations(source, table):
    statements = {
        "schema_version": ("update toolbako_runtime.schema_version set version=999", ()),
        "marketplace_state": ("update toolbako_runtime.marketplace_state set revision=revision+1", ()),
        "audit_events": ("insert into toolbako_runtime.audit_events values(%s,null,'new','new','new',now(),'new')", (str(uuid4()),)),
        "stripe_operations": ("update toolbako_runtime.stripe_operations set request_hash='changed'", ()),
        "email_outbox": ("update toolbako_runtime.email_outbox set body='COMMITTED_CHANGE'", ()),
        "finance_receipts": ("insert into toolbako_runtime.finance_receipts(payment_intent,order_id,seller_id,amount,fee) values('pi_new','new','seller',20,2)", ()),
        "finance_payouts": ("update toolbako_runtime.finance_payouts set request_key='changed'", ()),
        "finance_allocations": ("insert into toolbako_runtime.finance_allocations(id,payout_id,receipt_id,amount,transfer_amount) values(%s,%s,'pi_synthetic',10,10)", (str(uuid4()), str(uuid4()))),
        "finance_entries": ("insert into toolbako_runtime.finance_entries(id,kind,amount,content_hash) values('new','new',2,'new')", ()),
    }
    # An additional existing payout lets allocation drift be the only changed
    # table after the initial observation (unique receipt/payout pair preserved).
    if table == "finance_allocations":
        payout_id = statements[table][1][1]
        read(source, """insert into toolbako_runtime.finance_payouts(id,seller_id,account_id,amount,fee,net_amount,status,scheduled_for,request_key,content_hash)
                     values(%s,'seller','acct_synthetic',10,0,10,'paid',now(),'new','new')""", (payout_id,))
    with pytest.raises(drain.SourceDrainError, match="source_changed|schema_mismatch"):
        with held(source) as boundary:
            read(source, *statements[table])
            # Except marketplace_state, none of these commits bumps revision.
            if table != "marketplace_state":
                assert read(source, "select revision from toolbako_runtime.marketplace_state") == [(42,)]
            boundary.verify()


def test_second_observation_is_not_hidden_by_repeatable_read_snapshot(source, monkeypatch):
    original = drain._observe
    calls = 0
    def observe(*args):
        nonlocal calls
        result = original(*args)
        calls += 1
        if calls == 1:
            read(source, "update toolbako_runtime.email_outbox set subject='OUTSIDE_COMMITTED'")
        return result
    monkeypatch.setattr(drain, "_observe", observe)
    with pytest.raises(drain.SourceDrainError, match="source_changed"):
        with held(source): pytest.fail("Independent observation must see committed outbox change")
    assert calls == 2


def test_bounded_lock_acquire_never_steals_existing_writer_lock(source):
    with local._connect(source.target._config, source.target.database_name, autocommit=True) as writer:
        writer.execute("select pg_advisory_lock(%s)", (drain.LOCK_ID,))
        with pytest.raises(drain.SourceDrainError, match="lock_busy"):
            with held(source, lock_timeout_seconds=0.05): pytest.fail("Must not yield")
        assert writer.execute("select pg_advisory_unlock(%s)", (drain.LOCK_ID,)).fetchone() == (True,)


@pytest.mark.parametrize("fault", ["connection_closed", "lock_released", "repeatable_read", "write_enabled"])
def test_lost_session_lock_or_read_policy_invalidates_boundary(source, fault):
    with pytest.raises(drain.SourceDrainError):
        with held(source) as boundary:
            conn = source.connections[-1]
            if fault == "connection_closed": conn.close()
            elif fault == "lock_released": conn.execute("select pg_advisory_unlock(%s)", (drain.LOCK_ID,))
            elif fault == "repeatable_read": conn.execute("set default_transaction_isolation='repeatable read'")
            else: conn.execute("set default_transaction_read_only=off")
            boundary.verify()
    assert source.connections[-1].closed


@pytest.mark.parametrize("fault", ["revision", "hash", "source", "mode", "version"])
def test_stale_manifest_never_reaches_export_consumer(source, fault):
    manifest = deepcopy(source.manifest)
    if fault == "revision": manifest["source_revision"] += 1
    elif fault == "hash": manifest["snapshot_sha256"] = "b"*64
    elif fault == "source": manifest["source"]["database_fingerprint"] = "b"*64
    elif fault == "mode": manifest["source"]["mode"] = "live"
    else: manifest["source_schema_version"] = 99
    with pytest.raises(drain.SourceDrainError, match="manifest_stale"):
        with held(source) as boundary:
            boundary.export(lambda _: pytest.fail("Must not export stale source"), manifest=manifest)


@pytest.mark.parametrize("fault", ["consumer_mutates", "consumer_exception", "outside_commit"])
def test_export_checks_input_and_source_after_consumer_and_sanitizes_failure(source, fault, capsys):
    before = inventory(source)
    def consume(state):
        if fault == "consumer_mutates": state["tools"].clear()
        elif fault == "consumer_exception": raise RuntimeError("SECRET_PROVIDER_MESSAGE")
        else: read(source, "update toolbako_runtime.stripe_operations set request_hash='outside'")
        return "not_exported_on_error"
    with pytest.raises(drain.SourceDrainError) as error:
        with held(source) as boundary:
            boundary.export(consume, manifest=source.manifest)
    assert "SECRET" not in str(error.value)
    if fault != "outside_commit": assert inventory(source) == before
    assert all(conn.closed for conn in source.connections)
    assert capsys.readouterr().out == ""


def test_absent_snapshot_is_not_initialized(source):
    read(source, "delete from toolbako_runtime.marketplace_state")
    with pytest.raises(drain.SourceDrainError, match="source_uninitialized"):
        with held(source): pytest.fail("Must not initialize empty source")
    assert read(source, "select count(*) from toolbako_runtime.marketplace_state") == [(0,)]


def test_arbitrary_claim_cannot_construct_a_live_export_handle():
    with pytest.raises(drain.SourceDrainError, match="boundary_inactive"):
        drain.HeldSource(None, {"password": "SECRET"}, ("a"*64, 1), None, "b"*64)
