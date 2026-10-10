"""Shared state transitions for verified webhooks and provider reconciliation.

These functions perform no network I/O. Callers must validate provider facts
and commit state, audit and queued email in the same persistence boundary.
"""
from datetime import datetime, timezone

from .refunds import apply_refund_result


def apply_checkout_payment(store, settings, order, extra, session, subscription=None):
    emails = []
    if extra:
        if extra.get("status") == "paid":
            return emails
        store.add_extra_payment(order, extra["amount"], extra["note"])
        extra["platform_fee"] = round(extra["amount"] * order.get("platform_fee_rate", settings.platform_fee_rate))
        extra.update(status="paid", payment_reference=session.get("payment_intent"))
        store.notify(order["seller_id"], "追加支払いが届きました", f"{order['tool_name']} · ¥{extra['amount']:,}", f"/orders/{order['id']}", "transactions")
        return [(order.get("seller_email"), f"[ツールバコ] {order['tool_name']} の追加支払が完了しました", f"追加支払: ¥{extra['amount']:,}\n{settings.site_base_url}/orders/{order['id']}")]
    if order.get("payment_status") == "paid" and order.get("payment_reference") == session.get("payment_intent"):
        return emails
    order["payment_status"] = "paid"
    order.setdefault("primary_platform_fee", order["platform_fee"])
    order["payment_reference"] = session.get("payment_intent")
    if not order.get("sales_recorded"):
        tool = store.get(order.get("tool_slug", ""))
        if tool:
            tool["sales_count"] += 1
        order["sales_recorded"] = True
    store.complete_instant_order(order)
    customer_email = (session.get("customer_details") or {}).get("email")
    if customer_email:
        order["buyer_email"] = customer_email
    if session.get("subscription") and subscription:
        subscription.update(status="active", provider_subscription_id=session["subscription"])
        invoice_id = session.get("invoice")
        if invoice_id and not any(item.get("provider_invoice_id") == invoice_id for item in subscription.setdefault("payments", [])):
            subscription["payments"].append({"provider_invoice_id": invoice_id, "amount": subscription["amount"], "status": "paid", "paid_at": datetime.now(timezone.utc)})
    store.notify(order["buyer_id"], "購入が完了しました", order["tool_name"], f"/orders/{order['id']}", "transactions")
    store.notify(order["seller_id"], "新しい注文が入りました", f"{order.get('buyer_name') or '購入者'}さんが購入しました", f"/orders/{order['id']}", "transactions")
    return [
        (order.get("buyer_email"), f"[ツールバコ] {order['tool_name']} のご購入を確認しました", f"ご購入ありがとうございます。取引ルームが開きました。\n{settings.site_base_url}/orders/{order['id']}"),
        (order.get("seller_email"), f"[ツールバコ] {order['tool_name']} が購入されました", f"新しい取引を確認してください。\n{settings.site_base_url}/orders/{order['id']}"),
    ]


def apply_checkout_expiry(store, order, extra=None):
    if extra:
        if extra.get("status") == "pending":
            extra["status"] = "expired"
    elif order.get("payment_status") == "pending":
        store.release_coupon(order)
        order.update(payment_status="expired", status="cancelled", updated_at=datetime.now(timezone.utc))
        for subscription in store.subscriptions:
            if subscription.get("order_id") == order["id"]:
                subscription["status"] = "expired"


def apply_refund_payment(store, order, extra, obj, event_type):
    was_complete = order.get("refund_status") == "completed"
    complete = apply_refund_result(order, extra, obj, event_type)
    if complete and order.get("status") == "cancel_pending":
        order.update(status="cancelled", cancel_requested_by=None)
        order.pop("cancel_previous_status", None)
    if complete and order.get("sales_recorded"):
        tool = store.get(order.get("tool_slug", ""))
        if tool:
            tool["sales_count"] = max(0, tool["sales_count"] - 1)
        order["sales_recorded"] = False
    if complete and not was_complete:
        return [(order.get("buyer_email"), f"[ツールバコ] {order['tool_name']} の全額返金を確認しました", "カード会社側へ返金が反映されるまで時間がかかる場合があります。")]
    return []
