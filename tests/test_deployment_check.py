import asyncio
import json

import httpx
import pytest

from app.config import Settings
from app import deployment_check as checks


def configured(**changes):
    return Settings(**{
        "environment": "staging", "demo_mode": False,
        "site_base_url": "https://toolbako.example", "allowed_hosts": ("toolbako.example",),
        "session_secret": "test-only-not-a-real-secret-1234567890",
        "database_url": "postgresql://localhost/test?sslmode=require",
        "supabase_url": "https://project.supabase.co",
        "supabase_anon_key": "anon-fixture", "supabase_service_role_key": "service-fixture",
        "stripe_secret_key": "", **changes,
    })


@pytest.mark.parametrize("changes", [
    {"environment": "development"}, {"session_secret": "short"},
    {"site_base_url": "http://toolbako.example"}, {"allowed_hosts": ("*",)},
    {"supabase_url": "https://user:password@project.supabase.co"},
    {"supabase_url": "https://project.supabase.co?key=private"},
    {"supabase_service_role_key": ""}, {"supabase_anon_key": ""}, {"database_url": ""},
    {"stripe_secret_key": "sk_live_fixture"}, {"stripe_secret_key": "rk_live_fixture"},
])
def test_preflight_invalid_config_never_makes_external_calls(changes, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid config must not make network requests")
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    result = asyncio.run(checks.connection_checks(configured(**changes)))
    assert not all(result.values())
    assert result["database_schema"] is False


@pytest.mark.parametrize("failure", [None, "database", "anonymous_access", "redirect", "timeout", "missing_columns"])
def test_preflight_reads_only_and_fails_closed(failure, monkeypatch):
    calls = []
    async def probe(backend):
        assert backend.production is True
        return failure != "database"
    monkeypatch.setattr(checks.PostgresStateStore, "probe", probe)
    def request(req):
        calls.append(req)
        assert req.method in {"GET", "HEAD"}
        assert req.url.host == "project.supabase.co"
        if failure == "timeout":
            raise httpx.ConnectTimeout("must-not-log-provider-secret", request=req)
        if failure == "redirect":
            return httpx.Response(302, headers={"location": "https://unrelated.example"})
        if req.method == "HEAD":
            assert req.url.params["limit"] == "0"
            if req.headers["apikey"] == "anon-fixture":
                return httpx.Response(200 if failure == "anonymous_access" else 401)
            if failure == "missing_columns":
                return httpx.Response(400)
        return httpx.Response(200)
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(request), **kwargs))
    result = asyncio.run(checks.connection_checks(configured()))
    assert all(result.values()) is (failure is None)
    assert len(calls) == 4


def test_preflight_cli_does_not_print_exception_or_credentials(monkeypatch, capsys):
    async def fail(settings):
        raise RuntimeError("private-password sk_live_hidden")
    monkeypatch.setattr(checks, "connection_checks", fail)
    assert checks.main() == 1
    output = capsys.readouterr().out
    assert "private-password" not in output and "sk_live_hidden" not in output
    assert '"connection_preflight_passed": false' in output


@pytest.mark.parametrize("fault,expected,status", [
    (httpx.ConnectTimeout, "connect_timeout", None),
    (httpx.ReadTimeout, "read_timeout", None),
    (httpx.WriteTimeout, "write_timeout", None),
    (httpx.PoolTimeout, "pool_timeout", None),
    (httpx.ConnectError, "connect_error", None),
    (httpx.RemoteProtocolError, "remote_protocol_error", None),
    (httpx.ReadError, "http_error", None),
    (400, "unexpected_status", 400),
    (503, "unexpected_status", 503),
])
def test_preflight_failure_diagnostics_are_actionable_and_secret_free(fault, expected, status, monkeypatch, caplog):
    async def probe(backend):
        return True
    monkeypatch.setattr(checks.PostgresStateStore, "probe", probe)
    def request(req):
        if req.url.path.endswith("creator_badges"):
            if isinstance(fault, int):
                return httpx.Response(fault, text="PRIVATE_PROVIDER_BODY", headers={"x-private": "PRIVATE_HEADER"})
            raise fault("PRIVATE_EXCEPTION service-fixture anon-fixture", request=req)
        return httpx.Response(401 if req.method == "HEAD" and req.headers["apikey"] == "anon-fixture" else 200)
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(request), **kwargs))
    with caplog.at_level("WARNING", logger=checks.__name__):
        result = asyncio.run(checks.connection_checks(configured()))
    assert result["badge_api"] is False
    assert all(value for key, value in result.items() if key != "badge_api")
    messages = [record.getMessage() for record in caplog.records if record.name == checks.__name__]
    assert len(messages) == 1
    diagnostic = json.loads(messages[0].split(" ", 1)[1])
    assert diagnostic == {"check": "badge_api", "failure": expected, "status_code": status,
                          "elapsed_ms": diagnostic["elapsed_ms"]}
    assert isinstance(diagnostic["elapsed_ms"], int) and diagnostic["elapsed_ms"] >= 0
    for secret in ("PRIVATE_PROVIDER_BODY", "PRIVATE_HEADER", "PRIVATE_EXCEPTION", "service-fixture", "anon-fixture", "project.supabase.co"):
        assert secret not in messages[0]


@pytest.mark.parametrize("get_status,body,expected", [
    (200, [], "not_auth_error"),
    (401, {"message": "Invalid API key", "details": "PRIVATE_BODY"}, "api_key_rejected"),
    (401, {"code": "PGRST301", "message": "PRIVATE_BODY"}, "jwt_rejected"),
    (403, {"code": "42501", "message": "PRIVATE_BODY"}, "permission_denied"),
    (401, {"message": "Forbidden use of secret API key in browser"}, "browser_key_rejected"),
    (401, {"message": "PRIVATE_BODY", "code": "PRIVATE_CODE"}, "unclassified"),
    (401, {"message": ["PRIVATE_BODY"], "code": {"private": "PRIVATE_CODE"}}, "unclassified"),
    (401, ["PRIVATE_BODY"], "unclassified"),
    (302, {"message": "PRIVATE_BODY"}, "not_auth_error"),
])
def test_failed_server_head_get_diagnosis_does_not_approve_deploy(get_status, body, expected, monkeypatch, caplog):
    async def probe(backend):
        return True
    monkeypatch.setattr(checks.PostgresStateStore, "probe", probe)
    seen = []
    def request(req):
        seen.append(req)
        assert req.url.host == "project.supabase.co"
        if req.url.path.endswith("creator_badges"):
            assert req.url.params["limit"] == "0"
            assert req.headers["apikey"] == "service-fixture"
            if req.method == "HEAD":
                return httpx.Response(401)
            assert req.method == "GET"
            return httpx.Response(get_status, json=body, headers={"location": "https://unrelated.example"})
        return httpx.Response(401 if req.method == "HEAD" and req.headers["apikey"] == "anon-fixture" else 200)
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(request), **kwargs))
    with caplog.at_level("WARNING", logger=checks.__name__):
        result = asyncio.run(checks.connection_checks(configured()))
    assert result["badge_api"] is False  # Even when diagnostic GET succeeds.
    assert all(v for k, v in result.items() if k != "badge_api")
    assert len(seen) == 5
    entries = [r.getMessage() for r in caplog.records if r.name == checks.__name__]
    assert len(entries) == 1
    diagnostic = json.loads(entries[0].split(" ", 1)[1])
    assert diagnostic["status_code"] == 401
    assert diagnostic["diagnostic_get_status"] == get_status
    assert diagnostic["diagnostic_auth_category"] == expected
    for private in ("PRIVATE_BODY", "PRIVATE_CODE", "service-fixture", "anon-fixture", "project.supabase.co"):
        assert private not in entries[0]


def test_diagnostic_timeout_keeps_original_failure_and_redacts_exception(monkeypatch, caplog):
    async def probe(backend):
        return True
    monkeypatch.setattr(checks.PostgresStateStore, "probe", probe)
    def request(req):
        if req.url.path.endswith("creator_badges"):
            if req.method == "HEAD":
                return httpx.Response(401)
            raise httpx.ReadTimeout("PRIVATE_BODY service-fixture", request=req)
        return httpx.Response(401 if req.method == "HEAD" and req.headers["apikey"] == "anon-fixture" else 200)
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(request), **kwargs))
    with caplog.at_level("WARNING", logger=checks.__name__):
        result = asyncio.run(checks.connection_checks(configured()))
    assert result["badge_api"] is False
    diagnostic = json.loads(caplog.records[-1].getMessage().split(" ", 1)[1])
    assert diagnostic["status_code"] == 401
    assert diagnostic["diagnostic_failure"] == "read_timeout"
    assert "PRIVATE_BODY" not in caplog.text and "service-fixture" not in caplog.text


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_deployment_health_does_not_approve_launch_or_payments(monkeypatch, method):
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    import app.main as main
    async def probe():
        return True
    monkeypatch.setattr(main, "settings", configured())
    monkeypatch.setattr(main, "database_store", SimpleNamespace(probe=probe))
    client = TestClient(main.app)
    operational = client.request(method, "/deploymentz")
    assert operational.status_code == 200
    if method == "GET":
        assert operational.json() == {"status": "ok", "live_payments_enabled": False}
    else:
        assert operational.content == b""
    assert "no-store" in operational.headers["cache-control"]
    launch = client.get("/readyz")
    assert launch.status_code == 503 and not launch.json()["ready"]
    assert not next(c for c in launch.json()["checks"] if c["key"] == "payments")["ok"]


@pytest.mark.parametrize("failure", ["missing_config", "database", "live_key"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_deployment_health_fails_closed(monkeypatch, failure, method):
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    import app.main as main
    calls = []
    async def probe():
        calls.append(True)
        return failure != "database"
    settings = configured(**({"supabase_anon_key": ""} if failure == "missing_config" else
                             {"stripe_secret_key": "sk_live_forbidden"} if failure == "live_key" else {}))
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "database_store", SimpleNamespace(probe=probe))
    response = TestClient(main.app).request(method, "/deploymentz")
    assert response.status_code == 503
    if method == "GET":
        assert response.json()["live_payments_enabled"] is False
    else:
        assert response.content == b""
    assert len(calls) == (1 if failure == "database" else 0)


@pytest.mark.parametrize("path", ["/healthz", "/login"])
def test_public_monitor_head_matches_get_without_body(path):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        response = client.head(path)
        get = client.get(path)
    assert response.status_code == get.status_code == 200
    assert response.content == b"" and get.content
    assert response.headers["content-type"] == get.headers["content-type"]
    assert "no-store" in response.headers["cache-control"]


@pytest.mark.parametrize("cookie", [False, True])
def test_head_auth_evidence_preserves_failure_and_detects_cookie_change(monkeypatch, caplog, cookie):
    async def probe(backend):
        return True
    monkeypatch.setattr(checks.PostgresStateStore, "probe", probe)
    seen = []
    def transport(request):
        seen.append(request)
        if request.url.path.endswith("creator_badges"):
            if request.method == "HEAD":
                return httpx.Response(401, headers={
                    "cf-ray": "0123456789abcdef-NRT", "cf-cache-status": "DYNAMIC",
                    **({"set-cookie": "private_cookie=PRIVATE_COOKIE; Path=/"} if cookie else {}),
                }, extensions={"http_version": b"HTTP/1.1"})
            return httpx.Response(200, json=[], headers={
                "cf-ray": "fedcba9876543210-KIX", "cf-cache-status": "MISS", "age": "0",
            }, extensions={"http_version": b"HTTP/1.1"})
        return httpx.Response(401 if request.headers["apikey"] == "anon-fixture" and request.method == "HEAD" else 200)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(transport), **kwargs))
    with caplog.at_level("WARNING", logger=checks.__name__):
        results = asyncio.run(checks.connection_checks(configured()))
    assert results["badge_api"] is False and len(seen) == 5
    entry = json.loads(next(r.getMessage() for r in caplog.records if r.name == checks.__name__).split(" ", 1)[1])
    assert entry["head_response"]["cf_ray"] == "0123456789abcdef-NRT"
    assert entry["head_response"]["http_version"] == "HTTP/1.1"
    assert entry["head_response"]["cache_status"] == "DYNAMIC"
    assert entry["head_request"]["user_agent_kind"] == "python_httpx"
    assert entry["head_request"]["configured_auth_matches"] is True
    assert entry["diagnostic_request"]["cookie_present"] is cookie
    assert entry["diagnostic_request_headers_match_head"] is (not cookie)
    assert entry["diagnostic_request_auth_matches_head"] is True
    assert entry["diagnostic_request_url_matches_head"] is True
    assert entry["diagnostic_request_body_matches_head"] is True
    assert entry["diagnostic_response"]["age_seconds"] == 0
    for secret in ("PRIVATE_COOKIE", "service-fixture", "anon-fixture", "project.supabase.co"):
        assert secret not in caplog.text


@pytest.mark.parametrize("headers,version", [
    ({"cf-ray": "PRIVATE_RESPONSE", "cf-cache-status": "PRIVATE_RESPONSE", "age": "PRIVATE_RESPONSE"}, b"PRIVATE_VERSION"),
    ({"cf-ray": "0123456789abcdef-NRT\nPRIVATE_RESPONSE", "age": "-1"}, b"HTTP/2"),
    ({"cf-ray": "0123456789abcdef-NRT", "age": "999999999999999999999999"}, b"HTTP/3"),
])
def test_response_evidence_never_exports_unrecognized_headers(headers, version):
    result = checks._response_evidence(httpx.Response(401, headers=headers, extensions={"http_version": version}))
    encoded = json.dumps(result)
    assert "PRIVATE" not in encoded
    assert result["cache_status"] in {"missing", "unknown"}
    assert result["age_seconds"] is None
    assert result["cf_ray"] in {None, "0123456789abcdef-NRT"}


def test_parallel_preflight_keeps_opaque_admin_and_anon_auth_separate(monkeypatch, caplog):
    async def probe(backend):
        return True
    monkeypatch.setattr(checks.PostgresStateStore, "probe", probe)
    seen = []
    async def transport(request):
        await asyncio.sleep(0)
        seen.append(request)
        assert "authorization" not in request.headers and request.content == b""
        if request.url.path.endswith("creator_badges") and request.method == "HEAD":
            return httpx.Response(401)
        if request.method == "HEAD" and request.headers["apikey"] == "sb_publishable_SYNTHETIC":
            return httpx.Response(401)
        return httpx.Response(200, json=[])
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(transport), **kwargs))
    with caplog.at_level("WARNING", logger=checks.__name__):
        result = asyncio.run(checks.connection_checks(configured(
            supabase_anon_key="sb_publishable_SYNTHETIC", supabase_service_role_key="sb_secret_SYNTHETIC")))
    assert result["badge_api"] is False and len(seen) == 5
    for request in seen:
        if request.url.path.endswith("creator_badges"):
            assert request.headers["apikey"] == "sb_secret_SYNTHETIC"
        elif request.url.path.endswith("settings"):
            assert request.headers["apikey"] == "sb_publishable_SYNTHETIC"
    entry = json.loads(caplog.records[-1].getMessage().split(" ", 1)[1])
    assert entry["head_request"]["api_key_kind"] == "opaque_secret"
    assert entry["head_request"]["configured_auth_matches"] is True
    assert entry["diagnostic_request_headers_match_head"] is True
    assert "SYNTHETIC" not in caplog.text


def test_request_evidence_classifies_browser_ua_without_echoing_it():
    expected = {"apikey": "sb_secret_PRIVATE_KEY"}
    request = httpx.Request("HEAD", "https://private.example/rest/v1/profiles?limit=0", headers={
        **expected, "user-agent": "Mozilla/5.0 PRIVATE_UA", "origin": "PRIVATE_ORIGIN",
        "sec-fetch-mode": "cors", "cookie": "PRIVATE_COOKIE",
    })
    result = checks._request_evidence(request, expected)
    assert result == {"api_key_kind": "opaque_secret", "configured_auth_matches": True,
                      "user_agent_kind": "browser_like", "origin_present": True,
                      "fetch_metadata_present": True, "cookie_present": True, "body_empty": True}
    assert "PRIVATE" not in json.dumps(result)
    assert checks._request_evidence(request, {"apikey": "different"})["configured_auth_matches"] is False


@pytest.mark.parametrize("message,expected", [
    ("JWT expired", "expired"), ("JWT not yet valid", "not_yet_valid"),
    ("JWT issued at future", "issued_at_future"), ("JWT not in audience", "audience_mismatch"),
    ("PRIVATE_ERROR sb_secret_PRIVATE_KEY", "unclassified"),
])
def test_head_auth_claim_reason_is_fixed_without_exposing_challenge(message, expected):
    response = httpx.Response(401, headers={
        "proxy-status": "PostgREST; error=PGRST303",
        "www-authenticate": f'Bearer error="invalid_token", error_description="{message}"',
        "sb-request-id": "01a1a0ed-d180-70c7-ab79-122a39c93847",
    })
    result = checks._response_evidence(response)
    assert result["postgrest_error"] == "PGRST303"
    assert result["jwt_claim_category"] == expected
    assert result["sb_request_id"] == "01a1a0ed-d180-70c7-ab79-122a39c93847"
    assert "PRIVATE" not in json.dumps(result)


def test_response_evidence_rejects_injected_proxy_and_request_id():
    result = checks._response_evidence(httpx.Response(401, headers={
        "proxy-status": "PostgREST; error=PGRST303; PRIVATE_RESPONSE",
        "sb-request-id": "sk_live_PRIVATE_KEY", "www-authenticate": "PRIVATE_RESPONSE",
    }))
    assert result["postgrest_error"] is None and result["sb_request_id"] is None
    assert result["jwt_claim_category"] == "unclassified"
    assert "PRIVATE" not in json.dumps(result)
