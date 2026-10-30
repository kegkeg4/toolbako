"""Queue warnings, failure recovery, privacy and real restricted DB isolation."""
import asyncio
import json
import signal

import pytest

from app import mail_monitor, mail_worker
from test_database import database, query
from test_mail_worker_scope import enqueue, restricted_worker, rows


PRIVATE = "synthetic-private-value-never-log"


def counts(**overrides):
    return {key: overrides.get(key, 0) for key in mail_monitor.COUNT_KEYS}


class ImmediateStop:
    """Simulate elapsed intervals without sleeping or changing runtime bounds."""
    stopped = False

    def is_set(self):
        return self.stopped

    async def wait(self):
        raise TimeoutError


def test_watch_reports_warning_unavailable_and_recovery_without_sending(monkeypatch):
    stop, events = ImmediateStop(), []
    snapshots = iter([
        counts(mail_pending=2, mail_overdue=2),
        RuntimeError(PRIVATE),
        counts(mail_pending=2, mail_overdue=2),
        counts(),
    ])
    async def inspect(_):
        result = next(snapshots)
        if isinstance(result, Exception):
            raise result
        return {"counts": {**result, "unexpected_private_field": PRIVATE}, "notice": PRIVATE}
    async def forbidden(*args, **kwargs):
        pytest.fail("Observer must never deliver mail")
    def emit(event):
        events.append(event)
        if len(events) == 4:
            stop.stopped = True
    monkeypatch.setattr(mail_monitor, "inspect_mail", inspect)
    monkeypatch.setattr(mail_worker, "deliver_pending", forbidden)
    assert asyncio.run(mail_monitor.observe(None, watch=True, stop=stop, emit=emit)) == 0
    assert [e.get("status") for e in events[:4]] == ["attention", "unavailable", "attention", "clear"]
    assert [e["consecutive_failures"] for e in events[:4]] == [0, 1, 0, 0]
    assert events[1]["counts"] is None  # Never publish stale counts as a new success.
    assert events[1]["last_success_at"] == events[0]["last_success_at"]
    assert events[2]["level"] == "warning" and not events[2]["will_exit"]
    assert events[-1]["event"] == "mail_monitor_stopped"
    assert PRIVATE not in json.dumps(events)
    assert all(e["read_only"] and not e["delivery_enabled"] for e in events)


def test_repeated_failure_exits_nonzero_without_exception_text(monkeypatch):
    events = []
    async def fails(_):
        raise RuntimeError(PRIVATE)
    monkeypatch.setattr(mail_monitor, "inspect_mail", fails)
    assert asyncio.run(mail_monitor.observe(None, watch=True, stop=ImmediateStop(), emit=events.append)) == 1
    assert len(events) == 3 and events[-1]["will_exit"]
    assert [event["consecutive_failures"] for event in events] == [1, 2, 3]
    assert all(e["counts"] is None and e["last_success_at"] is None for e in events)
    assert PRIVATE not in json.dumps(events)


def test_probe_timeout_cancels_inflight_work_and_reports_unavailable(monkeypatch):
    closed, events = [], []
    async def stalls(_):
        try:
            await asyncio.Event().wait()
        finally:
            closed.append(True)
    monkeypatch.setattr(mail_monitor, "inspect_mail", stalls)
    monkeypatch.setattr(mail_monitor, "PROBE_TIMEOUT", 0.01)
    assert asyncio.run(mail_monitor.observe(None, emit=events.append)) == 1
    assert closed == [True] and events[0]["status"] == "unavailable"


@pytest.mark.parametrize("overrides,code", [({}, 0), ({"mail_overdue": 1}, 2),
    ({"mail_expired_lease": 1}, 2), ({"mail_review": 1}, 2), ({"mail_missing_lease": 1}, 2),
    ({"mail_pending": 1, "mail_sending": 1}, 0)])
def test_one_shot_retains_warning_exit_codes(monkeypatch, overrides, code):
    async def inspect(_):
        return {"counts": counts(**overrides)}
    monkeypatch.setattr(mail_monitor, "inspect_mail", inspect)
    events = []
    assert asyncio.run(mail_monitor.observe(None, emit=events.append)) == code
    assert len(events) == 1 and events[0]["will_exit"]


@pytest.mark.parametrize("value", [PRIVATE, -1, True, None, {}])
def test_unexpected_count_payload_is_not_exported(monkeypatch, value):
    async def inspect(_):
        return {"counts": counts(mail_pending=value)}
    monkeypatch.setattr(mail_monitor, "inspect_mail", inspect)
    events = []
    assert asyncio.run(mail_monitor.observe(None, emit=events.append)) == 1
    assert events[0]["counts"] is None and PRIVATE not in json.dumps(events)


@pytest.mark.parametrize("args", [["--interval", "59"], ["--interval", "3601"], ["--apply"], ["--all-pending"]])
def test_invalid_or_sending_options_rejected_before_connection(monkeypatch, args):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid CLI must not initialize DB")
    monkeypatch.setattr(mail_monitor, "PostgresStateStore", forbidden)
    with pytest.raises(SystemExit) as exc:
        mail_monitor.main(args)
    assert exc.value.code == 2


def test_sigterm_stops_observer_and_restores_signal_handlers(monkeypatch):
    previous = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    async def observe(_, *, stop, **kwargs):
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        await asyncio.wait_for(stop.wait(), timeout=1)
        return 0
    monkeypatch.setattr(mail_monitor, "observe", observe)
    assert asyncio.run(mail_monitor.run_monitor(None, watch=True, interval=60)) == 0
    assert all(signal.getsignal(s) == handler for s, handler in previous.items())


def test_restricted_database_observation_preserves_every_queue_field(restricted_worker):
    backend, dsn = restricted_worker
    enqueue(dsn)
    overdue = enqueue(dsn)
    query(dsn, "update toolbako_runtime.email_outbox set created_at=now()-interval '1 hour' where id=%s returning id", (overdue,))
    before, events = rows(dsn), []
    assert asyncio.run(mail_monitor.observe(backend, emit=events.append)) == 2
    assert events[0]["counts"] == counts(mail_pending=2, mail_overdue=1)
    assert rows(dsn) == before
    assert "recipient" not in json.dumps(events) and "Synthetic" not in json.dumps(events)
