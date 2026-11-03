"""Synthetic local-PG role checks. No production project/credential is used."""
import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest

from app import live_dataset_import as local
from app.data import DemoStore
from app.database import PostgresStateStore, StorageUnavailable, content_hash, snapshot
from app.financial_scope import FinancialScope, SCOPE_ENDPOINT, SCOPE_OPERATION_ID
from app.live_database_config import RUNTIME_ROLE
from app.live_runtime_role import RUNTIME_GROUP, RuntimeRoleError, verify_runtime_role

ROOT = Path(__file__).resolve().parents[1]
SCOPE = FinancialScope("live", "acct_1Ty4WnIogL39waUY", "56387c08-fdb4-4dd7-b63f-aeae34b6de54")
SYNTHETIC_ROLE_PASSWORD = "synthetic-runtime-role-only"


def run(coro):
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def admin_config():
    dsn = os.environ.get("TEST_POSTGRES_ADMIN_DSN")
    if not dsn:
        pytest.skip("Local disposable PostgreSQL required")
    config = local.local_config(dsn)
    with local._connect(config, autocommit=True) as conn:
        # Never adopt/delete a pre-existing role. The suite owns both new names.
        assert not conn.execute("select 1 from pg_roles where rolname in (%s,%s)", (RUNTIME_ROLE, RUNTIME_GROUP)).fetchall()
        conn.execute((ROOT / "deploy/runtime-role/roles.sql").read_text())
        # Local Windows uses trust while CI uses host SCRAM. Give only this
        # disposable role a known synthetic password; production roles.sql
        # deliberately keeps NOLOGIN/PASSWORD NULL for the operator handoff.
        conn.execute("set password_encryption='scram-sha-256'")
        conn.execute(sql.SQL("alter role {} login password {}").format(
            sql.Identifier(RUNTIME_ROLE), sql.Literal(SYNTHETIC_ROLE_PASSWORD)))
        assert conn.execute("select rolpassword like 'SCRAM-SHA-256$%%' from pg_authid where rolname=%s",
                            (RUNTIME_ROLE,)).fetchone() == (True,)
    try:
        yield config
    finally:
        with local._connect(config, autocommit=True) as conn:
            conn.execute(sql.SQL("drop role {}").format(sql.Identifier(RUNTIME_ROLE)))
            conn.execute(sql.SQL("drop role {}").format(sql.Identifier(RUNTIME_GROUP)))


@pytest.fixture
def database(admin_config):
    name = "toolbako_import_" + uuid4().hex
    with local._connect(admin_config, autocommit=True) as conn:
        conn.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    try:
        with local._connect(admin_config, name) as conn:
            conn.execute((ROOT / "app/runtime_schema.sql").read_text())
            state = snapshot(DemoStore(seed=False))
            conn.execute("insert into toolbako_runtime.marketplace_state(singleton,payload) values(true,%s::jsonb)", (json.dumps(state),))
            conn.execute("insert into toolbako_runtime.stripe_operations(operation_id,endpoint,request_hash,status,response) values(%s,%s,%s,'succeeded',%s::jsonb)",
                         (SCOPE_OPERATION_ID, SCOPE_ENDPOINT, content_hash(SCOPE.payload()), json.dumps(SCOPE.payload())))
            # Only this newly created throwaway DB: simulate a reviewed fix of
            # default PUBLIC TEMP. Production SQL itself does not revoke it.
            conn.execute(sql.SQL("revoke temporary on database {} from public").format(sql.Identifier(name)))
            conn.execute((ROOT / "deploy/runtime-role/grants.sql").read_text())
        config = {**admin_config, "dbname": name, "user": RUNTIME_ROLE, "password": SYNTHETIC_ROLE_PASSWORD}
        yield name, config, admin_config
    finally:
        with local._connect(admin_config, autocommit=True) as conn:
            conn.execute(sql.SQL("drop database {} with (force)").format(sql.Identifier(name)))


@contextmanager
def administrator(database):
    name, _, config = database
    with local._connect(config, name, autocommit=True) as conn:
        yield conn


async def verdict(database):
    async with await psycopg.AsyncConnection.connect(make_conninfo(**database[1]), autocommit=True) as conn:
        await conn.execute("set default_transaction_read_only=on")
        return await verify_runtime_role(conn)


def test_exact_effective_role_and_rls_permissions(database):
    try:
        assert run(verdict(database)) is True
    except RuntimeRoleError as error:
        # Only synthetic local SQL during testing; production keeps fixed errors.
        if error.__context__:
            raise error.__context__
        raise


def test_precreation_inventory_returns_only_fixed_booleans_and_counts(database):
    with administrator(database) as conn:
        with conn.transaction():
            conn.execute("set transaction read only")
            result = conn.execute((ROOT / "deploy/runtime-role/inventory.sql").read_text()).fetchone()[0]
            detail = conn.execute((ROOT / "deploy/runtime-role/public-excess-inventory.sql").read_text()).fetchone()[0]
    assert result["runtime_rls_enabled_count"] == result["runtime_base_table_count"] == 9
    assert result["public_database_temp"] is False
    assert result["public_callable_definer_count"] == result["public_sequence_grant_count"] == 0
    assert all(type(value) in (bool, int) for value in result.values())
    assert detail == {"database_public_privileges": ["CONNECT"], "objects": []}


def test_real_backend_session_audit_journal_queue_and_finance_round_trip(database):
    from app.config import Settings
    from app.finance import request_payout
    from app.operations import connection_summary
    from test_finance import order
    async def scenario():
        backend = PostgresStateStore(make_conninfo(**database[1]))
        store = DemoStore(seed=False)
        async with backend.request(store):
            await backend.assert_financial_scope(SCOPE)
            store.account_sessions["synthetic-session"] = {"user": {"id": "buyer"}}
            store.orders.append(order())
            store.connected_accounts["seller"] = {"account_id": "acct_synthetic", "payouts_enabled": True, "details_submitted": True}
            payout = request_payout(store, Settings(), "seller", 850)
            store.audit(None, "synthetic.runtime_role", "fixture")
            backend.enqueue_email("synthetic@example.invalid", "sender@example.invalid", "synthetic", "never sent")
            await backend.begin_operation("synthetic-intent", "fixture/no-network", {"amount": 1})
            await backend.finish_operation("synthetic-intent", "unknown")
            await backend.reconcile_operation("synthetic-intent", endpoint="fixture/no-network",
                                              request_hash=content_hash({"amount": 1}), response={"id": "synthetic"})
            await backend.save()
            counts = await connection_summary(backend._connection, Settings())
            assert counts["counts"]["mail_pending"] == 1
        async with backend.request(store):
            assert store.account_sessions["synthetic-session"]["user"]["id"] == "buyer"
            assert (await backend.operation_result("synthetic-intent"))["status"] == "succeeded"
            assert store.payouts[0]["id"] == payout["id"]
            await backend.save()  # real ON CONFLICT and immutable fact readback
        await backend.close()
    run(scenario())
    with administrator(database) as conn:
        assert conn.execute("select count(*) from toolbako_runtime.finance_receipts").fetchone() == (1,)
        assert conn.execute("select count(*) from toolbako_runtime.finance_allocations").fetchone() == (1,)
        assert conn.execute("select count(*) from toolbako_runtime.audit_events").fetchone()[0] >= 1


@pytest.mark.parametrize("statement", [
    "create table toolbako_runtime.forbidden(id int)",
    "create table public.forbidden(id int)",
    "create temporary table forbidden(id int)",
    "create schema forbidden",
    "delete from toolbako_runtime.marketplace_state",
    "truncate toolbako_runtime.marketplace_state",
    "update toolbako_runtime.schema_version set version=99",
    "insert into toolbako_runtime.marketplace_state(singleton,payload) values(true,'{}')",
    "select body from toolbako_runtime.email_outbox",
    "select recipient from toolbako_runtime.email_outbox",
    "update toolbako_runtime.email_outbox set status='sending'",
    "update toolbako_runtime.audit_events set content_hash='wrong'",
    "update toolbako_runtime.finance_receipts set amount=99",
    "update toolbako_runtime.finance_allocations set amount=99",
    "update toolbako_runtime.finance_entries set amount=99",
    "update toolbako_runtime.finance_payouts set net_amount=99",
    "update toolbako_runtime.stripe_operations set request_hash='wrong'",
    "grant select on toolbako_runtime.marketplace_state to public",
    "alter table toolbako_runtime.marketplace_state disable row level security",
    "set role postgres",
])
def test_forbidden_permissions_are_rejected(database, statement):
    with psycopg.connect(make_conninfo(**database[1]), autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(statement)


def test_scope_binding_is_visible_but_cannot_be_modified_or_inserted(database):
    with psycopg.connect(make_conninfo(**database[1]), autocommit=True) as conn:
        assert conn.execute("select response from toolbako_runtime.stripe_operations where operation_id=%s", (SCOPE_OPERATION_ID,)).fetchone()[0] == SCOPE.payload()
        assert conn.execute("update toolbako_runtime.stripe_operations set status='unknown',response=null where operation_id=%s", (SCOPE_OPERATION_ID,)).rowcount == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("insert into toolbako_runtime.stripe_operations(operation_id,endpoint,request_hash,status) values('bad','internal/financial-scope-v1','x','pending')")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("insert into toolbako_runtime.stripe_operations(operation_id,endpoint,request_hash,status) values('toolbako-financial-scope-v1','otherwise-valid','x','pending') on conflict do nothing")
    with administrator(database) as conn:
        assert conn.execute("select status,response from toolbako_runtime.stripe_operations where operation_id=%s", (SCOPE_OPERATION_ID,)).fetchone() == ("succeeded", SCOPE.payload())


@pytest.mark.parametrize("change", [
    "grant select on toolbako_runtime.email_outbox to toolbako_runtime_web",
    "grant select(body) on toolbako_runtime.email_outbox to public",
    "grant update(status) on toolbako_runtime.email_outbox to toolbako_runtime_web",
    "grant select(id) on toolbako_runtime.audit_events to toolbako_runtime_web with grant option",
    "alter table toolbako_runtime.marketplace_state disable row level security",
    "drop policy toolbako_web_select on toolbako_runtime.marketplace_state",
    "alter policy toolbako_web_select on toolbako_runtime.marketplace_state using(false)",
    "create policy extra on toolbako_runtime.stripe_operations for all to public using(true) with check(true)",
    "grant create on schema public to public",
    "create table public.leak(secret text); grant select(secret) on public.leak to public",
    "create function public.leak() returns int language sql security definer as 'select 1'",
    "create sequence public.leak; grant usage on sequence public.leak to public",
])
def test_effective_excess_or_missing_rights_fail_closed(database, change):
    with administrator(database) as conn:
        conn.execute(change)
    with pytest.raises(RuntimeRoleError, match="^live_runtime_role_unverified$"):
        run(verdict(database))


def test_public_database_temp_is_effective_even_without_role_grant(database):
    with administrator(database) as conn:
        conn.execute(sql.SQL("grant temp on database {} to public").format(sql.Identifier(database[0])))
    with pytest.raises(RuntimeRoleError):
        run(verdict(database))


@pytest.mark.parametrize("definition,query", [
    ("create table opaque.leak(secret text); grant select(secret) on opaque.leak to public", "select secret from opaque.leak"),
    ("create sequence opaque.leak; grant usage on sequence opaque.leak to public", "select nextval('opaque.leak')"),
    ("create function opaque.leak() returns int language sql security definer as 'select 1'", "select opaque.leak()"),
])
def test_unusable_namespace_acl_requires_effective_schema_usage(database, definition, query):
    with administrator(database) as conn:
        conn.execute("create schema opaque; revoke all on schema opaque from public")
        conn.execute(definition)
    with psycopg.connect(make_conninfo(**database[1]), autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(query)
    assert run(verdict(database)) is True
    for grantee in (RUNTIME_GROUP, "public"):
        with administrator(database) as conn:
            conn.execute(sql.SQL("grant usage on schema opaque to {}").format(sql.Identifier(grantee)))
        try:
            with pytest.raises(RuntimeRoleError, match="^live_runtime_role_unverified$"):
                run(verdict(database))
        finally:
            with administrator(database) as conn:
                conn.execute(sql.SQL("revoke usage on schema opaque from {}").format(sql.Identifier(grantee)))
        assert run(verdict(database)) is True


def test_connection_identity_cannot_be_impersonated_by_set_role(database):
    async def scenario():
        admin = {**database[2], "dbname": database[0]}
        async with await psycopg.AsyncConnection.connect(make_conninfo(**admin), autocommit=True) as conn:
            await conn.execute(sql.SQL("set role {}").format(sql.Identifier(RUNTIME_ROLE)))
            await conn.execute("set default_transaction_read_only=on")
            with pytest.raises(RuntimeRoleError):
                await verify_runtime_role(conn)  # session_user is still admin
    run(scenario())


@pytest.mark.parametrize("apply,undo", [
    ("alter role toolbako_web_live_v1 bypassrls", "alter role toolbako_web_live_v1 nobypassrls"),
    ("alter role toolbako_web_live_v1 createdb", "alter role toolbako_web_live_v1 nocreatedb"),
    ("alter role toolbako_web_live_v1 createrole", "alter role toolbako_web_live_v1 nocreaterole"),
    ("grant pg_read_all_data to toolbako_web_live_v1", "revoke pg_read_all_data from toolbako_web_live_v1"),
    ("grant toolbako_runtime_web to toolbako_web_live_v1 with admin option", "revoke admin option for toolbako_runtime_web from toolbako_web_live_v1"),
])
def test_privileged_role_attributes_or_membership_are_not_adopted(database, apply, undo):
    with administrator(database) as conn:
        conn.execute(apply)
    try:
        with pytest.raises(RuntimeRoleError):
            run(verdict(database))
    finally:
        with administrator(database) as conn:
            conn.execute(undo)


def test_runtime_verifier_itself_requires_readonly_transaction(database):
    async def scenario():
        async with await psycopg.AsyncConnection.connect(make_conninfo(**database[1]), autocommit=True) as conn:
            with pytest.raises(RuntimeRoleError):
                await verify_runtime_role(conn)
    run(scenario())


def test_missing_snapshot_is_not_silently_seeded(database):
    with administrator(database) as conn:
        conn.execute("delete from toolbako_runtime.marketplace_state")
    async def scenario():
        backend = PostgresStateStore(make_conninfo(**database[1]))
        with pytest.raises(StorageUnavailable):
            async with backend.request(DemoStore(seed=False)):
                pytest.fail("Runtime must not initialize an empty target")
        await backend.close()
    run(scenario())
    with administrator(database) as conn:
        assert conn.execute("select count(*) from toolbako_runtime.marketplace_state").fetchone() == (0,)


def test_failed_save_rolls_back_snapshot_and_outbox(database):
    async def scenario():
        backend = PostgresStateStore(make_conninfo(**database[1]))
        store = DemoStore(seed=False)
        async with backend.request(store):
            store.account_sessions["will-not-commit"] = {"fixture": True}
            backend.enqueue_email("x@example.invalid", "x@example.invalid", "fixture", "never sent")
            # Exercise actual transactional failure after queued mail INSERT.
            store.audit_logs.append({"id": "invalid-uuid", "action": "fixture", "target": "fixture", "created_at": "invalid"})
            with pytest.raises(StorageUnavailable):
                await backend.save()
        await backend.close()
    run(scenario())
    with administrator(database) as conn:
        assert conn.execute("select count(*) from toolbako_runtime.email_outbox").fetchone() == (0,)
        assert conn.execute("select payload->'account_sessions' from toolbako_runtime.marketplace_state").fetchone() == ({},)
