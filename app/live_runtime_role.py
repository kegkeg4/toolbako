"""Read-only verification of the reviewed non-owner Web database role.

This module neither creates roles nor grants permissions. The separate reviewed
SQL is an operator action, never an application migration. Effective privileges
include PUBLIC and inherited grants; a grant we did not issue is not a denial.
Only fixed verdicts escape. No row payload, password or catalog name is logged.
"""
from .live_database_config import RUNTIME_ROLE


RUNTIME_GROUP = "toolbako_runtime_web"
SCHEMA = "toolbako_runtime"
NON_SCOPE = "operation_id <> 'toolbako-financial-scope-v1' AND endpoint <> 'internal/financial-scope-v1'"
# Column grants are intentional. In particular Web cannot read queued mail
# contents, claim mail, create an empty snapshot or change a scope binding.
PERMISSIONS = {
    "schema_version": {"SELECT": "singleton version"},
    "marketplace_state": {"SELECT": "singleton revision payload", "UPDATE": "payload revision updated_at"},
    "audit_events": {"SELECT": "id content_hash", "INSERT": "id actor_id action target request_id occurred_at content_hash"},
    "stripe_operations": {"SELECT": "operation_id endpoint request_hash status response created_at", "INSERT": "operation_id endpoint request_hash status response", "UPDATE": "status response updated_at"},
    "email_outbox": {"SELECT": "id status available_at created_at lease_until", "INSERT": "id recipient sender subject body"},
    "finance_receipts": {"SELECT": "payment_intent order_id seller_id amount fee", "INSERT": "payment_intent order_id seller_id amount fee"},
    "finance_payouts": {"SELECT": "id seller_id request_key provider_reference content_hash status scheduled_for", "INSERT": "id seller_id account_id amount fee net_amount status scheduled_for request_key content_hash", "UPDATE": "status provider_reference updated_at"},
    "finance_allocations": {"SELECT": "id payout_id receipt_id amount transfer_amount", "INSERT": "id payout_id receipt_id amount transfer_amount"},
    "finance_entries": {"SELECT": "id content_hash", "INSERT": "id kind amount receipt_id payout_id provider_reference content_hash"},
}


class RuntimeRoleError(RuntimeError):
    def __init__(self):
        super().__init__("live_runtime_role_unverified")


def _require(value):
    if not value:
        raise RuntimeRoleError()


def _expression(value):
    # These policies contain no parentheses-dependent alternation. Accept only
    # PostgreSQL's whitespace/parentheses/text-cast formatting differences.
    return "" if value is None else "".join(value.replace("::text", "").replace("(", "").replace(")", "").split())


def _policies(table):
    result = {}
    for verb in PERMISSIONS[table]:
        name = "toolbako_web_" + verb.lower()
        expression = NON_SCOPE if table == "stripe_operations" and verb != "SELECT" else (
            "singleton" if table == "marketplace_state" else "true")
        using = expression if verb != "INSERT" else None
        check = expression if verb != "SELECT" else None
        result[name] = (verb[0].lower() if verb != "INSERT" else "a", True,
                        [RUNTIME_GROUP], _expression(using), _expression(check))
    # pg_policy uses r for SELECT, a for INSERT, w for UPDATE.
    if "toolbako_web_select" in result:
        result["toolbako_web_select"] = ("r", *result["toolbako_web_select"][1:])
    if "toolbako_web_update" in result:
        result["toolbako_web_update"] = ("w", *result["toolbako_web_update"][1:])
    return result


def _membership_shape(rows, postgres, server_version):
    """Accept only the reviewed runtime edge and PG16+ creator-admin edges.

    A non-superuser CREATEROLE gets an automatic, non-inheriting/non-SET
    ADMIN grant from the bootstrap superuser (OID 10) on each role it creates.
    This is administration by the already trusted postgres operator, never
    membership of the application role in postgres or an extra group.
    """
    _require(isinstance(postgres, tuple) and len(postgres) == 3)
    creator_oid, superuser, createrole = postgres
    _require(type(creator_oid) is int and creator_oid > 0
             and type(superuser) is bool and type(createrole) is bool
             and (superuser or createrole) and type(server_version) is int)
    if server_version < 160000:
        _require(rows == [(RUNTIME_GROUP, RUNTIME_ROLE, False)])
        return
    expected = {(RUNTIME_GROUP, RUNTIME_ROLE, False, True, True, creator_oid)}
    if not superuser:
        expected.update((role, "postgres", True, False, False, 10)
                        for role in (RUNTIME_GROUP, RUNTIME_ROLE))
    _require(len(rows) == len(expected) and set(rows) == expected)


async def verify_runtime_memberships(conn):
    """Check the exact operator-created membership graph without granting it."""
    postgres = await (await conn.execute(
        "select oid,rolsuper,rolcreaterole from pg_catalog.pg_roles where rolname='postgres'"
    )).fetchone()
    if conn.info.server_version >= 160000 and postgres is not None and postgres[1] is False:
        # PostgreSQL emits these automatic ADMIN grants as its bootstrap
        # superuser. The Supabase bootstrap role is supabase_admin (OID 10).
        bootstrap = await (await conn.execute(
            "select oid,rolsuper from pg_catalog.pg_roles where oid=10"
        )).fetchone()
        _require(bootstrap == (10, True))
    extra = ",m.inherit_option,m.set_option,m.grantor" if conn.info.server_version >= 160000 else ""
    rows = await (await conn.execute(
        "select parent.rolname,child.rolname,m.admin_option" + extra + " from pg_catalog.pg_auth_members m "
        "join pg_catalog.pg_roles parent on parent.oid=m.roleid "
        "join pg_catalog.pg_roles child on child.oid=m.member "
        "where parent.rolname in (%s,%s) or child.rolname in (%s,%s)",
        (RUNTIME_ROLE, RUNTIME_GROUP, RUNTIME_ROLE, RUNTIME_GROUP))).fetchall()
    _membership_shape(rows, postgres, conn.info.server_version)


async def verify_runtime_role(conn, *, mail_queue_since=0):
    """Verify actual SQL identity and effective rights. Call in READ ONLY mode.

    Connection target/TLS and nonempty bound dataset are independently checked
    by deployment_check. No test override or arbitrary-role argument exists.
    This checks privileges, not a runtime DML trial or provider readiness.
    Zero keeps the original exact 19-policy contract. A positive reviewed mail
    cutoff permits only four dedicated mail policies AND independently proves
    the separate mail role's effective rights. It never grants those rights.
    """
    try:
        _require(type(mail_queue_since) is int and 0 <= mail_queue_since <= 4102444800)
        mail_policies = {}
        if mail_queue_since:
            # The mail verifier reuses this module's Web contract; load it only
            # after module initialization and only for the explicit extension.
            from .live_mail_runtime import mail_policy_rows, normalize_mail_expression, verify_mail_access
            mail_policies = mail_policy_rows(mail_queue_since)
        identity = await (await conn.execute("select current_user, session_user, current_setting('transaction_read_only')")).fetchone()
        _require(identity == (RUNTIME_ROLE, RUNTIME_ROLE, "on"))
        roles = await (await conn.execute(
            "select rolname,rolsuper,rolinherit,rolcreaterole,rolcreatedb,rolcanlogin,rolreplication,rolbypassrls "
            "from pg_catalog.pg_roles where rolname in (%s,%s)", (RUNTIME_ROLE, RUNTIME_GROUP))).fetchall()
        _require(set(roles) == {
            (RUNTIME_ROLE, False, True, False, False, True, False, False),
            (RUNTIME_GROUP, False, False, False, False, False, False, False),
        })
        await verify_runtime_memberships(conn)
        database = await (await conn.execute(
            "select has_database_privilege(current_database(),'CONNECT'),"
            "has_database_privilege(current_database(),'CREATE'),"
            "has_database_privilege(current_database(),'TEMP'),d.datdba = r.oid "
            "from pg_catalog.pg_database d,pg_catalog.pg_roles r "
            "where d.datname=current_database() and r.rolname=current_user")).fetchone()
        _require(database == (True, False, False, False))
        schemas = await (await conn.execute(
            "select n.nspname,has_schema_privilege(n.oid,'USAGE'),has_schema_privilege(n.oid,'CREATE'),"
            "pg_has_role(n.nspowner,'USAGE') from pg_catalog.pg_namespace n "
            "where n.nspname !~ '^pg_' and n.nspname <> 'information_schema'")).fetchall()
        _require(any(row[:3] == (SCHEMA, True, False) for row in schemas))
        _require(all(not row[2] and not row[3] for row in schemas))
        tables = await (await conn.execute(
            "select c.relname,c.oid,c.relrowsecurity,c.relkind,pg_has_role(c.relowner,'USAGE'), "
            "(select jsonb_agg(jsonb_build_array(a.attname,v.verb,has_column_privilege(c.oid,a.attnum,v.verb),"
            "has_column_privilege(c.oid,a.attnum,v.verb||' WITH GRANT OPTION'))) "
            "from pg_catalog.pg_attribute a cross join (values ('SELECT'),('INSERT'),('UPDATE'),('REFERENCES')) v(verb) "
            "where a.attrelid=c.oid and a.attnum>0 and not a.attisdropped), "
            "(select bool_or(has_table_privilege(c.oid,verb)) from "
            "(values ('SELECT'),('INSERT'),('UPDATE'),('DELETE'),('TRUNCATE'),('REFERENCES'),('TRIGGER')) v(verb)), "
            "(select jsonb_agg(jsonb_build_array(p.polname,p.polcmd,p.polpermissive,"
            "array(select r.rolname::text from pg_catalog.pg_roles r where r.oid=any(p.polroles) order by r.rolname),"
            "pg_get_expr(p.polqual,p.polrelid),pg_get_expr(p.polwithcheck,p.polrelid),0=any(p.polroles))) "
            "from pg_catalog.pg_policy p where p.polrelid=c.oid) "
            "from pg_catalog.pg_class c join pg_catalog.pg_namespace n on n.oid=c.relnamespace "
            "where n.nspname=%s and c.relkind in ('r','p','v','m','f','S')", (SCHEMA,))).fetchall()
        _require({row[0] for row in tables} == set(PERMISSIONS) and len(tables) == 9)
        for table, oid, rls, kind, owned, columns, dangerous, policies in tables:
            _require(rls is True and kind == "r" and owned is False)
            seen = set()
            for column, verb, enabled, grant_option in columns:
                expected = column in PERMISSIONS[table].get(verb, "").split()
                _require(enabled is expected and grant_option is False)
                if expected:
                    seen.add((verb, column))
            _require(seen == {(verb, column) for verb, names in PERMISSIONS[table].items() for column in names.split()})
            _require(dangerous is False)
            expected_mail = mail_policies.get(table, {})
            expected_policies = {**_policies(table), **expected_mail}
            _require(isinstance(policies, list) and len(policies) == len(expected_policies))
            for name, command, permissive, policy_roles, using, check, public in policies:
                normalize = normalize_mail_expression if name in expected_mail else _expression
                _require(public is False and expected_policies.get(name) ==
                         (command, permissive, policy_roles, normalize(using), normalize(check)))
        if conn.info.server_version >= 170000:
            maintain = await (await conn.execute(
                "select exists(select 1 from pg_catalog.pg_class c join pg_catalog.pg_namespace n on n.oid=c.relnamespace "
                "where n.nspname !~ '^pg_' and n.nspname <> 'information_schema' "
                "and has_schema_privilege(n.oid,'USAGE') "
                "and c.relkind in ('r','p','m') and has_table_privilege(c.oid,'MAINTAIN'))")).fetchone()
            _require(maintain == (False,))
        # Check PUBLIC and any grant on non-runtime relations, including column
        # grants (table-only predicates would miss them) and sequences. An ACL
        # inside a namespace without effective USAGE is not executable access;
        # PUBLIC/direct/inherited USAGE becoming available is checked afresh.
        outside = await (await conn.execute(
            "select exists(select 1 from pg_catalog.pg_class c join pg_catalog.pg_namespace n on n.oid=c.relnamespace "
            "where n.nspname !~ '^pg_' and n.nspname not in ('information_schema',%s) "
            "and has_schema_privilege(n.oid,'USAGE') "
            "and c.relkind in ('r','p','v','m','f') and ("
            "has_any_column_privilege(c.oid,'SELECT,INSERT,UPDATE,REFERENCES') or "
            "has_table_privilege(c.oid,'DELETE,TRUNCATE,TRIGGER') or pg_has_role(c.relowner,'USAGE'))) "
            "or exists(select 1 from pg_catalog.pg_class c join pg_catalog.pg_namespace n on n.oid=c.relnamespace "
            "where n.nspname !~ '^pg_' and n.nspname <> 'information_schema' "
            "and has_schema_privilege(n.oid,'USAGE') "
            "and case when c.relkind='S' then has_sequence_privilege(c.oid,'USAGE,SELECT,UPDATE') else false end) "
            "or exists(select 1 from pg_catalog.pg_proc p join pg_catalog.pg_namespace n on n.oid=p.pronamespace "
            "where n.nspname !~ '^pg_' and n.nspname <> 'information_schema' and p.prosecdef "
            "and has_schema_privilege(n.oid,'USAGE') and has_function_privilege(p.oid,'EXECUTE'))", (SCHEMA,))).fetchone()
        _require(outside == (False,))
        if mail_queue_since:
            await verify_mail_access(conn, queue_since=mail_queue_since)
        return True
    except RuntimeRoleError:
        raise
    except Exception:
        raise RuntimeRoleError() from None
