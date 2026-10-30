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


async def probe_health(client, url):
    """Bounded public JSON check; a proxy's HTTP 200 error page is unhealthy."""
    try:
        async with client.stream("GET", url) as response:
            if response.status_code != 200:
                return False
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 1024:
                    return False
            payload = json.loads(body)
            return isinstance(payload, dict) and payload.get("status") == "ok"
    except (httpx.HTTPError, TimeoutError, ValueError):
        return False


async def check_site(client, login_url):
    # Both infrastructure health and the ordinary DB-backed login page must
    # work. These requests contain no account/session or business data.
    root = "https://toolbako.onellc-ai.com"
    try:
        async with asyncio.timeout(12):
            results = await asyncio.gather(probe(client, login_url),
                probe_health(client, root + "/healthz"),
                probe_health(client, root + "/deploymentz"))
        return all(results)
    except TimeoutError:
        return False


async def wait_for_next_check(stop, observer, *, interval=60):
    """A dead queue observer must fail the service so Railway can restart it."""
    stopping = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait({stopping, observer}, timeout=interval,
                           return_when=asyncio.FIRST_COMPLETED)
        if not stop.is_set() and observer.done():
            # Do not surface exception details: DB errors can contain secrets.
            print(json.dumps({"event": "mail_observer_unavailable", "level": "error"}), flush=True)
            return False
        return True
    finally:
        stopping.cancel()
        await asyncio.gather(stopping, return_exceptions=True)


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
                started = time.monotonic()
                healthy = await check_site(client, url)
                await alert.check(healthy)
                print(json.dumps({"event": "availability_observation", "healthy": healthy,
                                  "consecutive_failures": alert.failures,
                                  "checked_at": datetime.now(timezone.utc).isoformat()}), flush=True)
                if not await wait_for_next_check(stop, observer,
                        interval=max(0, 60 - (time.monotonic() - started))):
                    return 1
        return 0
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
    return asyncio.run(run(args.url, args.recipient))


if __name__ == "__main__":
    raise SystemExit(main())
