"""Bounded handshake overlap, isolation and cancellation using artificial data."""
import asyncio

import pytest

import app.database as db
from app.data import DemoStore
from test_request_failure_recovery import Connection


def test_next_handshake_overlaps_handler_but_sql_and_state_remain_serial(monkeypatch):
    async def run():
        store = DemoStore(seed=False)
        backend = db.PostgresStateStore("unused")
        first_entered, second_connected, third_queued = (asyncio.Event() for _ in range(3))
        release_first = asyncio.Event()
        connections, queries, entered = [], [], []
        original_acquire = backend._connection_slots.acquire
        arrivals = 0

        async def acquire():
            nonlocal arrivals
            arrivals += 1
            if arrivals == 3:
                third_queued.set()
            return await original_acquire()

        async def connect():
            conn = Connection(DemoStore(seed=False))
            number = len(connections)
            original_execute = conn.execute

            async def execute(sql, *args):
                queries.append(number)
                assert backend._local_lock.locked()
                return await original_execute(sql, *args)

            conn.execute = execute
            connections.append(conn)
            assert sum(not c.closed for c in connections) <= 2
            if number == 1:
                second_connected.set()
            return conn

        monkeypatch.setattr(backend._connection_slots, "acquire", acquire)
        monkeypatch.setattr(backend, "connect", connect)

        async def request(number):
            async with backend.request(store):
                entered.append(number)
                if number == 0:
                    store.notifications.append({"id": "uncommitted"})
                    first_entered.set()
                    await release_first.wait()
                else:
                    assert store.notifications == []

        first = asyncio.create_task(request(0))
        await asyncio.wait_for(first_entered.wait(), 1)
        second = asyncio.create_task(request(1))
        await asyncio.wait_for(second_connected.wait(), 1)
        third = asyncio.create_task(request(2))
        await asyncio.wait_for(third_queued.wait(), 1)
        assert entered == [0] and queries == [0, 0]
        assert len(connections) == 2
        assert backend._connection is connections[0]
        assert store.notifications == [{"id": "uncommitted"}]
        release_first.set()
        await asyncio.wait_for(asyncio.gather(first, second, third), 1)
        assert entered == [0, 1, 2]
        assert queries == [0, 0, 0, 1, 1, 1, 2, 2, 2]
        assert all(c.closed for c in connections)
        assert backend._connection_slots._value == 2
        assert not backend._local_lock.locked()

    asyncio.run(run())


@pytest.mark.parametrize("stage", ["slot", "connect", "local_lock", "handler"])
def test_cancelled_waiter_releases_only_its_own_resources(monkeypatch, stage):
    async def run():
        backend = db.PostgresStateStore("unused")
        store = DemoStore(seed=False)
        conn = Connection(store)
        reached = asyncio.Event()
        wait_forever = asyncio.Event()
        connect_calls = []
        owner_connection = object()
        if stage in {"slot", "local_lock"}:
            await backend._local_lock.acquire()
            backend._connection = owner_connection
        if stage == "slot":
            await backend._connection_slots.acquire()
            await backend._connection_slots.acquire()
            original_acquire = backend._connection_slots.acquire

            async def acquire():
                reached.set()
                return await original_acquire()

            monkeypatch.setattr(backend._connection_slots, "acquire", acquire)

        async def connect():
            connect_calls.append(True)
            if stage in {"connect", "local_lock"}:
                reached.set()
            if stage == "connect":
                await wait_forever.wait()
            return conn

        monkeypatch.setattr(backend, "connect", connect)

        async def request():
            async with backend.request(store):
                assert stage == "handler"
                store.notifications.append({"id": "not-saved"})
                reached.set()
                await wait_forever.wait()

        task = asyncio.create_task(request())
        await asyncio.wait_for(reached.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(connect_calls) == (0 if stage == "slot" else 1)
        if stage in {"slot", "local_lock"}:
            assert backend._local_lock.locked()
            assert backend._connection is owner_connection
            backend._local_lock.release()
        else:
            assert not backend._local_lock.locked()
            assert backend._connection is None
        if stage == "slot":
            assert backend._connection_slots._value == 0
            backend._connection_slots.release()
            backend._connection_slots.release()
        assert backend._connection_slots._value == 2
        assert conn.closed == (stage in {"local_lock", "handler"})
        assert store.notifications == []

    asyncio.run(run())


def test_slot_timeout_does_not_connect_or_release_occupied_slots(monkeypatch):
    async def run():
        backend = db.PostgresStateStore("unused")
        await backend._connection_slots.acquire()
        await backend._connection_slots.acquire()

        async def timeout(coro, timeout):
            coro.close()
            raise TimeoutError("private connection information")

        async def connect():
            pytest.fail("must not open an unbounded connection")

        monkeypatch.setattr(db.asyncio, "wait_for", timeout)
        monkeypatch.setattr(backend, "connect", connect)
        with pytest.raises(db.StorageUnavailable) as error:
            async with backend.request(DemoStore(seed=False)):
                pytest.fail("must not enter")
        assert error.value.diagnostic_code == "connection_capacity_timeout"
        assert error.value.diagnostic_phase == "connection_slot"
        assert "private" not in str(error.value)
        assert backend._connection_slots._value == 0
        backend._connection_slots.release()
        backend._connection_slots.release()

    asyncio.run(run())


def test_two_queues_share_five_seconds_excluding_connection_handshake(monkeypatch):
    async def run():
        backend = db.PostgresStateStore("unused")
        store = DemoStore(seed=False)
        conn = Connection(store)
        clock, timeouts = [0.0], []
        original_wait = asyncio.wait_for
        monkeypatch.setattr(db, "_request_clock", lambda: clock[0])

        async def wait(coro, timeout):
            timeouts.append(timeout)
            result = await original_wait(coro, timeout)
            if len(timeouts) == 1:
                clock[0] += 3.0
            return result

        async def connect():
            clock[0] += 4.0
            return conn

        monkeypatch.setattr(db.asyncio, "wait_for", wait)
        monkeypatch.setattr(backend, "connect", connect)
        async with backend.request(store) as timing:
            assert timeouts == [5, 2]
        assert timing.durations["connection_slot"] == 3
        assert timing.durations["connect"] == 4
        assert conn.closed and backend._connection_slots._value == 2

    asyncio.run(run())
