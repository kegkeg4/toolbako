import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.data import DemoStore
from app.operations import operation_summary
from test_database import database, query


def test_backlog_summary_is_read_only_and_contains_no_email_or_provider_payload(database):
    backend, dsn = database
    async def run():
        store = DemoStore(seed=False)
        at = datetime.now(timezone.utc)
        async with backend.request(store):
            backend.enqueue_email("private-address@example.test", "from@example.test", "private subject", "private body")
            await backend.save()
            await backend.begin_operation("unknown-operation", "refunds", {"private": "request-payload"})
            await backend.finish_operation("unknown-operation", "unknown")
        query(dsn, "update toolbako_runtime.email_outbox set created_at=%s,available_at=%s returning id", (at - timedelta(minutes=16), at - timedelta(minutes=1)))
        before = query(dsn, "select revision from toolbako_runtime.marketplace_state")
        async with backend.request(store):
            result = await operation_summary(backend, Settings(), at=at)
            assert result["counts"]["mail_pending"] == result["counts"]["mail_overdue"] == 1
            assert result["counts"]["payment_unknown"] == 1
            assert len(result["alerts"]) >= 5
            assert "private-address" not in str(result) and "private subject" not in str(result) and "request-payload" not in str(result)
        assert query(dsn, "select revision from toolbako_runtime.marketplace_state") == before
        assert query(dsn, "select status from toolbako_runtime.stripe_operations") == [("unknown",)]
    asyncio.run(run())


def test_expired_mail_lease_and_review_are_visible_without_automatic_retry(database):
    backend, dsn = database
    async def run():
        store = DemoStore(seed=False)
        at = datetime.now(timezone.utc)
        async with backend.request(store):
            for _ in range(2): backend.enqueue_email("private@example.test", "from@example.test", "subject", "body")
            await backend.save()
        ids = query(dsn, "select id from toolbako_runtime.email_outbox order by id")
        query(dsn, "update toolbako_runtime.email_outbox set status='sending',lease_until=%s where id=%s returning id", (at-timedelta(minutes=1), ids[0][0]))
        query(dsn, "update toolbako_runtime.email_outbox set status='review' where id=%s returning id", (ids[1][0],))
        async with backend.request(store):
            result = await operation_summary(backend, Settings(), at=at)
            assert result["counts"]["mail_expired_lease"] == result["counts"]["mail_review"] == 1
        assert set(query(dsn, "select status from toolbako_runtime.email_outbox")) == {("sending",), ("review",)}
    asyncio.run(run())
