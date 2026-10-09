"""Full cancellation refunds are per payment, not just the first Checkout."""
from .finance import FinanceConflict, RESERVED


async def request_order_refunds(store, order, stripe):
    if order.get("billing_type", "one_time") != "one_time":
        raise FinanceConflict("月額契約の返金は請求明細の確認が必要です。運営へお問い合わせください")
    if any(p.get("finance_v2") and p["status"] in RESERVED
           and any(a["order_id"] == order["id"] for a in p["allocations"]) for p in store.payouts):
        raise FinanceConflict("振込申請済みの売上です。分配の取り消しを運営で確認してから返金します")
    extras = [x for x in order.get("pending_extras", []) if x.get("status") == "paid"]
    primary_amount = order.get("primary_payment_amount", order["amount"])
    if primary_amount + sum(x["amount"] for x in extras) != order["amount"]:
        raise FinanceConflict("追加支払いの決済明細を確認できません。運営へお問い合わせください")
    parts = [(order, None), *((extra, extra["id"]) for extra in extras)]
    if any(not part.get("payment_reference", "").startswith("pi_") for part, _ in parts):
        raise FinanceConflict("返金対象の決済を確認できません")
    order["refund_status"] = "processing"
    for part, extra_id in parts:
        if part.get("refund_reference"):
            continue
        kwargs = {"extra_id": extra_id} if extra_id else {}
        refund = await stripe.refund_payment(part["payment_reference"], order["id"], **kwargs)
        part["refund_reference"] = refund["id"]
        if extra_id:
            part["refund_status"] = "pending"
        else:
            order["primary_refund_status"] = "pending"
    order["refund_status"] = "pending"


def apply_refund_result(order, extra, obj, event_type):
    """Preserve cumulative refund facts; old events cannot undo completion."""
    part = extra or order
    expected = extra["amount"] if extra else order.get("primary_payment_amount", order["amount"])
    amount_key = "refunded_amount" if extra else "primary_refunded_amount"
    status_key = "refund_status" if extra else "primary_refund_status"
    if event_type == "charge.refunded":
        confirmed = obj["amount_refunded"]
    elif obj.get("status") == "succeeded":
        # This endpoint creates full refunds only. Multiple partial refund
        # objects are reconciled through charge.refunded's cumulative amount.
        confirmed = obj["amount"]
    else:
        confirmed = 0
    part[amount_key] = max(part.get(amount_key, 0), confirmed)
    if part[amount_key] >= expected:
        part[status_key] = "completed"
    elif part[amount_key]:
        part[status_key] = "partial"
    elif obj.get("status") in {"failed", "canceled"}:
        part[status_key] = "failed"
    else:
        part[status_key] = "pending"
    extras = [x for x in order.get("pending_extras", []) if x.get("status") == "paid"]
    primary = order.get("primary_payment_amount", order["amount"])
    confirmed_total = order.get("primary_refunded_amount", 0) + sum(x.get("refunded_amount", 0) for x in extras)
    complete = (primary + sum(x["amount"] for x in extras) == order["amount"]
                and confirmed_total == order["amount"])
    order["refund_confirmed_amount"] = confirmed_total
    order["refund_status"] = "completed" if complete else ("partial" if confirmed_total else "review" if part[status_key] == "failed" else "pending")
    return complete
