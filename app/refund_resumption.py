"""Four-eyes resumption of an interrupted full cancellation, Sandbox only.

This is NOT unknown-operation recovery. A new POST is allowed only for a part
with NO durable intent, NO local refund facts and NO provider refunds. Every
previous intent must be a verified, fully succeeded refund. One approval sends
at most one new refund, using the original deterministic operation key.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .database import content_hash
from .finance import FinanceConflict, RESERVED
from .integrations import StripeOutcomeUnknown
from .payment_reconciliation import require_sandbox_review, payment_metadata_matches, sandbox_payment_proof
from .payment_state import apply_refund_payment


def _parts(store, order):
    if (order.get("billing_type", "one_time") != "one_time" or order.get("payment_status") != "paid"
            or order.get("status") not in {"cancel_pending", "cancelled"}
            or order.get("refund_status") not in {"processing", "review", "partial", "pending"}
            or order.get("refund_cancel_accepted_by") not in {order["buyer_id"], order["seller_id"]}
            or order.get("dispute_status")):
        raise FinanceConflict("キャンセル合意を記録した、処理中の単発全額返金だけ再開できます")
    if any(p.get("finance_v2") and p["status"] in RESERVED
           and any(a["order_id"] == order["id"] for a in p["allocations"]) for p in store.payouts):
        raise FinanceConflict("振込申請済みの売上は返金再開できません")
    extras = [x for x in order.get("pending_extras", []) if x.get("status") == "paid"]
    parts = [(None, order, order.get("primary_payment_amount", order["amount"])),
             *((extra, extra, extra["amount"]) for extra in extras)]
    if (len(parts) > 10 or any(type(amount) is not int or amount <= 0 for _, _, amount in parts)
            or type(order["amount"]) is not int or sum(amount for _, _, amount in parts) != order["amount"]
            or any(not isinstance(x.get("id"), str) or not x["id"] for x in extras)
            or len({x["id"] for x in extras}) != len(extras)):
        raise FinanceConflict("返金明細・全額・件数を確認できません")
    references = [part.get("payment_reference") for _, part, _ in parts]
    if (any(not isinstance(ref, str) or not ref.startswith("pi_") for ref in references)
            or len(set(references)) != len(references)):
        raise FinanceConflict("返金元の決済明細が重複しています")
    for other in store.orders:
        if other is order:
            continue
        if any(part.get("payment_reference") in references for part in [other, *other.get("pending_extras", [])]):
            raise FinanceConflict("返金元の決済が別注文にも記録されています")
    return parts


def _refund_fact(remote, order, extra, part, amount, proof, at, *, since=0, require_complete=True):
    if (remote.get("object") != "refund" or not str(remote.get("id", "")).startswith("re_")
            or type(remote.get("amount")) is not int or remote["amount"] != amount
            or remote.get("currency") != "jpy" or remote.get("payment_intent") != part["payment_reference"]
            or remote.get("charge") != proof["charge"] or not payment_metadata_matches(remote.get("metadata"), order, extra)
            or type(remote.get("created")) is not int or not since <= remote["created"] <= int(at.timestamp()) + 60
            or remote.get("status") not in ({"succeeded"} if require_complete else {"succeeded", "pending", "requires_action"})
            or (part.get("refund_reference") and part["refund_reference"] != remote["id"])
            or (remote["status"] == "succeeded" and proof["amount_refunded"] != amount)):
        raise FinanceConflict("既存返金の全額・元決済・成功を確認できません")


async def _inspect(backend, store, stripe, order, at):
    parts = _parts(store, order)
    keys = [f"checkout-{order['id']}", f"refund-{order['id']}"]
    for extra in order.get("pending_extras", []):
        keys.extend((f"extra-{extra['id']}", f"refund-extra-{order['id']}-{extra['id']}"))
    for key in keys:
        operation = await backend.operation_details(key)
        if operation and operation["status"] in {"pending", "unknown"}:
            raise FinanceConflict("結果不明の購入・返金を先に照合してください")
    existing, missing, identity = [], [], []
    for extra, part, amount in parts:
        data = stripe.refund_data(part["payment_reference"], order["id"], extra_id=extra["id"] if extra else None)
        key = data.pop("_idempotency_key")
        operation = await backend.operation_details(key)
        if operation and (operation["status"] != "succeeded" or operation["endpoint"] != "refunds"
                          or operation["request_hash"] != content_hash(data)):
            raise FinanceConflict("既存の返金要求が不明・失敗・不一致です。再送せず先に照合してください")
        proof = await sandbox_payment_proof(stripe, order, extra, part["payment_reference"], amount, refund=True)
        # No created filter: an old/manual refund must also prevent a new POST.
        objects = await stripe.list_financial_objects("refunds", params={"payment_intent": part["payment_reference"]})
        if not operation:
            if (objects or proof["amount_refunded"] or part.get("refund_reference")
                    or part.get("refunded_amount" if extra else "primary_refunded_amount", 0)
                    or part.get("refund_status" if extra else "primary_refund_status")):
                raise FinanceConflict("未要求の明細に返金記録があります。新しい返金は作成しません")
            missing.append((extra, part, amount, key))
            remote = None
        else:
            result = await backend.operation_result(key)
            if len(objects) != 1 or not result or (result.get("response") or {}).get("id") != objects[0].get("id"):
                raise FinanceConflict("既存返金と台帳が一意に一致しません")
            remote = objects[0]
            _refund_fact(remote, order, extra, part, amount, proof, at, since=int(operation["created_at"].timestamp()) - 60)
            existing.append((extra, remote))
        identity.append({"key": key, "request_hash": content_hash(data), "payment": proof,
                         "refund": {k: remote.get(k) for k in ("id", "amount", "currency", "payment_intent", "charge", "metadata", "status", "created")} if remote else None})
    if not existing:
        raise FinanceConflict("再開対象の既存返金がありません")
    fingerprint = content_hash({"parts": identity, "amount": order["amount"], "status": order["status"],
                                "accepted_by": order["refund_cancel_accepted_by"]})
    return existing, missing, fingerprint


async def _bounded_inspect(*args):
    try:
        async with asyncio.timeout(30):
            return await _inspect(*args)
    except TimeoutError:
        raise FinanceConflict("返金明細の照合がタイムアウトしました。処理を進めず再確認してください") from None


async def propose_refund_resumption(backend, store, stripe, settings, order, actor_id, *, at=None):
    require_sandbox_review(backend, store, stripe, settings, actor_id)
    at = at or datetime.now(timezone.utc)
    _, missing, fingerprint = await _bounded_inspect(backend, store, stripe, order, at)
    proposal = {"id": str(uuid4()), "status": "proposed", "proposed_by": actor_id, "proposed_at": at,
                "fingerprint": fingerprint, "next_operation": missing[0][3] if missing else "",
                "remaining_count": len(missing), "next_amount": missing[0][2] if missing else 0}
    order["refund_resumption"] = proposal
    store.audit(actor_id, "refund.resumption_proposed", f"{order['id']}:{proposal['next_operation'] or 'state-only'}")
    await backend.save()
    return proposal


async def approve_refund_resumption(backend, store, stripe, settings, order, actor_id, proposal_id, email_sender, *, at=None):
    require_sandbox_review(backend, store, stripe, settings, actor_id)
    at = at or datetime.now(timezone.utc)
    proposal = order.get("refund_resumption") or {}
    if (proposal.get("status") != "proposed" or proposal.get("id") != proposal_id
            or proposal.get("proposed_by") == actor_id or proposal.get("proposed_by") not in settings.admin_user_ids
            or not isinstance(proposal.get("proposed_at"), datetime) or not proposal["proposed_at"].tzinfo
            or not timedelta(0) <= at - proposal["proposed_at"] <= timedelta(minutes=30)):
        raise FinanceConflict("別の管理者が30分以内に返金再開を承認してください")
    existing, missing, fingerprint = await _bounded_inspect(backend, store, stripe, order, at)
    if fingerprint != proposal["fingerprint"] or (missing[0][3] if missing else "") != proposal["next_operation"]:
        raise FinanceConflict("返金明細が変更されています。再確認してください")
    proposal.update(status="executing", approved_by=actor_id, approved_at=at)
    store.audit(actor_id, "refund.resumption_approved", f"{order['id']}:{proposal['next_operation'] or 'state-only'}:{proposal['proposed_by']}")
    for extra, remote in existing:
        (extra or order)["refund_reference"] = remote["id"]
        for message in apply_refund_payment(store, order, extra, remote, "refund.updated"):
            await email_sender(*message)
    # Persist approval before external I/O; StripeIntegration then commits the
    # new intent before POST. A crash never turns this into an automatic retry.
    await backend.save()
    if missing:
        extra, part, amount, key = missing[0]
        try:
            remote = await stripe.refund_payment(part["payment_reference"], order["id"], extra_id=extra["id"] if extra else None)
            proof = await sandbox_payment_proof(stripe, order, extra, part["payment_reference"], amount, refund=True)
            _refund_fact(remote, order, extra, part, amount, proof, at, require_complete=False)
            part["refund_reference"] = remote["id"]
            for message in apply_refund_payment(store, order, extra, remote, "refund.updated"):
                await email_sender(*message)
        except (StripeOutcomeUnknown, FinanceConflict, RuntimeError):
            order.update(refund_status="review", payment_reconciliation_required=True)
            proposal["status"] = "review"
            store.audit(actor_id, "refund.resumption_review", f"{order['id']}:{key}")
            await backend.save()
            raise
    order["payment_reconciliation_required"] = False
    proposal["status"] = "executed"
    await backend.save()
    return order["refund_status"]


def refund_resumption_inventory(store):
    candidates = [o for o in store.orders if o.get("refund_cancel_accepted_by")
                  and o.get("refund_status") in {"processing", "review", "partial", "pending"}]
    return {"items": [{"order_id": o["id"], "tool_name": o["tool_name"], "proposal": o.get("refund_resumption")}
                      for o in candidates[:100]], "truncated": len(candidates) > 100}
