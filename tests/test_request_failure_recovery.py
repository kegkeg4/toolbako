"""Synthetic failures only; no remote connections or customer records."""
import asyncio
from contextlib import asynccontextmanager
import json
import logging

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.database as db
from app.data import DemoStore


class Cursor:
    def __init__(self, row):
        self.row = row

    async def fetchone(self):
        return self.row


class Connection:
    def __init__(self, store, *, busy=False, close_error=False):
        self.payload = db.snapshot(store)
        self.busy, self.close_error = busy, close_error
        self.closed = False

    async def execute(self, query, *args):
        if "pg_try_advisory_lock" in query:
            return Cursor((not self.busy,))
        return Cursor((db.SCHEMA_VERSION, 0, self.payload))

    async def close(self):
        self.closed = True
        if self.close_error:
            raise RuntimeError("private-connection-string")


@pytest.mark.parametrize("failure,phase,reason", [
    ("connect", "connect", "connection_timeout"),
    ("busy", "advisory_lock", "database_busy"),
    ("load", "load", "query_timeout"),
    ("restore", "cleanup", "request_failed"),
    ("close", "cleanup", "request_failed"),
])
def test_failure_releases_lock_and_preserves_safe_diagnosis(monkeypatch, failure, phase, reason):
    store = DemoStore(seed=False)
    backend = db.PostgresStateStore("unused")
    conn = Connection(store, busy=failure == "busy", close_error=failure in {"busy", "close"})

    async def connect():
        if failure == "connect":
            raise db.StorageUnavailable(diagnostic_code="connection_timeout")
        return conn

    original_execute = conn.execute

    async def execute(query, *args):
        if failure == "load" and "schema_version" in query:
            raise psycopg.errors.QueryCanceled("private SQL and values")
        return await original_execute(query, *args)

    monkeypatch.setattr(backend, "connect", connect)
    monkeypatch.setattr(conn, "execute", execute)

    async def run():
        with pytest.raises(db.StorageUnavailable) as error:
            async with backend.request(store):
                if failure == "restore":
                    def fail_restore(*args):
                        raise ValueError("private state")
                    monkeypatch.setattr(db, "restore", fail_restore)
        assert (error.value.diagnostic_phase, error.value.diagnostic_code) == (phase, reason)
        assert "private" not in str(error.value)
        assert not backend._local_lock.locked()
        assert backend._connection is None
        if failure != "connect":
            assert conn.closed
        # A failed cleanup must not leave the next request permanently blocked.
        await asyncio.wait_for(backend._local_lock.acquire(), .1)
        backend._local_lock.release()

    asyncio.run(run())


def test_lock_timeout_does_not_release_another_requests_lock(monkeypatch):
    backend = db.PostgresStateStore("unused")

    async def time_out(coro, timeout):
        coro.close()
        raise TimeoutError("private lock description")

    async def run():
        await backend._local_lock.acquire()
        monkeypatch.setattr(db.asyncio, "wait_for", time_out)
        with pytest.raises(db.StorageUnavailable) as error:
            async with backend.request(DemoStore(seed=False)):
                pytest.fail("must not enter")
        assert error.value.diagnostic_code == "local_lock_timeout"
        assert error.value.diagnostic_phase == "local_lock"
        assert backend._local_lock.locked()
        backend._local_lock.release()

    asyncio.run(run())


def test_cancelled_connection_attempt_releases_its_lock(monkeypatch):
    backend = db.PostgresStateStore("unused")

    async def connect():
        raise asyncio.CancelledError()

    monkeypatch.setattr(backend, "connect", connect)

    async def run():
        with pytest.raises(asyncio.CancelledError):
            async with backend.request(DemoStore(seed=False)):
                pytest.fail("must not enter")
        assert not backend._local_lock.locked()

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["start", "handler", "save", "cleanup"])
def test_boundary_returns_no_success_and_logs_only_safe_fields(caplog, failure):
    class Backend:
        @asynccontextmanager
        async def request(self, store):
            if failure == "start":
                raise db.StorageUnavailable("private payload", diagnostic_code="local_lock_timeout", diagnostic_phase="local_lock")
            yield
            if failure == "cleanup":
                raise RuntimeError("private query")

        async def save(self):
            if failure == "save":
                raise psycopg.errors.QueryCanceled("private SQL")

    app = FastAPI()
    app.add_middleware(db.DatabaseBoundaryMiddleware, backend=Backend(), store=None)

    @app.middleware("http")
    async def server_id(request, call_next):
        request.state.request_id = "a" * 32
        return await call_next(request)

    @app.get("/private-account")
    async def route():
        if failure == "handler":
            raise RuntimeError("private provider credentials")
        return {"success": True}

    with caplog.at_level(logging.WARNING, logger="toolbako.database"):
        response = TestClient(app).get("/private-account?token=private", headers={"X-Request-ID": "private-client-value"})
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["retry-after"] == "30"
    assert "success" not in response.json()
    assert "private" not in response.text
    record = next(r for r in caplog.records if r.name == "toolbako.database")
    assert not record.exc_info
    assert "private" not in record.getMessage()
    report = json.loads(record.getMessage().split("sent ", 1)[1])
    assert report["phase"] == ("local_lock" if failure == "start" else failure)
    assert report["request_id"] == "a" * 32
    assert report["reason"] in db.DATABASE_FAILURE_CODES
    assert report["elapsed_ms"] >= 0
