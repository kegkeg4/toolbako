import asyncio

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
    {"supabase_service_role_key": ""}, {"database_url": ""},
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


def test_deployment_health_does_not_approve_launch_or_payments(monkeypatch):
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    import app.main as main
    async def probe():
        return True
    monkeypatch.setattr(main, "settings", configured())
    monkeypatch.setattr(main, "database_store", SimpleNamespace(probe=probe))
    client = TestClient(main.app)
    operational = client.get("/deploymentz")
    assert operational.status_code == 200
    assert operational.json() == {"status": "ok", "live_payments_enabled": False}
    assert "no-store" in operational.headers["cache-control"]
    launch = client.get("/readyz")
    assert launch.status_code == 503 and not launch.json()["ready"]
    assert not next(c for c in launch.json()["checks"] if c["key"] == "payments")["ok"]


@pytest.mark.parametrize("failure", ["missing_config", "database", "live_key"])
def test_deployment_health_fails_closed(monkeypatch, failure):
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
    response = TestClient(main.app).get("/deploymentz")
    assert response.status_code == 503 and response.json()["live_payments_enabled"] is False
    assert len(calls) == (1 if failure == "database" else 0)
