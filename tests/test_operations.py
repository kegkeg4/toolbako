import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.data import DemoStore
from app.operations import inspect_operations, operation_summary
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


def test_independent_inspection_does_not_initialize_state_or_wait_for_request_lock(database):
    from app.database import LOCK_ID
    import psycopg
    backend, dsn = database
    assert query(dsn, 'select count(*) from toolbako_runtime.marketplace_state') == [(0,)]
    with psycopg.connect(dsn, autocommit=True) as locked:
        locked.execute('select pg_advisory_lock(%s)', (LOCK_ID,))
        result = asyncio.run(inspect_operations(backend, Settings()))
        assert result['counts']['mail_pending'] == 0
    assert query(dsn, 'select count(*) from toolbako_runtime.marketplace_state') == [(0,)]


def test_independent_inspection_enforces_database_read_only(database, monkeypatch):
    import psycopg
    import pytest
    backend, _ = database
    async def attempt_write(conn):
        await conn.execute('delete from toolbako_runtime.schema_version')
    monkeypatch.setattr(backend, '_check_schema', attempt_write)
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        asyncio.run(inspect_operations(backend, Settings()))


def test_missing_mail_lease_is_reported_without_changing_or_exposing_message(database):
    backend, dsn = database
    query(dsn, "insert into toolbako_runtime.email_outbox(id,recipient,sender,subject,body,status) values (gen_random_uuid(),'private@example.invalid','sender@example.invalid','private subject','private body','sending') returning id")
    result = asyncio.run(inspect_operations(backend, Settings()))
    assert result['counts']['mail_missing_lease'] == result['counts']['mail_sending'] == 1
    assert any('処理期限が記録されていない' in alert for alert in result['alerts'])
    assert 'private@example' not in str(result) and 'private body' not in str(result)
    assert query(dsn, 'select status,attempts from toolbako_runtime.email_outbox') == [('sending', 0)]


def test_mail_worker_dry_run_needs_no_key_never_sends_and_keeps_outbox_unchanged(database, monkeypatch, capsys):
    import json
    from app import config, mail_worker
    backend, dsn = database
    query(dsn, "insert into toolbako_runtime.email_outbox(id,recipient,sender,subject,body,created_at) values (gen_random_uuid(),'private@example.invalid','sender@example.invalid','private subject','private body',now()-interval '1 hour') returning id")
    before = query(dsn, 'select row_to_json(t)::text from toolbako_runtime.email_outbox t')
    monkeypatch.setattr(config, 'settings', replace(Settings(), database_url=dsn, environment='development', email_api_key=''))
    async def forbidden(*args, **kwargs):
        raise AssertionError('Dry run must never claim or send mail')
    monkeypatch.setattr(mail_worker, 'deliver_pending', forbidden)
    assert mail_worker.main(['--dry-run']) == 2
    output = capsys.readouterr().out
    assert json.loads(output)['counts']['mail_overdue'] == 1
    assert 'private@example' not in output and 'private body' not in output
    assert query(dsn, 'select row_to_json(t)::text from toolbako_runtime.email_outbox t') == before
