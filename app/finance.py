"""JPY seller receivables and withdrawal reservations.

The private relational tables are committed with the marketplace snapshot. All
callers (HTTP and workers) must hold PostgresStateStore.request's database lock.
This is a seller subledger, not a replacement for ONE's general accounting.
"""
from datetime import datetime, timezone
from uuid import uuid4

from .payout_policy import payout_fee, scheduled_payout_date


class FinanceConflict(ValueError):
    pass


PAYOUT_LABELS = {
    "requested": "申請受付", "transferring": "売上分配中",
    "awaiting_funds": "Stripe残高の確定待ち", "bank_pending": "銀行振込の処理中",
    "paid": "振込完了", "held": "振込保留", "review": "運営確認中",
    "reversing": "分配取消中",
    "bank_failed": "振込失敗・口座確認待ち", "cancelled": "申請取消",
    "processing": "処理中（デモ）", "completed": "完了（デモ）",
}
RESERVED = frozenset(PAYOUT_LABELS) - {"cancelled"}


def yen(value, *, zero=False):
    if type(value) is not int or value < (0 if zero else 1):
        raise FinanceConflict("金額は円単位の整数で指定してください")
    return value


def receipts(store):
    """Only individually identifiable, one-time Stripe payments are payable.

    Subscription renewals require invoice-level accounting and are deliberately
    excluded, rather than paying the original order repeatedly.
    """
    result = {}
    for order in store.orders:
        if order.get("billing_type", "one_time") != "one_time" or not str(order.get("payment_reference") or "").startswith("pi_"):
            continue
        extras = [x for x in order.get("pending_extras", []) if x.get("status") == "paid"]
        extra_fees = [x.get("platform_fee", round(x["amount"] * order.get("platform_fee_rate", 0.15))) for x in extras]
        primary_fee = order.get("primary_platform_fee", order["platform_fee"] - sum(extra_fees))
        parts = [(order["payment_reference"], order.get("primary_payment_amount", order["amount"]), primary_fee, order)]
        parts += [(x.get("payment_reference", ""), x["amount"], fee, x) for x, fee in zip(extras, extra_fees)]
        if sum(p[1] for p in parts) != order["amount"] or sum(p[2] for p in parts) != order["platform_fee"]:
            raise FinanceConflict("決済明細と注文金額が一致しません")
        for reference, amount, fee, part in parts:
            yen(amount); yen(fee, zero=True)
            if fee > amount or not reference.startswith("pi_") or reference in result:
                raise FinanceConflict("決済参照または手数料が不正です")
            refunded = (order.get("primary_refund_status", order.get("refund_status")) if part is order else part.get("refund_status")) == "completed"
            result[reference] = {
                "id": reference, "order_id": order["id"], "seller_id": order["seller_id"],
                "amount": amount, "fee": fee, "net": amount - fee,
                "created_at": part.get("created_at", order["created_at"]),
                "refunded": refunded,
                "eligible": order.get("status") == "completed" and order.get("payment_status") == "paid"
                and not order.get("refund_status") and not part.get("refund_status")
                and not order.get("dispute_status") and not order.get("payment_reconciliation_required"),
            }
    return result


def reserved_amounts(store):
    reserved = {}
    for payout in store.payouts:
        if payout.get("finance_v2") and payout["status"] in RESERVED:
            for allocation in payout["allocations"]:
                key = allocation["receipt_id"]
                reserved[key] = reserved.get(key, 0) + allocation["amount"]
    return reserved


def balance(store, seller_id):
    reserved = reserved_amounts(store)
    return sum(max(0, item["net"] - reserved.get(key, 0)) for key, item in receipts(store).items()
               if item["seller_id"] == seller_id and item["eligible"])


def summary(store, original):
    """Admin uses the same reservations as the seller page, not gross totals."""
    facts = receipts(store)
    reserved = reserved_amounts(store)
    payouts = [p for p in store.payouts if p.get("finance_v2")]
    rows = []
    for seller in original["sellers"]:
        seller = dict(seller)
        account = store.connected_accounts.get(seller["seller_id"], {})
        seller["available"] = balance(store, seller["seller_id"])
        seller["reserved"] = sum(p["amount"] for p in payouts if p["seller_id"] == seller["seller_id"] and p["status"] in RESERVED - {"paid"})
        seller["paid_out"] = sum(p["net_amount"] for p in payouts if p["seller_id"] == seller["seller_id"] and p["status"] == "paid")
        can_pay = account.get("payouts_enabled") and account.get("details_submitted") and not account.get("payouts_paused")
        seller["payout_eligible"] = seller["available"] if can_pay else 0
        seller["blocked_for_payout"] = seller["available"] - seller["payout_eligible"]
        rows.append(seller)
    available_orders = {f["order_id"] for key, f in facts.items() if f["eligible"] and f["net"] > reserved.get(key, 0)}
    transactions = []
    for order in original["transactions"]:
        item = dict(order)
        if item["fund_state"] == "available" and item["id"] not in available_orders:
            item["fund_state"] = "payout_blocked"
        if item.get("refund_status") and item["refund_status"] != "completed":
            item["fund_state"] = "refund_pending"
        transactions.append(item)
    return {**original, "sellers": rows, "transactions": transactions,
            "paid_out": sum(p["net_amount"] for p in payouts if p["status"] == "paid"),
            "payout_fee_total": sum(p["fee"] for p in payouts if p["status"] == "paid"),
            "payout_reserved": sum(p["amount"] for p in payouts if p["status"] in RESERVED - {"paid"}),
            "available_to_payout": sum(s["payout_eligible"] for s in rows),
            "blocked_for_payout": sum(s["blocked_for_payout"] for s in rows),
            "refund_total": sum(o.get("refund_confirmed_amount", o.get("primary_payment_amount", o.get("amount", 0)) if o.get("refund_status") == "completed" else 0) for o in store.orders)}


def request_payout(store, settings, seller_id, amount, *, at=None, request_key=None):
    request_key = request_key or str(uuid4())
    previous = next((p for p in store.payouts if p.get("finance_v2") and p["seller_id"] == seller_id and p.get("request_key") == request_key), None)
    if previous:
        if previous["amount"] != amount:
            raise FinanceConflict("同じ申請番号で金額が変更されています。画面を再読み込みしてください")
        return previous
    fee = payout_fee(amount, settings)
    account = store.connected_accounts.get(seller_id, {})
    if account.get("payouts_paused") or not account.get("payouts_enabled") or not account.get("details_submitted") or not account.get("account_id", "").startswith("acct_"):
        raise FinanceConflict("振込先の登録・本人確認、または振込保留の解除が必要です")
    if amount > balance(store, seller_id):
        raise FinanceConflict("振込可能な残高を超えています。履歴を再読み込みしてください")
    at = at or datetime.now(timezone.utc)
    payout = {"id": str(uuid4()), "finance_v2": True, "seller_id": seller_id, "request_key": request_key,
              "account_id": account["account_id"], "amount": amount, "fee": fee,
              "net_amount": amount - fee, "kind": "requested", "status": "requested",
              "created_at": at, "scheduled_for": scheduled_payout_date(at), "allocations": []}
    remaining, remaining_fee = amount, fee
    reserved = reserved_amounts(store)
    for key, item in sorted(receipts(store).items(), key=lambda pair: (pair[1]["created_at"], pair[0])):
        if item["seller_id"] != seller_id or not item["eligible"]:
            continue
        take = min(remaining, max(0, item["net"] - reserved.get(key, 0)))
        if not take:
            continue
        charged_fee = min(take, remaining_fee)
        payout["allocations"].append({"id": str(uuid4()), "receipt_id": key, "order_id": item["order_id"],
                                      "amount": take, "transfer_amount": take - charged_fee})
        remaining -= take; remaining_fee -= charged_fee
        if not remaining:
            break
    if remaining or remaining_fee:
        raise FinanceConflict("売上の割当を確認できません")
    store.payouts.insert(0, payout)
    return payout


async def sync_finance(conn, store):
    """Idempotent append-only facts + normalized payout read model, atomically."""
    from .database import content_hash
    facts = receipts(store)
    existing_receipts = {row[0] for row in await (await conn.execute("select payment_intent from toolbako_runtime.finance_receipts")).fetchall()}
    existing_payouts = {row[0] for row in await (await conn.execute("select id::text from toolbako_runtime.finance_payouts")).fetchall()}
    payouts = [p for p in store.payouts if p.get("finance_v2")]
    if not existing_receipts <= facts.keys() or not existing_payouts <= {p["id"] for p in payouts}:
        raise FinanceConflict("Financial history cannot be removed")

    async def entry(key, kind, amount, *, receipt=None, payout=None, reference=None):
        if amount == 0:
            return
        values = (kind, amount, receipt, payout, reference)
        digest = content_hash(values)
        await conn.execute(
            "insert into toolbako_runtime.finance_entries(id,kind,amount,receipt_id,payout_id,provider_reference,content_hash) "
            "values(%s,%s,%s,%s,%s,%s,%s) on conflict(id) do nothing",
            (key, *values, digest),
        )
        row = await (await conn.execute("select content_hash from toolbako_runtime.finance_entries where id=%s", (key,))).fetchone()
        if row[0] != digest:
            raise FinanceConflict("Financial event was changed")

    for key, item in facts.items():
        values = (item["order_id"], item["seller_id"], item["amount"], item["fee"])
        await conn.execute(
            "insert into toolbako_runtime.finance_receipts(payment_intent,order_id,seller_id,amount,fee) "
            "values(%s,%s,%s,%s,%s) on conflict(payment_intent) do nothing", (key, *values),
        )
        row = await (await conn.execute("select order_id,seller_id,amount,fee from toolbako_runtime.finance_receipts where payment_intent=%s", (key,))).fetchone()
        if row != values:
            raise FinanceConflict("Recorded payment facts cannot change")
        await entry(f"credit:{key}", "seller_credit", item["net"], receipt=key)
        if item["refunded"]:
            await entry(f"refund:{key}", "seller_refund", -item["net"], receipt=key)

    for key, amount in reserved_amounts(store).items():
        if key not in facts or amount > facts[key]["net"]:
            raise FinanceConflict("Payout allocations exceed seller earnings")
    for payout in payouts:
        if payout["status"] not in RESERVED | {"cancelled"}:
            raise FinanceConflict("Unknown payout status")
        allocations = payout["allocations"]
        if sum(a["amount"] for a in allocations) != payout["amount"] or sum(a["transfer_amount"] for a in allocations) != payout["net_amount"] or payout["net_amount"] + payout["fee"] != payout["amount"]:
            raise FinanceConflict("Payout does not balance")
        fixed = (payout["seller_id"], payout["account_id"], payout["amount"], payout["fee"], payout["net_amount"], payout["scheduled_for"], payout["request_key"])
        digest = content_hash(fixed)
        await conn.execute(
            "insert into toolbako_runtime.finance_payouts(id,seller_id,account_id,amount,fee,net_amount,status,scheduled_for,request_key,content_hash) "
            "values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict(id) do nothing",
            (payout["id"], *fixed[:5], payout["status"], fixed[5], fixed[6], digest),
        )
        row = await (await conn.execute("select content_hash from toolbako_runtime.finance_payouts where id=%s", (payout["id"],))).fetchone()
        if row[0] != digest:
            raise FinanceConflict("Payout identity or amount changed")
        old_allocations = {row[0] for row in await (await conn.execute("select id::text from toolbako_runtime.finance_allocations where payout_id=%s", (payout["id"],))).fetchall()}
        if not old_allocations <= {a["id"] for a in allocations}:
            raise FinanceConflict("Payout allocations cannot be removed")
        if payout["status"] == "cancelled":
            bank_attempt = await (await conn.execute("select status from toolbako_runtime.stripe_operations where operation_id=%s", (f"bank-payout-{payout['id']}",))).fetchone()
            if payout.get("provider_payout_id") or bank_attempt:
                raise FinanceConflict("Bank payout requires reconciliation before cancellation")
        await conn.execute("update toolbako_runtime.finance_payouts set status=%s,provider_reference=%s,updated_at=now() where id=%s",
                           (payout["status"], payout.get("provider_payout_id"), payout["id"]))
        for allocation in allocations:
            key = allocation["receipt_id"]
            if facts[key]["seller_id"] != payout["seller_id"] or facts[key]["order_id"] != allocation["order_id"]:
                raise FinanceConflict("Payout belongs to another seller")
            yen(allocation["amount"]); yen(allocation["transfer_amount"], zero=True)
            if allocation["transfer_amount"] > allocation["amount"]:
                raise FinanceConflict("Transfer exceeds reservation")
            identity = (payout["id"], key, allocation["amount"], allocation["transfer_amount"])
            await conn.execute("insert into toolbako_runtime.finance_allocations(id,payout_id,receipt_id,amount,transfer_amount) values(%s,%s,%s,%s,%s) on conflict(id) do nothing", (allocation["id"], *identity))
            row = await (await conn.execute("select payout_id::text,receipt_id,amount,transfer_amount from toolbako_runtime.finance_allocations where id=%s", (allocation["id"],))).fetchone()
            if row != identity:
                raise FinanceConflict("Payout allocation changed")
            await entry(f"reserve:{allocation['id']}", "withdrawal_reserved", -allocation["amount"], receipt=key, payout=payout["id"])
            if payout["status"] == "cancelled":
                attempt = await (await conn.execute("select status from toolbako_runtime.stripe_operations where operation_id=%s", (f"allocation-{allocation['id']}",))).fetchone()
                if attempt and attempt[0] != "rejected" and not allocation.get("reversal_id"):
                    raise FinanceConflict("Unreconciled transfer cannot be released")
                if allocation.get("transfer_id") and not allocation.get("reversal_id"):
                    raise FinanceConflict("A transferred allocation cannot be released before reversal")
                await entry(f"release:{allocation['id']}", "withdrawal_released", allocation["amount"], receipt=key, payout=payout["id"])
            for field, kind, amount in (("transfer_id", "connect_transfer", allocation["transfer_amount"]), ("reversal_id", "transfer_reversal", -allocation["transfer_amount"])):
                if allocation.get(field):
                    await entry(f"{field}:{allocation['id']}", kind, amount, receipt=key, payout=payout["id"], reference=allocation[field])
        if payout["status"] == "paid":
            if not payout.get("provider_payout_id"):
                raise FinanceConflict("A provider payout is required before marking paid")
            await entry(f"paid:{payout['id']}", "bank_paid", payout["net_amount"], payout=payout["id"], reference=payout["provider_payout_id"])
            await entry(f"fee:{payout['id']}", "withdrawal_fee", payout["fee"], payout=payout["id"])
