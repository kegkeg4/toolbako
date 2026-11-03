"""Synthetic Web + mail role contract; never connects to the live database."""
import json

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest

from app import live_dataset_import as local
from app.database import content_hash
from app.financial_scope import SCOPE_OPERATION_ID
from app.live_mail_preparation import MAIL_GROUP, MAIL_ROLE, PreparedMailAccess, SCOPE
from app.live_mail_runtime import verify_mail_access
from app.live_runtime_role import RuntimeRoleError, verify_runtime_role
from test_live_runtime_role import admin_config, administrator, database, run


CUTOFF = 1793700000
PLAN = PreparedMailAccess(CUTOFF, "0" * 64)


@pytest.fixture(scope="module")
def mail_roles(admin_config):
    with local._connect(admin_config, autocommit=True) as conn:
        assert not conn.execute("select 1 from pg_roles where rolname in (%s,%s)",
                                (MAIL_ROLE, MAIL_GROUP)).fetchall()
        conn.execute(PLAN.roles_sql())
    try:
        yield
    finally:
        # All fixture databases are dropped before these module-owned roles.
        with local._connect(admin_config, autocommit=True) as conn:
            conn.execute(sql.SQL("drop role {}").format(sql.Identifier(MAIL_ROLE)))
            conn.execute(sql.SQL("drop role {}").format(sql.Identifier(MAIL_GROUP)))


@pytest.fixture
def mail_database(database, mail_roles):
    with administrator(database) as conn:
        conn.execute("update toolbako_runtime.stripe_operations set request_hash=%s,response=%s::jsonb "
                     "where operation_id=%s", (content_hash(SCOPE.payload()), json.dumps(SCOPE.payload()), SCOPE_OPERATION_ID))
        conn.execute(PLAN.grants_sql())
        conn.execute(sql.SQL("alter role {} login password 'synthetic-mail-fixture-only'").format(sql.Identifier(MAIL_ROLE)))
    try:
        yield database
    finally:
        with administrator(database) as conn:
            conn.execute(sql.SQL("alter role {} nologin password null").format(sql.Identifier(MAIL_ROLE)))


async def verdict(database, **kwargs):
    async with await psycopg.AsyncConnection.connect(make_conninfo(**database[1]), autocommit=True) as conn:
        await conn.execute("set default_transaction_read_only=on")
        return await verify_runtime_role(conn, **kwargs)


def test_explicit_cutoff_accepts_only_exact_web_and_mail_contract(mail_database):
    try:
        assert run(verdict(mail_database, mail_queue_since=CUTOFF)) is True
    except RuntimeRoleError as error:
        if error.__context__:
            raise error.__context__
        raise
    with administrator(mail_database) as conn:
        assert conn.execute("select count(*) from pg_policy p join pg_class c on c.oid=p.polrelid "
                            "join pg_namespace n on n.oid=c.relnamespace where n.nspname='toolbako_runtime'").fetchone() == (23,)


@pytest.mark.parametrize("kwargs", [{}, {"mail_queue_since": 0}, {"mail_queue_since": CUTOFF - 1}, {"mail_queue_since": CUTOFF + 1}])
def test_mail_policies_require_same_explicit_cutoff(mail_database, kwargs):
    with pytest.raises(RuntimeRoleError, match="^live_runtime_role_unverified$"):
        run(verdict(mail_database, **kwargs))


def test_positive_mail_cutoff_does_not_accept_original_web_only_contract(database):
    assert run(verdict(database)) is True
    with pytest.raises(RuntimeRoleError):
        run(verdict(database, mail_queue_since=CUTOFF))


@pytest.mark.parametrize("value", [True, False, None, -1, 4102444801, 1.0, "1793700000", "0"])
def test_invalid_optional_cutoff_is_rejected_before_sql(value):
    class Never:
        async def execute(self, *args, **kwargs):
            pytest.fail("Invalid cutoff must not query or mutate any database")
    with pytest.raises(RuntimeRoleError, match="^live_runtime_role_unverified$"):
        run(verify_runtime_role(Never(), mail_queue_since=value))


@pytest.mark.parametrize("change", [
    "alter policy toolbako_mail_live_select on toolbako_runtime.email_outbox using(true)",
    "alter policy toolbako_mail_live_progress on toolbako_runtime.email_outbox with check(true)",
    "alter policy toolbako_mail_live_binding on toolbako_runtime.stripe_operations using(true)",
    "alter policy toolbako_mail_live_schema on toolbako_runtime.schema_version using(singleton)",
    "alter policy toolbako_mail_live_select on toolbako_runtime.email_outbox to public",
    "alter policy toolbako_mail_live_select on toolbako_runtime.email_outbox to toolbako_runtime_web",
    "drop policy toolbako_mail_live_progress on toolbako_runtime.email_outbox",
    "create policy unknown on toolbako_runtime.email_outbox for select to toolbako_runtime_mail_live using(true)",
    "grant select(payload) on toolbako_runtime.marketplace_state to toolbako_runtime_mail_live",
    "grant update(response) on toolbako_runtime.stripe_operations to toolbako_runtime_mail_live",
    "grant update(body) on toolbako_runtime.email_outbox to toolbako_runtime_mail_live",
    "grant select on toolbako_runtime.email_outbox to toolbako_runtime_mail_live",
    "grant select(body) on toolbako_runtime.email_outbox to toolbako_runtime_mail_live with grant option",
    "revoke update(status) on toolbako_runtime.email_outbox from toolbako_runtime_mail_live",
    "grant create on schema public to toolbako_runtime_mail_live",
    "create table public.leak(secret text); grant select(secret) on public.leak to toolbako_runtime_mail_live",
    "create sequence public.leak; grant usage on sequence public.leak to toolbako_runtime_mail_live",
    "create function public.leak() returns int language sql security definer as 'select 1'; revoke all on function public.leak() from public; grant execute on function public.leak() to toolbako_runtime_mail_live",
])
def test_optional_mail_contract_rejects_missing_broad_or_unrelated_rights(mail_database, change):
    with administrator(mail_database) as conn:
        conn.execute(change)
    with pytest.raises(RuntimeRoleError, match="^live_runtime_role_unverified$"):
        run(verdict(mail_database, mail_queue_since=CUTOFF))


@pytest.mark.parametrize("change,undo", [
    ("alter role toolbako_mail_live_v1 nologin", "alter role toolbako_mail_live_v1 login"),
    ("alter role toolbako_mail_live_v1 bypassrls", "alter role toolbako_mail_live_v1 nobypassrls"),
    ("alter role toolbako_mail_live_v1 createdb", "alter role toolbako_mail_live_v1 nocreatedb"),
    ("alter role toolbako_mail_live_v1 set search_path=public", "alter role toolbako_mail_live_v1 reset search_path"),
    ("grant pg_read_all_data to toolbako_mail_live_v1", "revoke pg_read_all_data from toolbako_mail_live_v1"),
    ("grant toolbako_runtime_mail_live to toolbako_web_live_v1", "revoke toolbako_runtime_mail_live from toolbako_web_live_v1"),
    ("grant toolbako_runtime_web to toolbako_mail_live_v1", "revoke toolbako_runtime_web from toolbako_mail_live_v1"),
    ("grant toolbako_runtime_mail_live to toolbako_mail_live_v1 with admin option", "revoke admin option for toolbako_runtime_mail_live from toolbako_mail_live_v1"),
])
def test_optional_mail_contract_never_adopts_different_role_graph(mail_database, change, undo):
    with administrator(mail_database) as conn:
        conn.execute(change)
    try:
        with pytest.raises(RuntimeRoleError, match="^live_runtime_role_unverified$"):
            run(verdict(mail_database, mail_queue_since=CUTOFF))
    finally:
        with administrator(mail_database) as conn:
            conn.execute(undo)


def test_optional_mail_contract_does_not_give_web_mail_contents_or_claim_permission(mail_database):
    assert run(verdict(mail_database, mail_queue_since=CUTOFF)) is True
    with psycopg.connect(make_conninfo(**mail_database[1]), autocommit=True) as conn:
        for statement in ("select body from toolbako_runtime.email_outbox",
                          "update toolbako_runtime.email_outbox set status='sending'",
                          "set role toolbako_mail_live_v1"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)


def test_actual_mail_identity_verifies_and_reads_only_scope_schema(mail_database):
    async def scenario():
        mail = {**mail_database[1], "user": MAIL_ROLE, "password": "synthetic-mail-fixture-only"}
        async with await psycopg.AsyncConnection.connect(make_conninfo(**mail), autocommit=True) as conn:
            await conn.execute("set default_transaction_read_only=on")
            assert await (await conn.execute("select current_user,session_user")).fetchone() == (MAIL_ROLE, MAIL_ROLE)
            assert await verify_mail_access(conn, queue_since=CUTOFF) is True
            assert await (await conn.execute("select singleton,version from toolbako_runtime.schema_version")).fetchall() == [(True, 2)]
            rows = await (await conn.execute("select operation_id,response from toolbako_runtime.stripe_operations")).fetchall()
            assert rows == [(SCOPE_OPERATION_ID, SCOPE.payload())]
            for statement in ("select payload from toolbako_runtime.marketplace_state",
                              "select amount from toolbako_runtime.finance_receipts"):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    await conn.execute(statement)
    run(scenario())
