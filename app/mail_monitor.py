"""Observe the mail queue without claiming rows, sending or changing data.

One-shot checks retain the worker's exit codes: 0 clear, 2 needs attention,
1 unavailable. --watch keeps an observer alive through queue warnings; three
consecutive failed checks stop it with exit 1. Process uptime is not delivery
health. Every poll emits an explicit, privacy-limited status to platform logs.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import signal

from .database import PostgresStateStore
from .operations import inspect_mail


COUNT_KEYS = (
    "mail_pending", "mail_overdue", "mail_expired_lease", "mail_review",
    "mail_sending", "mail_missing_lease",
)
ATTENTION_KEYS = ("mail_overdue", "mail_expired_lease", "mail_review", "mail_missing_lease")
PROBE_TIMEOUT = 15
MAX_FAILURES = 3


def emit_status(event):
    print(json.dumps(event, ensure_ascii=True, separators=(",", ":")), flush=True)


async def observe(backend, *, watch=False, interval=60, stop=None, emit=emit_status):
    if type(interval) is not int or not 60 <= interval <= 3600:
        raise ValueError("Observation interval must be 60..3600 seconds")
    stop = stop if stop is not None else asyncio.Event()
    failures = 0
    last_success = None
    while not stop.is_set():
        counts = None
        try:
            async with asyncio.timeout(PROBE_TIMEOUT):
                snapshot = await inspect_mail(backend)
            # Never forward raw query results, exceptions, or future fields.
            counts = {key: snapshot["counts"][key] for key in COUNT_KEYS}
            if any(type(value) is not int or value < 0 for value in counts.values()):
                raise ValueError("Invalid counts")
        except Exception:
            failures += 1
            counts = None
            state, level, code = "unavailable", "error", 1
        else:
            failures = 0
            last_success = datetime.now(timezone.utc).isoformat()
            attention = any(counts[key] for key in ATTENTION_KEYS)
            state, level, code = ("attention", "warning", 2) if attention else ("clear", "info", 0)
        emit({
            "event": "mail_queue_observation", "level": level, "status": state,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "last_success_at": last_success, "counts": counts,
            "consecutive_failures": failures,
            "read_only": True, "delivery_enabled": False,
            "will_exit": not watch or failures >= MAX_FAILURES,
        })
        if not watch or failures >= MAX_FAILURES:
            return code
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass
    emit({"event": "mail_monitor_stopped", "level": "info", "reason": "stop_requested",
          "read_only": True, "delivery_enabled": False})
    return 0


async def run_monitor(backend, *, watch, interval):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, lambda *_: loop.call_soon_threadsafe(stop.set))
        return await observe(backend, watch=watch, interval=interval, stop=stop)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only mail queue observer. Never sends mail.")
    parser.add_argument("--watch", action="store_true", help="Keep checking; queue warnings remain visible in logs")
    parser.add_argument("--interval", type=int, default=60, help="Seconds between completed checks (60..3600)")
    args = parser.parse_args(argv)
    if not 60 <= args.interval <= 3600:
        parser.error("--interval must be between 60 and 3600 seconds")
    from .config import settings
    try:
        backend = PostgresStateStore(settings.database_url, production=settings.is_deployed)
        return asyncio.run(run_monitor(backend, watch=args.watch, interval=args.interval))
    except Exception:
        # Configuration and provider exceptions can contain credentials.
        emit_status({"event": "mail_monitor_failed", "level": "error", "status": "unavailable",
                     "read_only": True, "delivery_enabled": False})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
