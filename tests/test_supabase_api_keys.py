import asyncio

import httpx
import pytest

from app.config import Settings
from app.deployment_check import configuration_checks, connection_checks
from app.supabase_api import supabase_headers


PUBLIC = "sb_publishable_synthetic-fixture"
SERVER = "sb_secret_synthetic-fixture"


def current_settings(**changes):
    return Settings(**{
        "environment": "staging", "demo_mode": False,
        "supabase_url": "https://project.supabase.co", "supabase_publishable_key": PUBLIC,
        "supabase_secret_key": SERVER, "supabase_anon_key": "", "supabase_service_role_key": "",
        "site_base_url": "https://toolbako.example", "allowed_hosts": ("toolbako.example",),
        "session_secret": "test-only-not-a-real-secret-1234567890",
        "database_url": "postgresql://localhost/test?sslmode=require", "stripe_secret_key": "", **changes,
    })


def test_current_keys_take_precedence_without_disabling_legacy_compatibility():
    settings = current_settings(supabase_anon_key="old-public", supabase_service_role_key="old-server")
    assert settings.supabase_ready
    assert settings.supabase_anon_key == PUBLIC and settings.supabase_service_role_key == SERVER
    assert all(configuration_checks(settings).values())
    legacy = Settings(supabase_url="https://project.supabase.co", supabase_anon_key="old-public", supabase_service_role_key="old-server",
        supabase_publishable_key="", supabase_secret_key="")
    assert legacy.supabase_ready and legacy.supabase_anon_key == "old-public" and legacy.supabase_service_role_key == "old-server"


@pytest.mark.parametrize("key", [PUBLIC, SERVER])
def test_opaque_keys_are_not_sent_as_bearer_jwts(key):
    assert supabase_headers(key) == {"apikey": key}


def test_legacy_jwt_api_header_remains_compatible():
    assert supabase_headers("legacy-jwt-fixture") == {"apikey": "legacy-jwt-fixture", "Authorization": "Bearer legacy-jwt-fixture"}


@pytest.mark.parametrize("key", [PUBLIC, "legacy-jwt-fixture"])
def test_user_jwt_is_not_replaced_by_an_api_key(key):
    assert supabase_headers(key, access_token="user-session-jwt") == {"apikey": key, "Authorization": "Bearer user-session-jwt"}


@pytest.mark.parametrize("token", ["", PUBLIC, SERVER])
def test_api_key_cannot_be_used_as_a_user_session(token):
    with pytest.raises(ValueError, match="session token") as error:
        supabase_headers(PUBLIC, access_token=token)
    assert PUBLIC not in str(error.value) and SERVER not in str(error.value)


@pytest.mark.parametrize("changes", [{"supabase_publishable_key": SERVER}, {"supabase_secret_key": PUBLIC}])
def test_swapped_key_roles_fail_closed_before_any_network_request(changes, monkeypatch):
    def forbidden(**kwargs):
        pytest.fail("Wrong key roles must not reach the provider")
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    settings = current_settings(**changes)
    result = asyncio.run(connection_checks(settings))
    assert not result["supabase_key_roles"] and not result["auth_api"] and not result["database_schema"]


def test_current_key_preflight_reads_only_without_exporting_keys(monkeypatch):
    from app import deployment_check
    async def probe(backend):
        assert backend.production
        return True
    monkeypatch.setattr(deployment_check.PostgresStateStore, "probe", probe)
    calls = []
    def respond(request):
        calls.append(request)
        assert request.method in {"GET", "HEAD"} and "authorization" not in request.headers
        key = request.headers["apikey"]
        assert key in {PUBLIC, SERVER}
        return httpx.Response(401 if request.method == "HEAD" and key == PUBLIC else 200)
    cls = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: cls(transport=httpx.MockTransport(respond), **kwargs))
    result = asyncio.run(connection_checks(current_settings()))
    assert all(result.values()) and len(calls) == 4
    assert result["supabase_public_key_present"] and result["supabase_server_key_present"]
    assert PUBLIC not in str(result) and SERVER not in str(result)


@pytest.mark.parametrize("key", [SERVER, "legacy-server-fixture"])
def test_badge_updates_use_the_compatible_server_headers(key, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, "settings", Settings(supabase_url="https://project.supabase.co", supabase_anon_key=PUBLIC,
        supabase_service_role_key=key, supabase_publishable_key="", supabase_secret_key=""))
    calls = []
    def respond(request):
        calls.append(request)
        assert request.method == "POST" and request.url.path == "/rest/v1/creator_badges"
        assert request.headers["apikey"] == key
        if key == SERVER:
            assert "authorization" not in request.headers
        else:
            assert request.headers["authorization"] == f"Bearer {key}"
        return httpx.Response(201)
    cls = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: cls(transport=httpx.MockTransport(respond), **kwargs))
    asyncio.run(main.sync_creator_badges_to_supabase({"id": "synthetic-user"}, {"is_certified_creator": True}))
    assert len(calls) == 1
