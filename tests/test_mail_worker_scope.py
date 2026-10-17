"""Real queue isolation and least-privilege tests, using only a disposable DB."""
import asyncio
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from app import config, mail_worker
from app.config import Settings
from app.operations import inspect_mail
from test_database import database, query


def enqueue(dsn, *, status='pending'):
    return query(dsn,
        "insert into toolbako_runtime.email_outbox(id,recipient,sender,subject,body,status) "
        "values (gen_random_uuid(),'synthetic@example.invalid','sender@example.invalid',"
        "'Synthetic','Never actually sent',%s) returning id", (status,))[0][0]


def rows(dsn):
    return query(dsn, 'select id,row_to_json(t)::text from toolbako_runtime.email_outbox t order by id')


class FakeSender:
    def __init__(self, *args):
        pass

    async def send(self, *args, **kwargs):
        return True


def test_approved_message_only_preserves_pending_and_expired_unapproved_rows(database):
    backend, dsn = database
    enqueue(dsn)
    expired = enqueue(dsn, status='sending')
    query(dsn, "update toolbako_runtime.email_outbox set lease_until=now()-interval '1 minute' where id=%s returning id", (expired,))
    before = rows(dsn)
    approved = enqueue(dsn)
    result = asyncio.run(mail_worker.deliver_pending(backend, 'mock', message_ids=[approved], sender_factory=FakeSender))
    assert result == {'sent': 1, 'retry': 0, 'review': 0}
    assert [row for row in rows(dsn) if row[0] != approved] == before
    assert query(dsn, 'select status,attempts from toolbako_runtime.email_outbox where id=%s', (approved,)) == [('sent', 1)]
    # Re-running the approved ID must neither resend it nor fall back to others.
    assert asyncio.run(mail_worker.deliver_pending(backend, 'mock', message_ids=[approved], sender_factory=FakeSender))['sent'] == 0
    assert [row for row in rows(dsn) if row[0] != approved] == before


def test_nonexistent_scope_does_not_claim_any_pending_mail(database):
    backend, dsn = database
    enqueue(dsn)
    before = rows(dsn)
    assert asyncio.run(mail_worker.deliver_pending(backend, 'mock', message_ids=[uuid4()], sender_factory=FakeSender)) == {'sent': 0, 'retry': 0, 'review': 0}
    assert rows(dsn) == before


@pytest.mark.parametrize('scope', [[], ['invalid'], 'not-a-list', [uuid4()] * 101])
def test_invalid_scope_rejected_before_database_connect(scope):
    class NoDatabase:
        async def connect(self):
            pytest.fail('Invalid scope must not touch DB')
    with pytest.raises(ValueError):
        asyncio.run(mail_worker.deliver_pending(NoDatabase(), 'mock', message_ids=scope))


@pytest.mark.parametrize('args', [[], ['--limit', '1'], ['--dry-run']])
def test_cli_defaults_to_read_only_even_with_api_key(database, monkeypatch, capsys, args):
    _, dsn = database
    enqueue(dsn)
    before = rows(dsn)
    monkeypatch.setattr(config, 'settings', replace(Settings(), environment='development', database_url=dsn, email_api_key='mock'))
    async def forbidden(*args, **kwargs):
        pytest.fail('No explicit --apply: sending is forbidden')
    monkeypatch.setattr(mail_worker, 'deliver_pending', forbidden)
    assert mail_worker.main(args) == 0
    assert '"dry_run": true' in capsys.readouterr().out
    assert rows(dsn) == before


@pytest.mark.parametrize('args', [
    ['--apply'], ['--all-pending'], ['--message-id', str(uuid4())],
    ['--apply', '--message-id', 'private-mistaken-secret'],
    ['--apply', '--all-pending', '--message-id', str(uuid4())],
    ['--apply', '--dry-run', '--all-pending'], ['--limit', '0'],
])
def test_cli_requires_unambiguous_scope_before_network(args, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail('Invalid CLI must not initialize DB')
    monkeypatch.setattr(mail_worker, 'PostgresStateStore', forbidden)
    with pytest.raises(SystemExit) as error:
        mail_worker.main(args)
    assert error.value.code == 2
    assert 'private-mistaken-secret' not in capsys.readouterr().err


@pytest.fixture
def restricted_worker(database):
    backend, dsn = database
    script = (Path(__file__).resolve().parents[1] / 'deploy/mail-worker/grants.sql').read_text(encoding='utf-8')
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(script)
    class RestrictedBackend:
        _check_schema = backend._check_schema
        async def connect(self):
            conn = await backend.connect()
            await conn.execute('set role toolbako_mail_worker')
            return conn
    try:
        yield RestrictedBackend(), dsn
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            # Only the three policies/role created in this disposable test DB.
            conn.execute('drop policy mail_worker_schema_read on toolbako_runtime.schema_version')
            conn.execute('drop policy mail_worker_queue_read on toolbako_runtime.email_outbox')
            conn.execute('drop policy mail_worker_queue_progress on toolbako_runtime.email_outbox')
            conn.execute('drop owned by toolbako_mail_worker')
            conn.execute('drop role toolbako_mail_worker')


def test_restricted_worker_can_inspect_and_deliver_without_financial_privileges(restricted_worker):
    backend, dsn = restricted_worker
    approved = enqueue(dsn)
    async def run():
        assert (await inspect_mail(backend))['counts']['mail_pending'] == 1
        result = await mail_worker.deliver_pending(backend, 'mock', message_ids=[approved], sender_factory=FakeSender)
        assert result['sent'] == 1
        async with await backend.connect() as conn:
            for sql in [
                'select * from toolbako_runtime.marketplace_state',
                'select * from toolbako_runtime.stripe_operations',
                'select * from toolbako_runtime.finance_payouts',
                "update toolbako_runtime.email_outbox set recipient='wrong@example.invalid'",
                "update toolbako_runtime.email_outbox set subject='changed',body='changed'",
                'delete from toolbako_runtime.email_outbox',
                'truncate toolbako_runtime.email_outbox',
                "insert into toolbako_runtime.email_outbox(id,recipient,sender,subject,body) values (gen_random_uuid(),'a','b','c','d')",
                'update toolbako_runtime.schema_version set version=3',
                'create table toolbako_runtime.forbidden(id int)',
            ]:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    await conn.execute(sql)
            assert (await conn.execute("update toolbako_runtime.email_outbox set status='pending' where status='sent' returning id")).rowcount == 0
    asyncio.run(run())
    assert query(dsn, 'select status from toolbako_runtime.email_outbox') == [('sent',)]


def test_mail_inspection_enforces_read_only_with_restricted_worker(restricted_worker, monkeypatch):
    backend, _ = restricted_worker
    async def attempt_write(conn):
        await conn.execute("update toolbako_runtime.email_outbox set status='pending'")
    monkeypatch.setattr(backend, '_check_schema', attempt_write)
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        asyncio.run(inspect_mail(backend))
