"""Bounded slow-request diagnostics; no provider data or external traffic."""
import asyncio
from contextlib import asynccontextmanager
import json
import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import app.database as db
from app.data import DemoStore
from test_database import database


@pytest.mark.parametrize("slow_phase", ["connect", "load", "handler", "save", "cleanup", None])
def test_slow_phase_is_distinguished_without_request_content(monkeypatch, caplog, slow_phase):
    now = [0.0]
    monkeypatch.setattr(db, "_request_clock", lambda: now[0])
    cleaned = []

    def delay(phase):
        if phase == slow_phase:
            now[0] += 2.5

    class Backend:
        @asynccontextmanager
        async def request(self, store):
            timing = db.RequestTiming()
            for phase in ("connect", "advisory_lock", "load", "handler"):
                timing.mark(phase)
                delay(phase) if phase != "handler" else None
            try:
                yield timing
            finally:
                timing.mark("cleanup")
                delay("cleanup")
                timing.finish()
                cleaned.append(True)

        async def save(self):
            delay("save")

    app = FastAPI()
    app.add_middleware(db.DatabaseBoundaryMiddleware, backend=Backend(), store=None)

    @app.middleware("http")
    async def identifier(request, call_next):
        request.state.request_id = "a" * 32
        return await call_next(request)

    @app.get("/private-path")
    async def route():
        delay("handler")
        return {"ok": True}

    with caplog.at_level(logging.WARNING, logger="toolbako.database"):
        response = TestClient(app).get("/private-path?token=secret", headers={"X-Request-ID": "secret"})
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert cleaned == [True]
    logs = [r for r in caplog.records if r.name == "toolbako.database"]
    if slow_phase is None:
        assert not logs
        return
    assert len(logs) == 1 and not logs[0].exc_info
    message = logs[0].getMessage()
    assert "private" not in message and "secret" not in message
    report = json.loads(message.split("slow ", 1)[1])
    assert set(report) == {"event", "elapsed_ms", "phase_ms", "request_id"}
    assert report["event"] == "database_request_slow"
    assert report["request_id"] == "a" * 32
    assert report["elapsed_ms"] == 2500
    assert set(report["phase_ms"]) == db.DATABASE_REQUEST_PHASES
    assert report["phase_ms"][slow_phase] == 2500
    assert sum(report["phase_ms"].values()) == 2500


def test_logging_failure_does_not_turn_committed_success_into_retry(monkeypatch):
    committed = []
    cleaned = []

    def unavailable_log(*args):
        raise RuntimeError("private log sink configuration")

    monkeypatch.setattr(db.RequestTiming, "report", unavailable_log)

    class Backend:
        @asynccontextmanager
        async def request(self, store):
            yield db.RequestTiming()
            cleaned.append(True)

        async def save(self):
            committed.append(True)

    app = FastAPI()
    app.add_middleware(db.DatabaseBoundaryMiddleware, backend=Backend(), store=None)

    @app.post("/transaction")
    async def route():
        return {"ok": True}

    response = TestClient(app).post("/transaction")
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert committed == cleaned == [True]


def test_finished_timing_cannot_include_later_requests_or_untrusted_names(monkeypatch, caplog):
    now = [0.0]
    monkeypatch.setattr(db, "_request_clock", lambda: now[0])
    timing = db.RequestTiming()
    now[0] = 2.0
    timing.mark("private-url")
    timing.finish()
    now[0] = 999.0
    timing.mark("save")
    with caplog.at_level(logging.WARNING, logger="toolbako.database"):
        timing.report("private-client-id")
    report = json.loads(caplog.records[-1].getMessage().split("slow ", 1)[1])
    assert report["elapsed_ms"] == 2000
    assert report["phase_ms"]["local_lock"] == 2000
    assert report["phase_ms"]["save"] == 0
    assert report["request_id"] == ""
    assert "private" not in caplog.text


def test_concurrent_real_postgres_requests_remain_serial_and_reload_commits(database):
    backend, dsn = database
    store = DemoStore(seed=False)

    async def run():
        entered = []
        timings = []

        async def writer(number):
            async with backend.request(store) as timing:
                assert len(store.notifications) == len(entered)
                entered.append(number)
                store.notifications.append({"id": f"synthetic-{number}"})
                await asyncio.sleep(.01)
                timing.mark("save")
                await backend.save()
                timings.append(timing)

        await asyncio.gather(*(writer(i) for i in range(6)))
        assert len({id(t) for t in timings}) == 6
        assert all(t.finished is not None for t in timings)
        assert all(set(t.durations) == db.DATABASE_REQUEST_PHASES for t in timings)
        assert not backend._local_lock.locked()
        reloaded = DemoStore(seed=False)
        async with db.PostgresStateStore(dsn).request(reloaded):
            assert {n["id"] for n in reloaded.notifications} == {f"synthetic-{i}" for i in range(6)}

    asyncio.run(run())
