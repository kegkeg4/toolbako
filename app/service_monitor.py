"""Independent, bounded availability checks with private operator alerts.

No marketplace outbox is claimed or sent. The HTTP response body is discarded.
Run on the existing separate monitor service, not in the web request process.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import signal
import time
from uuid import uuid4

import httpx

from .integrations import EmailIntegration


class AvailabilityAlert:
    def __init__(self, sender, recipient, *, threshold=3, clock=time.monotonic):
        self.sender, self.recipient = sender, recipient
        self.threshold, self.clock = threshold, clock
        self.failures = 0
        self.incident = None
        self.delivered = False
        self.next_attempt = 0

    async def check(self, healthy, *, exercise=False):
        self.failures = 0 if healthy else self.failures + 1
        if self.failures < self.threshold:
            if healthy:
                self.incident, self.delivered, self.next_attempt = None, False, 0
            return False
        if self.incident is None:
            self.incident = uuid4().hex
        if self.delivered or self.clock() < self.next_attempt:
            return False
        self.next_attempt = self.clock() + 900
        subject = "【ツールバコ】障害検証用通知" if exercise else "【ツールバコ】サイト応答異常"
        text = ("これは障害検知から通知までの接続確認です。実際のサービス停止ではありません。\n"
                if exercise else "サイトの正常応答を3回連続で確認できませんでした。Railwayの稼働状態とDB接続を確認してください。\n")
        text += "個人情報・取引情報・秘密値は含んでいません。\n確認時刻: " + datetime.now(timezone.utc).isoformat()
        try:
            self.delivered = await self.sender.send(
                self.recipient, subject, text,
                idempotency_key="availability-" + self.incident,
            )
        except Exception:
            self.delivered = False
        print(json.dumps({"event": "availability_alert", "accepted": self.delivered,
                          "exercise": exercise}), flush=True)
        return self.delivered


async def probe(client, url):
    try:
        # Stream headers only; no session, page body, redirect or login access.
        async with client.stream("GET", url) as response:
            return response.status_code == 200
    except (httpx.HTTPError, TimeoutError):
        return False


async def run(url, recipient, *, stop=None):
    from .config import settings
    from .database import PostgresStateStore
    from .mail_monitor import observe
    stop = stop if stop is not None else asyncio.Event()
    loop = asyncio.get_running_loop()
    previous = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.signal(signum, lambda *_: loop.call_soon_threadsafe(stop.set))
    observer = asyncio.create_task(observe(
        PostgresStateStore(settings.database_url, production=settings.is_deployed),
        watch=True, interval=60, stop=stop,
    ))
    alert = AvailabilityAlert(EmailIntegration(settings.email_api_key, settings.email_from), recipient)
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False, headers={
            "User-Agent": "Toolbako-availability-monitor/1",
        }) as client:
            while not stop.is_set():
                healthy = await probe(client, url)
                await alert.check(healthy)
                print(json.dumps({"event": "availability_observation", "healthy": healthy,
                                  "consecutive_failures": alert.failures,
                                  "checked_at": datetime.now(timezone.utc).isoformat()}), flush=True)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=60)
                except TimeoutError:
                    pass
    finally:
        observer.cancel()
        await asyncio.gather(observer, return_exceptions=True)
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--recipient", required=True)
    args = parser.parse_args()
    # This operational command is deliberately scoped to the approved target.
    if args.url != "https://toolbako.onellc-ai.com/login" or args.recipient != "info@onellc-ai.com":
        parser.error("Unapproved availability target or recipient")
    from .config import settings
    if not settings.email_ready:
        parser.error("Operator alert email is not configured")
    asyncio.run(run(args.url, args.recipient))


if __name__ == "__main__":
    main()
