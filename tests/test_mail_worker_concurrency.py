import asyncio

from app.mail_worker import deliver_pending
from test_database import database, query


def enqueue(dsn, count=1):
    for _ in range(count):
        query(dsn, "insert into toolbako_runtime.email_outbox(id,recipient,sender,subject,body) values (gen_random_uuid(),'synthetic@example.invalid','sender@example.invalid','Synthetic','Never actually sent') returning id")


def test_concurrent_workers_claim_different_messages(database):
    backend, dsn = database
    enqueue(dsn, 2)
    async def run():
        entered = asyncio.Event()
        keys = []
        class Sender:
            def __init__(self, *args):
                pass
            async def send(self, *args, idempotency_key):
                keys.append(idempotency_key)
                if len(keys) == 2:
                    entered.set()
                await asyncio.wait_for(entered.wait(), 5)
                return True
        results = await asyncio.wait_for(asyncio.gather(*[
            deliver_pending(backend, 'mock', limit=1, sender_factory=Sender) for _ in range(2)
        ]), 10)
        assert sum(result['sent'] for result in results) == 2
        assert len(set(keys)) == 2
    asyncio.run(run())
    assert query(dsn, "select count(*) from toolbako_runtime.email_outbox where status='sent' and attempts=1") == [(2,)]


def test_expired_worker_cannot_overwrite_new_claim_result(database):
    backend, dsn = database
    enqueue(dsn)
    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()
        keys = []
        class SlowSender:
            def __init__(self, *args):
                pass
            async def send(self, *args, idempotency_key):
                keys.append(idempotency_key)
                entered.set()
                await release.wait()
                return False
        class RecoverySender:
            def __init__(self, *args):
                pass
            async def send(self, *args, idempotency_key):
                keys.append(idempotency_key)
                return True
        old = asyncio.create_task(deliver_pending(backend, 'mock', limit=1, sender_factory=SlowSender))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            query(dsn, "update toolbako_runtime.email_outbox set lease_until=now()-interval '1 minute' returning id")
            current = await deliver_pending(backend, 'mock', limit=1, sender_factory=RecoverySender)
            assert current['sent'] == 1
        finally:
            release.set()
            await asyncio.wait_for(old, 5)
        assert keys[0] == keys[1]
    asyncio.run(run())
    assert query(dsn, 'select status,attempts from toolbako_runtime.email_outbox') == [('sent', 2)]
