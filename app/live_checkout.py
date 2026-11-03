"""Narrow default-off live Checkout, using the existing durable journal.

Only this lane can POST a fixed card-only separate-charge Checkout Session.
The request reservation commits first. Pending, rejected and unknown attempts
never acquire a replacement key or another POST; two-admin GET recovery owns
uncertain outcomes. Creating a Session does not mark an order paid or deliver it.
"""
from datetime import datetime, timezone
import re
from urllib.parse import urlparse

import httpx

from .config import Settings
from .database import content_hash, restore
from .deployment_check import _live_configuration_verified, _live_database_config
from .finance import FinanceConflict
from .financial_scope import FinancialScope
from .integrations import StripeOutcomeUnknown
from .live_payout_execution import service_context, _seller
from .live_setup_plan import STRIPE_API_VERSION
from .payment_source_proof import payment_source_proof
from .security import SessionService


ENDPOINT = "checkout/sessions"
ACCOUNT_FIELD = "live_checkout_account_id"
RECEIPT_FIELDS = ("id", "object", "livemode", "mode", "amount_total", "currency", "metadata",
                  "status", "payment_status", "payment_intent", "success_url", "cancel_url",
                  "url", "created", "expires_at")


def _require(ok):
    if not ok:
        raise FinanceConflict("本番購入の注文・接続先・決済記録を確認できません。再送せず確認してください")


def checkout_configuration_ready(settings):
    """Static configuration gate only, not proof of seller or payment health."""
    return (isinstance(settings, Settings) and not settings.demo_mode
            and settings.stripe_connection_mode == "live_financial"
            and getattr(settings, "stripe_live_checkout_enabled", False) is True
            and _live_configuration_verified(settings))


def _target_bound_connection(backend, settings):
    _require(checkout_configuration_ready(settings) and backend.dsn == settings.database_url)
    config = _live_database_config(settings)
    conn = backend._connection
    _require(conn is not None and conn.info.host == config["host"]
             and conn.info.user == config["user"] and conn.info.port == 5432
             and conn.info.dbname == "postgres" and conn.pgconn.ssl_in_use is True)


def operation_key(order, extra=None):
    return f"extra-{extra['id']}" if extra else f"checkout-{order['id']}"


def checkout_metadata(scope, order, extra=None, *, account_id=None):
    account = account_id if account_id is not None else (extra or order).get(ACCOUNT_FIELD)
    _require(isinstance(scope, FinancialScope) and scope.mode == "live"
             and isinstance(account, str) and re.fullmatch(r"acct_[A-Za-z0-9]+", account)
             and account != scope.platform_account)
    result = {"order_id": order["id"], "ledger_id": scope.ledger_id,
              "platform_account_id": scope.platform_account, "seller_account_id": account,
              "checkout_operation_id": operation_key(order, extra)}
    if extra:
        result["extra_id"] = extra["id"]
    return result


def checkout_request_data(stripe, scope, order, extra=None, *, account_id=None):
    """One canonical request for creation, replay, recovery and notifications."""
    amount = extra.get("amount") if extra else order.get("primary_payment_amount", order.get("amount"))
    _require(order.get("billing_type", "one_time") == "one_time" and type(amount) is int and amount > 0
             and stripe.charge_mode == "separate" and stripe.financial_scope == scope)
    snapshot = {**order, "amount": amount}
    data = stripe.extra_checkout_data(snapshot, extra, "") if extra else stripe.checkout_data(snapshot, "")
    key = data.pop("_idempotency_key")
    _require(key == operation_key(order, extra))
    base = urlparse(stripe.base_url)
    _require(base.scheme == "https" and base.hostname and not base.username and not base.password
             and not base.query and not base.fragment and base.path in {"", "/"})
    for name in ("success_url", "cancel_url"):
        parsed = urlparse(data[name])
        _require(parsed.scheme == "https" and parsed.netloc == base.netloc
                 and parsed.path.startswith("/") and not parsed.username and not parsed.password
                 and not parsed.fragment and "\\" not in data[name])
    # No payer contact data is needed for routing or a durable financial proof.
    data.pop("customer_email", None)
    data["payment_method_types[0]"] = "card"
    for name, value in checkout_metadata(scope, order, extra, account_id=account_id).items():
        data[f"metadata[{name}]"] = value
        data[f"payment_intent_data[metadata][{name}]"] = value
    return key, data


def _metadata_matches(actual, expected):
    return (isinstance(actual, dict) and all(actual.get(k) == v for k, v in expected.items())
            and ("extra_id" in expected or "extra_id" not in actual)
            and "paid_out_case_id" not in actual)


def session_receipt(remote):
    """Never persist customer details, client secrets or arbitrary API fields."""
    receipt = {name: remote.get(name) for name in RECEIPT_FIELDS}
    receipt["metadata"] = {k: v for k, v in (remote.get("metadata") or {}).items()
        if k in {"order_id", "extra_id", "ledger_id", "platform_account_id", "seller_account_id", "checkout_operation_id"}}
    return receipt


async def checkout_session_proof(stripe, scope, order, extra, remote, data, *, at=None,
                                 since=None, allow_refunded=False):
    at = at or datetime.now(timezone.utc)
    part = extra or order
    metadata = checkout_metadata(scope, order, extra)
    _require(isinstance(remote, dict) and remote.get("object") == "checkout.session"
             and isinstance(remote.get("id"), str) and re.fullmatch(r"cs_live_[A-Za-z0-9]+", remote["id"])
             and remote.get("livemode") is True and remote.get("mode") == "payment"
             and remote.get("currency") == "jpy" and type(remote.get("amount_total")) is int
             and remote["amount_total"] == int(data["line_items[0][price_data][unit_amount]"])
             and remote.get("success_url") == data["success_url"] and remote.get("cancel_url") == data["cancel_url"]
             and _metadata_matches(remote.get("metadata"), metadata)
             and (not part.get("checkout_session_id") or part["checkout_session_id"] == remote["id"])
             and type(remote.get("created")) is int and remote["created"] <= int(at.timestamp()) + 60
             and (since is None or remote["created"] >= since - 60))
    status = remote.get("status"), remote.get("payment_status")
    _require(status in {("open", "unpaid"), ("complete", "paid"), ("complete", "unpaid"), ("expired", "unpaid")})
    proof = None
    if status == ("open", "unpaid"):
        url = remote.get("url")
        parsed = urlparse(url) if isinstance(url, str) else None
        _require(parsed is not None and parsed.scheme == "https" and parsed.netloc == "checkout.stripe.com"
                 and parsed.path.startswith("/c/") and not parsed.username and not parsed.password
                 and "\\" not in url and type(remote.get("expires_at")) is int
                 and remote["expires_at"] > int(at.timestamp()))
    if status == ("complete", "paid"):
        payment_id = remote.get("payment_intent")
        _require(isinstance(payment_id, str) and re.fullmatch(r"pi_[A-Za-z0-9]+", payment_id)
                 and (not part.get("payment_reference") or part["payment_reference"] == payment_id))
        payment = await stripe.retrieve_payment(payment_id)
        proof = payment_source_proof(payment, payment_id=payment_id, amount=remote["amount_total"],
            order_id=order["id"], expected_mode="live", allow_refunded=allow_refunded)
        charge = payment["latest_charge"]
        _require(_metadata_matches(payment.get("metadata"), metadata)
                 and _metadata_matches(charge.get("metadata"), metadata)
                 and charge.get("transfer_group") == f"order-{order['id']}")
    return proof


async def validate_live_checkout_receipt(backend, stripe, scope, order, extra, remote):
    """Require the exact committed intent + receipt before notification effects."""
    key, data = checkout_request_data(stripe, scope, order, extra)
    details, result = await backend.operation_details(key), await backend.operation_result(key)
    _require(details is not None and details["endpoint"] == ENDPOINT and details["status"] == "succeeded"
             and details["request_hash"] == content_hash(data)
             and result is not None and result["status"] == "succeeded")
    receipt = result.get("response") or {}
    # Status/URL can legitimately change after creation. Identity cannot.
    immutable = ("id", "object", "livemode", "mode", "amount_total", "currency", "metadata",
                 "success_url", "cancel_url", "created", "expires_at")
    current = session_receipt(remote)
    _require(all(receipt.get(k) == current.get(k) for k in immutable))
    await checkout_session_proof(stripe, scope, order, extra, remote, data,
        since=int(details["created_at"].timestamp()), allow_refunded=True)
    return remote["id"]


async def _extra_parent_proof(stripe, scope, order):
    """A delayed refund/dispute notification cannot authorize a new extra."""
    payment_id = order.get("payment_reference")
    _require(isinstance(payment_id, str) and re.fullmatch(r"pi_[A-Za-z0-9]+", payment_id))
    payment = await stripe.retrieve_payment(payment_id)
    payment_source_proof(payment, payment_id=payment_id,
        amount=order.get("primary_payment_amount", order.get("amount")),
        order_id=order["id"], expected_mode="live", allow_refunded=False)
    metadata = checkout_metadata(scope, order)
    _require(_metadata_matches(payment.get("metadata"), metadata)
             and _metadata_matches(payment["latest_charge"].get("metadata"), metadata)
             and payment["latest_charge"].get("transfer_group") == f"order-{order['id']}")


def _buyer(store, sessions, request, order, extra):
    _require(isinstance(sessions, SessionService) and sessions.store is store and request.method == "POST")
    user = sessions.current(request)
    _require(isinstance(user, dict) and user.get("id") == order.get("buyer_id")
             and user["id"] != order.get("seller_id")
             and sum(u.get("id") == user["id"] for u in store.registered_users.values()) == 1
             and sum(u.get("id") == order.get("seller_id") and not u.get("is_banned")
                     and not u.get("deleted_at") and not u.get("erased_at")
                     for u in store.registered_users.values()) == 1
             and sum(u.get("id") == user["id"] and not u.get("is_banned")
                     and not u.get("deleted_at") and not u.get("erased_at")
                     for u in store.registered_users.values()) == 1
             and sum(o.get("id") == order.get("id") for o in store.orders) == 1
             and any(o is order for o in store.orders)
             and order.get("billing_type", "one_time") == "one_time"
             and order.get("status") not in {"cancelled", "cancel_pending"}
             and not order.get("refund_status") and not order.get("dispute_status")
             and "paid_out_refund_case" not in order)
    if extra is not None:
        _require(any(p is extra for p in order.get("pending_extras", []))
                 and sum(p.get("id") == extra.get("id") for p in order.get("pending_extras", [])) == 1
                 and extra.get("status") == "pending" and order.get("payment_status") == "paid")
    else:
        _require(order.get("payment_status") == "pending")
    return user["id"]


async def _hold(backend, store, order, key):
    # Restore to the last committed intent if response validation/save failed.
    if backend._checkpoint is not None:
        restore(store, backend._checkpoint)
    backend._emails.clear()
    try:
        details = await backend.operation_details(key)
        if not details or details["status"] not in {"pending", "unknown"}:
            return  # A failed cached GET is not an uncertain financial POST.
        for current in store.orders:
            if current.get("id") == order["id"]:
                current["payment_reconciliation_required"] = True
        if details and details["status"] == "pending":
            await backend.finish_operation(key, "unknown")
        await backend.save()
    except Exception:
        pass  # A durable pending intent already blocks every future POST.


async def execute_live_checkout(backend, store, stripe, settings, session_service, request, order, *, extra=None):
    """Return a verified id/url; FinanceConflict only BEFORE any existing intent.

    After an intent exists every failure is StripeOutcomeUnknown. Callers must
    keep the business reservation and never retry under a fresh order/extra ID.
    """
    key = operation_key(order, extra)
    existing = await backend.operation_details(key)
    committed = existing is not None
    try:
        _require(checkout_configuration_ready(settings))
        _target_bound_connection(backend, settings)
        scope = await service_context(backend, store, stripe, settings)
        actor = _buyer(store, session_service, request, order, extra)
        _require(not backend._emails)
        account = (store.connected_accounts.get(order.get("seller_id")) or {}).get("account_id")
        await _seller(backend, store, stripe, scope, {"seller_id": order.get("seller_id"), "account_id": account})
        part = extra or order
        _require(not part.get(ACCOUNT_FIELD) or part[ACCOUNT_FIELD] == account)
        key, data = checkout_request_data(stripe, scope, order, extra, account_id=account)
        if existing:
            _require(existing["endpoint"] == ENDPOINT and existing["request_hash"] == content_hash(data)
                     and existing["status"] == "succeeded" and part.get(ACCOUNT_FIELD) == account)
            result = await backend.operation_result(key)
            receipt = (result or {}).get("response") or {}
            _require(isinstance(receipt.get("id"), str) and re.fullmatch(r"cs_live_[A-Za-z0-9]+", receipt["id"]))
            remote = await stripe._get(ENDPOINT + "/" + receipt["id"])
            await validate_live_checkout_receipt(backend, stripe, scope, order, extra, remote)
            _require(remote["status"] == "open" and remote["payment_status"] == "unpaid")
            _buyer(store, session_service, request, order, extra)
            return session_receipt(remote)
        _require(not order.get("payment_reconciliation_required") and not part.get("checkout_session_id")
                 and not part.get("checkout_session_url") and not part.get("checkout_url"))
        if extra is not None:
            await _extra_parent_proof(stripe, scope, order)
        part[ACCOUNT_FIELD] = account
        _buyer(store, session_service, request, order, extra)
        # begin_operation may commit successfully and lose the acknowledgment.
        # Any failure from this boundary onward must preserve the reservation.
        committed = True
        cached = await backend.begin_operation(key, ENDPOINT, data)
        _require(cached is None)
        _buyer(store, session_service, request, order, extra)
        headers = {"Authorization": f"Bearer {stripe.secret_key}", "Idempotency-Key": key,
                   "Stripe-Version": STRIPE_API_VERSION}
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            response = await client.post("https://api.stripe.com/v1/checkout/sessions", data=data, headers=headers)
        if response.status_code not in {200, 201}:
            raise ValueError("Checkout response held")
        returned = response.json()
        _require(isinstance(returned, dict) and isinstance(returned.get("id"), str)
                 and re.fullmatch(r"cs_live_[A-Za-z0-9]+", returned["id"]))
        remote = await stripe._get(ENDPOINT + "/" + returned["id"])
        details = await backend.operation_details(key)
        await checkout_session_proof(stripe, scope, order, extra, remote, data,
                                     since=int(details["created_at"].timestamp()))
        _require(remote["status"] == "open" and remote["payment_status"] == "unpaid")
        receipt = session_receipt(remote)
        part.update(checkout_session_id=remote["id"], checkout_session_url=remote["url"])
        store.audit(actor, "checkout.live_session_created", f"{order['id']}:{key}:{remote['id']}")
        # The same transaction commits receipt, order association, and audit.
        await backend.reconcile_operation(key, endpoint=ENDPOINT, request_hash=content_hash(data), response=receipt)
        return receipt
    except Exception:
        if committed:
            await _hold(backend, store, order, key)
            raise StripeOutcomeUnknown(key) from None
        raise FinanceConflict("本番購入の準備を確認できません。支払いは開始していません") from None
