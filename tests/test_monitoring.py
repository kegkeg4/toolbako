import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
import sentry_sdk
from sentry_sdk.attachments import Attachment
from sentry_sdk.transport import Transport

from app.config import Settings
from app.middleware import ProductionGuardMiddleware
from app.monitoring import initialize_monitoring, monitoring_options, private_error_event
from app.request_context import request_id_context


PRIVATE = "synthetic-private-value-never-export"


class CapturingTransport(Transport):
    """No socket, DNS, real account or external telemetry is used by these tests."""
    def __init__(self, options):
        super().__init__(options)
        self.envelopes = []

    def capture_envelope(self, envelope):
        self.envelopes.append(envelope)


def capture_error():
    try:
        raise RuntimeError(f"https://user:{PRIVATE}@provider.invalid/?token={PRIVATE}")
    except RuntimeError as error:
        return type(error), error, error.__traceback__


@pytest.mark.parametrize("environment", ["development", "staging", "production"])
def test_serialized_sdk_envelope_excludes_private_data_and_attachments(environment):
    client = sentry_sdk.Client(
        **monitoring_options(Settings(environment=environment, sentry_dsn="https://public@example.invalid/1")),
        transport=CapturingTransport,
    )
    token = request_id_context.set("f" * 32)
    try:
        event = {
            "exception": {"values": [{"type": "RuntimeError", "value": PRIVATE,
                "stacktrace": {"frames": [{"filename": f"/home/{PRIVATE}/worker.py", "vars": {"key": PRIVATE}, "context_line": PRIVATE}]}}]},
            "user": {"email": f"{PRIVATE}@example.invalid", "ip_address": "192.0.2.1"},
            "request": {"url": f"https://example.invalid/{PRIVATE}", "query_string": f"token={PRIVATE}",
                "headers": {"authorization": f"Bearer {PRIVATE}", "cookie": PRIVATE}, "data": {"password": PRIVATE}},
            "breadcrumbs": {"values": [{"message": PRIVATE, "data": {"url": PRIVATE}}]},
            "message": PRIVATE, "logentry": {"message": PRIVATE}, "transaction": PRIVATE,
            "extra": {"unknown_future_field": {"secret": PRIVATE}}, "tags": {"password": PRIVATE},
            "contexts": {"trace": {"dynamic_sampling_context": {"secret": PRIVATE}}, "private": {"token": PRIVATE}},
            "threads": {"values": [{"stacktrace": {"frames": [{"vars": {"password": PRIVATE}}]}}]},
            "server_name": PRIVATE, "release": PRIVATE, "dist": PRIVATE,
            "future_sdk_field": PRIVATE,
        }
        client.capture_event(event, hint={"exc_info": capture_error(),
            "attachments": [Attachment(bytes=PRIVATE.encode(), filename=f"{PRIVATE}.txt")]})
        assert len(client.transport.envelopes) == 1
        envelope = client.transport.envelopes[0]
        raw = envelope.serialize()
        assert PRIVATE.encode() not in raw and b"192.0.2.1" not in raw
        assert len(envelope.items) == 1 and envelope.items[0].type == "event"
        clean = envelope.items[0].payload.json
        assert set(clean) == {"event_id", "level", "platform", "exception", "environment", "tags"}
        assert clean["tags"] == {"request_id": "f" * 32}
        assert clean["exception"]["values"] == [{"type": "RuntimeError", "value": "Error details withheld"}]
        assert clean["environment"] == environment
    finally:
        request_id_context.reset(token)
        client.close(timeout=0)


def test_only_actual_exceptions_are_exported_and_other_telemetry_is_disabled():
    options = monitoring_options(Settings(sentry_dsn="https://public@example.invalid/1"))
    client = sentry_sdk.Client(**options, transport=CapturingTransport)
    try:
        client.capture_event({"message": PRIVATE})
        client.capture_event({"type": "transaction", "transaction": PRIVATE, "spans": []})
        assert client.transport.envelopes == []
        for key in ("enable_logs", "enable_metrics", "default_integrations", "auto_enabling_integrations",
                    "auto_session_tracking", "send_default_pii", "include_local_variables", "include_source_context", "propagate_traces"):
            assert options[key] is False
        assert options["traces_sample_rate"] == options["profiles_sample_rate"] == options["profile_session_sample_rate"] == 0
        for key in ("before_breadcrumb", "before_send_transaction", "before_send_log", "before_send_metric"):
            assert options[key]({"private": PRIVATE}, {}) is None
    finally:
        client.close(timeout=0)


@pytest.mark.parametrize("hint", [{}, {"exc_info": None}, {"exc_info": (None, "not-an-exception", None)}, {"exc_info": []}])
def test_invalid_exception_hints_fail_closed(hint):
    hint["attachments"] = [PRIVATE]
    assert private_error_event({"event_id": "f" * 32, "message": PRIVATE}, hint) is None
    assert "attachments" not in hint


def test_untrusted_event_metadata_and_context_id_are_not_exported():
    token = request_id_context.set(PRIVATE)
    try:
        clean = private_error_event({"event_id": "f" * 32, "environment": {"private": PRIVATE}}, {"exc_info": capture_error()})
        assert set(clean) == {"event_id", "level", "platform", "exception"}
        assert PRIVATE not in json.dumps(clean)
        assert private_error_event({"event_id": PRIVATE}, {"exc_info": capture_error()}) is None
    finally:
        request_id_context.reset(token)


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_request_ids_are_server_generated_and_safe_for_logs_and_monitoring(monkeypatch, caplog, environment):
    reports = []
    def capture(error):
        reports.append(private_error_event({"event_id": "a" * 32, "environment": environment},
            {"exc_info": (type(error), error, error.__traceback__)}))
    monkeypatch.setattr(sentry_sdk, "capture_exception", capture)
    app = FastAPI()
    @app.get("/test")
    def fails():
        raise RuntimeError(PRIVATE)
    app.add_middleware(ProductionGuardMiddleware, settings=Settings(environment=environment, redis_url=""))
    response = TestClient(app).get("/test", headers={"x-request-id": PRIVATE})
    assert response.status_code == 500
    request_id = response.headers["x-request-id"]
    assert re.fullmatch(r"[0-9a-f]{32}", request_id)
    assert PRIVATE not in caplog.text and PRIVATE not in response.text
    assert len(reports) == 1 and reports[0]["tags"]["request_id"] == request_id
    frames = reports[0]["exception"]["values"][0]["stacktrace"]["frames"]
    assert frames and frames[0]["filename"] == "app/middleware.py"
    assert all(set(frame) <= {"filename", "lineno", "function", "in_app"} for frame in frames)
    assert PRIVATE not in json.dumps(reports)


def test_initialization_is_explicit_and_uses_privacy_hooks(monkeypatch):
    calls = []
    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: calls.append(kwargs))
    assert not initialize_monitoring(SimpleNamespace(sentry_dsn=""))
    assert calls == []
    settings = Settings(sentry_dsn="https://public@example.invalid/1")
    assert initialize_monitoring(settings)
    assert calls == [monitoring_options(settings)]


def test_initialization_failure_does_not_expose_the_dsn(monkeypatch, caplog):
    def fails(**kwargs):
        raise ValueError(PRIVATE)
    monkeypatch.setattr(sentry_sdk, "init", fails)
    with pytest.raises(RuntimeError) as error:
        initialize_monitoring(Settings(sentry_dsn=PRIVATE))
    assert PRIVATE not in str(error.value) and PRIVATE not in caplog.text
    assert error.value.__suppress_context__


def test_monitoring_outage_does_not_break_the_safe_error_response(monkeypatch, caplog):
    def fails(*args, **kwargs):
        raise RuntimeError(PRIVATE)
    monkeypatch.setattr(sentry_sdk, "capture_exception", fails)
    app = FastAPI()
    @app.get("/test")
    def endpoint():
        raise RuntimeError(PRIVATE)
    app.add_middleware(ProductionGuardMiddleware, settings=Settings(environment="production", redis_url=""))
    response = TestClient(app).get("/test")
    assert response.status_code == 500
    assert response.json()["error"] == "internal_server_error"
    assert PRIVATE not in response.text and PRIVATE not in caplog.text
    assert all(record.exc_info is None for record in caplog.records if record.name == "toolbako.requests")


def test_deployment_commands_do_not_log_oauth_codes_or_private_search_queries():
    root = Path(__file__).resolve().parents[1]
    assert "--no-access-log" in json.loads((root / "railway.json").read_text())["deploy"]["startCommand"]
    assert "--no-access-log" in (root / "Procfile").read_text()
