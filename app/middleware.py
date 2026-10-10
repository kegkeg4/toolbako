from __future__ import annotations

from collections import defaultdict, deque
import inspect
import ipaddress
import logging
import re
import secrets
from threading import Lock
from time import monotonic
from urllib.parse import urlparse
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from .request_context import request_id_context

logger = logging.getLogger("toolbako.requests")

try:
    from redis.asyncio import Redis
except ImportError:  # Redis is optional in local/demo mode.
    Redis = None


class ProductionGuardMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings):
        super().__init__(app)
        self.settings = settings
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()
        self._request_count = 0
        self._redis = Redis.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=2, socket_timeout=2) if Redis and settings.redis_url else None

    async def _distributed_allowed(self, key: str, limit: int) -> bool | None:
        """Increment and set expiry atomically; configured Redis must fail closed."""
        if not self._redis:
            return None
        try:
            redis_key = f"toolbako:ratelimit:{key}"
            count = await self._redis.eval(
                "local n = redis.call('INCR', KEYS[1]); "
                "if n == 1 or redis.call('TTL', KEYS[1]) < 0 then redis.call('EXPIRE', KEYS[1], 60) end; return n",
                1, redis_key,
            )
            return count <= limit
        except Exception:
            # Do not log exception strings: they may include credential URLs.
            logger.warning("distributed rate limiter unavailable")
            return None

    async def dispatch(self, request: Request, call_next):
        security_path = str(request.scope.get("path", ""))
        # Client headers may contain secrets/PII even after character filtering.
        # Generate our own ID so logs and monitoring never copy caller content.
        request_id = uuid4().hex
        request_id_context.set(request_id)
        request.state.request_id = request_id
        request.state.csp_nonce = secrets.token_urlsafe(18)
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > self.settings.max_request_bytes:
            return self._finish(JSONResponse({"error":"request_too_large","request_id":request_id}, status_code=413), request, request_id)

        if not security_path.startswith(("/static/", "/healthz", "/readyz", "/deploymentz")):
            forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            direct_ip = request.client.host if request.client else "unknown"
            trusted_proxy = False
            if self.settings.trust_proxy_headers and self.settings.trusted_proxy_cidrs:
                try:
                    address = ipaddress.ip_address(direct_ip)
                    trusted_proxy = any(address in ipaddress.ip_network(cidr, strict=False) for cidr in self.settings.trusted_proxy_cidrs)
                except ValueError:
                    trusted_proxy = False
            client_ip = forwarded if trusted_proxy and forwarded else direct_ip
            bucket = "auth" if security_path.startswith(("/login","/signup","/auth/","/forgot-password","/security/mfa")) else "general"
            if self.settings.is_deployed:
                limit = self.settings.auth_rate_limit_per_minute if bucket == "auth" else self.settings.rate_limit_per_minute
            else:
                # Local development and the deterministic test client share one
                # loopback IP; production remains strictly rate-limited.
                limit = max(self.settings.rate_limit_per_minute, 10_000)
            key = f"{client_ip}:{bucket}"
            distributed_allowed = await self._distributed_allowed(key, limit) if self.settings.is_deployed else None
            if self.settings.is_deployed and self.settings.redis_url and distributed_allowed is None:
                return self._finish(JSONResponse({"error":"security_service_unavailable","request_id":request_id}, status_code=503, headers={"Retry-After":"30"}), request, request_id)
            if distributed_allowed is False:
                response = JSONResponse({"error":"rate_limited","request_id":request_id}, status_code=429, headers={"Retry-After":"60"})
                return self._finish(response, request, request_id)
            if distributed_allowed is True:
                # Redis is the source of truth across workers; skip the local
                # bucket so one process cannot double-count the same request.
                pass
            else:
                now = monotonic()
                with self._lock:
                    self._request_count += 1
                    if self._request_count % 1000 == 0:
                        stale = [stored_key for stored_key,stored_hits in self._hits.items() if not stored_hits or stored_hits[-1] < now-60]
                        for stored_key in stale: self._hits.pop(stored_key,None)
                    hits = self._hits[key]
                    while hits and hits[0] < now - 60: hits.popleft()
                    if len(hits) >= limit:
                        response = JSONResponse({"error":"rate_limited","request_id":request_id}, status_code=429, headers={"Retry-After":"60"})
                        return self._finish(response, request, request_id)
                    hits.append(now)

        if self.settings.is_deployed and request.method in {"POST","PUT","PATCH","DELETE"} and not security_path.startswith("/webhooks/"):
            source = request.headers.get("origin") or request.headers.get("referer")
            if not source:
                return self._finish(JSONResponse({"error":"origin_required","request_id":request_id}, status_code=403), request, request_id)
            source_url = urlparse(source)
            expected_url = urlparse(self.settings.site_base_url)
            if (source_url.scheme, source_url.netloc) != (expected_url.scheme, expected_url.netloc):
                return self._finish(JSONResponse({"error":"invalid_origin","request_id":request_id}, status_code=403), request, request_id)

        try:
            response = await call_next(request)
            if request.method in {"POST","PUT","PATCH","DELETE"} and response.status_code < 400:
                persist = getattr(request.app.state, "persist", None)
                if persist:
                    result = persist()
                    if inspect.isawaitable(result):
                        await result
        except Exception as exc:
            if not self.settings.is_deployed:
                raise
            # Provider exceptions may contain credential-bearing URLs or raw
            # response bodies. Keep production/staging logs useful for correlation
            # without emitting exception text, traceback locals or request paths.
            error_type = re.sub(r"[^A-Za-z0-9_.]", "", type(exc).__name__)[:80]
            logger.error("Unhandled request error request_id=%s error_type=%s", request_id, error_type)
            try:
                import sentry_sdk
                sentry_sdk.capture_exception(exc)
            except Exception:
                # Monitoring must not replace our safe 500 response with its own
                # exception (or cause a provider secret to reach Uvicorn logs).
                logger.warning("error monitoring unavailable")
            if "text/html" in request.headers.get("accept", ""):
                response = HTMLResponse(f'<!doctype html><html lang="ja"><meta charset="utf-8"><title>問題が発生しました</title><body><main><h1>一時的な問題が発生しました</h1><p>操作は繰り返さず、しばらくしてから再読み込みしてください。</p><p>お問い合わせ番号: <code>{request_id}</code></p><a href="/support">ヘルプを開く</a></main></body></html>',status_code=500)
            else:
                response = JSONResponse({"error":"internal_server_error","request_id":request_id}, status_code=500)
        return self._finish(response, request, request_id)

    def _finish(self, response: Response, request: Request, request_id: str) -> Response:
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=(self)"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Origin-Agent-Cluster"] = "?1"
        response.headers["X-Permitted-Cross-Domain-Policies"] = "none"
        if not self.settings.is_production:
            response.headers["X-Robots-Tag"] = "noindex, nofollow"
        csp_nonce = getattr(request.state, "csp_nonce", "")
        response.headers["Content-Security-Policy"] = f"default-src 'self'; script-src 'self' 'nonce-{csp_nonce}'; style-src 'self'; img-src 'self' data:; connect-src 'self'; font-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'"
        if self.settings.site_base_url.startswith("https://"):
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"
        security_path = str(request.scope.get("path", ""))
        if security_path.startswith(("/mypage","/orders/","/messages","/seller","/admin","/settings","/security","/verification","/library","/payouts","/purchases","/subscriptions","/notifications","/updates","/account/","/auth/","/checkout/","/transfer-inquiries/")) or security_path in {"/login", "/signup", "/forgot-password", "/healthz", "/readyz", "/deploymentz"}:
            response.headers["Cache-Control"] = "no-store, private"
        elif security_path.startswith("/static/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif request.cookies.get("toolbako_session") or "toolbako_session=" in response.headers.get("set-cookie", ""):
            response.headers["Cache-Control"] = "no-store, private"
        elif request.method in {"GET", "HEAD"} and response.status_code == 200:
            content_type = response.headers.get("content-type", "")
            if content_type.startswith("text/html"):
                response.headers["Cache-Control"] = "public, max-age=60, stale-while-revalidate=300"
            elif content_type.startswith(("application/xml", "text/plain")):
                response.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=600"
        if request.cookies.get("toolbako_session") or "toolbako_session=" in response.headers.get("set-cookie", ""):
            response.headers["Vary"] = "Cookie"
        return response
