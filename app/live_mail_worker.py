"""Dedicated live queue wrapper, disabled by default, retaining site monitoring.

No role/grant/credential is created. The existing service remains unchanged until
an operator approves and installs the exact new-role policies and connection.
There is no --all-pending switch and no source-database fallback.
"""
import argparse
import asyncio
from collections.abc import Mapping
import json
import os
from uuid import UUID

from .integrations import EmailIntegration
from .live_mail_preparation import PreparedMailAccess
from .live_mail_runtime import LiveMailBackend, LiveMailRuntimeError
from .mail_worker import deliver_pending
from . import service_monitor


LOGIN_URL = "https://toolbako.onellc-ai.com/login"
OPERATOR_RECIPIENT = "info@onellc-ai.com"
_FORBIDDEN = {"DATABASE_URL", "SESSION_SECRET", "TOOLBAKO_LIVE_RUNTIME_PASSWORD",
              "TOOLBAKO_LIVE_MIGRATION_PASSWORD",
              "TOOLBAKO_LIVE_DATABASE_PASSWORD", "TOOLBAKO_LIVE_DATABASE_URL",
              "SUPABASE_SECRET_KEY", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_ANON_KEY",
              "SUPABASE_PUBLISHABLE_KEY"}


def _require(ok):
    if not ok:
        raise LiveMailRuntimeError()


def _flag(value):
    _require(value in ("false", "true") and type(value) is str)
    return value == "true"


class LiveMailWorkerSettings:
    __slots__ = ("backend", "delivery_enabled", "queue_since", "__api_key")

    def __init__(self, *, environment=None):
        try:
            environment = os.environ if environment is None else environment
            _require(isinstance(environment, Mapping))
            _require(all(type(key) is str and not key.upper().startswith("PG")
                         and not key.upper().startswith("STRIPE_")
                         and (key.upper() not in _FORBIDDEN or not environment[key]) for key in environment))
            _require(environment.get("APP_ENV") in {"staging", "production"}
                     and environment.get("DEMO_MODE") == "false")
            policy = environment.get("LIVE_MAIL_POLICY_SINCE", "0")
            since = environment.get("LIVE_TRANSACTION_EMAILS_SINCE", "0")
            _require(type(policy) is str and policy.isascii() and policy.isdecimal() and since == policy)
            self.queue_since = int(policy)
            PreparedMailAccess(self.queue_since, "0" * 64)
            self.delivery_enabled = _flag(environment.get("LIVE_MAIL_DELIVERY_ENABLED", "false"))
            enqueue_enabled = _flag(environment.get("LIVE_TRANSACTION_EMAILS_ENABLED", "false"))
            _require(not self.delivery_enabled or enqueue_enabled)
            self.__api_key = environment.get("EMAIL_API_KEY", "")
            sender = environment.get("EMAIL_FROM", "")
            _require(type(self.__api_key) is str and bool(self.__api_key)
                     and type(sender) is str and bool(sender))
            self.backend = LiveMailBackend(environment.get("TOOLBAKO_LIVE_MAIL_PASSWORD"),
                queue_since=self.queue_since, environment=environment)
        except Exception:
            raise LiveMailRuntimeError() from None

    def __repr__(self):
        return "LiveMailWorkerSettings(<redacted>)"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("LiveMailWorkerSettings cannot be serialized")

    async def deliver(self, *, message_ids, sender_factory=EmailIntegration):
        """Explicit per-call delivery; None means only RLS-approved NEW queue.

        A call never creates message IDs, rewrites an uncertain row or changes
        the existing Resend 23h/idempotency behavior. Connect rechecks all guards.
        """
        _require(self.delivery_enabled is True)
        async with asyncio.timeout(240):
            return await deliver_pending(self.backend, self.__api_key, limit=20,
                                         message_ids=message_ids, sender_factory=sender_factory)


def selected_ids(values):
    try:
        _require(isinstance(values, (tuple, list)) and 1 <= len(values) <= 100)
        result = []
        for value in values:
            _require(type(value) is str and str(UUID(value)) == value)
            result.append(UUID(value))
        _require(len(set(result)) == len(result))
        return tuple(result)
    except Exception:
        raise LiveMailRuntimeError() from None


async def _delivery_loop(settings, *, ids, continuous, stop):
    while not stop.is_set():
        result = await settings.deliver(message_ids=ids)
        # Fixed keys/counts only; never message IDs, recipients or provider body.
        _require(type(result) is dict and set(result) == {"sent", "retry", "review"}
                 and all(type(value) is int and 0 <= value <= 20 for value in result.values()))
        print(json.dumps({"event": "live_mail_batch", **result, "continuous": continuous}), flush=True)
        if not continuous:
            # Canary finishes but the existing availability monitor stays alive.
            await stop.wait()
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
        except TimeoutError:
            pass


async def run(settings, *, apply=False, message_ids=None, new_queue=False, stop=None):
    """Always retain the existing availability monitor and private alerts.

    Delivery requires BOTH the disabled-by-default environment flag and explicit
    apply/scope arguments. A connection/worker fault fails the service, so the
    existing supervisor can surface it; it never falls back to another DB.
    """
    _require(type(apply) is bool and type(new_queue) is bool)
    _require(apply == bool(message_ids is not None or new_queue))
    _require(not (message_ids is not None and new_queue))
    ids = selected_ids(message_ids) if message_ids is not None else None
    _require(not apply or settings.delivery_enabled is True)
    stop = stop if stop is not None else asyncio.Event()
    tasks = []
    try:
        # Verify once before starting any operator alert or transactional send.
        async with await settings.backend.connect():
            pass
        print(json.dumps({"event": "live_mail_service", "transactional_delivery": apply,
                          "scope": "new_queue" if new_queue else "canary" if apply else "monitor_only"}), flush=True)
        tasks.append(asyncio.create_task(service_monitor.run(LOGIN_URL, OPERATOR_RECIPIENT,
                                                             stop=stop, backend=settings.backend)))
        if apply:
            tasks.append(asyncio.create_task(_delivery_loop(settings, ids=ids, continuous=new_queue, stop=stop)))
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            value = task.result()
            _require(value in (0, None) and stop.is_set())
        return 0
    finally:
        stop.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class _SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise LiveMailRuntimeError()


def main(argv=None):
    try:
        parser = _SafeParser(description="Monitor by default; delivery needs the reviewed live mail role, flag and exact new-message scope")
        parser.add_argument("--apply", action="store_true")
        scope = parser.add_mutually_exclusive_group()
        scope.add_argument("--message-id", action="append", help="Approved new UUID canary; monitoring continues afterward")
        scope.add_argument("--new-queue", action="store_true", help="Continuously deliver only the fixed-policy new queue")
        args = parser.parse_args(argv)
        settings = LiveMailWorkerSettings()
        return asyncio.run(run(settings, apply=args.apply, message_ids=args.message_id, new_queue=args.new_queue))
    except KeyboardInterrupt:
        return 0
    except Exception:
        print("Live mail service stopped: reviewed configuration or operation could not be verified. No credentials or message content were printed.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
