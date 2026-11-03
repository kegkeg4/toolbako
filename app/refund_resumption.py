"""Four-eyes full cancellation; live execution is explicitly default-off.

This is NOT unknown-operation recovery. A new POST is allowed only for a part
with NO durable intent, NO local refund facts and NO provider refunds. Every
previous intent must be a verified, fully succeeded refund. One approval sends
at most one new refund, using the original deterministic operation key.

The separate live inspection entrypoint performs authenticated GETs under a
bound database request, never persists an approval or sends a financial POST.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx

from .config import Settings
from .data import DemoStore
from .database import PostgresStateStore, content_hash, restore
from .finance import FinanceConflict, RESERVED
from .financial_scope import FinancialScope, configured_scope, key_mode
from .integrations import StripeIntegration, StripeOutcomeUnknown
from .mfa import SupabaseMFA
from .security import SessionService
from .payment_reconciliation import (
    require_sandbox_review, payment_metadata_matches, sandbox_payment_proof,
    refund_request_data, live_refund_review_record,
)
from .payment_source_proof import payment_source_proof
from .payment_state import apply_refund_payment
from .live_setup_plan import STRIPE_API_VERSION


def _parts(store, order):
    if ("paid_out_refund_case" in order
            or order.get("billing_type", "one_time") != "one_time" or order.get("payment_status") != "paid"
            or order.get("status") not in {"cancel_pending", "cancelled"}
            or order.get("refund_status") not in {"awaiting_approval", "processing", "review", "partial", "pending"}
            or order.get("refund_cancel_accepted_by") not in {order["buyer_id"], order["seller_id"]}
            or order.get("cancel_requested_by") not in {order["buyer_id"], order["seller_id"]}
            or order.get("cancel_requested_by") == order.get("refund_cancel_accepted_by")
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


def _refund_fact(remote, order, extra, part, amount, proof, at, *, since=0, require_complete=True, scope=None):
    if (remote.get("object") != "refund" or not str(remote.get("id", "")).startswith("re_")
            or type(remote.get("amount")) is not int or remote["amount"] != amount
            or remote.get("currency") != "jpy" or remote.get("payment_intent") != part["payment_reference"]
            or remote.get("charge") != proof["charge"] or not payment_metadata_matches(remote.get("metadata"), order, extra)
            or type(remote.get("created")) is not int or not since <= remote["created"] <= int(at.timestamp()) + 60
            or remote.get("status") not in ({"succeeded"} if require_complete else {"succeeded", "pending", "requires_action"})
            or (part.get("refund_reference") and part["refund_reference"] != remote["id"])
            or (remote["status"] == "succeeded" and proof["amount_refunded"] != amount)):
        raise FinanceConflict("既存返金の全額・元決済・成功を確認できません")
    if scope is not None and (remote["metadata"].get("ledger_id") != scope.ledger_id
                              or ("livemode" in remote and remote["livemode"] is not True)):
        raise FinanceConflict("返金の本番台帳が一致しません")


async def _source_proof(stripe, order, extra, payment_id, amount, scope):
    if scope is None:
        return await sandbox_payment_proof(stripe, order, extra, payment_id, amount, refund=True)
    payment = await stripe.retrieve_payment(payment_id)
    proof = payment_source_proof(payment, payment_id=payment_id, amount=amount,
                                order_id=order["id"], expected_mode="live", allow_refunded=True)
    charge = payment["latest_charge"]
    if (not payment_metadata_matches(payment.get("metadata"), order, extra)
            or payment["metadata"].get("ledger_id") != scope.ledger_id
            or not payment_metadata_matches(charge.get("metadata"), order, extra)
            or charge["metadata"].get("ledger_id") != scope.ledger_id
            or charge.get("transfer_group") != f"order-{order['id']}"
            or charge.get("refunded") is not (proof["amount_refunded"] == amount)):
        raise FinanceConflict("元決済・元請求の本番台帳が一致しません")
    return proof


async def _inspect(backend, store, stripe, order, at, scope=None):
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
        key, data = refund_request_data(stripe, order, extra, amount, scope=scope)
        operation = await backend.operation_details(key)
        if operation:
            _, data = refund_request_data(stripe, order, extra, amount, operation=operation, scope=scope)
        if operation and (operation["status"] != "succeeded" or operation["endpoint"] != "refunds"
                          or operation["request_hash"] != content_hash(data)):
            raise FinanceConflict("既存の返金要求が不明・失敗・不一致です。再送せず先に照合してください")
        proof = await _source_proof(stripe, order, extra, part["payment_reference"], amount, scope)
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
            _refund_fact(remote, order, extra, part, amount, proof, at,
                         since=int(operation["created_at"].timestamp()) - 60, scope=scope)
            existing.append((extra, remote))
        identity.append({"key": key, "request_hash": content_hash(data), "payment": proof,
                         "refund": {k: remote.get(k) for k in ("id", "amount", "currency", "payment_intent", "charge", "metadata", "status", "created")} if remote else None})
    if not existing and order.get("refund_status") != "awaiting_approval":
        raise FinanceConflict("再開対象の既存返金がありません")
    fingerprint = content_hash({"parts": identity, "amount": order["amount"], "status": order["status"],
                                "refund_status": order["refund_status"],
                                "buyer": order["buyer_id"], "seller": order["seller_id"],
                                "requested_by": order["cancel_requested_by"],
                                "accepted_by": order["refund_cancel_accepted_by"],
                                **({"scope": scope.payload()} if scope is not None else {})})
    return existing, missing, fingerprint


async def _bounded_inspect(*args):
    try:
        async with asyncio.timeout(30):
            return await _inspect(*args)
    except TimeoutError:
        raise FinanceConflict("返金明細の照合がタイムアウトしました。処理を進めず再確認してください") from None


async def live_refund_context(backend, store, stripe, settings, mfa, request):
    """Revalidate the real current session and bound journal; never a token."""
    if (not isinstance(settings, Settings) or not isinstance(store, DemoStore)
            or not isinstance(backend, PostgresStateStore) or backend._store is not store
            or backend._connection is None or backend.dsn != settings.database_url
            or settings.demo_mode or settings.stripe_charge_mode != "separate"
            or not isinstance(stripe, StripeIntegration) or stripe.journal is not backend
            or stripe.secret_key != settings.stripe_secret_key or stripe.charge_mode != "separate"
            or not isinstance(mfa, SupabaseMFA) or mfa.settings is not settings
            or not isinstance(mfa.sessions, SessionService) or mfa.sessions.store is not store
            or request is None):
        raise FinanceConflict("本番返金の認証済み照合環境を確認できません")
    admins = {u["id"] for u in store.registered_users.values()
              if u.get("id") in settings.admin_user_ids and not u.get("is_banned")
              and not u.get("deleted_at") and not u.get("erased_at")}
    user = mfa.sessions.current(request)
    if not user or user["id"] not in admins or len(admins) < 2 or not mfa.recent(request):
        raise FinanceConflict("本番返金の照合には追加認証済みの有効な管理者が必要です")
    scope = configured_scope(settings)
    if not isinstance(scope, FinancialScope) or scope.mode != "live" or stripe.financial_scope != scope:
        raise FinanceConflict("本番返金の取引台帳を確認できません")
    await backend.assert_financial_scope(scope)
    await stripe.verify_financial_scope()
    return user["id"], admins, scope


async def inspect_refund_resumption_live(backend, store, stripe, settings, mfa, request, *, order_id, at=None):
    """Fresh live GET evidence, NOT a permission token or a consumed approval.

    Caller holds backend.request(store). This function does not save, audit,
    reconcile a journal, send mail or POST. Missing/partial/unknown/paid-out cases
    remain held by the same rules as the Sandbox resumption path. Future live
    checkout must stamp order/extra/ledger metadata on both PI and Charge;
    unscoped historical payments cannot be relabelled by this inspection.
    """
    _, _, scope = await live_refund_context(backend, store, stripe, settings, mfa, request)
    orders = [o for o in store.orders if o.get("id") == order_id]
    if len(orders) != 1:
        raise FinanceConflict("返金対象の注文を一意に確認できません")
    at = at or datetime.now(timezone.utc)
    if not isinstance(at, datetime) or at.tzinfo is None:
        raise FinanceConflict("返金照合の日時を確認できません")
    existing, missing, fingerprint = await _bounded_inspect(backend, store, stripe, orders[0], at, scope)
    return {"scope": scope.payload(), "order_id": order_id, "fingerprint": fingerprint,
            "observed_at": at, "verified_refund_count": len(existing),
            "remaining_count": len(missing), "remaining_amount": sum(item[2] for item in missing),
            "execution_allowed": False}


def _live_execution_enabled(settings, stripe):
    # No new runtime mode/flag is activated by this domain module. Missing
    # configuration is OFF; read-only and seller-setup modes remain non-financial.
    if (settings.stripe_connection_mode != "live_financial"
            or getattr(settings, "stripe_live_refunds_enabled", False) is not True
            or settings.stripe_api_version != STRIPE_API_VERSION
            or stripe.api_version != STRIPE_API_VERSION):
        raise FinanceConflict("本番返金の実行は有効になっていません")


async def _review_context(backend, store, stripe, settings, order, actor_id, mfa, request):
    if key_mode(stripe.secret_key) != "live":
        require_sandbox_review(backend, store, stripe, settings, actor_id)
        return None, set(settings.admin_user_ids)
    _live_execution_enabled(settings, stripe)
    actor, admins, scope = await live_refund_context(backend, store, stripe, settings, mfa, request)
    if (actor != actor_id or request.method != "POST"
            or sum(o.get("id") == order.get("id") for o in store.orders) != 1
            or not any(o is order for o in store.orders) or backend._emails):
        raise FinanceConflict("本番返金の現在の管理者・注文を確認できません")
    return scope, admins


async def _live_liquidity(backend, store, stripe, scope, amount, *, current_operation=None, verified_order_id=None):
    # Unknown money movements cannot be treated as a zero reservation. Hold
    # new refunds conservatively until the ORIGINAL intent is reconciled.
    uncertain = await (await backend._connection.execute(
        "select operation_id from toolbako_runtime.stripe_operations where status in ('pending','unknown') "
        "and (endpoint in ('refunds','transfers','payouts') or endpoint like 'transfers/%%/reversals') "
        "and operation_id<>%s limit 1", (current_operation or "",),
    )).fetchone()
    if uncertain:
        raise FinanceConflict("結果不明の金融処理を照合するまで本番返金を保留します")
    reserved, reserve_ids = 0, set()
    from .paid_out_refund_execution import _verify_opening
    for other in store.orders:
        if "paid_out_refund_case" in other:
            case = other["paid_out_refund_case"]
            if not isinstance(case, dict) or case.get("scope") != scope.payload():
                raise FinanceConflict("既存の返金原資予約を確認できません")
            await _verify_opening(backend, case)
            reserve_id = case["funding"]["reserve_id"]
            if reserve_id in reserve_ids:
                raise FinanceConflict("既存の返金原資予約が重複しています")
            reserve_ids.add(reserve_id)
            if case.get("buyer_refund", {}).get("state") != "complete":
                value = case["case"]["refund_amount"]
                if type(value) is not int or value <= 0:
                    raise FinanceConflict("既存の返金原資予約額を確認できません")
                reserved += value
        elif other.get("id") != verified_order_id and (other.get("primary_refund_status") in {"pending", "partial"}
              or any(x.get("refund_status") in {"pending", "partial"} for x in other.get("pending_extras", []))):
            raise FinanceConflict("未確定の返金があるため、新しい返金要求を保留します")
    balance = await stripe._get("balance")
    values = balance.get("available") if isinstance(balance, dict) else None
    yen = [item for item in values if isinstance(item, dict) and item.get("currency") == "jpy"] if isinstance(values, list) else []
    if (not isinstance(balance, dict) or balance.get("object") != "balance" or balance.get("livemode") is not True or len(yen) != 1
            or type(yen[0].get("amount")) is not int or yen[0]["amount"] < amount + reserved):
        raise FinanceConflict("既存の返金原資予約を守った本番JPY残高を確認できません")


async def _hold_live_refund(backend, store, order, proposal, key, *, status="unknown"):
    details = await backend.operation_details(key)
    if details and details["status"] == "pending":
        await backend.finish_operation(key, status)
    order.update(refund_status="review", payment_reconciliation_required=True)
    proposal["status"] = "review"
    await backend.save()


async def _send_live_refund(backend, store, stripe, settings, mfa, request, order, proposal,
                            scope, extra, part, amount, key, data):
    """Only this consumed review's exact refund, never arbitrary transport."""
    try:
        _live_execution_enabled(settings, stripe)
        actor, admins, current_scope = await live_refund_context(backend, store, stripe, settings, mfa, request)
        canonical_key, canonical = refund_request_data(stripe, order, extra, amount, scope=scope)
        if (request.method != "POST" or current_scope != scope or actor != proposal["approved_by"]
                or proposal["proposed_by"] not in admins or proposal["proposed_by"] == actor
                or proposal["scope"] != scope.payload() or proposal["status"] != "executing"
                or not timedelta(0) <= datetime.now(timezone.utc) - proposal["proposed_at"] <= timedelta(minutes=30)
                or canonical_key != key or canonical != data or proposal["next_request_hash"] != content_hash(data)):
            raise FinanceConflict("本番返金の現在の承認・元要求を確認できません")
        details = await backend.operation_details(key)
        if (not details or details["status"] != "pending" or details["endpoint"] != "refunds"
                or details["request_hash"] != content_hash(data)):
            raise FinanceConflict("本番返金の保存済み要求を確認できません")
        proof = await _source_proof(stripe, order, extra, part["payment_reference"], amount, scope)
        refunds = await stripe.list_financial_objects("refunds", params={"payment_intent": part["payment_reference"]})
        if proof["amount_refunded"] or refunds:
            raise FinanceConflict("要求前に返金状態が変わったため再送せず確認してください")
        await _live_liquidity(backend, store, stripe, scope, amount, current_operation=key, verified_order_id=order["id"])
        # No generic live POST gate is removed. Path/account/method/body/key
        # are fixed here, after consumed approval + pending intent committed.
        headers = {"Authorization": f"Bearer {stripe.secret_key}", "Idempotency-Key": key,
                   "Stripe-Version": STRIPE_API_VERSION}
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            # The preceding provider GETs can outlast MFA/review validity.
            # Recheck locally immediately before the only money-moving call.
            user = mfa.sessions.current(request)
            active = {u["id"] for u in store.registered_users.values()
                      if u.get("id") in settings.admin_user_ids and not u.get("is_banned")
                      and not u.get("deleted_at") and not u.get("erased_at")}
            if (not user or user["id"] != actor or actor not in active
                    or proposal["proposed_by"] not in active or len(active) < 2 or not mfa.recent(request)
                    or not timedelta(0) <= datetime.now(timezone.utc) - proposal["proposed_at"] <= timedelta(minutes=30)):
                raise FinanceConflict("本番返金の追加認証・承認期限が切れています")
            response = await client.post("https://api.stripe.com/v1/refunds", data=data, headers=headers)
        if response.status_code in {400, 401, 403, 404, 422}:
            await _hold_live_refund(backend, store, order, proposal, key, status="rejected")
            raise FinanceConflict("本番返金要求は受け付けられませんでした。再送せず確認してください")
        if response.status_code not in {200, 201}:
            raise ValueError("unknown refund response")
        remote = response.json()
        if not isinstance(remote, dict):
            raise ValueError("invalid refund response")
        proof = await _source_proof(stripe, order, extra, part["payment_reference"], amount, scope)
        _refund_fact(remote, order, extra, part, amount, proof, datetime.now(timezone.utc),
                     since=int(details["created_at"].timestamp()) - 60, require_complete=False, scope=scope)
        return {name: remote[name] for name in ("id", "object", "amount", "currency", "payment_intent", "charge", "metadata", "status", "created")}
    except Exception:
        # Even a pre-send failure after intent commit stays held. A zero-result
        # GET is never permission to POST this or a replacement key again.
        details = await backend.operation_details(key)
        if details and details["status"] == "rejected":
            raise FinanceConflict("本番返金要求は拒否されています。再送せず確認してください") from None
        await _hold_live_refund(backend, store, order, proposal, key)
        raise StripeOutcomeUnknown(key) from None


async def _approve_live_refund(backend, store, stripe, settings, order, actor_id, proposal_id,
                               email_sender, mfa, request, scope, admins, at):
    proposal = order.get("refund_resumption") or {}
    if (proposal.get("status") != "proposed" or proposal.get("id") != proposal_id
            or proposal.get("scope") != scope.payload() or proposal.get("proposed_by") == actor_id
            or proposal.get("proposed_by") not in admins
            or not isinstance(proposal.get("proposed_at"), datetime) or not proposal["proposed_at"].tzinfo
            or not timedelta(0) <= at - proposal["proposed_at"] <= timedelta(minutes=30)):
        raise FinanceConflict("有効な別管理者が30分以内の本番返金要求を承認してください")
    await live_refund_review_record(backend, store, order, proposal, kind="resumption")
    existing, missing, fingerprint = await _bounded_inspect(backend, store, stripe, order, at, scope)
    if fingerprint != proposal["fingerprint"] or (missing[0][3] if missing else "") != proposal["next_operation"]:
        raise FinanceConflict("本番返金の明細が変わりました。再確認してください")
    if missing:
        extra, part, amount, key = missing[0]
        _, data = refund_request_data(stripe, order, extra, amount, scope=scope)
        if proposal["next_amount"] != amount or proposal["next_request_hash"] != content_hash(data):
            raise FinanceConflict("本番返金の承認金額・要求内容が変わっています")
        await _live_liquidity(backend, store, stripe, scope, amount, verified_order_id=order["id"])
    proposal.update(status="executing", approved_by=actor_id, approved_at=at)
    store.audit(actor_id, "refund.live_resumption_approved", f"{order['id']}:{proposal['next_operation']}:{proposal['proposed_by']}")
    try:
        for previous_extra, remote in existing:
            (previous_extra or order)["refund_reference"] = remote["id"]
            for message in apply_refund_payment(store, order, previous_extra, remote, "refund.updated"):
                await email_sender(*message)
        if not missing:
            proposal["status"] = "executed"
            order["payment_reconciliation_required"] = False
            await backend.save()
            return order["refund_status"]
        cached = await backend.begin_operation(key, "refunds", data)
        if cached is not None:
            raise FinanceConflict("本番返金要求は既に存在します")
    except Exception:
        if backend._checkpoint is not None:
            restore(store, backend._checkpoint)
        backend._emails.clear()
        raise
    remote = await _send_live_refund(backend, store, stripe, settings, mfa, request, order, proposal,
                                     scope, extra, part, amount, key, data)
    try:
        part["refund_reference"] = remote["id"]
        for message in apply_refund_payment(store, order, extra, remote, "refund.updated"):
            await email_sender(*message)
        proposal["status"] = "executed"
        order["payment_reconciliation_required"] = False
        # Provider result, consumed approval, order and queued notifications commit
        # together. A lost acknowledgement leaves this same intent recoverable.
        await backend.reconcile_operation(key, endpoint="refunds", request_hash=content_hash(data), response=remote)
    except Exception:
        # Also safe if an embedding route catches the error and saves a 409.
        if backend._checkpoint is not None:
            restore(store, backend._checkpoint)
        backend._emails.clear()
        raise
    return order["refund_status"]


async def propose_refund_resumption(backend, store, stripe, settings, order, actor_id, *, at=None, mfa=None, request=None):
    scope, _ = await _review_context(backend, store, stripe, settings, order, actor_id, mfa, request)
    at = at or datetime.now(timezone.utc)
    _, missing, fingerprint = await _bounded_inspect(backend, store, stripe, order, at, scope)
    proposal = {"id": str(uuid4()), "status": "proposed", "proposed_by": actor_id, "proposed_at": at,
                "fingerprint": fingerprint, "next_operation": missing[0][3] if missing else "",
                "remaining_count": len(missing), "next_amount": missing[0][2] if missing else 0}
    if scope is not None:
        if missing:
            extra, part, amount, key = missing[0]
            _, data = refund_request_data(stripe, order, extra, amount, scope=scope)
            await _live_liquidity(backend, store, stripe, scope, amount, verified_order_id=order["id"])
        proposal.update(scope=scope.payload(), next_request_hash=content_hash(data) if missing else "")
    order["refund_resumption"] = proposal
    store.audit(actor_id, "refund.resumption_proposed", f"{order['id']}:{proposal['next_operation'] or 'state-only'}")
    if scope is not None:
        await live_refund_review_record(backend, store, order, proposal, kind="resumption", create=True)
    else:
        await backend.save()
    return proposal


async def approve_refund_resumption(backend, store, stripe, settings, order, actor_id, proposal_id, email_sender, *, at=None, mfa=None, request=None):
    scope, admins = await _review_context(backend, store, stripe, settings, order, actor_id, mfa, request)
    at = at or datetime.now(timezone.utc)
    if scope is not None:
        return await _approve_live_refund(backend, store, stripe, settings, order, actor_id, proposal_id,
                                          email_sender, mfa, request, scope, admins, at)
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
            remote = await stripe.refund_payment(part["payment_reference"], order["id"], amount=amount,
                                                extra_id=extra["id"] if extra else None)
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
                  and o.get("refund_status") in {"awaiting_approval", "processing", "review", "partial", "pending"}]
    return {"items": [{"order_id": o["id"], "tool_name": o["tool_name"], "proposal": o.get("refund_resumption")}
                      for o in candidates[:100]], "truncated": len(candidates) > 100}
