"""Synthetic only: no PostgreSQL, credentials, or provider access."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from app import deployment_check as checks
from app.config import Settings
from app.database import SCHEMA_VERSION, content_hash
from app.financial_scope import FinancialScope, SCOPE_ENDPOINT, SCOPE_OPERATION_ID
from app.integrations import StripeIntegration
from app.production import readiness_summary


SCOPE = FinancialScope("live", "acct_1Ty4WnIogL39waUY", "56387c08-fdb4-4dd7-b63f-aeae34b6de54")
HOST = "aws-0-ap-northeast-1.pooler.supabase.com"
USER = "postgres.xijdnqnsuhuciqyygnkg"
DSN = (f"host={HOST} port=5432 user={USER} dbname=postgres "
       "password=synthetic sslmode=verify-full sslrootcert=/app/approved-ca.crt")


@pytest.fixture(autouse=True)
def no_inherited_libpq_settings(monkeypatch):
    # Never connect; this only makes the strict synthetic config independent of
    # another test process's local disposable-PG environment.
    import os
    for name in tuple(os.environ):
        if name.startswith("PG"):
            monkeypatch.delenv(name)


def stage(**changes):
    return Settings(**{
        "environment": "staging", "demo_mode": False,
        "site_base_url": "https://toolbako.example", "allowed_hosts": ("toolbako.example",),
        "session_secret": "synthetic-secret-only-at-least-32-characters",
        "supabase_url": "https://xnjzgfaasbemffgorvnj.supabase.co",
        "supabase_anon_key": "anon-fixture", "supabase_service_role_key": "server-fixture",
        "storage_provider": "supabase", "supabase_private_bucket": "private-fixture",
        "supabase_image_bucket": "images-fixture", "database_url": DSN,
        "stripe_secret_key": "rk_live_synthetic", "stripe_platform_account_id": SCOPE.platform_account,
        "stripe_ledger_id": SCOPE.ledger_id, "stripe_charge_mode": "separate",
        "stripe_connection_mode": "live_read_only", **changes,
    })


class Cursor:
    def __init__(self, row): self.row = row
    async def fetchone(self): return self.row


class Connection:
    def __init__(self):
        self.info = SimpleNamespace(host=HOST, port=5432, user=USER, dbname="postgres")
        self.pgconn = SimpleNamespace(ssl_in_use=True)
        self.row = (SCHEMA_VERSION, 1, SCOPE_ENDPOINT, content_hash(SCOPE.payload()), "succeeded", SCOPE.payload())
        self.calls = []
        self.closed = False
        self.failure = None
    async def __aenter__(self): return self
    async def __aexit__(self, *args): self.closed = True
    async def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if self.failure: raise self.failure
        if sql.lower().startswith("select"):
            assert params == (SCOPE_OPERATION_ID,)
            return Cursor(self.row)
        assert sql == "set default_transaction_read_only = on"
        return Cursor(None)


class Backend:
    def __init__(self, connection=None, dsn=DSN):
        self.dsn, self.conn, self.connects = dsn, connection or Connection(), 0
    async def connect(self):
        self.connects += 1
        return self.conn
    async def request(self, *args): pytest.fail("Readiness must not load/init/write marketplace state")
    async def probe(self): pytest.fail("Live stage must not accept schema-only probe")


def test_explicit_stage_accepts_complete_configuration_but_keeps_payments_off():
    settings = stage(stripe_webhook_secret="whsec_fixture", stripe_connect_webhook_secret="whsec_connect",
                     stripe_connect_client_id="ca_fixture")
    assert all(checks.configuration_checks(settings).values())
    assert settings.stripe_ready is False
    readiness = readiness_summary(settings, runtime_database_ready=True)
    assert not readiness["ready"]
    assert next(item for item in readiness["checks"] if item["key"] == "payments")["ok"] is False


@pytest.mark.parametrize("changes", [
    {"stripe_connection_mode": "sandbox"}, {"stripe_connection_mode": ""},
    {"stripe_connection_mode": "LIVE_READ_ONLY"}, {"stripe_connection_mode": " live_read_only"},
    {"stripe_connection_mode": True}, {"stripe_secret_key": "sk_live_synthetic"},
    {"stripe_secret_key": "rk_test_synthetic"}, {"stripe_secret_key": "rk_live_invalid/key"},
    {"stripe_platform_account_id": "acct_other"}, {"stripe_platform_account_id": ""},
    {"stripe_ledger_id": ""}, {"stripe_ledger_id": "not-a-ledger"},
    {"supabase_url": "https://xijdnqnsuhuciqyygnkg.supabase.co"},
    {"stripe_charge_mode": "destination"}, {"storage_provider": "local"},
    {"supabase_private_bucket": ""}, {"supabase_image_bucket": ""},
    {"database_url": DSN.replace("xijdnqnsuhuciqyygnkg", "xnjzgfaasbemffgorvnj")},
    {"database_url": DSN.replace("5432", "6543")},
    {"database_url": DSN.replace("verify-full", "require")},
    {"database_url": DSN.replace(HOST, "unapproved.example")},
    {"database_url": DSN.replace("dbname=postgres", "dbname=other")},
    {"database_url": DSN.replace("password=synthetic ", "")},
    {"database_url": DSN + " hostaddr=127.0.0.1"},
    {"database_url": DSN + " service=override"},
    {"database_url": DSN + " passfile=/private"},
    {"database_url": DSN + " options='-c search_path=public'"},
    {"database_url": DSN.replace(HOST, HOST + ",other.example")},
])
def test_ambiguous_or_wrong_stage_configuration_fails_before_io(changes, monkeypatch):
    def forbidden(*args, **kwargs): pytest.fail("Invalid configuration must not connect")
    monkeypatch.setattr(checks, "PostgresStateStore", forbidden)
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    result = asyncio.run(checks.connection_checks(stage(**changes)))
    assert not all(result.values()) and result["database_schema"] is False


@pytest.mark.parametrize("name", ["PGHOSTADDR", "PGSERVICE", "PGOPTIONS", "PGSSLMODE", "PGPORT"])
def test_libpq_environment_overrides_are_rejected(name, monkeypatch):
    monkeypatch.setenv(name, "synthetic-override")
    assert not all(checks.configuration_checks(stage()).values())


def test_runtime_probe_proves_binding_and_snapshot_without_stripe_or_writes(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: pytest.fail("Liveness must not call Stripe"))
    backend = Backend()
    assert asyncio.run(checks.deployment_database_check(stage(), backend)) is True
    assert backend.connects == 1 and backend.conn.closed
    assert len(backend.conn.calls) == 2
    assert backend.conn.calls[1][0].lower().startswith("select")


@pytest.mark.parametrize("fault", ["unbound", "wrong_mode", "wrong_account", "wrong_ledger", "wrong_hash",
                                  "pending_binding", "missing_snapshot", "wrong_schema", "no_row",
                                  "wrong_backend", "wrong_connection_host", "wrong_connection_user",
                                  "wrong_connection_port", "wrong_connection_database", "no_tls", "db_error"])
def test_runtime_probe_rejects_missing_or_stale_evidence(fault):
    backend = Backend()
    row = list(backend.conn.row)
    if fault == "unbound": row[2:] = [None] * 4
    elif fault in {"wrong_mode", "wrong_account", "wrong_ledger"}:
        change = {"wrong_mode": {"mode": "test"}, "wrong_account": {"platform_account": "acct_other"},
                  "wrong_ledger": {"ledger_id": "6317da82-f331-4f3b-9aa6-b01ae3b3449e"}}[fault]
        payload = replace(SCOPE, **change).payload()
        row[3], row[5] = content_hash(payload), payload
    elif fault == "wrong_hash": row[3] = "incorrect"
    elif fault == "pending_binding": row[4] = "pending"
    elif fault == "missing_snapshot": row[1] = None
    elif fault == "wrong_schema": row[0] = -1
    elif fault == "wrong_backend": backend.dsn = DSN.replace("xijdnqnsuhuciqyygnkg", "oldproject")
    elif fault.startswith("wrong_connection_"):
        attr = fault.removeprefix("wrong_connection_")
        setattr(backend.conn.info, "dbname" if attr == "database" else attr, 6543 if attr == "port" else "wrong")
    elif fault == "no_tls": backend.conn.pgconn.ssl_in_use = False
    elif fault == "db_error": backend.conn.failure = RuntimeError("PRIVATE_DB_DSN_PASSWORD")
    backend.conn.row = None if fault == "no_row" else tuple(row)
    assert asyncio.run(checks.deployment_database_check(stage(), backend)) is False
    assert backend.connects == (0 if fault == "wrong_backend" else 1)
    if backend.connects: assert backend.conn.closed


@pytest.mark.parametrize("path", ["checkout/sessions", "payment_intents", "refunds", "transfers", "payouts", "accounts", "account_links"])
def test_connection_stage_cannot_send_any_live_mutation(path, monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: pytest.fail("No live mutation or preparatory network"))
    journal = SimpleNamespace(begin_operation=lambda *args: pytest.fail("No journal mutation"))
    integration = StripeIntegration(stage().stripe_secret_key, "https://toolbako.example", journal=journal, financial_scope=SCOPE)
    with pytest.raises(RuntimeError, match="停止"):
        asyncio.run(integration._post(path, {"_idempotency_key": "synthetic"}))


@pytest.mark.parametrize("fault", [None, "account", "mode", "integer_mode", "permission", "timeout", "redirect", "unbound"])
def test_predeploy_requires_actual_bound_target_and_live_get_proof(fault, monkeypatch, capsys):
    backend = Backend()
    if fault == "unbound": backend.conn.row = (*backend.conn.row[:2], None, None, None, None)
    monkeypatch.setattr(checks, "PostgresStateStore", lambda *args, **kwargs: backend)
    calls = []
    def request(req):
        calls.append(req)
        assert req.method in {"GET", "HEAD"} and "stripe-account" not in req.headers
        if req.url.host == "api.stripe.com":
            assert req.method == "GET" and req.url.path in {"/v1/account", "/v1/balance"}
            if fault == "permission": return httpx.Response(403, text="PRIVATE_PROVIDER_BODY")
            if fault == "timeout": raise httpx.ConnectTimeout("PRIVATE_TIMEOUT_SECRET", request=req)
            if fault == "redirect": return httpx.Response(302, headers={"location": "https://unrelated.example"})
            if req.url.path == "/v1/account":
                return httpx.Response(200, json={"object": "account", "id": "acct_other" if fault == "account" else SCOPE.platform_account})
            return httpx.Response(200, json={"object": "balance", "livemode": 1 if fault == "integer_mode" else fault != "mode"})
        assert req.url.host == "xnjzgfaasbemffgorvnj.supabase.co"
        return httpx.Response(401 if req.headers["apikey"] == "anon-fixture" and req.method == "HEAD" else 200)
    original = httpx.AsyncClient
    def client(**kwargs):
        assert kwargs.get("follow_redirects") is False
        kwargs["transport"] = httpx.MockTransport(request)
        return original(**kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", client)
    from app.object_storage import SupabaseObjectStorage
    async def storage_proof(_): return {"private_delivery_bucket": True, "public_image_bucket": True}
    monkeypatch.setattr(SupabaseObjectStorage, "checks", storage_proof)
    result = asyncio.run(checks.connection_checks(stage()))
    assert all(result.values()) is (fault is None)
    assert result["live_stripe_connection_verified"] is (fault is None)
    assert backend.connects == 1
    if fault == "unbound": assert not calls
    assert "PRIVATE" not in capsys.readouterr().out
    assert stage().stripe_ready is False


def test_legacy_test_configuration_and_readiness_are_unchanged():
    settings = stage(stripe_connection_mode="sandbox", stripe_secret_key="rk_test_synthetic",
                     stripe_platform_account_id="", stripe_ledger_id="",
                     stripe_webhook_secret="one", stripe_connect_webhook_secret="two", stripe_connect_client_id="three")
    assert all(checks.configuration_checks(settings).values()) and settings.stripe_ready is True
    calls = []
    async def probe(): calls.append(True); return True
    assert asyncio.run(checks.deployment_database_check(settings, SimpleNamespace(probe=probe))) is True
    assert calls == [True]


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("bound", [True, False])
def test_live_stage_operational_route_uses_binding_not_provider(method, bound, monkeypatch):
    from fastapi.testclient import TestClient
    import app.main as main
    backend = Backend()
    if not bound: backend.conn.row = (*backend.conn.row[:2], None, None, None, None)
    monkeypatch.setattr(main, "settings", stage())
    monkeypatch.setattr(main, "database_store", backend)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: pytest.fail("Health must not depend on provider health/rate limits"))
    response = TestClient(main.app).request(method, "/deploymentz")
    assert response.status_code == (200 if bound else 503)
    assert backend.connects == 1 and backend.conn.closed
    assert "no-store" in response.headers["cache-control"]
    if method == "HEAD": assert response.content == b""
    else: assert response.json() == {"status": "ok" if bound else "unavailable", "live_payments_enabled": False}
