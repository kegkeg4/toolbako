"""Real, disposable PostgreSQL: reuse must not retain locks or dirty state."""
import asyncio

import pytest

from app.data import DemoStore
from app.database import LOCK_ID, PostgresStateStore, StorageUnavailable
from test_database import database, query


def test_reuse_reloads_other_workers_and_releases_session_lock(database):
    _, dsn = database

    async def run():
        backend = PostgresStateStore(dsn, reuse_connections=True)
        store = DemoStore(seed=False)
        try:
            async with backend.request(store):
                first = backend._connection
                store.audit(None, "reuse.saved", "first")
                await backend.save()
                store.audit(None, "reuse.unsaved", "discard")
            assert not first.closed and len(backend._idle_connections) == 1
            # A different session must acquire the same lock between requests.
            other = PostgresStateStore(dsn)
            async with other.request(DemoStore(seed=False)):
                other._store.audit(None, "reuse.other", "second")
                await other.save()
            async with backend.request(store):
                assert backend._connection is first
                assert {x["target"] for x in store.audit_logs} == {"first", "second"}
        finally:
            await backend.close()
        assert first.closed and not backend._idle_connections

    asyncio.run(run())


def test_dead_idle_connection_is_replaced_before_handler(database):
    _, dsn = database

    async def run():
        backend = PostgresStateStore(dsn, reuse_connections=True)
        store = DemoStore(seed=False)
        try:
            async with backend.request(store):
                old = backend._connection
                pid = old.info.backend_pid
            assert query(dsn, "select pg_terminate_backend(%s)", (pid,)) == [(True,)]
            async with backend.request(store):
                assert backend._connection is not old
                store.audit(None, "reuse.recovered", "recovered")
                await backend.save()
            assert old.closed
        finally:
            await backend.close()

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["handler", "cancel", "unlock", "transaction"])
def test_failed_requests_never_return_a_session_to_cache(database, failure):
    _, dsn = database

    async def run():
        backend = PostgresStateStore(dsn, reuse_connections=True)
        store = DemoStore(seed=False)
        expected = {"handler": ValueError, "cancel": asyncio.CancelledError,
                    "unlock": StorageUnavailable}.get(failure)

        async def attempt():
            async with backend.request(store):
                conn = backend._connection
                if failure == "handler":
                    raise ValueError("synthetic")
                if failure == "cancel":
                    raise asyncio.CancelledError()
                if failure == "unlock":
                    await conn.execute("select pg_advisory_unlock(%s)", (LOCK_ID,))
                if failure == "transaction":
                    await conn.execute("begin")
                    await conn.execute("create table public.must_rollback(id int)")

        if expected:
            with pytest.raises(expected):
                await attempt()
        else:
            await attempt()
            assert query(dsn, "select to_regclass('public.must_rollback')") == [(None,)]
        assert not backend._idle_connections
        assert backend._connection_slots._value == 2
        assert not backend._local_lock.locked()
        try:
            async with backend.request(store):
                assert store.audit_logs == []
        finally:
            await backend.close()

    asyncio.run(run())


def test_concurrent_reuse_bounds_connections_and_preserves_all_writes(database):
    _, dsn = database

    async def run():
        backend = PostgresStateStore(dsn, reuse_connections=True)
        store, connections = DemoStore(seed=False), set()

        async def client(number):
            for iteration in range(4):
                async with backend.request(store):
                    connections.add(backend._connection)
                    store.audit(None, "reuse.concurrent", f"{number}-{iteration}")
                    await backend.save()

        try:
            await asyncio.gather(*(client(n) for n in range(8)))
            assert len(connections) <= 2
            assert len(backend._idle_connections) <= 2
            async with backend.request(store):
                assert len({x["target"] for x in store.audit_logs}) == 32
        finally:
            await backend.close()
        assert all(c.closed for c in connections)

    asyncio.run(run())


def test_expired_or_different_loop_sessions_are_closed(database):
    _, dsn = database
    backend = PostgresStateStore(dsn, reuse_connections=True)

    async def prime():
        async with backend.request(DemoStore(seed=False)):
            return backend._connection

    old = asyncio.run(prime())

    async def verify():
        try:
            async with backend.request(DemoStore(seed=False)):
                second = backend._connection
                assert second is not old and old.closed
            conn, at, loop = backend._idle_connections.pop()
            backend._idle_connections.append((conn, at - 61, loop))
            async with backend.request(DemoStore(seed=False)):
                assert backend._connection is not second and second.closed
        finally:
            await backend.close()

    asyncio.run(verify())
