import asyncio

import httpx

from app.service_monitor import AvailabilityAlert, probe


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
