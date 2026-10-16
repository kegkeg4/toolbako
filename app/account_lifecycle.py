"""Account erasure eligibility shared by the HTTP route and the worker."""


def deletion_hold(store, user_id):
    orders = [o for o in store.orders if user_id in {o.get("buyer_id"), o.get("seller_id")}]
    if any(o.get("status") not in {"completed", "cancelled"} for o in orders):
        return "active_order"
    if any(o.get("dispute_status") not in {None, "", "resolved", "closed"}
           or o.get("payment_reconciliation_required")
           or o.get("refund_status") not in {None, "", "completed"} for o in orders):
        return "financial_review"
    if any(user_id in {s.get("buyer_id"), s.get("seller_id")}
           and s.get("status") not in {"cancelled", "expired"} for s in store.subscriptions):
        return "active_subscription"
    if any(user_id in {x.get("buyer_id"), x.get("seller_id")}
           and x.get("status") not in {"declined", "closed"} for x in store.transfer_inquiries):
        return "active_transfer"
    if any(p.get("seller_id") == user_id and p.get("status") not in {"paid", "completed", "cancelled"}
           for p in store.payouts):
        return "unsettled_payout"
    if store.available_balance(user_id) > 0:
        return "unpaid_balance"
    if any(case.get("status") not in {"resolved", "closed"}
           and (case.get("user_id") == user_id or case.get("order_id") in {o["id"] for o in orders})
           for case in store.support_cases):
        return "open_support_case"
    return None


def account_erased_or_erasing(store, user_id):
    return any(d.get("user_id") == user_id and d.get("status") in {"erasing", "completed"}
               for d in getattr(store, "account_deletions", ()))
