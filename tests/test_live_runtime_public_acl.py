"""Execute the operator's exact ACL delta in disposable numeric-loopback DBs."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

from app import live_dataset_import as local
from app.database import content_hash, snapshot
from app.data import DemoStore

ROOT = Path(__file__).resolve().parents[1]
SCOPE = {"mode": "live", "platform_account": "acct_1Ty4WnIogL39waUY", "ledger_id": "667c8593-cee4-4bec-94ed-bbbd95600867"}
SCRIPT = (ROOT / "deploy/runtime-role/restrict-reviewed-public.sql").read_text()
MANAGED_NAMES = ("anon", "authenticated", "service_role")


@pytest.fixture(scope="module")
def local_admin():
    dsn = os.environ.get("TEST_POSTGRES_ADMIN_DSN")
    if not dsn:
        pytest.skip("Local disposable PostgreSQL required")
    config = local.local_config(dsn)
    owned = []
    with local._connect(config, autocommit=True) as conn:
        for name in MANAGED_NAMES:
            if not conn.execute("select 1 from pg_roles where rolname=%s", (name,)).fetchone():
                conn.execute(sql.SQL("create role {} nologin").format(sql.Identifier(name)))
                owned.append(name)
    try:
        yield config
    finally:
        with local._connect(config, autocommit=True) as conn:
            for name in reversed(owned):
                conn.execute(sql.SQL("drop role {}").format(sql.Identifier(name)))


@pytest.fixture
def isolated(local_admin):
    name = "toolbako_import_" + uuid4().hex
    with local._connect(local_admin, autocommit=True) as conn:
        conn.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    try:
        with local._connect(local_admin, name, autocommit=True) as conn:
            conn.execute((ROOT / "app/runtime_schema.sql").read_text())
            conn.execute("insert into toolbako_runtime.marketplace_state(singleton,payload) values(true,%s::jsonb)", (json.dumps(snapshot(DemoStore(seed=False))),))
            conn.execute("insert into toolbako_runtime.stripe_operations(operation_id,endpoint,request_hash,status,response) values('toolbako-financial-scope-v1','internal/financial-scope-v1',%s,'succeeded',%s::jsonb)", (content_hash(SCOPE), json.dumps(SCOPE)))
            conn.execute("create function public.rls_auto_enable() returns event_trigger language plpgsql security definer as 'begin return; end;'")
            conn.execute("grant execute on function public.rls_auto_enable() to anon,authenticated,service_role")
            conn.execute("create event trigger synthetic_rls_event on ddl_command_end when tag in ('CREATE TABLE') execute function public.rls_auto_enable()")
            conn.execute("create schema opaque; revoke all on schema opaque from public; create table opaque.extension_fixture(id int); grant select on opaque.extension_fixture to public")
        yield name, local_admin
    finally:
        with local._connect(local_admin, autocommit=True) as conn:
            conn.execute(sql.SQL("drop database {} with(force)").format(sql.Identifier(name)))


@contextmanager
def admin(isolated):
    name, config = isolated
    with local._connect(config, name, autocommit=True) as conn:
        yield conn


def disposable_script(isolated):
    # Only test substitution: production requires database postgres, whereas
    # tests must not touch that pre-existing database. No runtime bypass exists.
    name = isolated[0]
    assert name.startswith("toolbako_import_") and len(name) == 48
    assert SCRIPT.count("current_database() <> 'postgres'") == 1
    assert SCRIPT.count("revoke temporary on database postgres from public") == 1
    return SCRIPT.replace("current_database() <> 'postgres'", "current_database() <> '" + name + "'").replace(
        "revoke temporary on database postgres from public", "revoke temporary on database " + name + " from public")


def facts(conn):
    return conn.execute("""select
      (select jsonb_agg(to_jsonb(s) order by singleton) from toolbako_runtime.marketplace_state s),
      (select jsonb_agg(to_jsonb(s) order by operation_id) from toolbako_runtime.stripe_operations s),
      (select relacl::text from pg_class where oid='opaque.extension_fixture'::regclass),
      (select to_jsonb(e) from pg_event_trigger e where evtname='synthetic_rls_event'),
      (select jsonb_agg(jsonb_build_array(a.grantee,a.grantor,a.privilege_type,a.is_grantable) order by a.grantee)
        from pg_proc p,lateral aclexplode(p.proacl) a where p.oid='public.rls_auto_enable()'::regprocedure and a.grantee<>0)
    """).fetchone()


def public_acl(conn):
    return conn.execute("""select
      (select datacl::text from pg_database where datname=current_database()),
      (select proacl::text from pg_proc where oid='public.rls_auto_enable()'::regprocedure)
    """).fetchone()


def test_exact_public_delta_preserves_explicit_grants_data_extension_and_event_trigger(isolated):
    with admin(isolated) as conn:
        before = facts(conn)
        conn.execute(disposable_script(isolated))
        assert facts(conn) == before
        assert conn.execute("""select
          exists(select 1 from pg_database d,lateral aclexplode(d.datacl) a where d.datname=current_database() and a.grantee=0 and a.privilege_type='TEMPORARY'),
          exists(select 1 from pg_proc p,lateral aclexplode(p.proacl) a where p.oid='public.rls_auto_enable()'::regprocedure and a.grantee=0)
        """).fetchone() == (False, False)
        after = public_acl(conn)
        with pytest.raises(psycopg.errors.RaiseException, match="reviewed_public_acl_expectation_mismatch"):
            conn.execute(disposable_script(isolated))
        conn.execute("rollback")
        assert public_acl(conn) == after  # Unknown outcomes need readback, not replay.


@pytest.mark.parametrize("fault", ["wrong_database", "wrong_ledger", "wrong_hash", "function_invoker", "extra_public_create", "missing_explicit_grant", "grant_option"])
def test_changed_identity_or_acl_refuses_all_deltas(isolated, fault):
    with admin(isolated) as conn:
        statements = {
            "wrong_ledger": "update toolbako_runtime.stripe_operations set response=jsonb_set(response,'{ledger_id}','\"00000000-0000-0000-0000-000000000001\"')",
            "wrong_hash": "update toolbako_runtime.stripe_operations set request_hash='not-the-reviewed-binding'",
            "function_invoker": "alter function public.rls_auto_enable() security invoker",
            "extra_public_create": sql.SQL("grant create on database {} to public").format(sql.Identifier(isolated[0])),
            "missing_explicit_grant": "revoke execute on function public.rls_auto_enable() from anon",
            "grant_option": "grant execute on function public.rls_auto_enable() to anon with grant option",
        }
        if fault in statements:
            conn.execute(statements[fault])
        before, acl = facts(conn), public_acl(conn)
        with pytest.raises(psycopg.Error):
            conn.execute(SCRIPT if fault == "wrong_database" else disposable_script(isolated))
        conn.execute("rollback")
        assert facts(conn) == before and public_acl(conn) == acl


def test_transaction_abort_after_first_revoke_restores_both_acls(isolated):
    with admin(isolated) as conn:
        before, acl = facts(conn), public_acl(conn)
        # Synthetic transport-independent fault BETWEEN the two real changes.
        script = disposable_script(isolated).replace(
            "  revoke execute on function public.rls_auto_enable() from public;",
            "  raise exception 'synthetic_after_first_revoke';\n  revoke execute on function public.rls_auto_enable() from public;")
        with pytest.raises(psycopg.errors.RaiseException, match="synthetic_after_first_revoke"):
            conn.execute(script)
        conn.execute("rollback")
        assert facts(conn) == before and public_acl(conn) == acl
