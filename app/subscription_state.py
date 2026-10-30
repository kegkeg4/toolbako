"""Invoice-level deduplication; a late event never revives a cancelled contract."""
from datetime import datetime, timezone

TERMINAL = frozenset({"cancelled", "expired"})


def apply_invoice(store, settings, order, subscription, obj, event_type):
    invoice_id = obj["id"]
    paid = event_type == "invoice.paid"
    payments = subscription.setdefault("payments", [])
    previous = next((p for p in payments if p.get("provider_invoice_id") == invoice_id), None)
    period_end = obj.get("period_end", 0)
    last_period = subscription.get("latest_invoice_period_end", 0)
    current_period = period_end >= last_period
    terminal = subscription.get("status") in TERMINAL
    if paid:
        facts = {"provider_invoice_id": invoice_id, "payment_intent": obj.get("payment_intent"),
                 "charge": obj.get("charge"), "amount": obj["amount_paid"], "status": "paid"}
        if previous:
            # Checkout can arrive before invoice.paid and omit charge IDs.
            previous.update({k: v for k, v in facts.items() if v is not None})
        else:
            payments.append({**facts, "paid_at": datetime.now(timezone.utc)})
        if current_period:
            subscription.update(last_invoice_id=invoice_id, last_paid_at=datetime.now(timezone.utc),
                                latest_invoice_period_end=period_end)
            for field, value in (("last_payment_intent", obj.get("payment_intent")), ("last_charge", obj.get("charge"))):
                if value: subscription[field] = value
            if not terminal:
                subscription["status"] = "active"
                order["payment_status"] = "paid"
            if period_end: subscription["next_billing_at"] = datetime.fromtimestamp(period_end, timezone.utc)
        return []
    # A paid invoice cannot subsequently become unpaid; a distinct event ID
    # for the same invoice is also a duplicate for notification purposes.
    failures = subscription.setdefault("billing_events", [])
    if previous or any(p.get("provider_invoice_id") == invoice_id and p.get("status") == "payment_failed" for p in failures):
        return []
    failures.append({"provider_invoice_id": invoice_id, "status": "payment_failed",
                     "period_end": period_end, "occurred_at": datetime.now(timezone.utc)})
    if terminal or not current_period:
        return []
    subscription.update(status="past_due", latest_invoice_period_end=period_end)
    order["payment_status"] = "past_due"
    store.notify(subscription["buyer_id"], "月額契約のお支払いを確認できません", subscription["tool_name"], "/subscriptions", "transactions")
    return [(order.get("buyer_email"), f"[ツールバコ] {subscription['tool_name']} のお支払いをご確認ください",
             f"決済方法を確認してください。\n{settings.site_base_url}/subscriptions")]
