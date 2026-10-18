"""Bounded, read-only checks of the existing private security services.

PING proves connectivity; VERSION checks the loaded daily database age, not
malware detection, shared counters, or alert delivery. Never send files or keys.
Only fixed status fields leave this module; no URLs, credentials or exceptions.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import re
from time import monotonic

from redis.asyncio import Redis

INTERVAL_SECONDS = 60
MAX_AGE_SECONDS = 120
PROBE_TIMEOUT_SECONDS = 3
CLOSE_TIMEOUT_SECONDS = 0.25
SERVICES = ("clamav", "redis", "clamav_signatures")
SIGNATURE_MAX_AGE_SECONDS = 72 * 3600
SIGNATURE_FUTURE_TOLERANCE_SECONDS = 300


def signature_status(reply: bytes, now: datetime) -> str:
    # clamd VERSION uses ctime in the daemon's timezone. Our scanner entrypoint
    # pins TZ=UTC0 and LC_ALL=C. Do not infer a timezone from the web process.
    match = re.fullmatch(
        rb"ClamAV [0-9]+\.[0-9]+\.[0-9]+/[1-9][0-9]*/"
        rb"(Mon|Tue|Wed|Thu|Fri|Sat|Sun) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
        rb"( [1-9]|[0-3][0-9]) ([0-2][0-9]):([0-5][0-9]):([0-5][0-9]) ([0-9]{4})\x00",
        reply,
    )
    if not match:
        return "unavailable"
    weekday, month, day, hour, minute, second, year = (x.decode("ascii") for x in match.groups())
    try:
        stamp = datetime(int(year), "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split().index(month) + 1,
                         int(day), int(hour), int(minute), int(second), tzinfo=timezone.utc)
        if "Mon Tue Wed Thu Fri Sat Sun".split()[stamp.weekday()] != weekday:
            return "unavailable"
        age = (now - stamp).total_seconds()
    except (ValueError, TypeError):
        return "unavailable"
    if age < -SIGNATURE_FUTURE_TOLERANCE_SECONDS:
        return "clock_invalid"
    return "definitions_stale" if age > SIGNATURE_MAX_AGE_SECONDS else "ready"


async def probe_clamav_signatures(settings) -> str:
    if not settings.clamav_host:
        return "not_configured"
    writer = None
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            reader, writer = await asyncio.open_connection(settings.clamav_host, settings.clamav_port, limit=128)
            writer.write(b"zVERSION\0")
            await writer.drain()
            return signature_status(await reader.readuntil(b"\0"), datetime.now(timezone.utc))
    except Exception:
        return "unavailable"
    finally:
        if writer is not None:
            writer.close()
            try:
                async with asyncio.timeout(CLOSE_TIMEOUT_SECONDS):
                    await writer.wait_closed()
            except Exception:
                pass


async def probe_clamav(settings) -> str:
    if not settings.clamav_host:
        return "not_configured"
    writer = None
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            reader, writer = await asyncio.open_connection(
                settings.clamav_host, settings.clamav_port, limit=32,
            )
            writer.write(b"zPING\0")
            await writer.drain()
            reply = await reader.readuntil(b"\0")
            # A generic TCP connection or an error string is not a healthy PING.
            return "ready" if reply == b"PONG\0" else "unavailable"
    except Exception:
        return "unavailable"
    finally:
        if writer is not None:
            writer.close()
            try:
                async with asyncio.timeout(CLOSE_TIMEOUT_SECONDS):
                    await writer.wait_closed()
            except Exception:
                pass


async def probe_redis(settings) -> str:
    if not settings.redis_url:
        return "not_configured"
    client = None
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            client = Redis.from_url(
                settings.redis_url, max_connections=1,
                socket_connect_timeout=2, socket_timeout=2,
                retry_on_timeout=False, health_check_interval=0,
            )
            return "ready" if await client.ping() is True else "unavailable"
    except Exception:
        return "unavailable"
    finally:
        if client is not None:
            try:
                async with asyncio.timeout(CLOSE_TIMEOUT_SECONDS):
                    await client.aclose()
            except Exception:
                pass


def emit_status(event):
    print(json.dumps(event, ensure_ascii=True, separators=(",", ":")), flush=True)


def unchecked_snapshot():
    return {"checked_at": None, "fresh": False, "read_only": True,
            "services": {name: {"ready": False, "status": "not_checked"} for name in SERVICES}}


class SecurityServiceMonitor:
    def __init__(self, settings, *, clock=monotonic, emit=emit_status):
        self.settings = settings
        self.clock = clock
        self.emit = emit
        self._configuration = self.configuration(settings)
        self._checked_at = None
        self._checked_clock = None
        self._states = {name: "not_checked" for name in SERVICES}
        self._stopped = False

    @staticmethod
    def configuration(settings):
        # Internal comparison only. Never serialize this tuple.
        return (settings.environment, settings.demo_mode, settings.clamav_host,
                settings.clamav_port, settings.redis_url)

    def matches(self, settings):
        return self._configuration == self.configuration(settings)

    def snapshot(self):
        age = None if self._checked_clock is None else self.clock() - self._checked_clock
        fresh = not self._stopped and age is not None and 0 <= age <= MAX_AGE_SECONDS
        states = {}
        for name in SERVICES:
            status = self._states[name]
            if self._stopped:
                status = "stopped"
            elif age is not None and not fresh:
                status = "stale"
            states[name] = {"ready": fresh and status == "ready", "status": status}
        return {"checked_at": self._checked_at, "fresh": fresh,
                "read_only": True, "services": states}

    async def check(self):
        if self.settings.demo_mode or not self.settings.is_deployed:
            self._states = {name: "disabled" for name in SERVICES}
        else:
            try:
                # Also bounds an unexpected regression in an individual probe.
                async with asyncio.timeout(PROBE_TIMEOUT_SECONDS + 1):
                    async with asyncio.TaskGroup() as group:
                        clamav = group.create_task(probe_clamav(self.settings))
                        redis = group.create_task(probe_redis(self.settings))
                        signatures = group.create_task(probe_clamav_signatures(self.settings))
                    results = (clamav.result(), redis.result(), signatures.result())
                self._states = {
                    name: status if status in {"ready", "unavailable", "not_configured", "definitions_stale", "clock_invalid"} else "unavailable"
                    for name, status in zip(SERVICES, results)
                }
            except Exception:
                self._states = {name: "unavailable" for name in SERVICES}
        self._checked_at = datetime.now(timezone.utc).isoformat()
        self._checked_clock = self.clock()
        self._stopped = False
        return self.snapshot()

    async def run(self):
        try:
            while True:
                result = await self.check()
                self.emit({"event": "security_service_probe",
                           "level": "info" if all(s["ready"] for s in result["services"].values()) else "warning",
                           **result})
                await asyncio.sleep(INTERVAL_SECONDS)
        finally:
            # Shutdown/crash must not leave the previous healthy status usable.
            self._stopped = True


def main():
    from .config import Settings
    monitor = SecurityServiceMonitor(Settings())
    result = asyncio.run(monitor.check())
    emit_status(result)
    return 0 if all(s["ready"] for s in result["services"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
