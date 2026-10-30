"""Bounded mixed reads/writes on disposable LOCAL Postgres, no external users."""
import asyncio

from app.data import DemoStore
from test_database import database, query


def test_concurrent_reads_writes_and_failed_requests_keep_committed_state(database):
    backend, dsn = database

    async def run():
        store = DemoStore(seed=False)
        committed = set()

        async def client(number):
            for iteration in range(8):
                key = f"synthetic-{number}-{iteration}"
                try:
                    async with backend.request(store):
                        assert {x["target"] for x in store.audit_logs} == committed
                        if iteration % 3 == 0:
                            store.audit(None, "test.mixed_load", key)
                            await backend.save()
                            committed.add(key)
                        elif iteration % 3 == 1:
                            store.audit(None, "test.must_rollback", key)
                            raise ValueError("synthetic handler failure")
                except ValueError:
                    pass

        await asyncio.wait_for(asyncio.gather(*(client(n) for n in range(8))), timeout=90)
        async with backend.request(store):
            assert len(committed) == 24
            assert {x["target"] for x in store.audit_logs} == committed
        assert not backend._local_lock.locked()
        assert backend._connection_slots._value == 2

    asyncio.run(run())
    assert query(dsn, "select count(*) from toolbako_runtime.audit_events") == [(24,)]
