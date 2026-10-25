"""A pooled server may keep a session alive after the client socket closes."""
import asyncio

import pytest

from app.data import DemoStore
from app.database import LOCK_ID, PostgresStateStore, StorageUnavailable
from test_request_failure_recovery import Connection, Cursor
from test_database import database


@pytest.mark.parametrize("failure", [None, "load", "handler", "unlock"])
def test_unlock_is_acknowledged_before_releasing_local_lock(monkeypatch, failure):
    async def run():
        backend = PostgresStateStore("unused")
        store = DemoStore(seed=False)
        calls = []
        conn = Connection(store)
        original = conn.execute

        async def execute(sql, args=None):
            if "pg_advisory_unlock" in sql:
                assert backend._local_lock.locked()
                assert args == (LOCK_ID,)
                calls.append("unlock")
                if failure == "unlock":
                    raise RuntimeError("private failure")
                return Cursor((True,))
            if "schema_version" in sql and failure == "load":
                raise RuntimeError("private failure")
            return await original(sql, args)

        async def close():
            assert backend._local_lock.locked()
            calls.append("close")

        async def connect():
            return conn

        monkeypatch.setattr(conn, "execute", execute)
        monkeypatch.setattr(conn, "close", close)
        monkeypatch.setattr(backend, "connect", connect)
        try:
            async with backend.request(store):
                if failure == "handler":
                    raise ValueError("handler failed")
        except (StorageUnavailable, ValueError):
            assert failure is not None
        else:
            assert failure is None
        assert calls == ["unlock", "close"]
        assert not backend._local_lock.locked()
        assert backend._connection_slots._value == 2
    asyncio.run(run())


def test_real_server_unlocks_even_if_pool_keeps_old_session_alive(database, monkeypatch):
    backend, _ = database
    original = backend.connect
    kept = []

    class PoolSession:
        def __init__(self, conn): self.conn = conn
        def __getattr__(self, name): return getattr(self.conn, name)
        async def close(self): pass  # Model delayed pooled server disconnection.

    async def connect():
        conn = await original()
        kept.append(conn)
        return PoolSession(conn)

    monkeypatch.setattr(backend, "connect", connect)

    async def run():
        try:
            for _ in range(3):
                async with backend.request(DemoStore(seed=False)):
                    pass
            assert len(kept) == 3
        finally:
            for conn in kept:
                await conn.close()
    asyncio.run(run())
