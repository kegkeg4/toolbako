"""Real Postgres integration tests, never substitutes SQLite for PostgreSQL.

TEST_POSTGRES_ADMIN_DSN must point at a disposable LOCAL server. Each test
creates and removes its own toolbako_test_* database; no existing DB is erased.
"""
import asyncio
from pathlib import Path
import hashlib
import hmac
import json
import os
import time
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.config import Settings
from app.data import DemoStore
from app.database import (
    DatabaseBoundaryMiddleware, OperationConflict, PostgresStateStore,
    StorageUnavailable, migrate, snapshot,
)
from app.integrations import StripeIntegration, StripeOutcomeUnknown
from app.persistence import STATE_FIELDS
from app.webhooks import StripeWebhookService


def test_empty_store_never_contains_sample_accounts_or_sales():
    store = DemoStore(seed=False)
    assert all(not getattr(store, name) for name in STATE_FIELDS)
    assert DemoStore().tools


def test_database_configuration_disables_demo_login():
    assert not Settings(database_url="postgresql://localhost/test", demo_mode=True).demo_mode


def test_production_database_connection_requires_tls_without_leaking_dsn():
    db = PostgresStateStore("postgresql://secret:private@localhost/test", production=True)
    with pytest.raises(StorageUnavailable) as error:
        asyncio.run(db.connect())
    assert "secret" not in str(error.value) and "private" not in str(error.value)


def test_transaction_pooler_is_rejected():
    db = PostgresStateStore("postgresql://localhost:6543/test")
    with pytest.raises(StorageUnavailable):
        asyncio.run(db.connect())


def test_missing_production_database_fails_closed_but_health_is_accessible():
    app = FastAPI()
    @app.get("/")
    async def home():
        return {"should_not_be_seen": True}
    @app.get("/healthz")
    async def health():
        return {"alive": True}
    app.add_middleware(DatabaseBoundaryMiddleware, backend=None, store=DemoStore(seed=False))
    client = TestClient(app)
    assert client.get("/").status_code == 503
    assert client.get("/").headers["cache-control"] == "no-store"
    assert client.get("/healthz").status_code == 200


@pytest.fixture
def database():
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    admin_dsn = os.getenv("TEST_POSTGRES_ADMIN_DSN")
    if not admin_dsn:
        pytest.skip("Set TEST_POSTGRES_ADMIN_DSN to a disposable local PostgreSQL server")
    options = conninfo_to_dict(admin_dsn)
    host = options.get("host", "")
    if not (host.startswith("/tmp/") or host in {"127.0.0.1", "localhost", "::1"}):
        pytest.fail("Tests refuse non-local database servers")
    name = "toolbako_test_" + uuid4().hex
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    dsn = make_conninfo(admin_dsn, dbname=name)
    backend = PostgresStateStore(dsn)
    asyncio.run(migrate(backend))
    try:
        yield backend, dsn
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("drop database {} with (force)").format(sql.Identifier(name)))


def query(dsn, statement, params=()):
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as conn:
        return conn.execute(statement, params).fetchall()


def test_migration_is_idempotent_and_probe_detects_schema(database):
    backend, dsn = database
    asyncio.run(migrate(backend))
    assert asyncio.run(backend.probe())
    assert query(dsn, "select bool_and(relrowsecurity) from pg_class where relnamespace='toolbako_runtime'::regnamespace and relkind='r'") == [(True,)]
    assert query(dsn, "select count(*) from pg_indexes where schemaname='toolbako_runtime' and indexname in ('runtime_allocation_receipt','runtime_entry_receipt','runtime_entry_payout')") == [(3,)]


def test_finance_index_maintenance_is_additive_and_idempotent(database):
    _, dsn = database
    migration = Path(__file__).resolve().parents[1] / "supabase/runtime_finance_indexes.sql"
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as conn:
        for _ in range(2):
            conn.execute(migration.read_text())
    assert query(dsn, "select version from toolbako_runtime.schema_version where singleton") == [(2,)]
    assert query(dsn, "select count(*) from pg_indexes where schemaname='toolbako_runtime' and indexname in ('runtime_allocation_receipt','runtime_entry_receipt','runtime_entry_payout')") == [(3,)]


def test_first_database_request_does_not_import_demo_state(database):
    backend, _ = database
    store = DemoStore()
    async def run():
        async with backend.request(store):
            assert not store.tools and not store.registered_users and not store.orders
            await backend.save()
    asyncio.run(run())


def test_persisted_state_round_trips_across_independent_workers(database):
    backend, dsn = database
    store = DemoStore(seed=False)
    async def run():
        async with backend.request(store):
            store.registered_users["creator"] = {"id": "seller", "name": "作成者"}
            store.likes.add(("buyer", "tool"))
            store.audit("seller", "test.saved", "tool")
            await backend.save()
        second = PostgresStateStore(dsn)
        second_store = DemoStore(seed=False)
        async with second.request(second_store):
            assert second_store.registered_users["creator"]["id"] == "seller"
            assert ("buyer", "tool") in second_store.likes
            assert second_store.audit_logs[0]["created_at"].tzinfo is not None
    asyncio.run(run())
    assert len(query(dsn, "select id from toolbako_runtime.audit_events")) == 1


def test_failed_request_rolls_back_but_keeps_explicit_checkpoint(database):
    backend, _ = database
    store = DemoStore(seed=False)
    async def run():
        with pytest.raises(ValueError):
            async with backend.request(store):
                store.orders.append({"id": "checkpoint"})
                await backend.save()
                store.orders.append({"id": "must_roll_back"})
                raise ValueError("simulated process failure")
        async with backend.request(store):
            assert [o["id"] for o in store.orders] == ["checkpoint"]
    asyncio.run(run())


def test_second_worker_cannot_overwrite_a_request_in_progress(database):
    backend, dsn = database
    other = PostgresStateStore(dsn)
    first_store, other_store = DemoStore(seed=False), DemoStore(seed=False)
    async def run():
        async with backend.request(first_store):
            first_store.orders.append({"id": "first"})
            # A checkpoint does not release our session advisory lock.
            await backend.save()
            with pytest.raises(StorageUnavailable):
                async with other.request(other_store):
                    pytest.fail("Concurrent writer entered the critical section")
        async with other.request(other_store):
            assert other_store.orders == [{"id": "first"}]
            other_store.orders.append({"id": "second"})
            await other.save()
        async with backend.request(first_store):
            assert len(first_store.orders) == 2
    asyncio.run(run())


@pytest.mark.parametrize("statement", [
    "update toolbako_runtime.audit_events set action='tampered'",
    "delete from toolbako_runtime.audit_events",
    "truncate toolbako_runtime.audit_events",
])
def test_audit_table_rejects_modification_and_deletion(database, statement):
    import psycopg
    backend, dsn = database
    store = DemoStore(seed=False)
    async def run():
        async with backend.request(store):
            store.audit("user", "original", "target")
            await backend.save()
    asyncio.run(run())
    with pytest.raises(psycopg.errors.RaiseException):
        query(dsn, statement)
    assert query(dsn, "select action from toolbako_runtime.audit_events") == [("original",)]


def test_audit_and_business_state_commit_together(database):
    backend, dsn = database
    store = DemoStore(seed=False)
    async def run():
        async with backend.request(store):
            store.audit("user", "original", "target")
            await backend.save()
            store.audit_logs[0]["action"] = "tampered"
            store.orders.append({"id": "must_not_save"})
            with pytest.raises(StorageUnavailable):
                await backend.save()
        async with backend.request(store):
            assert not store.orders
            assert store.audit_logs[0]["action"] == "original"
    asyncio.run(run())
    assert query(dsn, "select action from toolbako_runtime.audit_events") == [("original",)]


def test_operation_reservation_survives_crash_and_is_not_reissued(database):
    backend, dsn = database
    store = DemoStore(seed=False)
    async def run():
        async with backend.request(store):
            store.orders.append({"id": "pending-order", "payment_status": "pending"})
            await backend.begin_operation("checkout-order", "checkout/sessions", {"amount": "1000"})
            # No final request save: simulate a process exit before HTTP I/O.
        second = PostgresStateStore(dsn)
        async with second.request(store):
            assert store.orders[0]["id"] == "pending-order"
            with pytest.raises(StripeOutcomeUnknown):
                await second.begin_operation("checkout-order", "checkout/sessions", {"amount": "1000"})
    asyncio.run(run())
    assert query(dsn, "select status from toolbako_runtime.stripe_operations") == [("pending",)]


def test_operation_key_cannot_be_reused_with_other_parameters(database):
    backend, _ = database
    store = DemoStore(seed=False)
    async def run():
        async with backend.request(store):
            await backend.begin_operation("same", "refunds", {"amount": "1000"})
            with pytest.raises(OperationConflict):
                await backend.begin_operation("same", "refunds", {"amount": "2000"})
    asyncio.run(run())


def test_provider_success_is_replayed_without_second_network_call(database, monkeypatch):
    backend, dsn = database
    store = DemoStore(seed=False)
    calls = []
    def handle(request):
        calls.append(request)
        # The durable reservation already exists before Stripe receives I/O.
        assert query(dsn, "select status from toolbako_runtime.stripe_operations") == [("pending",)]
        return httpx.Response(200, json={"id": "cs_test_once", "url": "https://checkout.stripe.com/test"})
    client_class = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client_class(transport=httpx.MockTransport(handle), **kw))
    stripe = StripeIntegration("sk_test_only", "https://toolbako.example", journal=backend)
    async def run():
        async with backend.request(store):
            data = {"_idempotency_key": "checkout-once", "amount": "1000"}
            first = await stripe._post("checkout/sessions", data)
            assert data["_idempotency_key"] == "checkout-once"
            second = await stripe._post("checkout/sessions", data)
            assert first == second
        assert len(calls) == 1
    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["timeout", "server_error", "invalid_json", "invalid_object"])
def test_unknown_provider_results_remain_reserved(database, monkeypatch, outcome):
    backend, dsn = database
    store = DemoStore(seed=False)
    def handle(request):
        if outcome == "timeout":
            raise httpx.ReadTimeout("simulated", request=request)
        if outcome == "server_error":
            return httpx.Response(500)
        if outcome == "invalid_json":
            return httpx.Response(200, text="bad")
        return httpx.Response(200, json=[])
    client_class = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client_class(transport=httpx.MockTransport(handle), **kw))
    stripe = StripeIntegration("sk_test_only", "https://toolbako.example", journal=backend)
    async def run():
        async with backend.request(store):
            with pytest.raises(StripeOutcomeUnknown):
                await stripe._post("checkout/sessions", {"_idempotency_key": "uncertain"})
    asyncio.run(run())
    assert query(dsn, "select status from toolbako_runtime.stripe_operations") == [("unknown",)]


def test_http_success_is_not_returned_if_commit_fails(database, monkeypatch):
    backend, _ = database
    store = DemoStore(seed=False)
    app = FastAPI()
    @app.post("/write")
    async def write():
        store.orders.append({"id": "must_not_save"})
        return {"saved": True}
    app.add_middleware(DatabaseBoundaryMiddleware, backend=backend, store=store)
    async def fail():
        raise StorageUnavailable("simulated disk/network outage")
    monkeypatch.setattr(backend, "save", fail)
    response = TestClient(app).post("/write")
    assert response.status_code == 503
    assert "saved" not in response.json()
    assert not store.orders


def test_read_only_request_lifecycle_mutations_are_persisted(database):
    backend, _ = database
    store = DemoStore(seed=False)
    app = FastAPI()
    @app.get("/read")
    async def read():
        store.notifications.append({"id": "expiry-lifecycle"})
        return {"ok": True}
    @app.post("/fail")
    async def fail():
        store.notifications.append({"id": "rollback"})
        return JSONResponse({"error": "failure"}, status_code=500)
    app.add_middleware(DatabaseBoundaryMiddleware, backend=backend, store=store)
    with TestClient(app) as client:
        assert client.get("/read").status_code == 200
        assert client.post("/fail").status_code == 500
    async def check():
        async with backend.request(store):
            assert store.notifications == [{"id": "expiry-lifecycle"}]
    asyncio.run(check())


def webhook_request(event, secret):
    body = json.dumps(event).encode()
    timestamp = int(time.time())
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    async def receive():
        return {"type": "http.request", "body": body}
    return Request({"type": "http", "method": "POST", "path": "/webhooks/stripe", "headers": [
        (b"stripe-signature", f"t={timestamp},v1={digest}".encode()),
    ]}, receive)


def test_webhook_deduplication_and_order_commit_survive_worker_restart(database):
    backend, dsn = database
    store = DemoStore(seed=False)
    secret = "test-webhook-signature"
    event = {"id": "evt_once", "type": "checkout.session.completed", "data": {"object": {
        "id": "cs_once", "currency": "jpy", "amount_total": 1000, "payment_status": "paid",
        "payment_intent": "pi_once", "metadata": {"order_id": "order-once"},
    }}}
    async def no_email(*args):
        return True
    async def run():
        async with backend.request(store):
            from datetime import datetime, timezone
            store.orders.append({"id": "order-once", "amount": 1000, "platform_fee": 150, "created_at": datetime.now(timezone.utc), "payment_status": "pending", "tool_slug": "test", "tool_name": "Test", "buyer_id": "buyer", "seller_id": "seller", "checkout_session_id": "cs_once", "billing_type": "one_time"})
            await backend.save()
            service = StripeWebhookService(Settings(stripe_webhook_secret=secret), store, no_email)
            await service.handle(webhook_request(event, secret))
            await backend.save()
        restarted = PostgresStateStore(dsn)
        async with restarted.request(store):
            service = StripeWebhookService(Settings(stripe_webhook_secret=secret), store, no_email)
            response = await service.handle(webhook_request(event, secret))
            assert response["duplicate"] is True
            assert store.orders[0]["payment_status"] == "paid"
            assert len(store.notifications) == 2
    asyncio.run(run())


def test_real_application_signup_listing_checkout_and_restart(database):
    import subprocess
    import sys
    from pathlib import Path
    _, dsn = database
    root = Path(__file__).resolve().parent.parent
    env = {**os.environ, "DATABASE_URL": dsn, "APP_ENV": "staging", "DEMO_MODE": "false",
           "SUPABASE_URL": "", "SUPABASE_ANON_KEY": "", "EMAIL_API_KEY": "", "STATE_DB_PATH": "",
           "SENTRY_DSN": "", "PRIVILEGED_MFA_REQUIRED": "false", "SITE_BASE_URL": "http://testserver",
           "STRIPE_SECRET_KEY": "sk_test_runtime", "STRIPE_WEBHOOK_SECRET": "whsec_runtime",
           "STRIPE_CONNECT_WEBHOOK_SECRET": "whsec_connect_runtime", "STRIPE_CONNECT_CLIENT_ID": "ca_runtime",
           "ALLOWED_HOSTS": "testserver,127.0.0.1,localhost", "PYTHONPATH": str(root)}
    result = subprocess.run([sys.executable, str(root / "tests/runtime_app_scenario.py")],
                            cwd=root, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_notification_outbox_commits_only_with_business_state(database):
    backend, dsn = database
    store = DemoStore(seed=False)
    async def run():
        async with backend.request(store):
            backend.enqueue_email("buyer@example.invalid", "from@example.invalid", "購入完了", "テスト本文")
            assert not query(dsn, "select id from toolbako_runtime.email_outbox")
            store.orders.append({"id": "committed"})
            await backend.save()
            assert len(query(dsn, "select id from toolbako_runtime.email_outbox")) == 1
            backend.enqueue_email("buyer@example.invalid", "from@example.invalid", "未確定", "ロールバック")
        async with backend.request(store):
            assert store.orders == [{"id": "committed"}]
    asyncio.run(run())
    assert query(dsn, "select subject from toolbako_runtime.email_outbox") == [("購入完了",)]


def test_email_worker_retries_with_same_key_and_does_not_send_twice(database):
    from app.mail_worker import deliver_pending
    backend, dsn = database
    store = DemoStore(seed=False)
    calls = []
    class FakeSender:
        def __init__(self, key, sender):
            pass
        async def send(self, *args, idempotency_key):
            calls.append(idempotency_key)
            return len(calls) > 1
    async def run():
        async with backend.request(store):
            backend.enqueue_email("buyer@example.invalid", "from@example.invalid", "購入完了", "本文")
            await backend.save()
        assert (await deliver_pending(backend, "test", sender_factory=FakeSender))["retry"] == 1
        assert (await deliver_pending(backend, "test", sender_factory=FakeSender))["sent"] == 0
        query(dsn, "update toolbako_runtime.email_outbox set available_at=now() returning id")
        assert (await deliver_pending(backend, "test", sender_factory=FakeSender))["sent"] == 1
        assert (await deliver_pending(backend, "test", sender_factory=FakeSender))["sent"] == 0
    asyncio.run(run())
    assert len(calls) == 2 and calls[0] == calls[1]


def test_email_retry_after_provider_idempotency_window_requires_review(database):
    from app.mail_worker import deliver_pending
    backend, dsn = database
    store = DemoStore(seed=False)
    class NeverSend:
        def __init__(self, *args):
            pytest.fail("A possibly delivered old email must not be resent")
    async def run():
        async with backend.request(store):
            backend.enqueue_email("buyer@example.invalid", "from@example.invalid", "確認", "本文")
            await backend.save()
        query(dsn, "update toolbako_runtime.email_outbox set attempts=1,first_attempt_at=now()-interval '25 hours' returning id")
        assert (await deliver_pending(backend, "test", sender_factory=NeverSend))["review"] == 1
    asyncio.run(run())
    assert query(dsn, "select status from toolbako_runtime.email_outbox") == [("review",)]


def test_webhook_recovers_checkout_after_response_recorded_before_process_crash(database):
    from fastapi import HTTPException
    backend, _ = database
    store = DemoStore(seed=False)
    secret = "test-signature"
    async def no_email(*args):
        return True
    event = {"id": "evt_recovery", "livemode": False, "type": "checkout.session.completed", "data": {"object": {
        "id": "cs_recovered", "currency": "jpy", "amount_total": 1000, "payment_status": "paid",
        "payment_intent": "pi_recovered", "metadata": {"order_id": "recovery"},
    }}}
    async def run():
        async with backend.request(store):
            from datetime import datetime, timezone
            store.orders.append({"id": "recovery", "amount": 1000, "platform_fee": 150, "created_at": datetime.now(timezone.utc), "payment_status": "pending", "tool_slug": "test", "tool_name": "Test", "buyer_id": "buyer", "seller_id": "seller", "billing_type": "one_time"})
            await backend.begin_operation("checkout-recovery", "checkout/sessions", {"amount": "1000"})
            await backend.finish_operation("checkout-recovery", "succeeded", {"id": "cs_recovered", "url": "https://checkout.stripe.com/test"})
        async with backend.request(store):
            service = StripeWebhookService(Settings(stripe_secret_key="sk_test_only", stripe_webhook_secret=secret), store, no_email, journal=backend)
            bad = {**event, "livemode": True}
            with pytest.raises(HTTPException):
                await service.handle(webhook_request(bad, secret))
            assert not store.orders[0].get("checkout_session_id")
            response = await service.handle(webhook_request(event, secret))
            assert response["received"]
            assert store.orders[0]["checkout_session_id"] == "cs_recovered"
            assert store.orders[0]["payment_status"] == "paid"
            duplicate = {**event, "id": "evt_second", "type": "checkout.session.async_payment_succeeded"}
            await service.handle(webhook_request(duplicate, secret))
            assert len(store.notifications) == 2
            await backend.save()
    asyncio.run(run())


def test_expired_extra_checkout_never_cancels_original_purchase():
    store = DemoStore(seed=False)
    store.orders.append({"id": "original", "payment_status": "paid", "status": "completed", "checkout_session_id": "cs_original",
                         "pending_extras": [{"id": "extra", "status": "pending", "checkout_session_id": "cs_extra"}]})
    event = {"id": "evt_extra_expired", "type": "checkout.session.expired", "data": {"object": {
        "id": "cs_extra", "metadata": {"order_id": "original", "extra_id": "extra"}}}}
    async def no_email(*args):
        return True
    service = StripeWebhookService(Settings(stripe_webhook_secret="test"), store, no_email)
    asyncio.run(service.handle(webhook_request(event, "test")))
    assert store.orders[0]["payment_status"] == "paid"
    assert store.orders[0]["status"] == "completed"
    assert store.orders[0]["pending_extras"][0]["status"] == "expired"


@pytest.fixture
def auth_database(database):
    """Supabase-shaped local fixture; never uses real auth users or passwords."""
    import psycopg
    _, dsn = database
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("""
        do $$ declare n text; begin
          foreach n in array array['anon','authenticated','service_role'] loop
            if not exists(select 1 from pg_roles where rolname=n) then
              execute format('create role %I nologin', n);
            end if;
          end loop;
        end $$;
        alter role service_role bypassrls;
        create schema auth;
        create table auth.users(id uuid primary key, raw_user_meta_data jsonb);
        create function auth.uid() returns uuid language sql stable as
          $$ select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$;
        grant usage on schema auth, public to anon, authenticated, service_role;
        grant execute on function auth.uid() to anon, authenticated;
        alter default privileges in schema public grant all on tables to anon, authenticated;
        create table public.posts(id integer primary key, content text);
        create table public.replies(id integer primary key);
        insert into public.posts values(1, 'existing data must stay');
        """)
    return dsn


def apply_auth_bootstrap(dsn):
    import psycopg
    from pathlib import Path
    source = Path(__file__).resolve().parents[1] / "supabase/bootstrap_runtime_auth.sql"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(source.read_text())


def insert_auth_user(dsn, metadata):
    user_id = uuid4()
    query(dsn, "insert into auth.users values (%s, %s::jsonb) returning id", (user_id, json.dumps(metadata)))
    return user_id


def test_auth_bootstrap_preserves_existing_tables_and_is_fail_closed(auth_database):
    import psycopg
    apply_auth_bootstrap(auth_database)
    assert query(auth_database, "select content from public.posts") == [("existing data must stay",)]
    with pytest.raises(psycopg.errors.RaiseException, match="already exist"):
        apply_auth_bootstrap(auth_database)
    assert query(auth_database, "select count(*) from public.profiles") == [(0,)]


def test_auth_bootstrap_refuses_existing_users_without_backfill(auth_database):
    import psycopg
    insert_auth_user(auth_database, {})
    with pytest.raises(psycopg.errors.RaiseException, match="reviewed backfill"):
        apply_auth_bootstrap(auth_database)
    assert query(auth_database, "select to_regclass('public.profiles')") == [(None,)]


def test_auth_bootstrap_ignores_untrusted_trust_fields_and_bounds_metadata(auth_database):
    apply_auth_bootstrap(auth_database)
    user_id = insert_auth_user(auth_database, {
        "user_name": "x" * 200, "display_name": "あ" * 100,
        "is_banned": True, "identity_status": "verified", "is_certified_creator": True,
    })
    row = query(auth_database, "select username, display_name, is_banned, identity_status from public.profiles where id=%s", (user_id,))[0]
    assert len(row[0]) <= 30 and row[1] == "あ" * 60
    assert row[2:] == (False, "unverified")
    assert query(auth_database, "select count(*) from public.creator_badges") == [(0,)]


def test_auth_bootstrap_generates_unique_bounded_names_and_keeps_display_name(auth_database):
    apply_auth_bootstrap(auth_database)
    first = insert_auth_user(auth_database, {"user_name": "A" * 30, "display_name": "希望の名前"})
    second = insert_auth_user(auth_database, {"user_name": "A" * 30, "display_name": "\n\t "})
    rows = query(auth_database, "select id, username, display_name from public.profiles")
    names = {r[1] for r in rows}
    assert len(names) == 2 and all(3 <= len(n) <= 30 for n in names)
    assert next(r[2] for r in rows if r[0] == first) == "希望の名前"
    assert next(r[2] for r in rows if r[0] == second)


def test_auth_bootstrap_only_reads_own_profile_and_prevents_client_edits(auth_database):
    import psycopg
    apply_auth_bootstrap(auth_database)
    user_id = insert_auth_user(auth_database, {"user_name": "first_user"})
    other_id = insert_auth_user(auth_database, {"user_name": "second_user"})
    with psycopg.connect(auth_database, autocommit=True) as conn:
        conn.execute("select set_config('request.jwt.claim.sub', %s, false)", (str(user_id),))
        conn.execute("set role authenticated")
        assert conn.execute("select id from public.profiles").fetchall() == [(user_id,)]
        assert conn.execute("select id from public.profiles where id=%s", (other_id,)).fetchall() == []
        for statement in (
            "update public.profiles set identity_status='verified'",
            "update public.profiles set display_name='bypass server'",
            "delete from public.profiles",
            "insert into public.creator_badges(profile_id, is_certified_creator) values (auth.uid(), true)",
            "select * from toolbako_runtime.marketplace_state",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)
        conn.execute("reset role")
        conn.execute("set role anon")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("select * from public.profiles")


def test_auth_bootstrap_allows_server_profile_updates_but_hides_banned_accounts(auth_database):
    import psycopg
    apply_auth_bootstrap(auth_database)
    user_id = insert_auth_user(auth_database, {"user_name": "test_creator"})
    with psycopg.connect(auth_database, autocommit=True) as conn:
        conn.execute("set role service_role")
        conn.execute("update public.profiles set bio=%s, headline='Hello', skills=array['Python'], is_banned=true where id=%s", ("あ" * 600, user_id))
        conn.execute("insert into public.creator_badges(profile_id, is_certified_creator) values (%s, true)", (user_id,))
        conn.execute("reset role")
        conn.execute("select set_config('request.jwt.claim.sub', %s, false)", (str(user_id),))
        conn.execute("set role authenticated")
        assert conn.execute("select * from public.profiles").fetchall() == []
        assert conn.execute("select * from public.creator_badges").fetchall() == []
