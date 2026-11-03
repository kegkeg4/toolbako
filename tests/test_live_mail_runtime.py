"""Local-only wiring tests. Provider calls and runtime connections are mocked."""
import asyncio
from contextlib import asynccontextmanager
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app import live_mail_runtime as runtime
from app import live_mail_worker as worker
from app.financial_scope import SCOPE_ENDPOINT
from app.database import content_hash
from app.live_database_config import TARGET_HOST
from app.live_mail_preparation import MAIL_ROLE, MAIL_USER, SCOPE


CUTOFF = 1793664000
SECRET = "synthetic-password-never-output"


def run(coro):
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return asyncio.run(coro)


def environment(**updates):
    return {"APP_ENV": "production", "DEMO_MODE": "false",
            "LIVE_MAIL_POLICY_SINCE": str(CUTOFF), "LIVE_TRANSACTION_EMAILS_SINCE": str(CUTOFF),
            "EMAIL_API_KEY": "synthetic-resend-key", "EMAIL_FROM": "sender@example.invalid",
            "TOOLBAKO_LIVE_MAIL_PASSWORD": SECRET, **updates}


def test_config_defaults_to_no_delivery_and_redacts_secrets():
    settings = worker.LiveMailWorkerSettings(environment=environment())
    assert settings.delivery_enabled is False and settings.queue_since == CUTOFF
    assert SECRET not in repr(settings) + str(settings.backend)
    assert "synthetic-resend-key" not in repr(settings)
    with pytest.raises(runtime.LiveMailRuntimeError):
        run(settings.deliver(message_ids=[uuid4()]))


@pytest.mark.parametrize("update", [
    {"PGPASSWORD": SECRET}, {"pgoptions": SECRET}, {"DATABASE_URL": SECRET},
    {"STRIPE_SECRET_KEY": SECRET}, {"TOOLBAKO_LIVE_RUNTIME_PASSWORD": SECRET},
    {"TOOLBAKO_LIVE_MIGRATION_PASSWORD": SECRET}, {"SESSION_SECRET": SECRET},
    {"SUPABASE_SERVICE_ROLE_KEY": SECRET}, {"SUPABASE_SECRET_KEY": SECRET},
    {"LIVE_MAIL_POLICY_SINCE": "0"}, {"LIVE_MAIL_POLICY_SINCE": "1e9"},
    {"LIVE_TRANSACTION_EMAILS_SINCE": str(CUTOFF + 1)}, {"LIVE_MAIL_DELIVERY_ENABLED": "1"},
    {"LIVE_MAIL_DELIVERY_ENABLED": "true"}, {"LIVE_TRANSACTION_EMAILS_ENABLED": "yes"},
    {"APP_ENV": "development"}, {"DEMO_MODE": "true"}, {"TOOLBAKO_LIVE_MAIL_PASSWORD": ""},
    {"EMAIL_API_KEY": ""}, {"EMAIL_FROM": ""},
])
def test_bad_configuration_is_fixed_error_with_no_fallback_or_secret_echo(update):
    with pytest.raises(runtime.LiveMailRuntimeError) as error:
        worker.LiveMailWorkerSettings(environment=environment(**update))
    assert str(error.value) == "live_mail_runtime_unverified"


def test_delivery_requires_both_explicit_flags_but_does_not_auto_send(monkeypatch):
    delivery = AsyncMock(return_value={"sent": 0, "retry": 0, "review": 0})
    monkeypatch.setattr(worker, "deliver_pending", delivery)
    settings = worker.LiveMailWorkerSettings(environment=environment(
        LIVE_MAIL_DELIVERY_ENABLED="true", LIVE_TRANSACTION_EMAILS_ENABLED="true"))
    assert settings.delivery_enabled is True and delivery.call_count == 0
    ids = [uuid4()]
    run(settings.deliver(message_ids=ids))
    assert delivery.await_args.kwargs["message_ids"] == ids
    assert delivery.await_args.kwargs["limit"] == 20


@pytest.mark.parametrize("values", [None, [], "value", ["not-a-uuid"], [SECRET], [str(uuid4()).upper()]])
def test_invalid_message_selection_never_falls_back_to_entire_queue(values):
    with pytest.raises(runtime.LiveMailRuntimeError) as error:
        worker.selected_ids(values)
    assert str(error.value) == "live_mail_runtime_unverified"


def test_message_selection_is_explicit_bounded_and_unique():
    value = str(uuid4())
    assert str(worker.selected_ids([value])[0]) == value
    with pytest.raises(runtime.LiveMailRuntimeError):
        worker.selected_ids([value, value])
    with pytest.raises(runtime.LiveMailRuntimeError):
        worker.selected_ids([str(uuid4()) for _ in range(101)])


class Connection:
    def __init__(self):
        self.info = SimpleNamespace(host=TARGET_HOST, port=5432, dbname="postgres", user=MAIL_USER)
        self.pgconn = SimpleNamespace(ssl_in_use=True)
        self.closed = False
        self.kind = None
        self.identity = (MAIL_ROLE, MAIL_ROLE)
        self.binding = (SCOPE_ENDPOINT, content_hash(SCOPE.payload()), "succeeded", SCOPE.payload())
    async def execute(self, statement, parameters=()):
        self.kind = "identity" if "current_user,session_user" in statement else (
            "schema" if "schema_version" in statement else "binding")
        return self
    async def fetchone(self):
        return self.identity
    async def fetchall(self):
        return [(True, 2)] if self.kind == "schema" else [self.binding]
    @asynccontextmanager
    async def transaction(self):
        yield self
    async def close(self):
        self.closed = True
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        await self.close()


def test_actual_connection_boundary_checks_fixed_target_and_binding(monkeypatch):
    privilege = AsyncMock(return_value=True)
    monkeypatch.setattr(runtime, "verify_mail_access", privilege)
    assert run(runtime.verify_mail_connection(Connection(), queue_since=CUTOFF)) is True
    assert privilege.await_count == 1


@pytest.mark.parametrize("field,value", [
    ("host", "old-source.supabase.co"), ("port", 6543), ("dbname", "other"),
    ("user", "postgres.xijdnqnsuhuciqyygnkg"), ("ssl", False),
    ("identity", ("postgres", "postgres")),
    ("binding", (SCOPE_ENDPOINT, "wrong", "succeeded", SCOPE.payload())),
    ("binding", (SCOPE_ENDPOINT, content_hash(SCOPE.payload()), "unknown", None)),
])
def test_wrong_connection_or_scope_cannot_reach_claim(monkeypatch, field, value):
    monkeypatch.setattr(runtime, "verify_mail_access", AsyncMock(return_value=True))
    connection = Connection()
    if field in {"identity", "binding"}:
        setattr(connection, field, value)
    elif field == "ssl":
        connection.pgconn.ssl_in_use = value
    else:
        setattr(connection.info, field, value)
    with pytest.raises(runtime.LiveMailRuntimeError):
        run(runtime.verify_mail_connection(connection, queue_since=CUTOFF))


def test_backend_reverifies_every_connection_and_never_uses_request_init(monkeypatch):
    first, second = Connection(), Connection()
    connect = AsyncMock(side_effect=[first, second])
    monkeypatch.setattr(runtime.psycopg.AsyncConnection, "connect", connect)
    verify = AsyncMock(return_value=True)
    monkeypatch.setattr(runtime, "verify_mail_connection", verify)
    backend = runtime.LiveMailBackend(SECRET, queue_since=CUTOFF, environment={})
    async def scenario():
        async with await backend.connect():
            pass
        async with await backend.connect():
            pass
    run(scenario())
    assert verify.await_count == connect.await_count == 2
    assert first.closed and second.closed
    assert not any(hasattr(backend, key) for key in ("request", "init", "save", "dsn"))


def test_backend_guard_failure_closes_connection_and_suppresses_provider_text(monkeypatch):
    connection = Connection()
    monkeypatch.setattr(runtime.psycopg.AsyncConnection, "connect", AsyncMock(return_value=connection))
    monkeypatch.setattr(runtime, "verify_mail_connection", AsyncMock(side_effect=ValueError(SECRET)))
    backend = runtime.LiveMailBackend(SECRET, queue_since=CUTOFF, environment={})
    with pytest.raises(runtime.LiveMailRuntimeError) as error:
        run(backend.connect())
    assert str(error.value) == "live_mail_runtime_unverified" and connection.closed


def test_cancelled_connect_verification_closes_without_swallowing_cancellation(monkeypatch):
    connection = Connection()
    monkeypatch.setattr(runtime.psycopg.AsyncConnection, "connect", AsyncMock(return_value=connection))
    monkeypatch.setattr(runtime, "verify_mail_connection", AsyncMock(side_effect=asyncio.CancelledError()))
    backend = runtime.LiveMailBackend(SECRET, queue_since=CUTOFF, environment={})
    with pytest.raises(asyncio.CancelledError):
        run(backend.connect())
    assert connection.closed


def test_default_wrapper_preserves_existing_monitor_without_transactional_send(monkeypatch):
    settings = SimpleNamespace(backend=SimpleNamespace(connect=AsyncMock(return_value=Connection())),
                               delivery_enabled=False, deliver=AsyncMock())
    seen = []
    async def monitor(url, recipient, *, stop, backend):
        seen.append((url, recipient, backend))
        stop.set()
        return 0
    monkeypatch.setattr(worker.service_monitor, "run", monitor)
    assert run(worker.run(settings)) == 0
    assert seen == [(worker.LOGIN_URL, worker.OPERATOR_RECIPIENT, settings.backend)]
    assert settings.deliver.await_count == 0


@pytest.mark.parametrize("arguments", [
    {"apply": True}, {"message_ids": [str(uuid4())]}, {"new_queue": True},
    {"apply": True, "new_queue": True},
    {"apply": True, "message_ids": [str(uuid4())], "new_queue": True},
])
def test_missing_flag_or_scope_fails_before_any_connection(arguments):
    settings = SimpleNamespace(backend=SimpleNamespace(connect=AsyncMock()), delivery_enabled=False)
    with pytest.raises(runtime.LiveMailRuntimeError):
        run(worker.run(settings, **arguments))
    assert settings.backend.connect.await_count == 0


def test_canary_only_runs_once_and_monitor_continues_until_stop(monkeypatch, capsys):
    settings = SimpleNamespace(backend=SimpleNamespace(connect=AsyncMock(return_value=Connection())), delivery_enabled=True)
    async def scenario():
        stop, delivered = asyncio.Event(), asyncio.Event()
        async def deliver(**kwargs):
            delivered.set()
            return {"sent": 1, "retry": 0, "review": 0}
        settings.deliver = AsyncMock(side_effect=deliver)
        async def monitor(url, recipient, *, stop, backend):
            await stop.wait()
            return 0
        monkeypatch.setattr(worker.service_monitor, "run", monitor)
        value = str(uuid4())
        task = asyncio.create_task(worker.run(settings, apply=True, message_ids=[value], stop=stop))
        await asyncio.wait_for(delivered.wait(), 1)
        await asyncio.sleep(0)
        assert not task.done() and settings.deliver.await_count == 1
        assert tuple(map(str, settings.deliver.await_args.kwargs["message_ids"])) == (value,)
        stop.set()
        assert await task == 0
        return value
    ident = run(scenario())
    output = capsys.readouterr().out
    assert "live_mail_batch" in output and ident not in output and SECRET not in output


def test_sender_crash_stops_supervised_service_without_fallback(monkeypatch):
    settings = SimpleNamespace(backend=SimpleNamespace(connect=AsyncMock(return_value=Connection())),
        delivery_enabled=True, deliver=AsyncMock(side_effect=runtime.LiveMailRuntimeError()))
    cancelled = []
    async def monitor(url, recipient, *, stop, backend):
        try:
            await stop.wait()
            return 0
        finally:
            cancelled.append(True)
    monkeypatch.setattr(worker.service_monitor, "run", monitor)
    with pytest.raises(runtime.LiveMailRuntimeError):
        run(worker.run(settings, apply=True, new_queue=True))
    assert cancelled == [True] and settings.deliver.await_count == 1


def test_cli_does_not_echo_unknown_options_or_credentials(capsys):
    assert worker.main(["--unknown-" + SECRET]) == 1
    text = capsys.readouterr()
    assert SECRET not in text.out + text.err
