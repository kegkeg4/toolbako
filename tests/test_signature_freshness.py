import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import security_services as services
from app.config import Settings
from app.production import readiness_summary


NOW = datetime(2026, 10, 18, 10, tzinfo=timezone.utc)


def reply(stamp):
    weekdays = "Mon Tue Wed Thu Fri Sat Sun".split()
    months = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    return (f"ClamAV 1.5.4/28157/{weekdays[stamp.weekday()]} {months[stamp.month-1]} "
            f"{stamp.day:2} {stamp.hour:02}:{stamp.minute:02}:{stamp.second:02} {stamp.year}\0").encode()


@pytest.mark.parametrize("age,expected", [
    (0, "ready"), (72 * 3600, "ready"), (72 * 3600 + 1, "definitions_stale"),
    (100 * 86400, "definitions_stale"), (-300, "ready"), (-301, "clock_invalid"),
])
def test_loaded_signature_age_boundaries(age, expected):
    assert services.signature_status(reply(NOW-timedelta(seconds=age)), NOW) == expected


@pytest.mark.parametrize("raw", [
    b"PONG\0", b"ClamAV 1.5.4\0", b"COMMAND UNAVAILABLE\0", b"private-secret\0",
    b"ClamAV 1.5.4/28157/Sun Oct 18 25:00:00 2026\0",
    b"ClamAV 1.5.4/28157/Sun Feb 31 10:00:00 2026\0",
    b"ClamAV 1.5.4/28157/Mon Oct 18 10:00:00 2026\0",  # wrong weekday
    reply(NOW).replace(b"28157", b"0"), reply(NOW)+b"injected", reply(NOW)[:-1], b"\xff\0",
])
def test_unknown_or_malformed_version_is_never_healthy(raw):
    assert services.signature_status(raw, NOW) == "unavailable"


@pytest.mark.parametrize("mode", ["fresh", "stale", "oversized", "stall", "eof"])
def test_signature_probe_only_sends_version_and_bounds_reads(monkeypatch, mode):
    monkeypatch.setattr(services, "PROBE_TIMEOUT_SECONDS", .05)
    async def scenario():
        commands = []
        closed = asyncio.Event()
        async def handle(reader, writer):
            try:
                commands.append(await reader.readuntil(b"\0"))
                if mode in {"fresh", "stale"}:
                    stamp = datetime.now(timezone.utc) - timedelta(days=4 if mode == "stale" else 0)
                    value = reply(stamp)
                    writer.write(value[:20]); await writer.drain()
                    writer.write(value[20:]); await writer.drain()
                elif mode == "oversized":
                    writer.write(b"x" * 300); await writer.drain()
                elif mode == "stall":
                    assert await reader.read() == b""
            finally:
                writer.close()
                await writer.wait_closed()
                closed.set()
        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        async with server:
            result = await services.probe_clamav_signatures(Settings(clamav_host="127.0.0.1", clamav_port=server.sockets[0].getsockname()[1]))
            await asyncio.wait_for(closed.wait(), 1)
        assert commands == [b"zVERSION\0"]
        assert result == {"fresh": "ready", "stale": "definitions_stale"}.get(mode, "unavailable")
    asyncio.run(scenario())


def test_fresh_signatures_required_for_readiness_and_recovery(monkeypatch):
    settings = Settings(environment="staging", demo_mode=False, clamav_host="private", redis_url="redis://private")
    statuses = iter(["definitions_stale", "clock_invalid", "ready"])
    async def ping(_): return "ready"
    async def signatures(_): return next(statuses)
    monkeypatch.setattr(services, "probe_clamav", ping)
    monkeypatch.setattr(services, "probe_redis", ping)
    monkeypatch.setattr(services, "probe_clamav_signatures", signatures)
    monitor = services.SecurityServiceMonitor(settings)
    async def scenario():
        for expected in [False, False, True]:
            result = await monitor.check()
            observed = {name: value["ready"] for name, value in result["services"].items()}
            checks = {c["key"]: c["ok"] for c in readiness_summary(settings, runtime_security_ready=observed)["checks"]}
            assert checks["file_scan"] is expected
            assert checks["distributed_rate_limits"] is True
    asyncio.run(scenario())


def test_signature_missing_configuration_never_connects(monkeypatch):
    async def forbidden(*a, **k): pytest.fail("Unconfigured scanner must not connect")
    monkeypatch.setattr(services.asyncio, "open_connection", forbidden)
    assert asyncio.run(services.probe_clamav_signatures(Settings(clamav_host=""))) == "not_configured"
