"""Admission guards must reject malformed inputs before the database boundary."""
from collections import Counter
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from app.config import Settings
from app.database import DatabaseBoundaryMiddleware
from app.middleware import ProductionGuardMiddleware


def guarded_client(monkeypatch, *, distributed=False):
    calls = Counter()

    class Backend:
        @asynccontextmanager
        async def request(self, store):
            calls["database"] += 1
            yield

        async def save(self):
            calls["save"] += 1

    if distributed:
        counters = Counter()

        async def allowed(self, key, limit):
            counters[key] += 1
            return counters[key] <= limit

        monkeypatch.setattr(ProductionGuardMiddleware, "_distributed_allowed", allowed)

    settings = Settings(
        environment="production", demo_mode=False, redis_url="",
        rate_limit_per_minute=2, auth_rate_limit_per_minute=2,
        site_base_url="https://toolbako.example",
    )
    app = FastAPI()
    app.add_middleware(DatabaseBoundaryMiddleware, backend=Backend(), store=None)
    app.add_middleware(ProductionGuardMiddleware, settings=settings)

    @app.post("/submit")
    @app.post("/webhooks/stripe")
    async def submit():
        calls["handler"] += 1
        return {"ok": True}

    for path in ("/healthz", "/readyz", "/deploymentz"):
        app.add_api_route(path, lambda: {"status": "ok"}, methods=["GET", "HEAD"])

    return TestClient(app, base_url="https://toolbako.example", raise_server_exceptions=False), calls


@pytest.mark.parametrize("distributed", [False, True])
@pytest.mark.parametrize("path", ["/healthz-extra", "/readyz-extra", "/deploymentz-extra"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_probe_prefix_cannot_bypass_rate_limit_or_keep_entering_database(monkeypatch, distributed, path, method):
    client, calls = guarded_client(monkeypatch, distributed=distributed)
    with client:
        responses = [client.request(method, path) for _ in range(3)]
    assert [response.status_code for response in responses] == [404, 404, 429]
    assert calls["database"] == calls["save"] == 2
    assert responses[-1].headers["retry-after"] == "60"
    assert responses[-1].headers["x-request-id"]


@pytest.mark.parametrize("path", ["/healthz", "/readyz", "/deploymentz"])
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_exact_probes_remain_available_without_database_or_rate_budget(monkeypatch, path, method):
    client, calls = guarded_client(monkeypatch, distributed=True)
    with client:
        assert [client.request(method, path).status_code for _ in range(3)] == [200, 200, 200]
    assert calls["database"] == calls["save"] == 0


@pytest.mark.parametrize("header", ["origin", "referer"])
@pytest.mark.parametrize("source", ["https://[", "https://[::1", "https://[not-ipv6]/private-input"])
def test_malformed_origin_is_safe_403_before_handler_or_database(monkeypatch, header, source):
    client, calls = guarded_client(monkeypatch)
    with client:
        response = client.post("/submit", headers={header: source})
    assert response.status_code == 403
    assert response.json()["error"] == "invalid_origin"
    assert response.headers["x-request-id"] == response.json()["request_id"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert source not in response.text
    assert calls["handler"] == calls["database"] == calls["save"] == 0


@pytest.mark.parametrize("header", ["origin", "referer"])
def test_valid_origin_or_referer_preserves_submission(monkeypatch, header):
    client, calls = guarded_client(monkeypatch)
    with client:
        response = client.post("/submit", headers={header: "https://toolbako.example/form"})
    assert response.status_code == 200
    assert calls["handler"] == calls["database"] == calls["save"] == 1


def test_valid_origin_retains_precedence_over_malformed_referer(monkeypatch):
    client, calls = guarded_client(monkeypatch)
    with client:
        response = client.post("/submit", headers={"origin": "https://toolbako.example", "referer": "https://["})
    assert response.status_code == 200
    assert calls["handler"] == 1


def test_webhook_admission_keeps_its_separate_signature_guard_contract(monkeypatch):
    # Synthetic downstream only; no Stripe request or event is sent/processed.
    client, calls = guarded_client(monkeypatch)
    with client:
        response = client.post("/webhooks/stripe", headers={"origin": "https://["})
    assert response.status_code == 200
    assert calls["handler"] == 1
