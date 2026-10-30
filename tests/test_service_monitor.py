import asyncio

import httpx
import pytest

from app.service_monitor import AvailabilityAlert, probe, probe_health, check_site, wait_for_next_check


class Sender:
    def __init__(self): self.calls, self.ok = [], True
    async def send(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.ok


def test_threshold_deduplication_recovery_and_private_content():
    async def run():
        sender = Sender()
        alert = AvailabilityAlert(sender, "info@onellc-ai.com")
        for _ in range(2):
            assert not await alert.check(False, exercise=True)
        assert not sender.calls
        assert await alert.check(False, exercise=True)
        for _ in range(5):
            await alert.check(False, exercise=True)
        assert len(sender.calls) == 1
        assert sender.calls[0][0][1] == "【ツールバコ】障害検証用通知"
        assert "実際のサービス停止ではありません" in sender.calls[0][0][2]
        await alert.check(True)
        for _ in range(3): await alert.check(False)
        assert len(sender.calls) == 2
        assert sender.calls[0][1] != sender.calls[1][1]
    asyncio.run(run())


def test_failed_delivery_throttles_and_reuses_idempotency_key():
    async def run():
        now = [0]
        sender = Sender()
        sender.ok = False
        alert = AvailabilityAlert(sender, "info@onellc-ai.com", clock=lambda: now[0])
        for _ in range(4): await alert.check(False)
        assert len(sender.calls) == 1
        now[0] = 900
        sender.ok = True
        assert await alert.check(False)
        assert sender.calls[0][1] == sender.calls[1][1]
    asyncio.run(run())


def test_http_failure_and_redirect_are_unhealthy_without_reading_body():
    async def run():
        for status, expected in ((200, True), (302, False), (503, False)):
            transport = httpx.MockTransport(lambda _: httpx.Response(status, text="private body"))
            async with httpx.AsyncClient(transport=transport) as client:
                assert await probe(client, "https://example.invalid/login") is expected
        def timeout(_): raise httpx.ReadTimeout("secret host")
        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
            assert not await probe(client, "https://example.invalid/login")
    asyncio.run(run())


@pytest.mark.parametrize("failure", [False, True])
def test_queue_observer_exit_stops_service_without_leaking_error(failure, capsys):
    async def run():
        async def observe():
            if failure:
                raise RuntimeError("postgres://private-secret")
            return 1
        observer = asyncio.create_task(observe())
        assert not await wait_for_next_check(asyncio.Event(), observer, interval=1)
        await asyncio.gather(observer, return_exceptions=True)
    asyncio.run(run())
    output = capsys.readouterr().out
    assert 'mail_observer_unavailable' in output
    assert 'private-secret' not in output


def test_queue_observer_keeps_running_until_normal_stop():
    async def run():
        stop = asyncio.Event()
        observer = asyncio.create_task(stop.wait())
        assert await wait_for_next_check(stop, observer, interval=0.001)
        assert not observer.done()
        stop.set()
        assert await wait_for_next_check(stop, observer, interval=1)
        await observer
    asyncio.run(run())


@pytest.mark.parametrize("body,expected", [
    ('{"status":"ok"}', True), ('{"status":"error"}', False),
    ('<html>upstream unavailable</html>', False), ('[]', False),
    ('{"status":"ok","padding":"' + 'x'*1100 + '"}', False),
])
def test_json_probe_rejects_false_positive_and_oversized_bodies(body, expected):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda _: httpx.Response(200, text=body))) as client:
            assert await probe_health(client, 'https://example.invalid/healthz') is expected
    asyncio.run(run())


@pytest.mark.parametrize('broken', [None, '/login', '/healthz', '/deploymentz'])
def test_site_requires_page_and_both_health_checks(broken):
    async def run():
        calls = []
        def respond(request):
            calls.append(request.url.path)
            return httpx.Response(503 if request.url.path == broken else 200,
                                  json={'status':'ok'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            assert await check_site(client, 'https://toolbako.onellc-ai.com/login') is (broken is None)
        assert sorted(calls) == ['/deploymentz', '/healthz', '/login']
    asyncio.run(run())
