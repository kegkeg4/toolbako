"""Opt-in five-minute drill against disposable LOCAL PostgreSQL only."""
import asyncio
import json
import os
import time

import pytest

from app.data import DemoStore
from app.database import PostgresStateStore
from test_database import database, query
from test_member_read import sample_store


class SyntheticRollback(Exception):
    pass


@pytest.mark.skipif(os.getenv("TOOLBAKO_LONG_LOAD") != "1", reason="Explicit opt-in for five-minute local drill")
def test_sustained_member_public_and_mutations(database, record_property):
    _, dsn = database

    async def run():
        backend = PostgresStateStore(dsn, reuse_connections=True)
        store = DemoStore(seed=False)
        committed = set()
        phases = []
        try:
            async with backend.request(store):
                initial = sample_store()
                for field in ("tools", "registered_users", "account_sessions", "notifications"):
                    setattr(store, field, getattr(initial, field))
                for item in store.notifications:
                    item["title"] = item["body"] = "generation-0"
                await backend.save()
            for concurrency in (8, 16):
                started = time.monotonic()
                timings = {key: [] for key in ("public", "member", "write", "rollback")}

                async def client(number):
                    iteration = 0
                    while time.monotonic() - started < 150:
                        kind = tuple(timings)[(iteration + number) % 4]
                        begin = time.monotonic()
                        if kind == "public":
                            view = await backend.public_view(catalog=True)
                            assert len(view.tools) == 1
                        elif kind == "member":
                            owner = "a" if number % 2 == 0 else "b"
                            view = await backend.member_view("sid-" + owner, "/notifications")
                            assert set(view.account_sessions) == {"sid-" + owner}
                            assert set(view.registered_users) == {owner}
                            assert len(view.notifications) == 1
                            notice = view.notifications[0]
                            assert notice["user_id"] == owner
                            assert notice["title"] == notice["body"]
                            assert notice["title"].startswith("generation-")
                            assert "provider_tokens" not in view.account_sessions["sid-" + owner]
                            assert "password_hash" not in view.registered_users[owner]
                        else:
                            key = f"{concurrency}-{number}-{iteration}"
                            try:
                                async with backend.request(store):
                                    assert {x["target"] for x in store.audit_logs} == committed
                                    store.audit(None, "drill." + kind, key)
                                    for item in store.notifications:
                                        item["title"] = item["body"] = (
                                            "UNCOMMITTED" if kind == "rollback" else "generation-" + key)
                                    if kind == "rollback":
                                        raise SyntheticRollback()
                                    await backend.save()
                                    committed.add(key)
                            except SyntheticRollback:
                                pass
                        timings[kind].append((time.monotonic() - begin) * 1000)
                        iteration += 1
                        await asyncio.sleep(0.05)

                await asyncio.wait_for(asyncio.gather(*(client(n) for n in range(concurrency))), 210)
                metrics = {}
                for kind, samples in timings.items():
                    ordered = sorted(samples)
                    assert ordered
                    metrics[kind] = {"count": len(ordered), "p50_ms": round(ordered[(len(ordered)-1)//2], 2),
                                     "p95_ms": round(ordered[int((len(ordered)-1)*.95)], 2),
                                     "max_ms": round(ordered[-1], 2)}
                phases.append({"concurrency": concurrency, "seconds": round(time.monotonic()-started, 2),
                               "metrics": metrics, "errors": 0})
                assert len(backend._public_connections) <= 2
                assert not backend._local_lock.locked()
                assert backend._connection_slots._value == 2
            fresh = DemoStore(seed=False)
            async with PostgresStateStore(dsn).request(fresh):
                assert {x["target"] for x in fresh.audit_logs} == committed
                assert all(x["title"].startswith("generation-") for x in fresh.notifications)
            assert query(dsn, "select count(*) from toolbako_runtime.audit_events") == [(len(committed),)]
            return {"scope": "local_synthetic_repository_not_production_http_capacity", "phases": phases,
                    "durable_writes": len(committed), "owner_isolation": True,
                    "atomic_snapshots": True, "rollback_integrity": True, "connection_reuse": True}
        finally:
            await backend.close()

    result = asyncio.run(run())
    record_property("sustained_load", json.dumps(result))
