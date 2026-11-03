"""Default-off HTTP admission barrier for a separately authorized cutover.

Mount INSIDE TrustedHostMiddleware and OUTSIDE ProductionGuardMiddleware,
request-body buffering, webhook dispatch/signature checks, sessions and the DB
boundary. A terminal maintenance response has its own stricter static security
headers; off-mode and the two operational health exceptions keep all existing
guards. No request body, cookie, signature or query value is read/reflected.

This is not a drain/freeze proof: requests admitted before this gate was enabled,
ASGI lifespan/startup/shutdown tasks, other replicas and CLI/cron/background
writers must be stopped/drained separately before source snapshot or cutover.
Health checks may still read the database. A 503 webhook response deliberately
does not acknowledge delivery; the provider may retry after maintenance ends.
"""
from starlette.responses import HTMLResponse


_HEALTH_PATHS = frozenset({"/healthz", "/deploymentz"})
_PAGE = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>メンテナンス中 | ツールバコ</title></head>
<body><main><h1>現在、メンテナンス中です</h1>
<p>データ移行のため、ご利用を一時停止しています。</p>
<p>しばらくしてから画面を再読み込みしてください。</p>
<p>購入・保存の再送はせず、再開をお待ちください。</p>
</main></body></html>"""


class MigrationMaintenanceMiddleware:
    def __init__(self, app, *, mode="off", https_only=False):
        # Typographical/ambiguous configuration must fail deployment, not
        # silently admit writers. No request header or cookie can toggle this.
        if not isinstance(mode, str) or mode not in {"off", "on"}:
            raise ValueError("Invalid migration maintenance mode")
        if type(https_only) is not bool:
            raise ValueError("Invalid migration maintenance HTTPS setting")
        self.app = app
        self.enabled = mode == "on"
        self.https_only = https_only

    async def __call__(self, scope, receive, send):
        if not self.enabled:
            return await self.app(scope, receive, send)
        kind = scope["type"]
        if kind == "websocket":
            # No accept, session lookup or downstream WebSocket handler.
            return await send({"type": "websocket.close", "code": 1013,
                               "reason": "メンテナンス中です"})
        if kind != "http":
            # Do not falsify ASGI startup/shutdown completion or claim to stop
            # background writers. Their lifecycle has a separate drain plan.
            return await self.app(scope, receive, send)
        if scope.get("method") in {"GET", "HEAD"} and scope.get("path") in _HEALTH_PATHS:
            return await self.app(scope, receive, send)

        headers = {
            "Retry-After": "60", "Cache-Control": "no-store, private",
            "Pragma": "no-cache", "Expires": "0",
            "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
            "Cross-Origin-Opener-Policy": "same-origin",
            "Cross-Origin-Resource-Policy": "same-origin", "Origin-Agent-Cluster": "?1",
            "X-Permitted-Cross-Domain-Policies": "none", "X-Robots-Tag": "noindex, nofollow",
            "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        }
        if self.https_only:
            headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"
        if scope.get("http_version", "1.1") in {"1.0", "1.1"}:
            # Request bytes are intentionally unread; do not reuse HTTP/1 for
            # another request. HTTP/2+ forbids this connection-specific header.
            headers["Connection"] = "close"
        response = HTMLResponse(_PAGE, status_code=503, headers=headers)
        if scope.get("method") == "HEAD":
            # Retain the GET representation's Content-Length, without a body.
            response.body = b""
        await response(scope, receive, send)
