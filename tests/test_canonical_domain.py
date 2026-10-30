from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.config import Settings
from app.middleware import ProductionGuardMiddleware


def client():
    app = FastAPI()
    app.add_middleware(ProductionGuardMiddleware, settings=Settings(
        environment="staging", redis_url="", site_base_url="https://toolbako.onellc-ai.com",
        canonical_redirect_hosts=("legacy.example.test",),
    ))

    @app.api_route("/{path:path}", methods=["GET", "HEAD", "POST"])
    def page(path):
        return {"ok": True}

    return TestClient(app)


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_legacy_get_preserves_path_and_query_but_has_fixed_destination(method):
    response = client().request(method, "https://legacy.example.test/login?next=%2Fsettings&redirect_to=https://evil.test", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "https://toolbako.onellc-ai.com/login?next=%2Fsettings&redirect_to=https://evil.test"
    assert "no-store" in response.headers["cache-control"]
    assert response.headers["referrer-policy"] == "no-referrer"


@pytest.mark.parametrize("path", ["/healthz", "/readyz", "/deploymentz", "/webhooks/stripe"])
def test_health_checks_and_webhook_paths_keep_existing_host(path):
    assert client().get("https://legacy.example.test" + path, follow_redirects=False).status_code == 200


def test_old_post_is_rejected_without_redirecting_credentials():
    response = client().post("https://legacy.example.test/login", data={"password": "test-only"},
                             headers={"Origin": "https://legacy.example.test"}, follow_redirects=False)
    assert response.status_code == 403 and "location" not in response.headers


@pytest.mark.parametrize("host", ["toolbako.onellc-ai.com", "healthcheck.railway.app", "unlisted.example.test"])
def test_unlisted_and_canonical_hosts_are_not_redirected(host):
    assert client().get(f"https://{host}/login", follow_redirects=False).status_code == 200


def test_canonical_origin_post_is_allowed():
    response = client().post("https://toolbako.onellc-ai.com/login", headers={"Origin": "https://toolbako.onellc-ai.com"})
    assert response.status_code == 200
