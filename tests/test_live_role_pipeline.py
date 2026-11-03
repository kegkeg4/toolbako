"""Fresh catalog batching: fixed guards, no cached verdict or SQL-write path.

The optional local-PG case compares pipelined and sequential execution against
the same restricted identities and deliberately changed privilege/RLS state.
"""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import psycopg
from psycopg.conninfo import make_conninfo
import pytest

from app import live_runtime_role as web
from app import live_mail_runtime as mail
from test_live_runtime_role import admin_config, administrator, database, run
from test_live_runtime_mail_role import CUTOFF, mail_roles, mail_database


class CursorSpy:
    def __init__(self, connection, cursor):
        self.connection, self.cursor = connection, cursor

    async def fetchone(self):
        assert not self.connection.inside, "Fetching inside the pipeline would add an RTT"
        return await self.cursor.fetchone()

    async def fetchall(self):
        assert not self.connection.inside, "Fetching inside the pipeline would add an RTT"
        return await self.cursor.fetchall()


class ConnectionSpy:
    def __init__(self, connection=None, *, version=170000, sequential=False, failure=None):
        self.connection = connection
        self.info = connection.info if connection else SimpleNamespace(server_version=version)
        self.inside = False
        self.pipeline_count = 0
        self.commands = []
        self.sequential, self.failure = sequential, failure

    @asynccontextmanager
    async def pipeline(self):
        assert not self.inside
        self.pipeline_count += 1
        self.inside = True
        try:
            if self.failure == "enter":
                raise RuntimeError("PRIVATE_PIPELINE_ERROR")
            if self.connection and not self.sequential:
                async with self.connection.pipeline():
                    yield
            else:
                yield
            if self.failure == "sync":
                raise RuntimeError("PRIVATE_PIPELINE_ERROR")
        finally:
            self.inside = False

    async def execute(self, query, parameters=None):
        assert query.lstrip().lower().startswith("select "), "Catalog audit must be read-only"
        self.commands.append((query, parameters, self.inside))
        if self.failure == "execute" and self.inside:
            raise RuntimeError("PRIVATE_SQL_ERROR")
        if self.connection:
            return CursorSpy(self, await self.connection.execute(query, parameters))
        value = (web.RUNTIME_ROLE, web.RUNTIME_ROLE, "on") if "current_user" in query else ("on",)
        class Cursor:
            async def fetchone(self):
                return value
            async def fetchall(self):
                return []
        return CursorSpy(self, Cursor())


@pytest.mark.parametrize("version,count", [(150000, 7), (160000, 8), (170000, 9)])
@pytest.mark.parametrize("collector", [web._runtime_catalog_cursors, mail._mail_catalog_cursors])
def test_catalog_queries_are_queued_before_any_result_fetch(version, count, collector):
    conn = ConnectionSpy(version=version)
    cursors = run(collector(conn))
    assert conn.pipeline_count == 1 and not conn.inside
    assert len(conn.commands) == len(cursors) == count
    assert all(inside for _, _, inside in conn.commands)
    assert ("maintain" in cursors) is (version >= 170000)
    assert ("bootstrap" in cursors) is (version >= 160000)


@pytest.mark.parametrize("kind", ["web", "mail"])
@pytest.mark.parametrize("failure", ["enter", "execute", "sync"])
def test_pipeline_failure_is_fixed_error_without_sequential_fallback(kind, failure, capsys):
    conn = ConnectionSpy(failure=failure)
    error = web.RuntimeRoleError if kind == "web" else mail.LiveMailRuntimeError
    action = web.verify_runtime_role(conn) if kind == "web" else mail.verify_mail_access(conn, queue_since=CUTOFF)
    with pytest.raises(error) as caught:
        run(action)
    assert str(caught.value) == ("live_runtime_role_unverified" if kind == "web" else "live_mail_runtime_unverified")
    assert conn.pipeline_count == 1 and not conn.inside
    assert all(inside for _, _, inside in conn.commands[1:])
    assert "PRIVATE" not in capsys.readouterr().out


@pytest.mark.parametrize("identity", [None, ("postgres", "postgres", "on"),
                                      (web.RUNTIME_ROLE, web.RUNTIME_ROLE, "off")])
def test_web_identity_and_read_only_are_rejected_before_catalog_batch(identity):
    conn = ConnectionSpy()
    class Cursor:
        async def fetchone(self): return identity
    async def execute(*args): return Cursor()
    conn.execute = execute
    with pytest.raises(web.RuntimeRoleError):
        run(web.verify_runtime_role(conn))
    assert conn.pipeline_count == 0


@pytest.mark.parametrize("value", [None, ("off",), (True,)])
def test_mail_read_only_is_rejected_before_catalog_batch(value):
    conn = ConnectionSpy()
    class Cursor:
        async def fetchone(self): return value
    async def execute(*args): return Cursor()
    conn.execute = execute
    with pytest.raises(mail.LiveMailRuntimeError):
        run(mail.verify_mail_access(conn, queue_since=CUTOFF))
    assert conn.pipeline_count == 0


async def checked_verdict(database, *, kind, sequential=False):
    async with await psycopg.AsyncConnection.connect(make_conninfo(**database[1]), autocommit=True,
                                                    prepare_threshold=None) as raw:
        await raw.execute("set default_transaction_read_only=on")
        conn = ConnectionSpy(raw, sequential=sequential)
        try:
            result = (await web.verify_runtime_role(conn, mail_queue_since=CUTOFF) if kind == "web"
                      else await mail.verify_mail_access(conn, queue_since=CUTOFF))
        except (web.RuntimeRoleError, mail.LiveMailRuntimeError):
            result = False
        # No prevalidated result is reused between connections/calls.
        return result, conn


def test_real_pg_fresh_privilege_changes_have_identical_batched_verdicts(mail_database):
    changes = [
        ("web", "revoke select(version) on toolbako_runtime.schema_version from toolbako_runtime_web",
         "grant select(version) on toolbako_runtime.schema_version to toolbako_runtime_web"),
        ("web", "grant select(body) on toolbako_runtime.email_outbox to toolbako_runtime_web",
         "revoke select(body) on toolbako_runtime.email_outbox from toolbako_runtime_web"),
        ("web", "alter table toolbako_runtime.marketplace_state disable row level security",
         "alter table toolbako_runtime.marketplace_state enable row level security"),
        ("web", "alter policy toolbako_web_update on toolbako_runtime.stripe_operations with check(true)",
         "alter policy toolbako_web_update on toolbako_runtime.stripe_operations with check(" + web.NON_SCOPE + ")"),
        ("mail", "revoke update(status) on toolbako_runtime.email_outbox from toolbako_runtime_mail_live",
         "grant update(status) on toolbako_runtime.email_outbox to toolbako_runtime_mail_live"),
        ("mail", "grant select(payload) on toolbako_runtime.marketplace_state to toolbako_runtime_mail_live",
         "revoke select(payload) on toolbako_runtime.marketplace_state from toolbako_runtime_mail_live"),
        ("mail", "alter table toolbako_runtime.email_outbox disable row level security",
         "alter table toolbako_runtime.email_outbox enable row level security"),
        ("mail", "alter policy toolbako_mail_live_select on toolbako_runtime.email_outbox using(true)",
         f"alter policy toolbako_mail_live_select on toolbako_runtime.email_outbox using(created_at>=to_timestamp({CUTOFF}::double precision))"),
    ]
    for kind in ("web", "mail"):
        result, conn = run(checked_verdict(mail_database, kind=kind))
        assert result is True
        assert conn.pipeline_count == (2 if kind == "web" else 1)
        assert not conn.commands[0][2]  # actual READ ONLY checked before batch
        assert run(checked_verdict(mail_database, kind=kind, sequential=True))[0] is True
    for kind, change, restore in changes:
        with administrator(mail_database) as admin:
            admin.execute(change)
        try:
            assert run(checked_verdict(mail_database, kind=kind))[0] is False, change
            assert run(checked_verdict(mail_database, kind=kind, sequential=True))[0] is False, change
            # Mail privilege errors also reject the Web verifier with positive cutoff.
            if kind == "mail":
                assert run(checked_verdict(mail_database, kind="web"))[0] is False, change
        finally:
            with administrator(mail_database) as admin:
                admin.execute(restore)
        assert run(checked_verdict(mail_database, kind=kind))[0] is True, restore


def test_missing_pipeline_capability_fails_closed():
    conn = ConnectionSpy()
    conn.pipeline = None
    with pytest.raises(web.RuntimeRoleError):
        run(web.verify_runtime_role(conn))
    assert len(conn.commands) == 1


def test_cancellation_during_pipeline_propagates_and_exits():
    class Cancelled(ConnectionSpy):
        async def execute(self, query, parameters=None):
            if self.inside:
                raise asyncio.CancelledError()
            return await super().execute(query, parameters)
    conn = Cancelled()
    with pytest.raises(asyncio.CancelledError):
        run(web.verify_runtime_role(conn))
    assert not conn.inside
