"""Explicit error-only monitoring; never export marketplace/private payloads.

Do not add raw request, exception-message, breadcrumb or scope fields here.
Rebuild events from an allowlist instead of guessing every possible secret key.
"""
from __future__ import annotations

from pathlib import Path
import logging
import re
from types import TracebackType

from .request_context import request_id_context

APP_ROOT = Path(__file__).resolve().parent
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,79}\Z")
EVENT_ID = re.compile(r"[0-9a-f]{32}\Z")


def discard_telemetry(value, hint):
    return None


def private_error_event(event: dict, hint: dict) -> dict | None:
    """Only actual exceptions can leave the server, without their string value.

Sentry adds scope/request data before before_send. Ignore all of that and retain
only the exception class, our own code locations, and a server-generated ID.
Attachments live in the hint rather than the event, so remove those too.
"""
    hint.pop("attachments", None)
    info = hint.get("exc_info")
    if not isinstance(info, tuple) or len(info) != 3 or not isinstance(info[1], BaseException):
        return None
    error_name = type(info[1]).__name__
    if not IDENTIFIER.fullmatch(error_name):
        error_name = "Exception"
    event_id = event.get("event_id", "")
    if not isinstance(event_id, str) or not EVENT_ID.fullmatch(event_id):
        return None

    frames = []
    trace = info[2]
    while isinstance(trace, TracebackType):
        code = trace.tb_frame.f_code
        try:
            relative = Path(code.co_filename).resolve().relative_to(APP_ROOT)
        except (ValueError, OSError):
            relative = None
        # Dependency paths and absolute server/home paths are deliberately absent.
        if relative is not None and len(frames) < 40:
            frame = {"filename": f"app/{relative.as_posix()}", "lineno": trace.tb_lineno, "in_app": True}
            if IDENTIFIER.fullmatch(code.co_name):
                frame["function"] = code.co_name
            frames.append(frame)
        trace = trace.tb_next

    exception = {"type": error_name, "value": "Error details withheld"}
    if frames:
        exception["stacktrace"] = {"frames": frames}
    clean = {"event_id": event_id, "level": "error", "platform": "python", "exception": {"values": [exception]}}
    environment = event.get("environment")
    if isinstance(environment, str) and environment in {"development", "staging", "production"}:
        clean["environment"] = environment
    request_id = request_id_context.get()
    if isinstance(request_id, str) and EVENT_ID.fullmatch(request_id):
        clean["tags"] = {"request_id": request_id}
    return clean


def monitoring_options(settings) -> dict:
    """No automatic integrations, tracing, profiling, logs, metrics or sessions.

ProductionGuardMiddleware explicitly captures unexpected request exceptions.
An SDK/package update must pass the serialized-envelope tests before deploying.
"""
    return {
        "dsn": settings.sentry_dsn,
        "environment": settings.environment,
        "send_default_pii": False,
        "default_integrations": False,
        "auto_enabling_integrations": False,
        "include_local_variables": False,
        "include_source_context": False,
        "max_request_body_size": "never",
        "max_breadcrumbs": 0,
        "traces_sample_rate": 0.0,
        "profiles_sample_rate": 0.0,
        "profile_session_sample_rate": 0.0,
        "trace_propagation_targets": [],
        "propagate_traces": False,
        "auto_session_tracking": False,
        "enable_logs": False,
        "enable_metrics": False,
        "send_client_reports": False,
        "debug": False,
        "before_send": private_error_event,
        "before_breadcrumb": discard_telemetry,
        "before_send_transaction": discard_telemetry,
        "before_send_log": discard_telemetry,
        "before_send_metric": discard_telemetry,
    }


def initialize_monitoring(settings) -> bool:
    if not settings.sentry_dsn:
        return False
    import sentry_sdk

    try:
        sentry_sdk.init(**monitoring_options(settings))
    except Exception:
        # Invalid DSNs/options can include the original value in SDK exceptions.
        logging.getLogger("toolbako.monitoring").error("Error monitoring configuration is invalid")
        raise RuntimeError("Error monitoring configuration is invalid") from None
    return True
