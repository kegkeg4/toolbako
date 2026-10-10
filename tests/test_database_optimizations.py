"""Avoid redundant writes without weakening request/financial boundaries.

These tests use the same disposable, local-only real Postgres fixture as the
repository tests. They are not a production load test or browser CWV evidence.
"""
import asyncio
import json

import pytest

from app.data import DemoStore
from app.database import PostgresStateStore, StorageUnavailable
from test_database import database, query


def revision(dsn):
    return query(dsn, "select revision from toolbako_runtime.marketplace_state where singleton")[0][0]


def test_unchanged_navigation_never_opens_a_save_transaction(database, monkeypatch):
    backend, dsn = database
    store = DemoStore(seed=False)

    async def run():
        async with backend.request(store):
            await backend.save()
        original_revision = revision(dsn)

        async def forbidden(*args, **kwargs):
            pytest.fail("Unchanged navigation must not write the snapshot")
        monkeypatch.setattr(backend, "_save", forbidden)
        for _ in range(3):
            async with backend.request(store):
                await backend.save()
        assert revision(dsn) == original_revision
    asyncio.run(run())


def test_first_save_of_each_worker_still_verifies_financial_history(database, monkeypatch):
    import app.finance as finance
    backend, dsn = database
    store = DemoStore(seed=False)
    calls = []
    original = finance.sync_finance

    async def record(conn, state):
        calls.append(True)
        await original(conn, state)
    monkeypatch.setattr(finance, "sync_finance", record)

    async def run():
        for worker in (backend, PostgresStateStore(dsn)):
            async with worker.request(store):
                await worker.save()
                await worker.save()
        assert len(calls) == 2
    asyncio.run(run())


@pytest.mark.parametrize("field", ["notifications", "account_sessions", "likes", "registered_users", "audit_logs", "orders"])
def test_nonempty_mutations_are_never_skipped(database, field):
    backend, dsn = database
    store = DemoStore(seed=False)

    async def run():
        async with backend.request(store):
            await backend.save()
        before = revision(dsn)
        async with backend.request(store):
            if field == "audit_logs":
                store.audit("tester", "test.lifecycle", "target")
            elif field == "likes":
                store.likes.add(("buyer", "tool"))
            elif isinstance(getattr(store, field), dict):
                getattr(store, field)["new"] = {"id": "new"}
            else:
                getattr(store, field).append({"id": "new"})
            await backend.save()
        assert revision(dsn) == before + 1
        async with PostgresStateStore(dsn).request(store):
            assert getattr(store, field)
    asyncio.run(run())


def test_outbox_only_change_still_commits(database):
    backend, dsn = database
    store = DemoStore(seed=False)

    async def run():
        async with backend.request(store):
            await backend.save()
        before = revision(dsn)
        async with backend.request(store):
            backend.enqueue_email("buyer@example.invalid", "from@example.invalid", "テスト", "本文")
            await backend.save()
            await backend.save()
        assert revision(dsn) == before + 1
        assert query(dsn, "select count(*) from toolbako_runtime.email_outbox") == [(1,)]
    asyncio.run(run())


def test_other_worker_changes_are_loaded_before_noop_decision(database, monkeypatch):
    backend, dsn = database
    store = DemoStore(seed=False)
    other = PostgresStateStore(dsn)

    async def run():
        async with backend.request(store):
            await backend.save()
        async with other.request(DemoStore(seed=False)):
            other._store.notifications.append({"id": "other-worker"})
            await other.save()
        before = revision(dsn)
        async def forbidden(*args, **kwargs):
            pytest.fail("Reloading another worker's unchanged nonfinancial state needs no write")
        monkeypatch.setattr(backend, "_save", forbidden)
        async with backend.request(store):
            assert store.notifications == [{"id": "other-worker"}]
            await backend.save()
        assert revision(dsn) == before
    asyncio.run(run())


def test_rollback_after_a_noop_restores_fresh_committed_state(database):
    backend, dsn = database
    store = DemoStore(seed=False)

    async def run():
        async with backend.request(store):
            store.notifications.append({"id": "saved"})
            await backend.save()
        before = revision(dsn)
        with pytest.raises(ValueError):
            async with backend.request(store):
                await backend.save()
                store.notifications.append({"id": "rollback"})
                raise ValueError("test failure")
        assert store.notifications == [{"id": "saved"}]
        assert revision(dsn) == before
    asyncio.run(run())


def test_json_set_order_from_other_worker_does_not_cause_rewrite(database, monkeypatch):
    backend, dsn = database
    store = DemoStore(seed=False)

    async def run():
        async with backend.request(store):
            store.likes.update(("buyer", str(i)) for i in range(20))
            await backend.save()
        payload = query(dsn, "select payload from toolbako_runtime.marketplace_state where singleton")[0][0]
        payload["likes"]["value"].reverse()
        query(dsn, "update toolbako_runtime.marketplace_state set payload=%s::jsonb returning revision", (json.dumps(payload),))
        before = revision(dsn)
        async def forbidden(*args, **kwargs):
            pytest.fail("A set's JSON array order is not a business change")
        monkeypatch.setattr(backend, "_save", forbidden)
        async with backend.request(store):
            assert len(store.likes) == 20
            await backend.save()
        assert revision(dsn) == before
    asyncio.run(run())


def test_schema_and_state_are_read_together_only_after_lock(database, monkeypatch):
    backend, _ = database
    original = backend.connect
    statements = []

    class RecordedConnection:
        def __init__(self, connection): self.connection = connection
        def __getattr__(self, key): return getattr(self.connection, key)
        async def execute(self, statement, *args, **kwargs):
            statements.append(statement)
            return await self.connection.execute(statement, *args, **kwargs)
    async def connect(): return RecordedConnection(await original())
    monkeypatch.setattr(backend, "connect", connect)

    async def run():
        async with backend.request(DemoStore(seed=False)):
            pass
    asyncio.run(run())
    assert statements[0] == "select pg_try_advisory_lock(%s)"
    assert "schema_version" in statements[1] and "marketplace_state" in statements[1]
    assert "advisory" not in statements[1]
    assert len([s for s in statements if s.startswith("select")]) == 2


@pytest.mark.parametrize("schema_change", ["delete from toolbako_runtime.schema_version returning version", "update toolbako_runtime.schema_version set version=999 returning version"])
def test_combined_read_rejects_missing_or_future_schema(database, schema_change):
    backend, dsn = database
    query(dsn, schema_change)
    async def run():
        with pytest.raises(StorageUnavailable):
            async with backend.request(DemoStore(seed=False)):
                pytest.fail("An incompatible schema must not enter a request")
    asyncio.run(run())
    assert query(dsn, "select count(*) from toolbako_runtime.marketplace_state") == [(0,)]


def test_other_worker_financial_change_still_runs_integrity_check(database, monkeypatch):
    import app.finance as finance
    backend, dsn = database
    store = DemoStore(seed=False)
    calls = []
    original = finance.sync_finance

    async def run():
        async with backend.request(store):
            await backend.save()
        other = PostgresStateStore(dsn)
        async with other.request(DemoStore(seed=False)):
            other._store.orders.append({"id": "pending-other-worker"})
            await other.save()
        async def record(conn, state):
            calls.append(True)
            await original(conn, state)
        monkeypatch.setattr(finance, "sync_finance", record)
        async with backend.request(store):
            assert store.orders == [{"id": "pending-other-worker"}]
            await backend.save()
            await backend.save()
        assert len(calls) == 1
    asyncio.run(run())


def test_operation_reservation_is_not_optimized_away_with_unchanged_state(database):
    backend, dsn = database
    store = DemoStore(seed=False)
    async def run():
        async with backend.request(store):
            await backend.save()
        before = revision(dsn)
        async with backend.request(store):
            await backend.begin_operation("noop-checkout", "checkout/sessions", {"amount": "1000"})
        assert revision(dsn) == before + 1
        assert query(dsn, "select status from toolbako_runtime.stripe_operations where operation_id='noop-checkout'") == [("pending",)]
    asyncio.run(run())


def test_existing_audit_tampering_is_still_rejected(database):
    backend, _ = database
    store = DemoStore(seed=False)
    async def run():
        async with backend.request(store):
            store.audit("tester", "test.original", "target")
            await backend.save()
        async with backend.request(store):
            store.audit_logs[0]["action"] = "tampered"
            with pytest.raises(StorageUnavailable):
                await backend.save()
        assert store.audit_logs[0]["action"] == "test.original"
    asyncio.run(run())
