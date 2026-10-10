"""Sandbox-only, two-person recovery of uncertain Checkout/full refunds.

GETs never authorize another POST, even if a complete list has no match.
Provider proofs are re-fetched at approval. Journal, state, audit and email
outbox are committed together; no synthetic webhook events are generated.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from uuid import uuid4

from .database import content_hash
from .finance import FinanceConflict, RESERVED
from .payment_state import apply_checkout_payment, apply_checkout_expiry, apply_refund_payment
from .payout_worker import sandbox_payouts_ready


KINDS = {"checkout": "購入決済", "extra": "追加支払い", "refund": "購入分の返金", "refund_extra": "追加支払いの返金"}


def require_sandbox_review(backend, store, stripe, settings, actor_id):
    if (not sandbox_payouts_ready(settings) or stripe.journal is not backend
            or stripe.secret_key != settings.stripe_secret_key or stripe.charge_mode != "separate"
            or actor_id not in settings.admin_user_ids or len(set(settings.admin_user_ids)) < 2
            or backend._store is not store):
        raise FinanceConflict("接続済みのテスト環境と、別々の管理者2名が必要です")


def _part(order, kind, extra_id):
    if kind not in KINDS or (kind in {"extra", "refund_extra"}) != bool(extra_id):
        raise FinanceConflict("照合対象の種類が一致しません")
    extras = [x for x in order.get("pending_extras", []) if x.get("id") == extra_id] if extra_id else []
    if extra_id and len(extras) != 1:
        raise FinanceConflict("追加支払いの明細を確認できません")
    return extras[0] if extras else None


def _key(order, kind, extra):
    if kind == "checkout": return f"checkout-{order['id']}"
    if kind == "extra": return f"extra-{extra['id']}"
    if kind == "refund": return f"refund-{order['id']}"
    return f"refund-extra-{order['id']}-{extra['id']}"


async def _target(backend, store, stripe, order, kind, extra_id):
    extra = _part(order, kind, extra_id)
    part = extra or order
    amount = extra["amount"] if extra else order.get("primary_payment_amount", order["amount"])
    if order.get("billing_type", "one_time") != "one_time" or type(amount) is not int or amount <= 0:
        raise FinanceConflict("単発の有料決済だけ照合できます")
    if kind in {"checkout", "extra"}:
        if (order.get("status") in {"cancelled", "cancel_pending"} or order.get("refund_status") or order.get("dispute_status")
                or (extra and order.get("payment_status") != "paid")
                or (extra and extra.get("status") != "pending")
                or (not extra and order.get("payment_status") != "pending")):
            raise FinanceConflict("購入の状態が変更されています。運営で詳細確認してください")
        destination = (store.connected_accounts.get(order["seller_id"]) or {}).get("account_id", "")
        data = stripe.extra_checkout_data(order, extra, destination) if extra else stripe.checkout_data(order, destination)
        endpoint = "checkout/sessions"
    else:
        if ((extra and extra.get("status") != "paid") or order.get("payment_status") != "paid"
                or not isinstance(part.get("payment_reference"), str) or not part["payment_reference"].startswith("pi_")):
            raise FinanceConflict("返金元の支払済み決済を確認できません")
        if any(p.get("finance_v2") and p["status"] in RESERVED
               and any(a["order_id"] == order["id"] for a in p["allocations"]) for p in store.payouts):
            raise FinanceConflict("振込申請済みの売上です。運営で詳細確認してください")
        data = stripe.refund_data(part["payment_reference"], order["id"], extra_id=extra_id or None)
        endpoint = "refunds"
    operation_id = data.pop("_idempotency_key")
    operation = await backend.operation_details(operation_id)
    if not operation or operation["status"] not in {"pending", "unknown"}:
        raise FinanceConflict("結果不明な決済・返金がありません")
    if operation["endpoint"] != endpoint or operation["request_hash"] != content_hash(data):
        raise FinanceConflict("元の要求内容と一致しません。再送せず詳細確認してください")
    return extra, amount, data, operation_id, operation


def payment_metadata_matches(metadata, order, extra):
    return (isinstance(metadata, dict) and metadata.get("order_id") == order["id"]
            and (metadata.get("extra_id") == extra["id"] if extra else "extra_id" not in metadata))


async def sandbox_payment_proof(stripe, order, extra, payment_id, amount, *, refund=False):
    if not isinstance(payment_id, str) or not payment_id.startswith("pi_"):
        raise FinanceConflict("元決済の識別情報を確認できません")
    payment = await stripe.retrieve_payment(payment_id)
    if (payment.get("id") != payment_id or payment.get("object") != "payment_intent"
            or payment.get("livemode") is not False or payment.get("status") != "succeeded"
            or type(payment.get("amount")) is not int or payment["amount"] != amount
            or type(payment.get("amount_received")) is not int or payment["amount_received"] != amount
            or payment.get("currency") != "jpy" or not payment_metadata_matches(payment.get("metadata"), order, extra)
            or payment.get("transfer_group") != f"order-{order['id']}"
            or payment.get("transfer_data") is not None or payment.get("application_fee_amount") is not None
            or payment.get("on_behalf_of") is not None):
        raise FinanceConflict("元決済の金額・環境・注文が一致しません")
    charge = payment.get("latest_charge")
    if (not isinstance(charge, dict) or charge.get("object") != "charge"
            or not isinstance(charge.get("id"), str) or not charge["id"].startswith("ch_")
            or charge.get("livemode") is not False or charge.get("payment_intent") != payment_id
            or type(charge.get("amount")) is not int or charge["amount"] != amount
            or type(charge.get("amount_captured")) is not int or charge["amount_captured"] != amount
            or charge.get("currency") != "jpy" or charge.get("paid") is not True
            or charge.get("captured") is not True or charge.get("status") != "succeeded"
            or charge.get("disputed") is not False or charge.get("transfer_group") != f"order-{order['id']}"
            or type(charge.get("amount_refunded")) is not int or not 0 <= charge["amount_refunded"] <= amount
            or (not refund and (charge["amount_refunded"] != 0 or charge.get("refunded") is not False))):
        raise FinanceConflict("元請求の入金・返金・異議申し立てを確認できません")
    # Never retain a PaymentIntent client_secret or payer details in a proposal.
    return {"id": payment_id, "amount": amount, "charge": charge["id"],
            "amount_refunded": charge["amount_refunded"], "status": payment["status"]}


async def _inspect(backend, store, stripe, order, kind, extra_id, at):
    extra, amount, data, operation_id, operation = await _target(backend, store, stripe, order, kind, extra_id)
    params = {"created[gte]": str(int(operation["created_at"].timestamp()) - 60)}
    if operation["endpoint"] == "refunds": params["payment_intent"] = data["payment_intent"]
    objects = await stripe.list_financial_objects(operation["endpoint"], params=params)
    if any(not isinstance(obj.get("metadata"), dict) for obj in objects):
        raise FinanceConflict("Stripeの照合記録の形式を確認できません")
    matches = [obj for obj in objects if payment_metadata_matches(obj["metadata"], order, extra)]
    if len(matches) != 1:
        raise FinanceConflict("該当記録が0件または複数あります。再送せずStripeで詳細確認してください")
    remote = matches[0]
    if (type(remote.get("created")) is not int or remote["created"] < int(params["created[gte]"])
            or remote["created"] > int(at.timestamp()) + 60 or remote.get("currency") != "jpy"):
        raise FinanceConflict("照合記録の作成日時・通貨が一致しません")
    part = extra or order
    proof = None
    if operation["endpoint"] == "checkout/sessions":
        if (remote.get("object") != "checkout.session" or not str(remote.get("id", "")).startswith("cs_test_")
                or remote.get("livemode") is not False or remote.get("mode") != "payment"
                or type(remote.get("amount_total")) is not int or remote["amount_total"] != amount
                or remote.get("success_url") != data["success_url"] or remote.get("cancel_url") != data["cancel_url"]
                or (remote.get("customer_details") is not None and not isinstance(remote["customer_details"], dict))
                or (data.get("customer_email") and remote.get("customer_email") != data["customer_email"])
                or (part.get("checkout_session_id") and part["checkout_session_id"] != remote["id"])):
            raise FinanceConflict("購入決済の注文・金額・戻り先が一致しません")
        status, payment_status = remote.get("status"), remote.get("payment_status")
        if status == "complete" and payment_status == "paid":
            if part.get("payment_reference") and part["payment_reference"] != remote.get("payment_intent"):
                raise FinanceConflict("元決済が変更されています")
            proof = await sandbox_payment_proof(stripe, order, extra, remote.get("payment_intent"), amount)
        elif status == "open" and payment_status == "unpaid":
            url = remote.get("url")
            parsed = urlparse(url) if isinstance(url, str) else None
            if (not parsed or parsed.scheme != "https" or parsed.netloc != "checkout.stripe.com"
                    or not parsed.path.startswith("/c/") or type(remote.get("expires_at")) is not int
                    or remote["expires_at"] <= int(at.timestamp())):
                raise FinanceConflict("有効なStripeの購入画面を確認できません")
        elif status != "expired" or payment_status != "unpaid":
            raise FinanceConflict("入金の確定を確認できません。再送せず確認を続けてください")
        identity = {key: remote.get(key) for key in ("id", "metadata", "status", "payment_status", "amount_total", "currency", "payment_intent", "success_url", "cancel_url", "url", "expires_at", "livemode")}
    else:
        if (remote.get("object") != "refund" or not str(remote.get("id", "")).startswith("re_")
                or type(remote.get("amount")) is not int or remote["amount"] != amount
                or remote.get("payment_intent") != data["payment_intent"]
                or remote.get("status") not in {"succeeded", "pending", "requires_action", "failed", "canceled"}
                or (part.get("refund_reference") and part["refund_reference"] != remote["id"])):
            raise FinanceConflict("返金の注文・全額・元決済が一致しません")
        # Refund objects have no livemode attribute. Prove it on their parent PI.
        proof = await sandbox_payment_proof(stripe, order, extra, remote["payment_intent"], amount, refund=True)
        if remote.get("charge") != proof["charge"] or (remote["status"] == "succeeded" and proof["amount_refunded"] != amount):
            raise FinanceConflict("返金の元請求・確定額が一致しません")
        identity = {key: remote.get(key) for key in ("id", "metadata", "status", "amount", "currency", "payment_intent", "charge")}
    return extra, operation_id, operation, remote, content_hash({"remote": identity, "payment": proof})


async def _bounded_inspect(*args):
    try:
        async with asyncio.timeout(30):
            return await _inspect(*args)
    except TimeoutError:
        raise FinanceConflict("Stripeの照合がタイムアウトしました。再送せず再確認してください") from None


async def propose_payment_recovery(backend, store, stripe, settings, order, kind, actor_id, *, extra_id="", at=None):
    require_sandbox_review(backend, store, stripe, settings, actor_id)
    at = at or datetime.now(timezone.utc)
    extra, operation_id, operation, remote, fingerprint = await _bounded_inspect(backend, store, stripe, order, kind, extra_id, at)
    proposal = {"id": str(uuid4()), "status": "proposed", "proposed_by": actor_id, "proposed_at": at,
                "operation_id": operation_id, "endpoint": operation["endpoint"], "request_hash": operation["request_hash"],
                "provider_reference": remote["id"], "fingerprint": fingerprint, "kind": kind}
    (extra or order).setdefault("payment_reconciliations", {})[kind] = proposal
    store.audit(actor_id, "payment.reconciliation_proposed", f"{order['id']}:{operation_id}:{remote['id']}")
    await backend.save()
    return proposal


async def approve_payment_recovery(backend, store, stripe, settings, order, kind, actor_id, proposal_id, email_sender, *, extra_id="", at=None):
    require_sandbox_review(backend, store, stripe, settings, actor_id)
    extra = _part(order, kind, extra_id)
    proposal = (extra or order).get("payment_reconciliations", {}).get(kind, {})
    at = at or datetime.now(timezone.utc)
    if (proposal.get("status") != "proposed" or proposal.get("id") != proposal_id
            or proposal.get("proposed_by") == actor_id or proposal.get("proposed_by") not in settings.admin_user_ids
            or not isinstance(proposal.get("proposed_at"), datetime) or not proposal["proposed_at"].tzinfo
            or not timedelta(0) <= at - proposal["proposed_at"] <= timedelta(minutes=30)):
        raise FinanceConflict("別の管理者が30分以内の照合結果を確認してください。古い画面は再読み込みしてください")
    extra, operation_id, operation, remote, fingerprint = await _bounded_inspect(backend, store, stripe, order, kind, extra_id, at)
    if (operation_id != proposal["operation_id"] or operation["endpoint"] != proposal["endpoint"]
            or operation["request_hash"] != proposal["request_hash"] or remote["id"] != proposal["provider_reference"]
            or fingerprint != proposal["fingerprint"]):
        raise FinanceConflict("照合結果が変更されています。再確認が必要です")
    part = extra or order
    emails = []
    if kind in {"checkout", "extra"}:
        part.update(checkout_session_id=remote["id"], checkout_url=remote.get("url") if remote["status"] == "open" else "")
        if remote["status"] == "complete":
            # Recovery is about financial identity, not changing the account's
            # contact email based on payer details in a Checkout response.
            emails = apply_checkout_payment(store, settings, order, extra, {**remote, "customer_details": None})
        elif remote["status"] == "expired":
            apply_checkout_expiry(store, order, extra)
    else:
        part["refund_reference"] = remote["id"]
        emails = apply_refund_payment(store, order, extra, remote, "refund.updated")
    # Keep the hold if ANY other part remains uncertain, including a process
    # crash before the exception handler could mark it in business state.
    keys = [f"checkout-{order['id']}", f"refund-{order['id']}"]
    for item in order.get("pending_extras", []):
        keys.extend((f"extra-{item['id']}", f"refund-extra-{order['id']}-{item['id']}"))
    unresolved = False
    for key in keys:
        if key != operation_id:
            result = await backend.operation_result(key)
            unresolved |= bool(result and result["status"] in {"pending", "unknown"})
    order["payment_reconciliation_required"] = unresolved
    proposal.update(status="approved", approved_by=actor_id, approved_at=at)
    store.audit(actor_id, "payment.reconciliation_approved", f"{order['id']}:{operation_id}:{remote['id']}:{proposal['proposed_by']}")
    for message in emails:
        await email_sender(*message)  # In DB mode this queues, it never sends.
    await backend.reconcile_operation(operation_id, endpoint=operation["endpoint"], request_hash=operation["request_hash"], response=remote)
    return part.get("refund_status") if kind.startswith("refund") else remote["status"]


async def payment_recovery_inventory(backend, store):
    inventory = await backend.unresolved_payment_operations()
    known = {}
    for order in store.orders:
        for kind in ("checkout", "refund"):
            key = _key(order, kind, None)
            known[key] = None if key in known else (order, kind, None)
        for extra in order.get("pending_extras", []):
            for kind in ("extra", "refund_extra"):
                key = _key(order, kind, extra)
                # Duplicate identifiers are not silently resolved by order.
                known[key] = None if key in known else (order, kind, extra)
    items = []
    for operation in inventory["items"]:
        target = known.get(operation["id"])
        if target:
            order, kind, extra = target
            items.append({**operation, "order_id": order["id"], "tool_name": order["tool_name"], "kind": kind,
                          "label": KINDS[kind], "extra_id": extra["id"] if extra else "",
                          "proposal": (extra or order).get("payment_reconciliations", {}).get(kind)})
        else:
            items.append({**operation, "label": "注文との対応を運営で詳細確認してください"})
    return {"items": items, "truncated": inventory["truncated"]}
