import asyncio
import json
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.production import readiness_summary
from app import security_services as services


PRIVATE = "synthetic-private-credential-never-export"


def configured(**changes):
    return Settings(**{"environment": "staging", "demo_mode": False,
                       "clamav_host": "127.0.0.1", "clamav_port": 3310,
                       "redis_url": f"redis://toolbako:{PRIVATE}@127.0.0.1:6379",
                       **changes})


@pytest.mark.parametrize("response,expected", [
    ([b"PO", b"NG\0"], "ready"),
    ([b"OK\0"], "unavailable"),
    ([b"PONG\n"], "unavailable"),
    ([b"PONG"], "unavailable"),
    ([b"x" * 100], "unavailable"),
    ([b"\xff\0"], "unavailable"),
])
def test_clamav_real_socket_requires_framed_pong_and_only_sends_ping(response, expected):
    async def scenario():
        commands = []
        async def handler(reader, writer):
            try:
                commands.append(await reader.readuntil(b"\0"))
                for part in response:
                    writer.write(part)
                    await writer.drain()
                    await asyncio.sleep(0)
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        async with server:
            port = server.sockets[0].getsockname()[1]
            result = await services.probe_clamav(configured(clamav_port=port))
        assert result == expected
        assert commands == [b"zPING\0"]
    asyncio.run(scenario())


def test_stalled_clamav_times_out_and_closes_socket(monkeypatch):
    monkeypatch.setattr(services, "PROBE_TIMEOUT_SECONDS", 0.03)
    async def scenario():
        closed = asyncio.Event()
        async def handler(reader, writer):
            try:
                assert await reader.readuntil(b"\0") == b"zPING\0"
                assert await reader.read() == b""
                closed.set()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        async with server:
            result = await services.probe_clamav(configured(clamav_port=server.sockets[0].getsockname()[1]))
            await asyncio.wait_for(closed.wait(), 1)
        assert result == "unavailable"
    asyncio.run(scenario())


@pytest.mark.parametrize("result", [True, False, "PONG", RuntimeError(PRIVATE)])
def test_redis_ping_requires_true_and_closes_without_key_commands(monkeypatch, result, capsys):
    calls = []
    class Client:
        async def ping(self):
            calls.append("ping")
            if isinstance(result, Exception):
                raise result
            return result
        async def aclose(self):
            calls.append("close")
    def factory(url, **kwargs):
        assert url == configured().redis_url
        assert kwargs["max_connections"] == 1
        return Client()
    monkeypatch.setattr(services.Redis, "from_url", factory)
    assert asyncio.run(services.probe_redis(configured())) == ("ready" if result is True else "unavailable")
    assert calls == ["ping", "close"]
    assert PRIVATE not in capsys.readouterr().out


def test_redis_timeout_cancels_and_closes(monkeypatch):
    monkeypatch.setattr(services, "PROBE_TIMEOUT_SECONDS", 0.01)
    calls = []
    class Client:
        async def ping(self):
            try:
                await asyncio.Event().wait()
            finally:
                calls.append("cancelled")
        async def aclose(self):
            calls.append("closed")
    monkeypatch.setattr(services.Redis, "from_url", lambda *a, **k: Client())
    assert asyncio.run(services.probe_redis(configured())) == "unavailable"
    assert calls == ["cancelled", "closed"]


def test_missing_configuration_and_demo_never_connect(monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Missing config or demo must not connect")
    monkeypatch.setattr(services.asyncio, "open_connection", forbidden)
    monkeypatch.setattr(services.Redis, "from_url", forbidden)
    assert asyncio.run(services.probe_clamav(configured(clamav_host=""))) == "not_configured"
    assert asyncio.run(services.probe_redis(configured(redis_url=""))) == "not_configured"
    result = asyncio.run(services.SecurityServiceMonitor(configured(demo_mode=True)).check())
    assert all(not status["ready"] and status["status"] == "disabled" for status in result["services"].values())


def test_failure_recovery_staleness_and_output_privacy(monkeypatch):
    now = [0.0]
    results = iter(["ready", "unavailable", "ready"])
    async def clamav(_):
        return next(results)
    async def redis(_):
        return "ready"
    monkeypatch.setattr(services, "probe_clamav", clamav)
    monkeypatch.setattr(services, "probe_redis", redis)
    monkeypatch.setattr(services, "probe_clamav_signatures", redis)
    monitor = services.SecurityServiceMonitor(configured(), clock=lambda: now[0])
    assert not monitor.snapshot()["fresh"]
    async def scenario():
        first = await monitor.check()
        assert all(s["ready"] for s in first["services"].values())
        failed = await monitor.check()
        assert not failed["services"]["clamav"]["ready"]
        recovered = await monitor.check()
        assert recovered["services"]["clamav"]["ready"]
    asyncio.run(scenario())
    for _ in range(1000):
        assert monitor.snapshot()["fresh"]  # Reading never probes or updates the timestamp.
    now[0] = services.MAX_AGE_SECONDS + 0.01
    stale = monitor.snapshot()
    assert not stale["fresh"] and all(s == {"ready": False, "status": "stale"} for s in stale["services"].values())
    assert PRIVATE not in json.dumps(stale)
    assert "127.0.0.1" not in json.dumps(stale)


def test_unexpected_probe_failure_cancels_sibling_without_exporting_errors(monkeypatch):
    closed = []
    async def broken(_):
        await asyncio.sleep(0)
        raise RuntimeError(PRIVATE)
    async def waiting(_):
        try:
            await asyncio.Event().wait()
        finally:
            closed.append(True)
    monkeypatch.setattr(services, "probe_clamav", broken)
    monkeypatch.setattr(services, "probe_redis", waiting)
    result = asyncio.run(services.SecurityServiceMonitor(configured()).check())
    assert closed == [True]
    assert all(not s["ready"] for s in result["services"].values())
    assert PRIVATE not in json.dumps(result)


def test_monitor_cancellation_invalidates_healthy_state(monkeypatch):
    async def ready(_):
        return "ready"
    monkeypatch.setattr(services, "probe_clamav", ready)
    monkeypatch.setattr(services, "probe_redis", ready)
    monkeypatch.setattr(services, "probe_clamav_signatures", ready)
    async def scenario():
        observed = asyncio.Event()
        events = []
        def emit(event):
            events.append(event)
            observed.set()
        monitor = services.SecurityServiceMonitor(configured(), emit=emit)
        task = asyncio.create_task(monitor.run())
        await asyncio.wait_for(observed.wait(), 1)
        assert monitor.snapshot()["fresh"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert all(s == {"ready": False, "status": "stopped"} for s in monitor.snapshot()["services"].values())
        assert PRIVATE not in json.dumps(events)
        assert events[0]["read_only"] is True
    asyncio.run(scenario())


def test_configured_endpoints_cannot_pass_readiness_without_current_probes():
    for values, expected in [(None, False), ({"clamav": "ready", "redis": 1}, False),
                             ({"clamav": True, "redis": True, "clamav_signatures": True}, True)]:
        checks = {c["key"]: c["ok"] for c in readiness_summary(configured(), runtime_security_ready=values)["checks"]}
        assert checks["file_scan"] is expected
        assert checks["distributed_rate_limits"] is expected
        assert checks["payments"] is False


def test_app_lifespan_observes_existing_services_and_stops_on_shutdown(monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    checked = []
    async def ready(_):
        checked.append(True)
        return "ready"
    async def db_probe():
        return True
    closed = []
    async def db_close():
        closed.append(True)
    monkeypatch.setattr(services, "probe_clamav", ready)
    monkeypatch.setattr(services, "probe_redis", ready)
    monkeypatch.setattr(services, "probe_clamav_signatures", ready)
    monkeypatch.setattr(main, "settings", configured())
    monkeypatch.setattr(main, "database_store", SimpleNamespace(probe=db_probe, close=db_close))
    with TestClient(main.app) as client:
        # The event loop starts the background task before this request.
        response = client.get("/readyz")
        assert response.status_code == 503  # Payments/other launch gates stay closed.
        checks = {c["key"]: c["ok"] for c in response.json()["checks"]}
        assert checks["file_scan"] and checks["distributed_rate_limits"]
        monitor = main.app.state.security_services
        assert len(checked) == 3
        for _ in range(5):
            assert client.get("/readyz").json()["security_services"]["fresh"]
        assert len(checked) == 3
        monkeypatch.setattr(main, "settings", configured(clamav_host="different.example"))
        assert not main.security_service_snapshot()["fresh"]
    assert main.app.state.security_services is None
    assert closed == [True]
    assert not monitor.snapshot()["fresh"]
