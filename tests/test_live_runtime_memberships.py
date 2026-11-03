"""Exact PG16+ creator-administration graph; synthetic local PG only."""
import os
from uuid import uuid4

from psycopg import sql
import pytest

from app import live_dataset_import as local
from app.live_runtime_role import (
    RUNTIME_GROUP, RUNTIME_ROLE, RuntimeRoleError, _membership_shape,
)


def graph():
    return [(RUNTIME_GROUP, RUNTIME_ROLE, False, True, True, 16384),
            (RUNTIME_GROUP, "postgres", True, False, False, 10),
            (RUNTIME_ROLE, "postgres", True, False, False, 10)]


@pytest.mark.parametrize("version", [160000, 170011, 180000])
def test_non_superuser_creator_admin_edges_are_not_application_privileges(version):
    _membership_shape(graph(), (16384, False, True), version)


def test_superuser_creation_has_only_explicit_runtime_membership():
    _membership_shape([(RUNTIME_GROUP, RUNTIME_ROLE, False, True, True, 10)], (10, True, True), 170011)


def test_pre_pg16_membership_remains_exact():
    _membership_shape([(RUNTIME_GROUP, RUNTIME_ROLE, False)], (16384, False, True), 150000)


@pytest.mark.parametrize("fault", [
    "missing_creator", "extra_role", "reverse_membership", "runtime_admin",
    "runtime_noinherit", "runtime_noset", "creator_inherit", "creator_set",
    "creator_noadmin", "other_creator", "wrong_auto_grantor", "wrong_runtime_grantor",
    "duplicate_edge", "wrong_creator_attributes", "superuser_with_creator_edges",
])
def test_unreviewed_membership_or_privilege_never_passes(fault):
    rows, creator = graph(), (16384, False, True)
    if fault == "missing_creator": rows.pop()
    elif fault == "extra_role": rows.append(("extra", RUNTIME_ROLE, False, True, True, 16384))
    elif fault == "reverse_membership": rows.append(("postgres", RUNTIME_ROLE, False, True, True, 10))
    elif fault == "runtime_admin": rows[0] = (*rows[0][:2], True, *rows[0][3:])
    elif fault == "runtime_noinherit": rows[0] = (*rows[0][:3], False, *rows[0][4:])
    elif fault == "runtime_noset": rows[0] = (*rows[0][:4], False, rows[0][5])
    elif fault == "creator_inherit": rows[1] = (*rows[1][:3], True, *rows[1][4:])
    elif fault == "creator_set": rows[1] = (*rows[1][:4], True, rows[1][5])
    elif fault == "creator_noadmin": rows[1] = (*rows[1][:2], False, *rows[1][3:])
    elif fault == "other_creator": rows[1] = (rows[1][0], "other_admin", *rows[1][2:])
    elif fault == "wrong_auto_grantor": rows[1] = (*rows[1][:5], 16384)
    elif fault == "wrong_runtime_grantor": rows[0] = (*rows[0][:5], 10)
    elif fault == "duplicate_edge": rows.append(rows[0])
    elif fault == "wrong_creator_attributes": creator = (16384, False, False)
    elif fault == "superuser_with_creator_edges": creator = (16384, True, True)
    with pytest.raises(RuntimeRoleError):
        _membership_shape(rows, creator, 170011)


def test_real_nonsuperuser_pg16_creator_membership_is_verified_and_rolled_back():
    dsn = os.environ.get("TEST_POSTGRES_ADMIN_DSN")
    if not dsn:
        pytest.skip("Local disposable PostgreSQL required")
    config = local.local_config(dsn)
    prefix = "synthetic_membership_" + uuid4().hex[:12]
    creator, group, member = (prefix + "_" + suffix for suffix in ("creator", "group", "member"))
    with local._connect(config, autocommit=True) as conn:
        if conn.info.server_version < 160000:
            pytest.skip("Automatic CREATEROLE administration starts with PG16")
        with conn.transaction(force_rollback=True):
            conn.execute(sql.SQL("create role {} nologin createrole noinherit").format(sql.Identifier(creator)))
            attributes = conn.execute("select oid,rolsuper,rolcreaterole from pg_roles where rolname=%s", (creator,)).fetchone()
            conn.execute(sql.SQL("set local role {}").format(sql.Identifier(creator)))
            conn.execute("set local createrole_self_grant=''")
            for role, inherit in ((group, "noinherit"), (member, "inherit")):
                conn.execute(sql.SQL("create role {} nologin {} nosuperuser nocreatedb nocreaterole noreplication nobypassrls password null").format(sql.Identifier(role), sql.SQL(inherit)))
            conn.execute(sql.SQL("grant {} to {}").format(sql.Identifier(group), sql.Identifier(member)))
            rows = conn.execute("select p.rolname,c.rolname,m.admin_option,m.inherit_option,m.set_option,m.grantor "
                "from pg_auth_members m join pg_roles p on p.oid=m.roleid join pg_roles c on c.oid=m.member "
                "where p.rolname=any(%s) or c.rolname=any(%s)", ([group, member], [group, member])).fetchall()
            names = {creator: "postgres", group: RUNTIME_GROUP, member: RUNTIME_ROLE}
            normalized = [(names[parent], names[child], *flags) for parent, child, *flags in rows]
            _membership_shape(normalized, attributes, conn.info.server_version)
            assert conn.execute("select pg_has_role(%s,%s,'USAGE'),pg_has_role(%s,%s,'SET')", (creator, group, creator, group)).fetchone() == (False, False)
            assert conn.execute("select pg_has_role(%s,%s,'USAGE'),pg_has_role(%s,%s,'SET')", (member, group, member, group)).fetchone() == (True, True)
        assert conn.execute("select count(*) from pg_roles where rolname=any(%s)", ([creator, group, member],)).fetchone() == (0,)
