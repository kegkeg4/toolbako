"""Admin-only entry points for the durable paid-out refund workflow.

No live financial writes are enabled. Every call is reauthorized by the service
against the current session, actual MFA, bound ledger and distinct admins.
"""
from fastapi import APIRouter, Form, HTTPException, Request
from starlette.responses import RedirectResponse

from .finance import FinanceConflict
from .financial_scope import configured_scope
from .integrations import StripeOutcomeUnknown
from .paid_out_refund_execution import (
    FIELD, open_paid_out_refund_case, propose_paid_out_refund_action,
    approve_paid_out_refund_action,
)


def paid_out_refund_inventory(store, settings, backend):
    scope = None
    try:
        scope = configured_scope(settings)
    except RuntimeError:
        pass
    ready = bool(backend and scope and not settings.demo_mode
                 and settings.stripe_charge_mode == "separate" and len(set(settings.admin_user_ids)) >= 2)
    paid_receipts = {row.get("receipt_id") for payout in store.payouts if payout.get("status") == "paid"
                     for row in payout.get("allocations", [])}
    candidates = [order for order in store.orders if FIELD in order or (
        order.get("payment_reference") in paid_receipts and order.get("status") == "cancel_pending"
        and order.get("refund_cancel_accepted_by"))]
    return {"ready": ready, "execute_ready": ready and scope.mode == "test", "truncated": len(candidates) > 100,
            "items": [{"order_id": order["id"], "tool_name": order.get("tool_name", "商品"),
                       "amount": order.get("amount", 0), "case": order.get(FIELD)} for order in candidates[:100]]}


def create_paid_out_refund_router(services, current_user, is_admin):
    router = APIRouter()

    @router.post("/admin/orders/{order_id}/paid-out-refund/{action}")
    async def paid_out_refund_admin(request: Request, order_id: str, action: str,
                                    reserve_id: str = Form("", max_length=100),
                                    reserve_amount: int = Form(0, ge=0),
                                    seller_recovery_amount: int = Form(0, ge=0),
                                    proposal_id: str = Form("", max_length=100),
                                    review_action: str = Form("execute")):
        backend, store, stripe, settings, mfa = services()
        user = current_user(request)
        if not is_admin(user):
            raise HTTPException(403, "管理者のみアクセスできます")
        if action not in {"open", "propose", "approve"}:
            raise HTTPException(404)
        if not mfa.recent(request):
            return RedirectResponse("/security/mfa?next=%2Fadmin%3Fsection%3Dfinance", 303)
        inventory = paid_out_refund_inventory(store, settings, backend)
        if not inventory["ready"]:
            raise HTTPException(409, "返金先の口座・取引台帳・管理者設定を確認してから操作してください")
        if not any(order.get("id") == order_id for order in store.orders):
            raise HTTPException(404)
        try:
            if action == "open":
                await open_paid_out_refund_case(backend, store, stripe, settings, mfa, request, order_id,
                    reserve_id=reserve_id, reserve_amount=reserve_amount, seller_recovery_amount=seller_recovery_amount)
            elif action == "propose":
                if review_action not in {"execute", "reconcile"}:
                    raise HTTPException(400, "確認方法を選び直してください")
                if review_action == "execute" and not inventory["execute_ready"]:
                    raise HTTPException(409, "本番の返金実行はまだ開始していません")
                await propose_paid_out_refund_action(backend, store, stripe, settings, mfa, request,
                                                     order_id, action=review_action)
            else:
                await approve_paid_out_refund_action(backend, store, stripe, settings, mfa, request,
                                                     order_id, proposal_id)
        except StripeOutcomeUnknown:
            raise
        except (FinanceConflict, RuntimeError):
            raise HTTPException(409, "返金原資・元決済・承認を確認できません。再送せず照合してください") from None
        return RedirectResponse("/admin?section=finance&paid_out_refund_saved=1#paid-out-refund", 303)

    return router
