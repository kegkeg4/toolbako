from __future__ import annotations

from .identity import IDENTITY_STATES, apply_identity_event

import asyncio
import hashlib
import hmac
import json
import re
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from fastapi import HTTPException, Request
from starlette.responses import JSONResponse
from .finance import FinanceConflict
from .payout_worker import apply_bank_status, verify_bank_payout
from .payment_state import apply_checkout_payment, apply_checkout_expiry, apply_refund_payment
from .request_limits import WEBHOOK_BODY_LIMIT
from .subscription_state import apply_invoice, TERMINAL


EmailSender = Callable[[str | None, str, str], Awaitable[bool]]
EmailMessage = tuple[str | None, str, str]
CONNECT_EVENTS = frozenset({'account.updated', 'payout.paid', 'payout.failed', 'payout.canceled', 'payout.updated', 'payout.created'})
PLATFORM_EVENTS = frozenset({'checkout.session.completed', 'checkout.session.async_payment_succeeded', 'checkout.session.expired',
    'refund.updated', 'charge.refunded', 'charge.dispute.created', 'invoice.paid', 'invoice.payment_failed',
    'customer.subscription.updated', 'customer.subscription.deleted'})


class StripeWebhookValidator:
    """Validate transport data without database or marketplace access."""

    def __init__(self, settings, *, durable=False):
        self.settings = settings
        self.durable = durable

    def verify_signature(self, payload: bytes, signature: str, *, connect: bool = False) -> bool:
        secret = self.settings.stripe_connect_webhook_secret if connect else self.settings.stripe_webhook_secret
        if not secret or len(signature) > 4096:
            return False
        parts: dict[str, list[str]] = {}
        for part in signature.split(","):
            if "=" in part:
                key, value = part.split("=", 1)
                parts.setdefault(key.strip(), []).append(value.strip())
        timestamps = parts.get('t', [])
        if len(timestamps) != 1 or not re.fullmatch(r'[0-9]{1,12}', timestamps[0]):
            return False
        timestamp = int(timestamps[0])
        if abs(int(time.time()) - timestamp) > 300:
            return False
        signed = f"{timestamp}.".encode() + payload
        expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        return any(re.fullmatch(r'[0-9a-f]{64}', supplied) and hmac.compare_digest(expected, supplied)
                   for supplied in parts.get('v1', []))

    def validate_event(self, payload: bytes, signature: str):
        try:
            event = json.loads(payload)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise HTTPException(400, "invalid payload") from exc
        if not isinstance(event, dict) or not isinstance(event.get("data"), dict) or not isinstance(event["data"].get("object"), dict):
            raise HTTPException(400, "invalid payload")
        event_id = event.get('id')
        event_type = event.get('type')
        if not isinstance(event_id, str) or not 1 <= len(event_id) <= 255:
            raise HTTPException(400, "missing event id")
        if not isinstance(event_type, str) or not re.fullmatch(r'[a-z][a-z0-9_.]{0,119}', event_type):
            raise HTTPException(400, 'invalid event type')
        connected = 'account' in event
        if connected and (not isinstance(event['account'], str) or not re.fullmatch(r'acct_[A-Za-z0-9_]+', event['account'])):
            raise HTTPException(400, 'invalid connected account')
        if not self.verify_signature(payload, signature, connect=connected):
            raise HTTPException(400, 'invalid signature')
        # Platform charges/Identity are never created as a connected account.
        # Matching metadata alone must not cross that ownership boundary.
        if ((event_type in CONNECT_EVENTS and not connected)
                or (connected and (event_type in PLATFORM_EVENTS or event_type.startswith('identity.verification_session.')))):
            raise HTTPException(400, 'event account scope does not match')
        if self.durable or self.settings.is_deployed or self.settings.stripe_secret_key or 'livemode' in event:
            expected_live = self.settings.stripe_secret_key.startswith(("sk_live_", "rk_live_"))
            if type(event.get("livemode")) is not bool or event["livemode"] != expected_live:
                raise HTTPException(400, "event environment does not match")

        return event


class StripeWebhookGuardMiddleware:
    """Reject invalid Stripe messages before opening a marketplace DB request.

    Installed inside the upload-size/time limiter and outside the DB boundary.
    The route revalidates the same raw bytes after acquiring the DB lock.
    """

    def __init__(self, app, *, settings):
        self.app = app
        self.validator = StripeWebhookValidator(settings, durable=bool(settings.database_url))

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['path'] != '/webhooks/stripe' or scope['method'] != 'POST':
            return await self.app(scope, receive, send)
        try:
            signatures = [value for key, value in scope['headers'] if key.lower() == b'stripe-signature']
            if len(signatures) != 1:
                raise HTTPException(400, 'invalid signature')
            payload = bytearray()
            async for chunk in Request(scope, receive).stream():
                if len(payload) + len(chunk) > WEBHOOK_BODY_LIMIT:
                    raise HTTPException(413, 'webhook payload too large')
                payload.extend(chunk)
            payload = bytes(payload)
            self.validator.validate_event(payload, signatures[0].decode('latin-1'))
        except HTTPException as exc:
            response = JSONResponse({'detail': exc.detail}, status_code=exc.status_code,
                                    headers={'Cache-Control': 'no-store'})
            return await response(scope, receive, send)
        replayed = False

        async def replay():
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {'type': 'http.request', 'body': payload, 'more_body': False}

        await self.app(scope, replay, send)


class StripeWebhookService(StripeWebhookValidator):
    """Validate and apply Stripe events atomically within one app process."""

    def __init__(self, settings: Any, store: Any, email_sender: EmailSender, *, journal=None, provider=None):
        super().__init__(settings, durable=bool(journal))
        self.store = store
        self.email_sender = email_sender
        self.journal = journal
        self.provider = provider

    async def handle(self, request: Request) -> dict[str, bool]:
        payload = bytearray()
        async for chunk in request.stream():
            if len(payload) + len(chunk) > WEBHOOK_BODY_LIMIT:
                raise HTTPException(413, "webhook payload too large")
            payload.extend(chunk)
        event = self.validate_event(bytes(payload), request.headers.get('stripe-signature', ''))
        event_id = event['id']
        # A committed event must be acknowledged even if its related state was
        # removed or changed since processing. Signature/scope/mode still apply.
        if event_id in self.store.processed_webhook_events:
            return {"received": True, "duplicate": True}
        state = self._resolve(event)
        await self._refresh_provider_state(event, state)
        payout = state.get("payout")
        if self.journal and payout and not payout.get("provider_payout_id"):
            attempt = await self.journal.operation_result(f"bank-payout-{payout['id']}")
            if attempt and attempt["status"] == "succeeded":
                state["recovered_payout_id"] = attempt["response"].get("id")
        if self.journal and state["event_type"] == "refund.updated" and state.get("order"):
            order, extra = state["order"], state.get("extra")
            refund_target = extra or order
            if not refund_target.get("refund_reference"):
                key = f"refund-extra-{order['id']}-{extra['id']}" if extra else f"refund-{order['id']}"
                attempt = await self.journal.operation_result(key)
                if attempt and attempt["status"] == "succeeded":
                    state["recovered_refund_id"] = attempt["response"].get("id")
        target = state.get("extra") or state.get("order")
        if self.journal and target and not target.get("checkout_session_id") and (state["paid_checkout_event"] or state["event_type"] == "checkout.session.expired"):
            key = f"extra-{target['id']}" if state.get("extra") else f"checkout-{target['id']}"
            attempt = await self.journal.operation_result(key)
            if attempt and attempt["status"] == "succeeded":
                # Recover only from a durably recorded provider response, not
                # from unvalidated event metadata or an unknown API outcome.
                state["recovered_checkout_session_id"] = attempt["response"].get("id")

        request_id = request.headers.get("x-request-id", "")
        with self.store._lock:
            if event_id in self.store.processed_webhook_events:
                return {"received": True, "duplicate": True}
            self._validate(event_id, event, state, request_id)
            mutable_objects = [item for item in (state.get("order"), state.get("subscription"), state.get("extra"), state.get("application"), state.get("payout"), state.get("account")) if isinstance(item, dict)]
            if state.get("order"):
                tool = self.store.get(state["order"].get("tool_slug", ""))
                if tool: mutable_objects.append(tool)
            object_snapshots = [(item, deepcopy(item)) for item in mutable_objects]
            notification_snapshot = deepcopy(self.store.notifications)
            try:
                if state.get("recovered_checkout_session_id"):
                    target["checkout_session_id"] = state["recovered_checkout_session_id"]
                if state.get("recovered_payout_id"):
                    payout["provider_payout_id"] = state["recovered_payout_id"]
                    payout["status"] = "bank_pending"
                if state.get("recovered_refund_id"):
                    (state.get("extra") or state["order"])["refund_reference"] = state["recovered_refund_id"]
                messages = self._apply(event, state)
            except Exception:
                for item, snapshot in object_snapshots:
                    item.clear(); item.update(snapshot)
                self.store.notifications[:] = notification_snapshot
                raise
            # Claim only after all state changes succeeded. The process lock
            # prevents another worker in this instance from applying in-between.
            if not self.store.claim_webhook_event(event_id):
                for item, snapshot in object_snapshots:
                    item.clear(); item.update(snapshot)
                self.store.notifications[:] = notification_snapshot
                return {"received": True, "duplicate": True}
            self.store.audit(None, "webhook.processed", f"{state['event_type']}:{event_id}", request_id)

        if messages:
            await asyncio.gather(*(self.email_sender(*message) for message in messages))
        return {"received": True}

    async def _refresh_provider_state(self, event, state):
        kind, obj = state["event_type"], state["obj"]
        if kind == "account.updated":
            state["account"] = next((a for a in self.store.connected_accounts.values() if a.get("account_id") == obj.get("id")), None)
            if not state["account"]: return
            path = "accounts"
        elif kind in {"customer.subscription.updated", "customer.subscription.deleted"}:
            if not any(s.get("provider_subscription_id") == obj.get("id") for s in self.store.subscriptions): return
            path = "subscriptions"
        else:
            return
        # Event.created has one-second resolution and is not an ordering key.
        # Mutable account permissions and subscription states require a fresh
        # provider read in any deployed/durable environment.
        if not (self.settings.is_deployed or self.journal): return
        reference = obj.get("id")
        prefix = "acct" if path == "accounts" else "sub"
        if not isinstance(reference, str) or not re.fullmatch(prefix + r"_[A-Za-z0-9_]+", reference):
            raise HTTPException(400, "invalid provider reference")
        if not self.provider: raise HTTPException(503, "provider reconciliation unavailable")
        try:
            current = await self.provider._get(f"{path}/{reference}")
        except RuntimeError:
            raise HTTPException(503, "provider reconciliation unavailable") from None
        if (current.get("id") != reference or current.get("object") != ("account" if path == "accounts" else "subscription")
                or (path == "subscriptions" and current.get("livemode") is not event.get("livemode"))):
            raise HTTPException(503, "provider object does not match")
        state["obj"] = current

    def _resolve(self, event: dict[str, Any]) -> dict[str, Any]:
        obj = event.get("data", {}).get("object", {})
        metadata = obj.get('metadata') if obj.get('metadata') is not None else {}
        if not isinstance(metadata, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in metadata.items()):
            raise HTTPException(400, "invalid metadata")
        event_type = event['type']
        paid_checkout_event = event_type in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}
        order = next((item for item in self.store.orders if item["id"] == metadata.get("order_id")), None) if metadata.get("order_id") else None
        payment_event = event_type in {"charge.refunded", "refund.updated", "charge.dispute.created"}
        if not order and payment_event and obj.get("payment_intent"):
            order = next((item for item in self.store.orders if item.get("payment_reference") == obj["payment_intent"] or any(x.get("payment_reference") == obj["payment_intent"] for x in item.get("pending_extras", []))), None)
        parent = obj.get('parent') or {}
        if not isinstance(parent, dict) or not isinstance(parent.get('subscription_details') or {}, dict):
            raise HTTPException(400, 'invalid subscription parent')
        provider_subscription_id = obj.get('subscription') or (parent.get('subscription_details') or {}).get('subscription')
        subscription = next((item for item in self.store.subscriptions if item.get("provider_subscription_id") == provider_subscription_id), None) if provider_subscription_id else None
        if not subscription and metadata.get("order_id"):
            subscription = next((item for item in self.store.subscriptions if item.get("order_id") == metadata.get("order_id")), None)
        if not order and subscription:
            order = next((item for item in self.store.orders if item["id"] == subscription.get("order_id")), None)
        extra = order and next((item for item in order.get("pending_extras", []) if item["id"] == metadata.get("extra_id")), None) if metadata.get("extra_id") else None
        if not extra and order and payment_event and obj.get("payment_intent"):
            extra = next((x for x in order.get("pending_extras", []) if x.get("payment_reference") == obj["payment_intent"]), None)
        application = self.store.identity_applications.get(metadata.get("user_id")) if metadata.get("user_id") else None
        payout = next((p for p in self.store.payouts if p.get("finance_v2") and (p["id"] == metadata.get("payout_id") or p.get("provider_payout_id") == obj.get("id"))), None) if event_type in CONNECT_EVENTS and event_type.startswith('payout.') else None
        return {"obj": obj, "metadata": metadata, "event_type": event_type, "paid_checkout_event": paid_checkout_event, "order": order, "provider_subscription_id": provider_subscription_id, "subscription": subscription, "extra": extra, "application": application, "payout": payout}

    def _validate(self, event_id: str, event: dict[str, Any], state: dict[str, Any], request_id: str) -> None:
        obj = state["obj"]
        metadata = state["metadata"]
        event_type = state["event_type"]
        order = state["order"]
        subscription = state["subscription"]
        extra = state["extra"]
        application = state["application"]
        provider_subscription_id = state["provider_subscription_id"]

        if event_type in {"payout.paid", "payout.failed", "payout.canceled", "payout.updated", "payout.created"}:
            payout = state["payout"]
            if not payout or not event.get("account") or event["account"] != payout["account_id"]:
                raise HTTPException(400, "payout account does not match")
            expected_status = {"payout.paid":"paid", "payout.failed":"failed", "payout.canceled":"canceled"}.get(event_type)
            if expected_status and obj.get("status") != expected_status:
                raise HTTPException(400, "payout status does not match event")
            try:
                verify_bank_payout({**payout, "provider_payout_id": state.get("recovered_payout_id") or payout.get("provider_payout_id")}, obj)
            except FinanceConflict:
                raise HTTPException(400, "payout does not match reservation") from None
        elif state["paid_checkout_event"]:
            if obj.get("customer_details") is not None and not isinstance(obj.get("customer_details"), dict):
                raise HTTPException(400, "invalid customer details")
            expected_amount = extra["amount"] if extra else (order and order.get("primary_payment_amount", order["amount"]))
            expected_session = state.get("recovered_checkout_session_id") or (extra.get("checkout_session_id") if extra else (order and order.get("checkout_session_id")))
            mismatch = not order or (metadata.get("extra_id") and not extra) or obj.get("payment_status") != "paid" or obj.get("id") != expected_session or obj.get("currency", "").lower() != "jpy" or obj.get("amount_total") != expected_amount
            previous_reference = extra.get("payment_reference") if extra else (order and order.get("payment_reference"))
            mismatch = mismatch or bool(previous_reference and obj.get("payment_intent") != previous_reference)
            if mismatch:
                self.store.audit(None, "webhook.rejected_mismatch", f"{event_type}:{event_id}", request_id)
                raise HTTPException(400, "checkout does not match order")
        elif event_type == "checkout.session.expired":
            expected_session = state.get("recovered_checkout_session_id") or (extra.get("checkout_session_id") if extra else (order and order.get("checkout_session_id")))
            if not order or (metadata.get("extra_id") and not extra) or obj.get("id") != expected_session:
                raise HTTPException(400, "checkout does not match order")
        elif event_type.startswith("identity.verification_session."):
            if not application or obj.get("id") != application.get("provider_reference"):
                raise HTTPException(400, "identity session does not match application")
            suffix = event_type.rsplit(".", 1)[-1]
            if suffix in IDENTITY_STATES and obj.get("status") != suffix:
                raise HTTPException(400, "identity status does not match event")
        elif event_type == "refund.updated":
            expected = extra["amount"] if extra else order and order.get("primary_payment_amount", order.get("amount"))
            reference = state.get("recovered_refund_id") or ((extra or order or {}).get("refund_reference"))
            payment_reference = (extra or order or {}).get("payment_reference")
            if not order or obj.get("status") not in {"succeeded", "pending", "requires_action", "failed", "canceled"} or obj.get("id") != reference or obj.get("currency", "").lower() != "jpy" or obj.get("amount") != expected or obj.get("payment_intent") != payment_reference:
                raise HTTPException(400, "refund does not match order")
        elif event_type == "charge.refunded":
            expected = extra["amount"] if extra else order and order.get("primary_payment_amount", order.get("amount", 0))
            reference = (extra or order or {}).get("payment_reference")
            refunded = obj.get("amount_refunded", 0)
            if not order or obj.get("currency", "").lower() != "jpy" or type(refunded) is not int or not 0 < refunded <= expected or (reference and obj.get("payment_intent") != reference):
                raise HTTPException(400, "refund does not match order")
        elif event_type in {"invoice.paid", "invoice.payment_failed"}:
            if not isinstance(obj.get("id"), str) or not re.fullmatch(r"in_[A-Za-z0-9_]+", obj["id"]):
                raise HTTPException(400, "invalid invoice reference")
            period = obj.get("period_end", 0)
            if type(period) is not int or not 0 <= period <= 253402214400 or (self.settings.is_deployed and not period):
                raise HTTPException(400, "invalid invoice period")
            if not order or not subscription or not provider_subscription_id or subscription.get("provider_subscription_id") != provider_subscription_id or obj.get("currency", "").lower() != "jpy":
                raise HTTPException(400, "invoice does not match subscription")
            if event_type == "invoice.paid" and (obj.get("status") != "paid" or obj.get("amount_paid") != subscription.get("amount")):
                raise HTTPException(400, "invoice amount does not match subscription")
            if event_type == "invoice.payment_failed" and obj.get("amount_due") is not None and obj.get("amount_due") != subscription.get("amount"):
                raise HTTPException(400, "invoice amount does not match subscription")
        elif event_type in {"customer.subscription.updated", "customer.subscription.deleted"}:
            subscription = next((item for item in self.store.subscriptions if item.get("provider_subscription_id") == obj.get("id")), None)
            if not subscription:
                raise HTTPException(400, "subscription does not match")
            state["subscription"] = subscription
            state["order"] = next((item for item in self.store.orders if item["id"] == subscription.get("order_id")), None)
            if obj.get("status") not in {"active", "trialing", "past_due", "incomplete", "unpaid", "incomplete_expired", "canceled", "paused"}:
                raise HTTPException(400, "unknown subscription status")
            if obj.get("current_period_end") is not None and (type(obj["current_period_end"]) is not int or not 0 < obj["current_period_end"] <= 253402214400):
                raise HTTPException(400, "invalid subscription period")
        elif event_type == "charge.dispute.created":
            reference = (extra or order or {}).get("payment_reference")
            if not order or (reference and obj.get("payment_intent") != reference):
                raise HTTPException(400, "dispute does not match order")
        elif event_type == "account.updated":
            if event.get("account") != obj.get("id") or any(type(obj.get(key)) is not bool for key in ("charges_enabled", "payouts_enabled", "details_submitted")):
                raise HTTPException(400, "connected account does not match")

    def _apply(self, event: dict[str, Any], state: dict[str, Any]) -> list[EmailMessage]:
        obj = state["obj"]
        metadata = state["metadata"]
        event_type = state["event_type"]
        order = state["order"]
        subscription = state["subscription"]
        extra = state["extra"]
        application = state["application"]
        emails: list[EmailMessage] = []

        if state.get("payout"):
            payout = state["payout"]
            previous = payout["status"]
            apply_bank_status(payout, obj)
            if payout["status"] != previous:
                title = "振込が完了しました" if payout["status"] == "paid" else "振込状況の確認が必要です"
                self.store.notify(payout["seller_id"], title, f"¥{payout['net_amount']:,}", "/payouts", "transactions")
        elif state["paid_checkout_event"] and metadata.get("order_id") and metadata.get("extra_id"):
            if order and extra:
                emails.extend(apply_checkout_payment(self.store, self.settings, order, extra, obj))
        elif state["paid_checkout_event"] and metadata.get("order_id") and order:
            emails.extend(apply_checkout_payment(self.store, self.settings, order, None, obj, subscription))
        elif event_type == "checkout.session.expired" and order:
            apply_checkout_expiry(self.store, order, extra)
        elif event_type.startswith("identity.verification_session.") and event_type.rsplit(".", 1)[-1] in IDENTITY_STATES and application:
            apply_identity_event(application, event)
        elif event_type in {"charge.refunded", "refund.updated"} and order:
            emails.extend(apply_refund_payment(self.store, order, extra, obj, event_type))
        elif event_type in {"invoice.paid", "invoice.payment_failed"} and subscription and order:
            emails.extend(apply_invoice(self.store, self.settings, order, subscription, obj, event_type))
        elif event_type in {"customer.subscription.updated", "customer.subscription.deleted"} and subscription:
            provider_status = obj.get("status", "")
            if subscription.get("status") in TERMINAL and provider_status not in {"canceled", "incomplete_expired"}:
                return emails
            subscription["status"] = "cancelled" if provider_status in {"canceled", "incomplete_expired"} else ("active" if provider_status == "active" else "past_due")
            subscription["cancel_at_period_end"] = bool(obj.get("cancel_at_period_end"))
            if order:
                order["payment_status"] = "paid" if subscription["status"] == "active" else subscription["status"]
            if obj.get("current_period_end"):
                subscription["next_billing_at"] = datetime.fromtimestamp(obj["current_period_end"], timezone.utc)
            if subscription["status"] == "cancelled":
                subscription["cancelled_at"] = datetime.now(timezone.utc)
        elif event_type == "charge.dispute.created" and order:
            order["dispute_status"] = "provider_dispute"
            order["payment_dispute_id"] = obj.get("id")
            self.store.notify(order["seller_id"], "決済に異議申し立てが発生しました", order["tool_name"], f"/orders/{order['id']}", "security")
        elif event_type == "account.updated" and obj.get("id"):
            account = next((item for item in self.store.connected_accounts.values() if item.get("account_id") == obj["id"]), None)
            if account:
                account.update({"charges_enabled":bool(obj.get("charges_enabled")), "payouts_enabled":bool(obj.get("payouts_enabled")), "details_submitted":bool(obj.get("details_submitted"))})
        return emails
