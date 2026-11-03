"""Offline contracts and disposable local PG only; no provider is contacted."""
import asyncio
from dataclasses import FrozenInstanceError
import hashlib
import json
import os
from pathlib import Path
import pickle
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import pytest

from app import live_dataset_import as local
from app.database import PostgresStateStore, content_hash
from app.financial_scope import SCOPE_ENDPOINT, SCOPE_OPERATION_ID
from app import live_mail_preparation as mail
from app.live_database_config import LiveDatabaseConfigError, TARGET_HOST, TARGET_PROJECT
from app.mail_worker import deliver_pending

ROOT = Path(__file__).resolve().parents[1]
CUTOFF = 1793664000
REVIEW = "a" * 64
PLAN = mail.PreparedMailAccess(CUTOFF, REVIEW)
PASSWORD = "synthetic-mail-role-only"


def run(coro):
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return asyncio.run(coro)


@pytest.mark.parametrize("since", [None, True, False, 0, -1, 1.5, "123", "1);drop role postgres;--", 4102444801])
def test_cutoff_rejects_nonliteral_or_invalid_seconds(since):
    with pytest.raises(mail.LiveMailPreparationError, match="^live_mail_preparation_invalid$"):
        mail.PreparedMailAccess(since, REVIEW)


@pytest.mark.parametrize("digest", [None, "", "b" * 63, "A" * 64, "secret-password", "x" * 64])
def test_review_trace_rejects_unsafe_digest_without_echo(digest):
    with pytest.raises(mail.LiveMailPreparationError) as error:
        mail.PreparedMailAccess(CUTOFF, digest)
    assert str(error.value) == "live_mail_preparation_invalid"


def test_preparation_is_frozen_nonactivating_and_deterministic():
    with pytest.raises(FrozenInstanceError):
        PLAN.queue_since = 1
    result = PLAN.manifest()
    assert result["preparation_only"] is True and result["delivery_enabled"] is False
    assert result["expected_additional_policy_count"] == 4
    assert result["grants_sql_sha256"] == hashlib.sha256(PLAN.grants_sql().encode()).hexdigest()
    assert result == mail.PreparedMailAccess(CUTOFF, REVIEW).manifest()
    changed = mail.PreparedMailAccess(CUTOFF + 1, REVIEW)
    assert changed.manifest()["grants_sql_sha256"] != result["grants_sql_sha256"]
    assert changed.manifest()["roles_sql_sha256"] == result["roles_sql_sha256"]
    assert content_hash(mail.SCOPE.payload()) in PLAN.grants_sql()
    assert "create role" in PLAN.roles_sql() and "password null" in PLAN.roles_sql()
    assert "disable row level security" not in PLAN.grants_sql().lower()
    assert "current_setting" not in PLAN.grants_sql().lower()
    assert "security definer" not in PLAN.grants_sql().lower()
    assert "grant all" not in PLAN.grants_sql().lower()
    assert "update toolbako_runtime.email_outbox" not in PLAN.grants_sql().lower()


def test_policy_contract_is_exact_and_freshly_owned():
    contracts = PLAN.policy_contract()
    assert sum(map(len, contracts.values())) == 4
    assert set(contracts) == {"schema_version", "stripe_operations", "email_outbox"}
    assert all(value[2] == [mail.MAIL_GROUP] for group in contracts.values() for value in group.values())
    assert all(str(CUTOFF) in value[3] for value in contracts["email_outbox"].values())
    contracts["email_outbox"]["toolbako_mail_live_select"][2].append("public")
    assert PLAN.policy_contract()["email_outbox"]["toolbako_mail_live_select"][2] == [mail.MAIL_GROUP]
    assert set(mail.PERMISSIONS["email_outbox"]["UPDATE"].split()) == {
        "status", "attempts", "first_attempt_at", "available_at", "lease_until", "claim_token", "sent_at"}
    assert set(mail.PERMISSIONS) == set(contracts)


@pytest.mark.parametrize("password", ["spaces quote'\\word=more", "日本語 password", "line\nbreak", "abc\";--"])
def test_fixed_mail_connection_escapes_password_and_does_not_render_it(password):
    configuration = mail.build_mail_database_config(password, environment={})
    values = conninfo_to_dict(configuration.for_connection(environment={}))
    assert values["password"] == password
    assert values["user"] == mail.MAIL_ROLE + "." + TARGET_PROJECT
    assert values["host"] == TARGET_HOST and values["port"] == "5432" and values["dbname"] == "postgres"
    assert values["sslmode"] == "verify-full" and Path(values["sslrootcert"]).is_file()
    assert password not in str(configuration) + repr(configuration)
    with pytest.raises(TypeError):
        pickle.dumps(configuration)


@pytest.mark.parametrize("key", ["PGPASSWORD", "pgservice", "PgHost", "PGOPTIONS"])
def test_inherited_libpq_configuration_cannot_select_old_database(key):
    with pytest.raises(LiveDatabaseConfigError):
        mail.build_mail_database_config("synthetic", environment={key: "never print"})


@pytest.fixture(scope="module")
def pg_database():
    dsn = os.environ.get("TEST_POSTGRES_ADMIN_DSN")
    if not dsn:
        pytest.skip("Disposable local PostgreSQL required")
    config = local.local_config(dsn)
    name = "toolbako_import_" + uuid4().hex
    with local._connect(config, autocommit=True) as conn:
        assert not conn.execute("select 1 from pg_roles where rolname in (%s,%s)", (mail.MAIL_ROLE, mail.MAIL_GROUP)).fetchall()
        conn.execute(PLAN.roles_sql())
        conn.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    try:
        with local._connect(config, name) as conn:
            conn.execute((ROOT / "app/runtime_schema.sql").read_text())
            conn.execute("insert into toolbako_runtime.marketplace_state(singleton,payload) values(true,'{}')")
            conn.execute("insert into toolbako_runtime.stripe_operations(operation_id,endpoint,request_hash,status,response) values(%s,%s,%s,'succeeded',%s::jsonb)",
                         (SCOPE_OPERATION_ID, SCOPE_ENDPOINT, content_hash(mail.SCOPE.payload()), json.dumps(mail.SCOPE.payload())))
            conn.execute(sql.SQL("revoke temporary on database {} from public").format(sql.Identifier(name)))
        with local._connect(config, name, autocommit=True) as conn:
            conn.execute(PLAN.grants_sql())
            # Synthetic local credential only, never emitted by production SQL.
            conn.execute("set password_encryption='scram-sha-256'")
            conn.execute(sql.SQL("alter role {} login password {}").format(sql.Identifier(mail.MAIL_ROLE), sql.Literal(PASSWORD)))
        yield config, name, make_conninfo(**(config | {"dbname": name, "user": mail.MAIL_ROLE, "password": PASSWORD}))
    finally:
        with local._connect(config, autocommit=True) as conn:
            conn.execute(sql.SQL("drop database {} with (force)").format(sql.Identifier(name)))
            conn.execute(sql.SQL("drop role {}").format(sql.Identifier(mail.MAIL_ROLE)))
            conn.execute(sql.SQL("drop role {}").format(sql.Identifier(mail.MAIL_GROUP)))


@pytest.fixture
def queue(pg_database):
    config, name, dsn = pg_database
    old_id, new_id = uuid4(), uuid4()
    with local._connect(config, name, autocommit=True) as conn:
        for ident, created in ((old_id, CUTOFF - 1), (new_id, CUTOFF)):
            conn.execute("insert into toolbako_runtime.email_outbox(id,recipient,sender,subject,body,created_at,available_at) values(%s,'synthetic@example.invalid','sender@example.invalid','synthetic','never externally sent',to_timestamp(%s),now()-interval '1 second')", (ident, created))
    try:
        yield old_id, new_id, dsn
    finally:
        with local._connect(config, name, autocommit=True) as conn:
            conn.execute("delete from toolbako_runtime.email_outbox where id=any(%s::uuid[])", ([old_id, new_id],))


def test_local_pg_policy_and_effective_column_privileges(pg_database):
    config, name, dsn = pg_database
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert conn.execute("select current_user,session_user").fetchone() == (mail.MAIL_ROLE, mail.MAIL_ROLE)
        assert conn.execute("select has_database_privilege(current_database(),'TEMP'),has_database_privilege(current_database(),'CREATE')").fetchone() == (False, False)
        rows = conn.execute("select c.relname,a.attname,v.verb,has_column_privilege(c.oid,a.attnum,v.verb),has_column_privilege(c.oid,a.attnum,v.verb||' WITH GRANT OPTION') from pg_class c join pg_namespace n on n.oid=c.relnamespace join pg_attribute a on a.attrelid=c.oid cross join(values('SELECT'),('INSERT'),('UPDATE'),('REFERENCES'))v(verb) where n.nspname='toolbako_runtime' and c.relkind='r' and a.attnum>0 and not a.attisdropped").fetchall()
        for table, column, verb, enabled, grantable in rows:
            assert enabled is (column in mail.PERMISSIONS.get(table, {}).get(verb, "").split())
            assert grantable is False
        assert conn.execute("select count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace where n.nspname='toolbako_runtime' and c.relkind='r' and relrowsecurity").fetchone() == (9,)
        policies = conn.execute("select tablename,policyname,cmd,roles,qual,with_check from pg_policies where schemaname='toolbako_runtime'").fetchall()
        assert len(policies) == 4
        assert {row[1] for row in policies} == {key for entries in PLAN.policy_contract().values() for key in entries}
        assert all(row[3] == [mail.MAIL_GROUP] for row in policies)
        assert all(str(CUTOFF) in row[4] for row in policies if row[0] == "email_outbox")
        assert conn.execute("select version from toolbako_runtime.schema_version where singleton").fetchone() == (2,)


def test_old_queue_hidden_and_cannot_be_updated_even_with_explicit_id(queue):
    old_id, new_id, dsn = queue
    with psycopg.connect(dsn, autocommit=True) as conn:
        assert conn.execute("select id from toolbako_runtime.email_outbox order by id").fetchall() == [(new_id,)]
        assert conn.execute("update toolbako_runtime.email_outbox set status='sending' where id=%s", (old_id,)).rowcount == 0
        assert conn.execute("update toolbako_runtime.email_outbox set status='sending' where id=%s", (new_id,)).rowcount == 1
        assert conn.execute("update toolbako_runtime.email_outbox set status='sent' where id=%s", (new_id,)).rowcount == 1
        assert conn.execute("update toolbako_runtime.email_outbox set status='pending' where id=%s", (new_id,)).rowcount == 0


def test_scope_binding_visible_but_financial_intents_hidden_and_unmodifiable(pg_database):
    config, name, dsn = pg_database
    with local._connect(config, name, autocommit=True) as conn:
        conn.execute("insert into toolbako_runtime.stripe_operations(operation_id,endpoint,request_hash,status) values('synthetic-mail-hidden-intent','v1/payment_intents','x','pending')")
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            assert conn.execute("select operation_id,endpoint,request_hash,status,response from toolbako_runtime.stripe_operations").fetchall() == [(SCOPE_OPERATION_ID, SCOPE_ENDPOINT, content_hash(mail.SCOPE.payload()), "succeeded", mail.SCOPE.payload())]
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("update toolbako_runtime.stripe_operations set status='unknown',response=null")
    finally:
        with local._connect(config, name, autocommit=True) as conn:
            conn.execute("delete from toolbako_runtime.stripe_operations where operation_id='synthetic-mail-hidden-intent'")


def test_worker_progress_rights_cannot_change_content_or_other_tables(queue):
    _, _, dsn = queue
    forbidden = [
        "select payload from toolbako_runtime.marketplace_state",
        "select * from toolbako_runtime.audit_events",
        "select * from toolbako_runtime.finance_receipts",
        "select * from toolbako_runtime.finance_payouts",
        "select * from toolbako_runtime.finance_allocations",
        "select * from toolbako_runtime.finance_entries",
        "update toolbako_runtime.email_outbox set body='changed'",
        "update toolbako_runtime.email_outbox set recipient='different@example.invalid'",
        "update toolbako_runtime.email_outbox set created_at=now()",
        "insert into toolbako_runtime.email_outbox(id,recipient,sender,subject,body) values(gen_random_uuid(),'x','y','z','w')",
        "delete from toolbako_runtime.email_outbox", "truncate toolbako_runtime.email_outbox",
        "update toolbako_runtime.schema_version set version=99",
        "create schema forbidden", "create temporary table forbidden(id int)",
        "alter table toolbako_runtime.email_outbox disable row level security", "set role postgres",
    ]
    with psycopg.connect(dsn, autocommit=True) as conn:
        for statement in forbidden:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)


def test_existing_worker_can_claim_new_only_with_fake_sender_and_same_idempotency_key(queue):
    old_id, new_id, dsn = queue
    calls = []
    class FakeSender:
        def __init__(self, key, sender):
            assert key == "synthetic-provider" and sender == "sender@example.invalid"
        async def send(self, recipient, subject, body, *, idempotency_key):
            calls.append((recipient, subject, body, idempotency_key))
            return True
    async def scenario():
        backend = PostgresStateStore(dsn)
        result = await deliver_pending(backend, "synthetic-provider", message_ids=[old_id, new_id], sender_factory=FakeSender)
        replay = await deliver_pending(backend, "synthetic-provider", message_ids=[new_id], sender_factory=FakeSender)
        await backend.close()
        return result, replay
    result, replay = run(scenario())
    assert result == {"sent": 1, "retry": 0, "review": 0}
    assert replay == {"sent": 0, "retry": 0, "review": 0}
    assert len(calls) == 1 and calls[0][3] == "toolbako-mail-" + str(new_id)


def test_uncertain_delivery_keeps_same_uuid_and_resend_key_on_retry(queue, pg_database):
    old_id, new_id, dsn = queue
    config, name, _ = pg_database
    calls = []
    class UncertainSender:
        def __init__(self, *args):
            pass
        async def send(self, *args, idempotency_key):
            calls.append(idempotency_key)
            if len(calls) == 1:
                # Synthetic acceptance with a lost response. No external send.
                raise TimeoutError("synthetic provider response lost")
            return True
    async def deliver():
        return await deliver_pending(PostgresStateStore(dsn), "synthetic", message_ids=[new_id], sender_factory=UncertainSender)
    assert run(deliver()) == {"sent": 0, "retry": 1, "review": 0}
    with local._connect(config, name, autocommit=True) as conn:
        assert conn.execute("select status,attempts,first_attempt_at is not null,lease_until is null from toolbako_runtime.email_outbox where id=%s", (new_id,)).fetchone() == ("pending", 1, True, True)
        # Simulate the existing five-minute retry wait in this disposable DB.
        conn.execute("update toolbako_runtime.email_outbox set available_at=now()-interval '1 second' where id=%s", (new_id,))
    assert run(deliver()) == {"sent": 1, "retry": 0, "review": 0}
    assert calls == ["toolbako-mail-" + str(new_id)] * 2
    with local._connect(config, name, autocommit=True) as conn:
        assert conn.execute("select status,attempts from toolbako_runtime.email_outbox where id=%s", (old_id,)).fetchone() == ("pending", 0)


def test_grants_stop_on_existing_active_role_without_changing_policies(pg_database):
    config, name, _ = pg_database
    with local._connect(config, name, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="live_mail_grants_precondition_failed"):
            conn.execute(PLAN.grants_sql())
        conn.rollback()
        assert conn.execute("select count(*) from pg_policies where schemaname='toolbako_runtime'").fetchone() == (4,)


@pytest.mark.parametrize("change", ["mode", "platform_account", "ledger_id", "request_hash"])
def test_wrong_binding_stops_before_any_new_grant_even_with_role_disabled(pg_database, change):
    config, name, _ = pg_database
    with local._connect(config, name, autocommit=True) as conn:
        conn.execute(sql.SQL("alter role {} nologin").format(sql.Identifier(mail.MAIL_ROLE)))
        altered = {**mail.SCOPE.payload(), change: "wrong"} if change != "request_hash" else mail.SCOPE.payload()
        altered_hash = "wrong" if change == "request_hash" else content_hash(altered)
        try:
            conn.execute("update toolbako_runtime.stripe_operations set request_hash=%s,response=%s::jsonb where operation_id=%s",
                         (altered_hash, json.dumps(altered), SCOPE_OPERATION_ID))
            with pytest.raises(psycopg.errors.RaiseException, match="live_mail_grants_precondition_failed"):
                conn.execute(PLAN.grants_sql())
            conn.rollback()
            assert conn.execute("select count(*) from pg_policies where schemaname='toolbako_runtime'").fetchone() == (4,)
        finally:
            conn.execute("update toolbako_runtime.stripe_operations set request_hash=%s,response=%s::jsonb where operation_id=%s",
                         (content_hash(mail.SCOPE.payload()), json.dumps(mail.SCOPE.payload()), SCOPE_OPERATION_ID))
            conn.execute(sql.SQL("alter role {} login").format(sql.Identifier(mail.MAIL_ROLE)))
