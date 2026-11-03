"""Exact live mail identity/privilege verification; never creates permissions.

Only the dedicated role may open a delivery connection. Verification is repeated
before each connection is returned to the existing monitor/claim implementation.
Every exception at this boundary is fixed text, with no DSN or message content.
"""
import asyncio
import psycopg

from .financial_scope import SCOPE_OPERATION_ID, validate_binding
from .live_database_config import TARGET_HOST, TARGET_PROJECT
from .live_mail_preparation import MAIL_GROUP, MAIL_ROLE, MAIL_USER, PERMISSIONS, SCOPE, PreparedMailAccess, build_mail_database_config
from .live_runtime_role import PERMISSIONS as WEB_TABLES, _policies as web_policies


class LiveMailRuntimeError(RuntimeError):
    def __init__(self):
        super().__init__("live_mail_runtime_unverified")


def _require(ok):
    if not ok:
        raise LiveMailRuntimeError()


def normalize_mail_expression(value):
    """Known policy expressions only; do not generalize arbitrary SQL equality."""
    if value is None:
        return ""
    return "".join(value.replace("pg_catalog.", "").replace("::text", "")
                   .replace("(", "").replace(")", "").split())


def mail_policy_rows(queue_since):
    # Reuse strict input validation without claiming the zero trace is approval.
    PreparedMailAccess(queue_since, "0" * 64)
    future = f"created_at >= to_timestamp({queue_since}::double precision)"
    values = {
        "schema_version": {"toolbako_mail_live_schema": ("r", "singleton AND version = 2", None)},
        "stripe_operations": {"toolbako_mail_live_binding": ("r", "operation_id = 'toolbako-financial-scope-v1' AND endpoint = 'internal/financial-scope-v1'", None)},
        "email_outbox": {
            "toolbako_mail_live_select": ("r", future, None),
            "toolbako_mail_live_progress": ("w", future + " AND status = ANY (ARRAY['pending', 'sending'])",
                future + " AND status = ANY (ARRAY['pending', 'sending', 'sent', 'review'])"),
        },
    }
    return {table: {name: (command, True, [MAIL_GROUP], normalize_mail_expression(using), normalize_mail_expression(check))
                    for name, (command, using, check) in policies.items()} for table, policies in values.items()}


async def verify_mail_access(conn, *, queue_since, require_login=True):
    """Read-only fixed-role audit; callable from the Web verifier as well.

    SQL current_user may be Web or the operator during preparation. All effective
    privilege predicates explicitly target MAIL_ROLE. The delivery backend also
    verifies actual SQL identity and the TLS connection independently below.
    """
    try:
        _require(type(require_login) is bool)
        extra_policies = mail_policy_rows(queue_since)
        _require(await (await conn.execute("select current_setting('transaction_read_only')")).fetchone() == ("on",))
        roles = await (await conn.execute(
            "select rolname,rolsuper,rolinherit,rolcreaterole,rolcreatedb,rolcanlogin,rolreplication,rolbypassrls,rolconfig "
            "from pg_catalog.pg_roles where rolname in (%s,%s)", (MAIL_ROLE, MAIL_GROUP))).fetchall()
        _require(set(roles) == {
            (MAIL_ROLE, False, True, False, False, require_login, False, False, None),
            (MAIL_GROUP, False, False, False, False, False, False, False, None),
        })
        postgres = await (await conn.execute("select oid,rolsuper,rolcreaterole from pg_catalog.pg_roles where rolname='postgres'")).fetchone()
        _require(postgres is not None and (postgres[1] or postgres[2]))
        creator, superuser, _ = postgres
        extra = ",m.inherit_option,m.set_option,m.grantor" if conn.info.server_version >= 160000 else ""
        memberships = await (await conn.execute(
            "select parent.rolname,child.rolname,m.admin_option" + extra + " from pg_catalog.pg_auth_members m "
            "join pg_catalog.pg_roles parent on parent.oid=m.roleid join pg_catalog.pg_roles child on child.oid=m.member "
            "where parent.rolname in (%s,%s) or child.rolname in (%s,%s)",
            (MAIL_ROLE, MAIL_GROUP, MAIL_ROLE, MAIL_GROUP))).fetchall()
        expected = {(MAIL_GROUP, MAIL_ROLE, False)}
        if conn.info.server_version >= 160000:
            expected = {(MAIL_GROUP, MAIL_ROLE, False, True, True, creator)}
            if not superuser:
                _require(await (await conn.execute("select oid,rolsuper from pg_catalog.pg_roles where oid=10")).fetchone() == (10, True))
                expected.update((role, "postgres", True, False, False, 10) for role in (MAIL_ROLE, MAIL_GROUP))
        _require(len(memberships) == len(expected) and set(memberships) == expected)
        database = await (await conn.execute(
            "select has_database_privilege(%s,current_database(),'CONNECT'),has_database_privilege(%s,current_database(),'CREATE'),"
            "has_database_privilege(%s,current_database(),'TEMP'),pg_has_role(%s,d.datdba,'USAGE') "
            "from pg_catalog.pg_database d where d.datname=current_database()", (MAIL_ROLE,) * 4)).fetchone()
        _require(database == (True, False, False, False))
        schemas = await (await conn.execute(
            "select n.nspname,has_schema_privilege(%s,n.oid,'USAGE'),has_schema_privilege(%s,n.oid,'CREATE'),"
            "pg_has_role(%s,n.nspowner,'USAGE') from pg_catalog.pg_namespace n where n.nspname !~ '^pg_' and n.nspname <> 'information_schema'",
            (MAIL_ROLE,) * 3)).fetchall()
        _require(any(row[:3] == ("toolbako_runtime", True, False) for row in schemas))
        _require(all(not row[2] and not row[3] for row in schemas))
        tables = await (await conn.execute(
            "select c.relname,c.relrowsecurity,c.relkind,pg_has_role(%s,c.relowner,'USAGE'), "
            "(select jsonb_agg(jsonb_build_array(a.attname,v.verb,has_column_privilege(%s,c.oid,a.attnum,v.verb),"
            "has_column_privilege(%s,c.oid,a.attnum,v.verb||' WITH GRANT OPTION'))) "
            "from pg_catalog.pg_attribute a cross join (values ('SELECT'),('INSERT'),('UPDATE'),('REFERENCES')) v(verb) "
            "where a.attrelid=c.oid and a.attnum>0 and not a.attisdropped), "
            "(select bool_or(has_table_privilege(%s,c.oid,verb)) from "
            "(values ('SELECT'),('INSERT'),('UPDATE'),('DELETE'),('TRUNCATE'),('REFERENCES'),('TRIGGER')) v(verb)), "
            "(select jsonb_agg(jsonb_build_array(p.polname,p.polcmd,p.polpermissive,"
            "array(select r.rolname::text from pg_catalog.pg_roles r where r.oid=any(p.polroles) order by r.rolname),"
            "pg_get_expr(p.polqual,p.polrelid),pg_get_expr(p.polwithcheck,p.polrelid),0=any(p.polroles))) "
            "from pg_catalog.pg_policy p where p.polrelid=c.oid) "
            "from pg_catalog.pg_class c join pg_catalog.pg_namespace n on n.oid=c.relnamespace "
            "where n.nspname='toolbako_runtime' and c.relkind in ('r','p','v','m','f','S')", (MAIL_ROLE,) * 4)).fetchall()
        _require(len(tables) == 9 and {row[0] for row in tables} == set(WEB_TABLES))
        for table, rls, kind, owned, columns, broad_grants, policies in tables:
            _require(rls is True and kind == "r" and owned is False and broad_grants is False)
            seen = set()
            for column, verb, enabled, grantable in columns:
                required = column in PERMISSIONS.get(table, {}).get(verb, "").split()
                _require(enabled is required and grantable is False)
                if required:
                    seen.add((verb, column))
            _require(seen == {(verb, column) for verb, names in PERMISSIONS.get(table, {}).items() for column in names.split()})
            expected_policies = {**web_policies(table), **extra_policies.get(table, {})}
            _require(isinstance(policies, list) and len(policies) == len(expected_policies))
            for name, command, permissive, policy_roles, using, check, public in policies:
                _require(public is False and expected_policies.get(name) == (command, permissive, policy_roles,
                    normalize_mail_expression(using), normalize_mail_expression(check)))
        if conn.info.server_version >= 170000:
            _require(await (await conn.execute(
                "select exists(select 1 from pg_class c join pg_namespace n on n.oid=c.relnamespace "
                "where n.nspname !~ '^pg_' and n.nspname <> 'information_schema' and has_schema_privilege(%s,n.oid,'USAGE') "
                "and c.relkind in ('r','p','m') and has_table_privilege(%s,c.oid,'MAINTAIN'))", (MAIL_ROLE,) * 2)).fetchone() == (False,))
        outside = await (await conn.execute(
            "select exists(select 1 from pg_class c join pg_namespace n on n.oid=c.relnamespace "
            "where n.nspname !~ '^pg_' and n.nspname not in ('information_schema','toolbako_runtime') "
            "and has_schema_privilege(%s,n.oid,'USAGE') and c.relkind in ('r','p','v','m','f') and "
            "(has_any_column_privilege(%s,c.oid,'SELECT,INSERT,UPDATE,REFERENCES') or has_table_privilege(%s,c.oid,'DELETE,TRUNCATE,TRIGGER') or pg_has_role(%s,c.relowner,'USAGE'))) "
            "or exists(select 1 from pg_class c join pg_namespace n on n.oid=c.relnamespace "
            "where n.nspname !~ '^pg_' and n.nspname <> 'information_schema' and has_schema_privilege(%s,n.oid,'USAGE') "
            "and case when c.relkind='S' then has_sequence_privilege(%s,c.oid,'USAGE,SELECT,UPDATE') else false end) "
            "or exists(select 1 from pg_proc p join pg_namespace n on n.oid=p.pronamespace "
            "where n.nspname !~ '^pg_' and n.nspname <> 'information_schema' and p.prosecdef "
            "and has_schema_privilege(%s,n.oid,'USAGE') and has_function_privilege(%s,p.oid,'EXECUTE'))", (MAIL_ROLE,) * 8)).fetchone()
        _require(outside == (False,))
        return True
    except LiveMailRuntimeError:
        raise
    except Exception:
        raise LiveMailRuntimeError() from None


async def verify_mail_connection(conn, *, queue_since):
    """Actual fixed TLS connection plus current role, schema and bound ledger."""
    try:
        _require(conn.info.host == TARGET_HOST and conn.info.port == 5432 and conn.info.dbname == "postgres"
                 and conn.info.user == MAIL_USER and conn.pgconn.ssl_in_use is True)
        _require(await (await conn.execute("select current_user,session_user")).fetchone() == (MAIL_ROLE, MAIL_ROLE))
        await verify_mail_access(conn, queue_since=queue_since)
        _require(await (await conn.execute("select singleton,version from toolbako_runtime.schema_version")).fetchall() == [(True, 2)])
        rows = await (await conn.execute(
            "select endpoint,request_hash,status,response from toolbako_runtime.stripe_operations where operation_id=%s", (SCOPE_OPERATION_ID,))).fetchall()
        _require(len(rows) == 1)
        validate_binding(rows[0], SCOPE)
        return True
    except LiveMailRuntimeError:
        raise
    except Exception:
        raise LiveMailRuntimeError() from None


class LiveMailBackend:
    """Minimal connection boundary, no marketplace-state mutation API.

    The monitor and deliver_pending need only connect() and _check_schema().
    There is deliberately no init/request/save/migration/financial method.
    """
    __slots__ = ("__configuration", "queue_since")

    def __init__(self, password, *, queue_since, environment=None):
        try:
            PreparedMailAccess(queue_since, "0" * 64)
            self.__configuration = build_mail_database_config(password, environment=environment)
            self.queue_since = queue_since
        except Exception:
            raise LiveMailRuntimeError() from None

    def __repr__(self):
        return "LiveMailBackend(<redacted>)"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("LiveMailBackend cannot be serialized")

    async def connect(self):
        conn = None
        try:
            conn = await psycopg.AsyncConnection.connect(self.__configuration.for_connection(), autocommit=True,
                prepare_threshold=None, connect_timeout=5, options="-c statement_timeout=10000")
            async with conn.transaction():
                await conn.execute("set transaction read only")
                await verify_mail_connection(conn, queue_since=self.queue_since)
            return conn
        except asyncio.CancelledError:
            if conn is not None:
                try:
                    await conn.close()
                except Exception:
                    pass
            raise
        except Exception:
            if conn is not None:
                try:
                    await conn.close()
                except Exception:
                    pass
            raise LiveMailRuntimeError() from None

    async def _check_schema(self, conn):
        try:
            _require(await (await conn.execute("select version from toolbako_runtime.schema_version where singleton")).fetchone() == (2,))
        except Exception:
            raise LiveMailRuntimeError() from None
