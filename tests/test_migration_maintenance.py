"""HTTP admission tests only: no DB, provider, worker, or network access."""
import asyncio

import pytest
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.migration_maintenance import MigrationMaintenanceMiddleware


def scope(kind="http", *, path="/", method="GET", http_version="1.1", host=b"toolbako.example"):
    return {"type": kind, "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": http_version, "scheme": "https", "path": path,
            "raw_path": path.encode(), "root_path": "", "query_string": b"",
            "method": method, "headers": [(b"host", host)],
            "client": ("127.0.0.1", 12345), "server": ("toolbako.example", 443)}


def exchange(app, request_scope, *, receive_forbidden=True):
    messages, received = [], []
    async def receive():
        received.append(True)
        if receive_forbidden:
            pytest.fail("Maintenance must not read or buffer request bodies")
        return {"type": "http.request", "body": b"", "more_body": False}
    async def send(message): messages.append(message)
    asyncio.run(app(request_scope, receive, send))
    return messages, received


def response(messages):
    start, *parts = messages
    assert start["type"] == "http.response.start"
    return start["status"], dict(start["headers"]), b"".join(p.get("body", b"") for p in parts)


class Downstream:
    def __init__(self): self.calls = []
    async def __call__(self, request_scope, receive, send):
        self.calls.append((request_scope, receive, send))
        if request_scope["type"] == "http":
            await send({"type": "http.response.start", "status": 209, "headers": [(b"x-original", b"untouched")]})
            await send({"type": "http.response.body", "body": b"existing-response"})
        elif request_scope["type"] == "websocket":
            await send({"type": "websocket.accept"})
        else:
            await send({"type": "lifespan.startup.complete"})


@pytest.mark.parametrize("path", ["/", "/login", "/tools/tool-fixture", "/seller", "/admin",
                                  "/webhooks/stripe", "/webhooks/stripe/live/platform",
                                  "/webhooks/stripe/live/connect", "/readyz", "/static/app.css"])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_default_off_preserves_request_scope_and_response(path, method):
    target = Downstream()
    incoming = scope(path=path, method=method)
    messages, received = exchange(MigrationMaintenanceMiddleware(target), incoming)
    assert target.calls[0][0] is incoming and received == []
    assert response(messages) == (209, {b"x-original": b"untouched"}, b"existing-response")


@pytest.mark.parametrize("path", ["/", "/login", "/tools/tool-fixture", "/seller/tools/new", "/admin",
                                  "/webhooks/stripe", "/webhooks/stripe/live/platform",
                                  "/webhooks/stripe/live/connect", "/readyz", "/static/app.css",
                                  "/healthz/", "/deploymentz/", "/healthz-extra"])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_enabled_blocks_all_non_exempt_before_downstream_or_body(path, method):
    target = Downstream()
    incoming = scope(path=path, method=method)
    incoming["headers"].extend([(b"content-length", b"99999999999999999"),
                                 (b"cookie", b"toolbako_session=PRIVATE_COOKIE"),
                                 (b"stripe-signature", b"PRIVATE_SIGNATURE")])
    messages, received = exchange(MigrationMaintenanceMiddleware(target, mode="on", https_only=True), incoming)
    status, headers, body = response(messages)
    assert status == 503 and target.calls == [] and received == []
    assert headers[b"retry-after"].isdigit() and int(headers[b"retry-after"]) > 0
    assert b"no-store" in headers[b"cache-control"]
    assert headers[b"content-type"] == b"text/html; charset=utf-8"
    assert "メンテナンス中" in body.decode() and "しばらくしてから" in body.decode()
    assert b"PRIVATE" not in body and b"set-cookie" not in headers
    assert b"strict-transport-security" in headers


@pytest.mark.parametrize("path", ["/healthz", "/deploymentz"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_only_exact_operational_get_head_are_exempt(path, method):
    target = Downstream()
    incoming = scope(path=path, method=method)
    incoming["query_string"] = b"ignored=1"
    messages, _ = exchange(MigrationMaintenanceMiddleware(target, mode="on"), incoming)
    assert response(messages)[0] == 209
    assert target.calls[0][0] is incoming


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"])
@pytest.mark.parametrize("path", ["/healthz", "/deploymentz"])
def test_operational_paths_are_not_write_or_other_method_bypasses(path, method):
    target = Downstream()
    messages, _ = exchange(MigrationMaintenanceMiddleware(target, mode="on"), scope(path=path, method=method))
    assert response(messages)[0] == 503 and target.calls == []


def test_maintenance_head_has_no_body_and_cannot_make_readyz_green():
    target = Downstream()
    app = MigrationMaintenanceMiddleware(target, mode="on")
    get, _ = exchange(app, scope(path="/readyz"))
    head, _ = exchange(app, scope(path="/readyz", method="HEAD"))
    get_status, get_headers, get_body = response(get)
    head_status, head_headers, head_body = response(head)
    assert get_status == head_status == 503 and head_body == b""
    assert head_headers == get_headers and int(head_headers[b"content-length"]) == len(get_body)
    assert target.calls == []


@pytest.mark.parametrize("path", ["/ws", "/admin", "/healthz", "/deploymentz"])
def test_websocket_is_closed_before_accept_or_downstream(path):
    target = Downstream()
    messages, received = exchange(MigrationMaintenanceMiddleware(target, mode="on"), scope("websocket", path=path))
    assert messages == [{"type": "websocket.close", "code": 1013, "reason": "メンテナンス中です"}]
    assert target.calls == [] and received == []


@pytest.mark.parametrize("kind", ["websocket", "lifespan"])
def test_off_is_transparent_to_non_http_protocols(kind):
    target = Downstream()
    incoming = scope(kind)
    exchange(MigrationMaintenanceMiddleware(target), incoming)
    assert target.calls[0][0] is incoming


def test_http_maintenance_does_not_claim_to_stop_lifespan_or_background_workers():
    target = Downstream()
    incoming = scope("lifespan")
    messages, _ = exchange(MigrationMaintenanceMiddleware(target, mode="on"), incoming)
    assert target.calls[0][0] is incoming
    assert messages == [{"type": "lifespan.startup.complete"}]


@pytest.mark.parametrize("mode", [True, False, None, "ON", "true", "on ", "", "typo"])
def test_ambiguous_modes_fail_at_startup_instead_of_silently_disabling(mode):
    with pytest.raises(ValueError, match="maintenance mode"):
        MigrationMaintenanceMiddleware(Downstream(), mode=mode)


def test_existing_trusted_host_validation_stays_outermost():
    target = Downstream()
    app = TrustedHostMiddleware(MigrationMaintenanceMiddleware(target, mode="on"), allowed_hosts=["toolbako.example"])
    valid, _ = exchange(app, scope())
    invalid, _ = exchange(app, scope(host=b"untrusted.example"))
    assert response(valid)[0] == 503 and response(invalid)[0] == 400
    assert target.calls == []


def test_static_security_headers_cover_terminal_response_without_downstream_security_work():
    target = Downstream()
    messages, _ = exchange(MigrationMaintenanceMiddleware(target, mode="on", https_only=True), scope())
    _, headers, _ = response(messages)
    assert headers[b"content-security-policy"] == b"default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    assert headers[b"x-frame-options"] == b"DENY"
    assert headers[b"x-content-type-options"] == b"nosniff"
    assert headers[b"referrer-policy"] == b"no-referrer"
    assert headers[b"permissions-policy"] == b"camera=(), microphone=(), geolocation=(), payment=()"
    assert headers[b"cross-origin-opener-policy"] == headers[b"cross-origin-resource-policy"] == b"same-origin"
    assert headers[b"origin-agent-cluster"] == b"?1"
    assert headers[b"x-permitted-cross-domain-policies"] == b"none"
    assert target.calls == []


@pytest.mark.parametrize("version", ["1.0", "1.1", "2", "3"])
def test_unread_http1_body_closes_connection_without_illegal_http2_header(version):
    target = Downstream()
    messages, received = exchange(MigrationMaintenanceMiddleware(target, mode="on"), scope(method="POST", http_version=version))
    _, headers, _ = response(messages)
    assert (headers.get(b"connection") == b"close") is (version in {"1.0", "1.1"})
    assert b"strict-transport-security" not in headers and received == []


@pytest.mark.parametrize("mode", ["off", "on"])
def test_composed_host_security_session_business_order_keeps_gate_before_processing(mode):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from starlette.middleware.sessions import SessionMiddleware
    from app.config import Settings
    from app.middleware import ProductionGuardMiddleware

    calls = []
    class ObservedProductionGuard(ProductionGuardMiddleware):
        async def dispatch(self, request, call_next):
            calls.append("security")
            return await super().dispatch(request, call_next)

    class ObservedSession(SessionMiddleware):
        async def __call__(self, request_scope, receive, send):
            if request_scope["type"] == "http": calls.append("session")
            return await super().__call__(request_scope, receive, send)

    app = FastAPI()
    @app.api_route("/admin", methods=["GET", "POST"])
    async def admin(request: Request):
        calls.append("business")
        assert "session" in request.scope
        request.session["synthetic"] = True
        return {"ok": True}
    @app.get("/healthz")
    async def health():
        calls.append("health")
        return {"ok": True}
    @app.get("/deploymentz")
    async def deployment():
        calls.append("deployment")
        return {"status": "ok", "live_payments_enabled": False}

    current = Settings(environment="staging", demo_mode=False, redis_url="",
                       site_base_url="https://toolbako.example", allowed_hosts=("toolbako.example",))
    app.add_middleware(ObservedSession, secret_key="synthetic", https_only=True)
    app.add_middleware(ObservedProductionGuard, settings=current)
    app.add_middleware(MigrationMaintenanceMiddleware, mode=mode, https_only=True)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["toolbako.example"])
    client = TestClient(app, base_url="https://toolbako.example")
    result = client.post("/admin", headers={"origin": "https://toolbako.example"})
    assert result.status_code == (503 if mode == "on" else 200)
    assert calls == ([] if mode == "on" else ["security", "session", "business"])
    assert ("set-cookie" not in result.headers) is (mode == "on")
    assert result.headers["x-frame-options"] == "DENY"
    assert result.headers["x-content-type-options"] == "nosniff"
    assert "strict-transport-security" in result.headers

    calls.clear()
    rejected = client.post("/admin", headers={"origin": "https://untrusted.example"})
    # Off-mode preserves origin security. On-mode rejects before any action,
    # session or security/body middleware; it never permits the bad request.
    assert rejected.status_code == (503 if mode == "on" else 403)
    assert calls == ([] if mode == "on" else ["security"])
    for path, handler in (("/healthz", "health"), ("/deploymentz", "deployment")):
        calls.clear()
        operational = client.get(path)
        assert operational.status_code == 200 and "no-store" in operational.headers["cache-control"]
        assert calls == ["security", "session", handler]
    calls.clear()
    invalid_host = client.get("/admin", headers={"host": "untrusted.example"})
    assert invalid_host.status_code == 400 and calls == []


def test_actual_main_registers_maintenance_before_session_and_business_boundaries():
    import app.main as main
    from app.middleware import ProductionGuardMiddleware
    from starlette.middleware.sessions import SessionMiddleware
    # user_middleware is the effective outer-to-inner order. This assertion
    # catches a registration-order mistake even while the runtime default is off.
    classes = [item.cls for item in main.app.user_middleware]
    assert classes.index(TrustedHostMiddleware) < classes.index(MigrationMaintenanceMiddleware)
    assert classes.index(MigrationMaintenanceMiddleware) < classes.index(ProductionGuardMiddleware)
    assert classes.index(MigrationMaintenanceMiddleware) < classes.index(SessionMiddleware)
    for item in main.app.user_middleware:
        if item.cls.__name__ in {"DatabaseBoundaryMiddleware", "LiveWebhookDispatcherMiddleware", "StripeWebhookGuardMiddleware"}:
            assert classes.index(MigrationMaintenanceMiddleware) < classes.index(item.cls)
    configured = next(item for item in main.app.user_middleware if item.cls is MigrationMaintenanceMiddleware)
    assert configured.kwargs["mode"] == main.settings.migration_maintenance_mode
    assert configured.kwargs["https_only"] is main.settings.site_base_url.startswith("https")


@pytest.mark.parametrize("path", ["/login", "/admin", "/webhooks/stripe",
                                  "/webhooks/stripe/live/platform", "/webhooks/stripe/live/connect"])
def test_actual_main_on_rejects_invalid_headers_before_security_body_session_or_webhooks(path, monkeypatch):
    import app.main as main
    from fastapi.testclient import TestClient
    from starlette.middleware import Middleware
    from starlette.middleware.sessions import SessionMiddleware
    from app.middleware import ProductionGuardMiddleware

    # Replace only the admission mode in a fresh in-process stack. No lifespan
    # startup or production setting is invoked, and original objects are restored.
    stack = [Middleware(MigrationMaintenanceMiddleware, mode="on", https_only=True)
             if item.cls is MigrationMaintenanceMiddleware else item
             for item in main.app.user_middleware]
    monkeypatch.setattr(main.app, "user_middleware", stack)
    monkeypatch.setattr(main.app, "middleware_stack", None)
    async def forbidden(*args, **kwargs):
        pytest.fail("Enabled maintenance reached downstream request processing")
    monkeypatch.setattr(ProductionGuardMiddleware, "dispatch", forbidden)
    monkeypatch.setattr(SessionMiddleware, "__call__", forbidden)
    client = TestClient(main.app)
    response = client.post(path, content=b"private synthetic body", headers={
        "origin": "https://wrong-origin.invalid", "content-length": "not-an-integer",
        "cookie": "toolbako_session=PRIVATE_INVALID_COOKIE", "stripe-signature": "PRIVATE_INVALID_SIGNATURE",
    })
    assert response.status_code == 503 and "no-store" in response.headers["cache-control"]
    assert "PRIVATE" not in response.text and "private synthetic body" not in response.text
    assert "set-cookie" not in response.headers
