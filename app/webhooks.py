from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from fastapi import HTTPException, Request
from .finance import FinanceConflict
from .payout_worker import apply_bank_status, verify_bank_payout
from .refunds import apply_refund_result


EmailSender = Callable[[str | None, str, str], Awaitable[bool]]
EmailMessage = tuple[str | None, str, str]


class StripeWebhookService:
    """Validate and apply Stripe events atomically within one app process."""

    def __init__(self, settings: Any, store: Any, email_sender: EmailSender, *, journal=None):
        self.settings = settings
        self.store = store
        self.email_sender = email_sender
        self.journal = journal

    def verify_signature(self, payload: bytes, signature: str) -> bool:
        secrets = [value for value in {self.settings.stripe_webhook_secret, self.settings.stripe_connect_webhook_secret} if value]
        if not secrets:
            return False
        parts: dict[str, list[str]] = {}
        for part in signature.split(","):
            if "=" in part:
                key, value = part.split("=", 1)
                parts.setdefault(key, []).append(value)
        try:
            timestamp = int(parts.get("t", ["0"])[0])
        except ValueError:
            return False
        if abs(int(time.time()) - timestamp) > 300:
            return False
        signed = f"{timestamp}.".encode() + payload
        expected_values = [hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest() for secret in secrets]
        return any(hmac.compare_digest(expected, supplied) for expected in expected_values for supplied in parts.get("v1", []))

    async def handle(self, request: Request) -> dict[str, bool]:
        payload = await request.body()
        if len(payload) > 1_000_000:
            raise HTTPException(413, "webhook payload too large")
        if not self.verify_signature(payload, request.headers.get("stripe-signature", "")):
            raise HTTPException(400, "invalid signature")
        try:
            event = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "invalid payload") from exc
        if not isinstance(event, dict) or not isinstance(event.get("data", {}), dict) or not isinstance(event.get("data", {}).get("object", {}), dict):
            raise HTTPException(400, "invalid payload")
        event_id = str(event.get("id", ""))
        if not event_id:
            raise HTTPException(400, "missing event id")
        if self.journal:
            expected_live = self.settings.stripe_secret_key.startswith(("sk_live_", "rk_live_"))
            if type(event.get("livemode")) is not bool or event["livemode"] != expected_live:
                raise HTTPException(400, "event environment does not match")

        state = self._resolve(event)
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
            mutable_objects = [item for item in (state.get("order"), state.get("subscription"), state.get("extra"), state.get("application"), state.get("payout")) if isinstance(item, dict)]
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

    def _resolve(self, event: dict[str, Any]) -> dict[str, Any]:
        obj = event.get("data", {}).get("object", {})
        metadata = obj.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise HTTPException(400, "invalid metadata")
        event_type = str(event.get("type", ""))[:120]
        paid_checkout_event = event_type in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}
        order = next((item for item in self.store.orders if item["id"] == metadata.get("order_id")), None) if metadata.get("order_id") else None
        payment_event = event_type in {"charge.refunded", "refund.updated", "charge.dispute.created"}
        if not order and payment_event and obj.get("payment_intent"):
            order = next((item for item in self.store.orders if item.get("payment_reference") == obj["payment_intent"] or any(x.get("payment_reference") == obj["payment_intent"] for x in item.get("pending_extras", []))), None)
        provider_subscription_id = obj.get("subscription") or (((obj.get("parent") or {}).get("subscription_details") or {}).get("subscription"))
        subscription = next((item for item in self.store.subscriptions if item.get("provider_subscription_id") == provider_subscription_id), None) if provider_subscription_id else None
        if not subscription and metadata.get("order_id"):
            subscription = next((item for item in self.store.subscriptions if item.get("order_id") == metadata.get("order_id")), None)
        if not order and subscription:
            order = next((item for item in self.store.orders if item["id"] == subscription.get("order_id")), None)
        extra = order and next((item for item in order.get("pending_extras", []) if item["id"] == metadata.get("extra_id")), None) if metadata.get("extra_id") else None
        if not extra and order and payment_event and obj.get("payment_intent"):
            extra = next((x for x in order.get("pending_extras", []) if x.get("payment_reference") == obj["payment_intent"]), None)
        application = self.store.identity_applications.get(metadata.get("user_id")) if metadata.get("user_id") else None
        payout = next((p for p in self.store.payouts if p.get("finance_v2") and (p["id"] == metadata.get("payout_id") or p.get("provider_payout_id") == obj.get("id"))), None) if event_type.startswith("payout.") else None
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
        elif event_type == "charge.dispute.created":
            reference = (extra or order or {}).get("payment_reference")
            if not order or (reference and obj.get("payment_intent") != reference):
                raise HTTPException(400, "dispute does not match order")
        elif event_type == "account.updated" and event.get("account") and event.get("account") != obj.get("id"):
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
            if order and extra and extra.get("status") != "paid":
                self.store.add_extra_payment(order, extra["amount"], extra["note"])
                extra["platform_fee"] = round(extra["amount"] * order.get("platform_fee_rate", self.settings.platform_fee_rate))
                extra["status"] = "paid"
                extra["payment_reference"] = obj.get("payment_intent")
                self.store.notify(order["seller_id"], "追加支払いが届きました", f"{order['tool_name']} · ¥{extra['amount']:,}", f"/orders/{order['id']}", "transactions")
                emails.append((order.get("seller_email"), f"[ツールバコ] {order['tool_name']} の追加支払が完了しました", f"追加支払: ¥{extra['amount']:,}\n{self.settings.site_base_url}/orders/{order['id']}"))
        elif state["paid_checkout_event"] and metadata.get("order_id") and order:
            if order.get("payment_status") == "paid" and order.get("payment_reference") == obj.get("payment_intent"):
                return emails
            order["payment_status"] = "paid"
            order.setdefault("primary_platform_fee", order["platform_fee"])
            order["payment_reference"] = obj.get("payment_intent")
            if not order.get("sales_recorded"):
                tool = self.store.get(order.get("tool_slug", ""))
                if tool:
                    tool["sales_count"] += 1
                order["sales_recorded"] = True
            self.store.complete_instant_order(order)
            customer_email = (obj.get("customer_details") or {}).get("email")
            if customer_email:
                order["buyer_email"] = customer_email
            if obj.get("subscription") and subscription:
                subscription["status"] = "active"
                subscription["provider_subscription_id"] = obj["subscription"]
                invoice_id = obj.get("invoice")
                if invoice_id and not any(item.get("provider_invoice_id") == invoice_id for item in subscription.setdefault("payments", [])):
                    subscription["payments"].append({"provider_invoice_id":invoice_id,"amount":subscription["amount"],"status":"paid","paid_at":datetime.now(timezone.utc)})
            emails.extend([
                (order.get("buyer_email"), f"[ツールバコ] {order['tool_name']} のご購入を確認しました", f"ご購入ありがとうございます。取引ルームが開きました。\n{self.settings.site_base_url}/orders/{order['id']}"),
                (order.get("seller_email"), f"[ツールバコ] {order['tool_name']} が購入されました", f"新しい取引を確認してください。\n{self.settings.site_base_url}/orders/{order['id']}"),
            ])
            self.store.notify(order["buyer_id"], "購入が完了しました", order["tool_name"], f"/orders/{order['id']}", "transactions")
            self.store.notify(order["seller_id"], "新しい注文が入りました", f"{order.get('buyer_name') or '購入者'}さんが購入しました", f"/orders/{order['id']}", "transactions")
        elif event_type == "checkout.session.expired" and extra:
            if extra.get("status") == "pending":
                extra["status"] = "expired"
        elif event_type == "checkout.session.expired" and order and order.get("payment_status") == "pending":
            self.store.release_coupon(order)
            order["payment_status"] = "expired"
            order["status"] = "cancelled"
            order["updated_at"] = datetime.now(timezone.utc)
            for item in self.store.subscriptions:
                if item.get("order_id") == order["id"]:
                    item["status"] = "expired"
        elif event_type == "identity.verification_session.verified" and application:
            application["status"] = "verified"
            application["verified_at"] = datetime.now(timezone.utc)
        elif event_type in {"charge.refunded", "refund.updated"} and order:
            was_complete = order.get("refund_status") == "completed"
            complete = apply_refund_result(order, extra, obj, event_type)
            if complete and order.get("status") == "cancel_pending":
                order["status"] = "cancelled"
                order["cancel_requested_by"] = None
                order.pop("cancel_previous_status", None)
            if complete and order.get("sales_recorded"):
                tool = self.store.get(order.get("tool_slug", ""))
                if tool:
                    tool["sales_count"] = max(0, tool["sales_count"] - 1)
                order["sales_recorded"] = False
            if complete and not was_complete:
                emails.append((order.get("buyer_email"), f"[ツールバコ] {order['tool_name']} の全額返金を確認しました", "カード会社側へ返金が反映されるまで時間がかかる場合があります。"))
        elif event_type == "identity.verification_session.requires_input" and application:
            application["status"] = "rejected"
            application["rejection_reason"] = "本人確認書類を確認できませんでした。審査画面から再提出してください。"
        elif event_type == "invoice.paid" and subscription:
            subscription["status"] = "active"
            if order:
                order["payment_status"] = "paid"
            subscription["last_paid_at"] = datetime.now(timezone.utc)
            subscription["last_invoice_id"] = obj.get("id")
            subscription["last_payment_intent"] = obj.get("payment_intent")
            subscription["last_charge"] = obj.get("charge")
            payments = subscription.setdefault("payments", [])
            if not any(item.get("provider_invoice_id") == obj.get("id") for item in payments):
                payments.append({"provider_invoice_id":obj.get("id"),"payment_intent":obj.get("payment_intent"),"charge":obj.get("charge"),"amount":obj.get("amount_paid"),"status":"paid","paid_at":datetime.now(timezone.utc)})
            lines = ((obj.get("lines") or {}).get("data") or [])
            period_end = obj.get("period_end") or (lines and (lines[0].get("period") or {}).get("end"))
            subscription["next_billing_at"] = datetime.fromtimestamp(period_end, timezone.utc) if period_end else datetime.now(timezone.utc) + timedelta(days=30)
        elif event_type == "invoice.payment_failed" and subscription:
            subscription["status"] = "past_due"
            if order:
                order["payment_status"] = "past_due"
            subscription.setdefault("billing_events", []).append({"provider_invoice_id":obj.get("id"),"status":"payment_failed","occurred_at":datetime.now(timezone.utc)})
            self.store.notify(subscription["buyer_id"], "月額契約のお支払いを確認できません", subscription["tool_name"], "/subscriptions", "transactions")
            emails.append((order and order.get("buyer_email"), f"[ツールバコ] {subscription['tool_name']} のお支払いをご確認ください", f"決済方法を確認してください。\n{self.settings.site_base_url}/subscriptions"))
        elif event_type in {"customer.subscription.updated", "customer.subscription.deleted"} and subscription:
            provider_status = obj.get("status", "")
            subscription["status"] = "cancelled" if event_type.endswith("deleted") or provider_status in {"canceled", "unpaid", "incomplete_expired"} else ("past_due" if provider_status in {"past_due", "incomplete"} else "active")
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
