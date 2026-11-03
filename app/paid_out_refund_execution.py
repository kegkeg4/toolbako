"""Durable paid-out refunds; live execution is narrowly default-off.

Cases live in the existing private order snapshot. Immutable opening evidence,
approval consumption and Stripe intent share the existing database boundary.
No schema migration, seller collection, netting, email or payout mutation is
performed. A production router is NOT installed by this module. Live GET review
is supported only for an explicitly bound dataset. The generic live POST block
remains; the separate refund flag never enables collection, transfer or payout.

ONE reserve references are accountable admin attestations, not automated proof
of economic ownership. Provider available JPY is checked separately as liquidity.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
from uuid import UUID, uuid4, uuid5

import httpx

from .database import content_hash, restore
from .finance import FinanceConflict
from .financial_scope import FinancialScope, configured_scope
from .integrations import StripeIntegration, StripeOutcomeUnknown
from .mfa import SupabaseMFA
from .paid_out_refund import (
    FUNDING_POLICY, REVIEW_WINDOW, PaidPayoutEvidence, PaidOutRefundProposal,
    capture_paid_payout, inspect_paid_out_refund, propose_paid_out_refund,
    review_paid_out_refund,
)
from .payment_reconciliation import require_sandbox_review
from .persistence import encode_value


FIELD = "paid_out_refund_case"
ENDPOINT = "internal/paid-out-refund-case-v1"


def _require(ok):
    if not ok:
        raise FinanceConflict("払出し後返金の状態・原資・承認を確認できません。再送せず確認してください")


def _now(at):
    value = at or datetime.now(timezone.utc)
    _require(isinstance(value, datetime) and value.tzinfo is not None)
    return value


def _admins(store, settings):
    return frozenset(user["id"] for user in store.registered_users.values()
                     if user.get("id") in settings.admin_user_ids and not user.get("is_banned")
                     and not user.get("deleted_at") and not user.get("erased_at"))


async def _context(backend, store, stripe, settings, mfa, request):
    _require(isinstance(mfa, SupabaseMFA) and mfa.settings is settings
             and mfa.sessions.store is store and backend._store is store
             and backend._connection is not None and isinstance(stripe, StripeIntegration)
             and stripe.journal is backend and stripe.secret_key == settings.stripe_secret_key
             and stripe.charge_mode == "separate" and not settings.demo_mode)
    user = mfa.sessions.current(request)
    admins = _admins(store, settings)
    _require(user is not None and user["id"] in admins and len(admins) >= 2 and mfa.recent(request))
    scope = configured_scope(settings)
    _require(isinstance(scope, FinancialScope) and stripe.financial_scope == scope)
    await backend.assert_financial_scope(scope)
    if scope.mode == "live":
        from .refund_resumption import live_refund_context
        verified_actor, verified_admins, verified_scope = await live_refund_context(
            backend, store, stripe, settings, mfa, request)
        _require((verified_actor, verified_scope) == (user["id"], scope) and set(admins) == verified_admins)
    return user["id"], admins, scope


def _order(store, order_id):
    matches = [order for order in store.orders if order.get("id") == order_id]
    _require(len(matches) == 1)
    return matches[0]


def _eligible(store, order):
    # Narrow existing mutually accepted cancellation; no new entitlement policy.
    buyer, seller = order.get("buyer_id"), order.get("seller_id")
    _require(buyer and seller and buyer != seller and order.get("billing_type", "one_time") == "one_time"
             and order.get("payment_status") == "paid" and order.get("status") == "cancel_pending"
             and order.get("cancel_requested_by") in {buyer, seller}
             and order.get("refund_cancel_accepted_by") in {buyer, seller}
             and order["cancel_requested_by"] != order["refund_cancel_accepted_by"]
             and isinstance(order.get("refund_cancel_accepted_at"), datetime)
             and not order.get("pending_extras") and not order.get("dispute_status")
             and not order.get("refund_reference") and not order.get("primary_refunded_amount", 0)
             and order.get("refund_status") not in {"completed", "partial", "pending", "processing"}
             and type(order.get("amount")) is int and order["amount"] > 0
             and order.get("primary_payment_amount", order["amount"]) == order["amount"])
    reference = order.get("payment_reference")
    _require(isinstance(reference, str) and reference.startswith("pi_")
             and sum(part.get("payment_reference") == reference for other in store.orders
                     for part in [other, *other.get("pending_extras", [])]) == 1)
    payouts = [p for p in store.payouts if any(a.get("receipt_id") == reference for a in p.get("allocations", []))]
    _require(len(payouts) == 1 and payouts[0].get("seller_id") == seller)
    return payouts[0]


def _eligibility(order):
    return {key: order.get(key) for key in ("id", "buyer_id", "seller_id", "amount", "payment_reference",
            "cancel_requested_by", "refund_cancel_accepted_by", "refund_cancel_accepted_at")}


def _opening(case):
    return {key: case[key] for key in ("case", "scope", "paid_identity_json", "funding", "eligibility")}


def _opening_key(case):
    return "paid-out-case-" + case["case"]["id"]


def _data(case):
    c = case["case"]
    return {"payment_intent": c["payment_id"], "amount": str(c["refund_amount"]),
            "metadata[order_id]": c["order_id"], "metadata[paid_out_case_id]": c["id"],
            "metadata[ledger_id]": case["scope"]["ledger_id"]}


def _key(case):
    return "paid-out-refund-" + case["case"]["id"]


async def _verify_opening(backend, case):
    row = await (await backend._connection.execute(
        "select endpoint,request_hash,status,response from toolbako_runtime.stripe_operations where operation_id=%s",
        (_opening_key(case),),
    )).fetchone()
    proof = _opening(case)
    _require(row == (ENDPOINT, content_hash(proof), "succeeded",
                     {"id": _opening_key(case), "proof": encode_value(proof)}))


async def _commit_opening(backend, store, case, actor):
    proof = _opening(case)
    store.audit(actor, "finance.paid_out_refund_opened", case["case"]["id"])
    async with backend._connection.transaction():
        await backend._connection.execute(
            "insert into toolbako_runtime.stripe_operations(operation_id,endpoint,request_hash,status,response) "
            "values(%s,%s,%s,'succeeded',%s::jsonb)",
            (_opening_key(case), ENDPOINT, content_hash(proof),
             json.dumps({"id": _opening_key(case), "proof": encode_value(proof)})),
        )
        committed = await backend._save(backend._connection)
    backend._accept_commit(committed)


async def _proposal_record(backend, case, *, create=False):
    proof = {"case_id": case["case"]["id"], "proposal": case["proposal"]}
    key = "paid-out-review-" + case["proposal"]["id"]
    response = {"id": key, "proof": encode_value(proof)}
    if create:
        async with backend._connection.transaction():
            await backend._connection.execute(
                "insert into toolbako_runtime.stripe_operations(operation_id,endpoint,request_hash,status,response) "
                "values(%s,%s,%s,'succeeded',%s::jsonb)",
                (key, ENDPOINT + "/proposal", content_hash(proof), json.dumps(response)),
            )
            committed = await backend._save(backend._connection)
        backend._accept_commit(committed)
    else:
        row = await (await backend._connection.execute(
            "select endpoint,request_hash,status,response from toolbako_runtime.stripe_operations where operation_id=%s", (key,),
        )).fetchone()
        _require(row == (ENDPOINT + "/proposal", content_hash(proof), "succeeded", response))


def _liquidity(balance):
    available = balance.get("available")
    _require(isinstance(available, list))
    yen = [x for x in available if isinstance(x, dict) and x.get("currency") == "jpy"]
    _require(len(yen) == 1 and type(yen[0].get("amount")) is int)
    return yen[0]["amount"]


def _reserved_other(store, case):
    reserved = 0
    for order in store.orders:
        other = order.get(FIELD)
        if not other or other is case:
            continue
        _require(other["scope"] == case["scope"]
                 and other["funding"]["reserve_id"] != case["funding"]["reserve_id"])
        if other.get("buyer_refund", {}).get("state") != "complete":
            reserved += other["case"]["refund_amount"]
    return reserved


async def _collect(backend, store, stripe, order, case, scope, at, *, verify_opening=True):
    payout = _eligible(store, order)
    _require(capture_paid_payout(payout, scope=scope).identity_json == case["paid_identity_json"])
    _require(case["scope"] == scope.payload() and case["eligibility"] == _eligibility(order))
    if verify_opening:
        await _verify_opening(backend, case)
    c, allocation = case["case"], payout["allocations"][0]
    # Legacy attempts, including definite rejections, must be reviewed separately.
    _require(await backend.operation_details(f"refund-{order['id']}") is None)
    account = await stripe._get("account")
    balance = await stripe._get("balance")
    payment = await stripe.retrieve_payment(c["payment_id"])
    bank = await stripe.retrieve_payout(payout["provider_payout_id"], payout["account_id"])
    transfer = await stripe._get("transfers/" + allocation["transfer_id"])
    if scope.mode == "live":
        await _live_paid_sources(backend, scope, order, payout, allocation, payment, bank, transfer)
    refunds = await stripe.list_financial_objects("refunds", params={"payment_intent": c["payment_id"]})
    reversals = await stripe.list_financial_objects("transfers/" + allocation["transfer_id"] + "/reversals")
    # Seller recovery is separate and not executed/automatically adopted here.
    _require(not reversals and len(refunds) <= 1)
    operation = await backend.operation_details(_key(case))
    items = []
    if operation:
        _require(operation["endpoint"] == "refunds" and operation["request_hash"] == content_hash(_data(case)))
        remote = refunds[0] if refunds else None
        if remote:
            metadata = remote.get("metadata") or {}
            _require(metadata.get("paid_out_case_id") == c["id"] and metadata.get("ledger_id") == scope.ledger_id
                     and remote.get("created", 0) >= int(operation["created_at"].timestamp()))
            _require("livemode" not in remote or remote["livemode"] is scope.livemode)
            previous = await backend.operation_result(_key(case))
            if previous["status"] == "succeeded":
                _require(previous["response"]["id"] == remote.get("id"))
        items.append({"id": _key(case), "case_id": c["id"], "scope": scope.payload(),
                      "amount": c["refund_amount"], "status": operation["status"], "provider": remote})
    else:
        _require(not refunds)
    inspection = inspect_paid_out_refund(c, scope=scope, bound_scope=scope,
        paid_evidence=PaidPayoutEvidence(scope, case["paid_identity_json"]), payout=payout, payment=payment,
        platform_account=account, platform_balance=balance, provider_payout=bank,
        payout_account_id=payout["account_id"], transfer=transfer,
        inventory={"complete": True, "scope": scope.payload(), "case_id": c["id"],
                   "payment_id": c["payment_id"], "transfer_id": allocation["transfer_id"],
                   "refunds": items, "recoveries": []},
        funding=case["funding"], observed_at=at, at=at)
    return inspection, operation, refunds[0] if refunds else None, balance


async def _live_paid_sources(backend, scope, order, payout, allocation, payment, bank, transfer):
    """The original live allocation/bank intents, not relabelled test history."""
    from .live_payout_execution import request_data, remote_proof
    for source in (payment, payment.get("latest_charge", {})):
        _require(isinstance(source, dict))
        metadata = source.get("metadata")
        _require(isinstance(metadata, dict) and metadata.get("order_id") == order["id"]
                 and metadata.get("ledger_id") == scope.ledger_id and "extra_id" not in metadata)
    charge = payment["latest_charge"]
    _require(charge.get("refunded") is (charge.get("amount_refunded") == order["amount"]))
    for item, remote in ((allocation, transfer), (None, bank)):
        key, path, data = request_data(scope, payout, item)
        details, result = await backend.operation_details(key), await backend.operation_result(key)
        _require(details is not None and details["endpoint"] == path and details["status"] == "succeeded"
                 and details["request_hash"] == content_hash(data) and result is not None
                 and result["status"] == "succeeded" and (result.get("response") or {}).get("id") == remote.get("id"))
        remote_proof(scope, payout, item, remote)


async def _live_reserves(backend, store, stripe, scope, case, *, current_operation=None):
    uncertain = await (await backend._connection.execute(
        "select operation_id from toolbako_runtime.stripe_operations where status in ('pending','unknown') "
        "and (endpoint in ('refunds','transfers','payouts') or endpoint like 'transfers/%%/reversals') "
        "and operation_id<>%s limit 1", (current_operation or "",),
    )).fetchone()
    _require(uncertain is None)
    reserves, ids = case["case"]["refund_amount"], {case["funding"]["reserve_id"]}
    for order in store.orders:
        if FIELD in order:
            other = order[FIELD]
            if other is case:
                continue
            _require(isinstance(other, dict) and other.get("scope") == scope.payload())
            await _verify_opening(backend, other)
            reserve_id = other["funding"]["reserve_id"]
            _require(reserve_id not in ids)
            ids.add(reserve_id)
            if other.get("buyer_refund", {}).get("state") != "complete":
                amount = other["case"]["refund_amount"]
                _require(type(amount) is int and amount > 0)
                reserves += amount
        else:
            _require(order.get("primary_refund_status") not in {"pending", "partial"}
                     and not any(e.get("refund_status") in {"pending", "partial"} for e in order.get("pending_extras", [])))
    balance = await stripe._get("balance")
    _require(balance.get("object") == "balance" and balance.get("livemode") is True
             and _liquidity(balance) >= reserves)


def _live_actor_now(store, settings, mfa, request, case, actor, *, executing=False):
    user, admins = mfa.sessions.current(request), _admins(store, settings)
    proposal = case["proposal"]
    _require(request.method == "POST" and user is not None and user["id"] == actor and actor in admins
             and mfa.recent(request) and len(admins) >= 2 and proposal["proposed_by"] in admins
             and proposal["proposed_by"] != actor and case["funding"]["approved_by"] in admins
             and datetime.now(timezone.utc) >= proposal["proposed_at"]
             and datetime.now(timezone.utc) - proposal["proposed_at"] <= REVIEW_WINDOW)
    if executing:
        approval = case.get("last_approval") or {}
        _require(case.get("status") == "submitting" and proposal["action"] == "execute"
                 and case["consumed_proposals"].count(proposal["id"]) == 1
                 and approval.get("id") == proposal["id"] and approval.get("action") == "execute"
                 and approval.get("approved_by") == actor and approval.get("proposed_by") == proposal["proposed_by"]
                 and approval.get("fingerprint") == proposal["fingerprint"])


def _restore_checkpoint(backend, store):
    if backend._checkpoint is not None:
        restore(store, backend._checkpoint)
    backend._emails.clear()


async def _execute_live(backend, store, stripe, settings, mfa, request, order, case, scope, actor):
    """One fixed refund after consumed approval + pending intent commit."""
    from .refund_resumption import _live_execution_enabled
    from .live_setup_plan import STRIPE_API_VERSION
    key, data = _key(case), _data(case)
    try:
        _live_execution_enabled(settings, stripe)
        await _proposal_record(backend, case)
        _live_actor_now(store, settings, mfa, request, case, actor, executing=True)
        cached = await backend.begin_operation(key, "refunds", data)
        _require(cached is None)
    except Exception:
        _restore_checkpoint(backend, store)
        raise
    try:
        inspection, operation, remote, _ = await _collect(backend, store, stripe, order, case, scope, _now(None))
        buyer = inspection.payload()["buyer_refund"]
        _require(operation is not None and operation["status"] == "pending" and remote is None
                 and buyer["confirmed"] == 0 and buyer["remaining"] == case["case"]["refund_amount"]
                 and set(buyer["holds"]) <= {"unknown_operation"})
        await _live_reserves(backend, store, stripe, scope, case, current_operation=key)
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            _live_execution_enabled(settings, stripe)
            _live_actor_now(store, settings, mfa, request, case, actor, executing=True)
            response = await client.post("https://api.stripe.com/v1/refunds", data=data, headers={
                "Authorization": f"Bearer {stripe.secret_key}", "Idempotency-Key": key,
                "Stripe-Version": STRIPE_API_VERSION})
        if response.status_code in {400, 401, 403, 404, 422}:
            await backend.finish_operation(key, "rejected")
            case["status"] = "held"
            await backend.save()
            raise FinanceConflict("返金要求は拒否されています。再送せず確認してください")
        _require(response.status_code in {200, 201})
        result = response.json()
        _require(isinstance(result, dict))
        inspection, operation, remote, _ = await _collect(backend, store, stripe, order, case, scope, _now(None))
        _require(remote is not None and _minimal(result) == _minimal(remote)
                 and operation["status"] == "pending")
    except Exception:
        details = await backend.operation_details(key)
        if details and details["status"] == "rejected":
            raise FinanceConflict("返金要求は拒否されています。再送せず確認してください") from None
        if details and details["status"] == "pending":
            await backend.finish_operation(key, "unknown")
        case["status"] = "unknown"
        await backend.save()
        raise StripeOutcomeUnknown(key) from None
    try:
        buyer = inspection.payload()["buyer_refund"]
        if buyer["confirmed"] == case["case"]["refund_amount"] and set(buyer["holds"]) <= {"unknown_operation"}:
            _settle_case(case, inspection, remote)
        else:
            case["buyer_refund"] = buyer | {"holds": sorted(set(buyer["holds"]) - {"unknown_operation"})}
            case["status"] = "awaiting_confirmation"
        await backend.reconcile_operation(key, endpoint="refunds", request_hash=content_hash(data), response=_minimal(remote))
    except Exception:
        _restore_checkpoint(backend, store)
        raise
    return case["status"]


async def open_paid_out_refund_case(backend, store, stripe, settings, mfa, request, order_id,
                                   *, reserve_id, reserve_amount, seller_recovery_amount, at=None):
    at = _now(at)
    actor, admins, scope = await _context(backend, store, stripe, settings, mfa, request)
    order = _order(store, order_id)
    payout = _eligible(store, order)
    _require(FIELD not in order and type(reserve_amount) is int and reserve_amount >= order["amount"])
    paid = capture_paid_payout(payout, scope=scope)
    allocation = payout["allocations"][0]
    case_id = str(uuid5(UUID(scope.ledger_id), "paid-out-refund:" + order["payment_reference"]))
    case = {"case": {"id": case_id, "order_id": order_id, "payment_id": order["payment_reference"],
                    "payment_amount": order["amount"], "refund_amount": order["amount"],
                    "seller_recovery_amount": seller_recovery_amount, "seller_id": order["seller_id"],
                    "payout_id": payout["id"], "allocation_id": allocation["id"]},
            "scope": scope.payload(), "paid_identity_json": paid.identity_json,
            "eligibility": _eligibility(order), "status": "opened", "consumed_proposals": [],
            "funding": {"policy": FUNDING_POLICY, "decision_id": str(uuid4()), "case_id": case_id,
                        "scope": scope.payload(), "refund_limit": order["amount"],
                        "recovery_limit": seller_recovery_amount, "reserve_id": reserve_id,
                        "available_amount": reserve_amount, "approved_by": actor, "approved_at": at}}
    inspection, operation, remote, balance = await _collect(backend, store, stripe, order, case, scope, at, verify_opening=False)
    _require(operation is None and not inspection.payload()["buyer_refund"]["holds"]
             and _liquidity(balance) >= order["amount"] + _reserved_other(store, case))
    if scope.mode == "live":
        await _live_reserves(backend, store, stripe, scope, case)
    case.update(buyer_refund=inspection.payload()["buyer_refund"], seller_recovery=inspection.payload()["seller_recovery"])
    order[FIELD] = case
    await _commit_opening(backend, store, case, actor)
    return deepcopy(case)


async def propose_paid_out_refund_action(backend, store, stripe, settings, mfa, request, order_id, *, action="execute", at=None):
    at = _now(at)
    actor, admins, scope = await _context(backend, store, stripe, settings, mfa, request)
    order = _order(store, order_id)
    case = order.get(FIELD)
    _require(isinstance(case, dict) and action in {"execute", "reconcile"})
    inspection, operation, remote, balance = await _collect(backend, store, stripe, order, case, scope, at)
    _action_ready(action, case, inspection, operation, remote, balance, store)
    if scope.mode == "live" and action == "execute":
        await _live_reserves(backend, store, stripe, scope, case)
    proposal = propose_paid_out_refund(inspection, proposal_id=str(uuid4()), actor_id=actor, admin_ids=admins, at=at)
    case["proposal"] = {"id": proposal.id, "case_id": proposal.case_id, "scope": scope.payload(),
                        "fingerprint": proposal.fingerprint, "proposed_by": actor, "proposed_at": at, "action": action}
    store.audit(actor, "finance.paid_out_refund_proposed", case["case"]["id"])
    await _proposal_record(backend, case, create=True)
    return deepcopy(case["proposal"])


def _action_ready(action, case, inspection, operation, remote, balance, store):
    buyer = inspection.payload()["buyer_refund"]
    if action == "execute":
        _require(operation is None and not buyer["holds"] and buyer["confirmed"] == 0
                 and buyer["remaining"] == case["case"]["refund_amount"]
                 and _liquidity(balance) >= buyer["remaining"] + _reserved_other(store, case))
    else:
        _require(operation is not None and operation["status"] in {"pending", "unknown", "succeeded"}
                 and remote is not None and remote.get("status") == "succeeded"
                 and buyer["confirmed"] == case["case"]["refund_amount"] and buyer["remaining"] == 0
                 and set(buyer["holds"]) <= {"unknown_operation"})


async def approve_paid_out_refund_action(backend, store, stripe, settings, mfa, request, order_id, proposal_id, *, at=None):
    at = _now(at)
    actor, admins, scope = await _context(backend, store, stripe, settings, mfa, request)
    order = _order(store, order_id)
    case = order.get(FIELD)
    _require(isinstance(case, dict) and isinstance(case.get("proposal"), dict))
    p = case["proposal"]
    _require(p["id"] == proposal_id and p["scope"] == scope.payload())
    await _proposal_record(backend, case)
    inspection, operation, remote, balance = await _collect(backend, store, stripe, order, case, scope, at)
    _action_ready(p["action"], case, inspection, operation, remote, balance, store)
    proposal = PaidOutRefundProposal(p["id"], p["case_id"], scope, p["fingerprint"], p["proposed_by"], p["proposed_at"])
    review_paid_out_refund(proposal, inspection, actor_id=actor, admin_ids=admins,
                          consumed_proposal_ids=frozenset(case["consumed_proposals"]), at=at)
    if p["action"] == "execute":
        if scope.mode == "live":
            from .refund_resumption import _live_execution_enabled
            _live_execution_enabled(settings, stripe)
            await _live_reserves(backend, store, stripe, scope, case)
        else:
            require_sandbox_review(backend, store, stripe, settings, actor)
    if scope.mode == "live":
        _live_actor_now(store, settings, mfa, request, case, actor)
    case["consumed_proposals"].append(p["id"])
    case["last_approval"] = {"id": p["id"], "proposed_by": p["proposed_by"], "approved_by": actor,
                             "approved_at": at, "fingerprint": inspection.fingerprint, "action": p["action"]}
    store.audit(actor, "finance.paid_out_refund_approved", json.dumps(encode_value(case["last_approval"]), sort_keys=True))
    if p["action"] == "reconcile":
        try:
            _settle_case(case, inspection, remote)
            if operation["status"] in {"pending", "unknown"}:
                await backend.reconcile_operation(_key(case), endpoint="refunds", request_hash=content_hash(_data(case)), response=_minimal(remote))
            else:
                await backend.save()
        except Exception:
            if scope.mode == "live":
                _restore_checkpoint(backend, store)
            raise
        return case["status"]
    case["status"] = "submitting"
    if scope.mode == "live":
        return await _execute_live(backend, store, stripe, settings, mfa, request, order, case, scope, actor)
    # begin_operation commits consumed approval + pending intent BEFORE POST.
    try:
        await stripe._post("refunds", {"_idempotency_key": _key(case), **_data(case)})
    except StripeOutcomeUnknown:
        case["status"] = "unknown"
        await backend.save()
        raise
    except RuntimeError:
        case["status"] = "held"
        await backend.save()
        raise
    # A successful POST does not imply money returned. Fresh GET parent/amount
    # verification follows; any subsequent failure leaves the intent durable.
    case["status"] = "awaiting_confirmation"
    await backend.save()
    try:
        result, _, remote, _ = await _collect(backend, store, stripe, order, case, scope, _now(None))
        buyer = result.payload()["buyer_refund"]
        if remote and buyer["confirmed"] == case["case"]["refund_amount"] and not buyer["holds"]:
            _settle_case(case, result, remote)
        else:
            case["buyer_refund"] = buyer
        await backend.save()
    except (FinanceConflict, RuntimeError):
        # GET failure or mismatch NEVER retries the POST or releases reserve.
        case["status"] = "awaiting_confirmation"
        await backend.save()
        raise
    return case["status"]


def _minimal(remote):
    return {key: remote[key] for key in ("id", "object", "amount", "currency", "payment_intent", "charge", "status", "created", "metadata")}


def _settle_case(case, inspection, remote):
    case["buyer_refund"] = inspection.payload()["buyer_refund"] | {"state": "complete", "holds": []}
    case["buyer_refund"]["provider_reference"] = remote["id"]
    case["seller_recovery"] = inspection.payload()["seller_recovery"]
    case["status"] = "buyer_refunded"
    # Do not call apply_refund_payment: it creates a seller_refund ledger fact.
    # Paid payout evidence/reservations and seller recovery remain independent.


async def reconcile_paid_out_refund_notification(event, store, backend, provider, scope):
    """Verified receiver callback: GET evidence only, no approval or POST.

Caller must authenticate the Event and atomically save its processed EventID
with this case update. An unknown intent remains for a fresh two-admin review.
Returning True means the notification was classified, NOT that funds settled.
"""
    _require(isinstance(scope, FinancialScope) and isinstance(provider, StripeIntegration)
             and provider.financial_scope == scope and provider.journal is backend
             and backend._store is store and backend._connection is not None
             and isinstance(event, dict) and isinstance(event.get("id"), str)
             and event["id"].startswith("evt_") and event.get("livemode") is scope.livemode
             and event.get("type") in {"refund.created", "refund.updated", "refund.failed", "charge.refunded"})
    await backend.assert_financial_scope(scope)
    obj = (event.get("data") or {}).get("object")
    _require(isinstance(obj, dict) and isinstance(obj.get("metadata"), dict))
    metadata = obj["metadata"]
    order = _order(store, metadata.get("order_id"))
    case = order.get(FIELD)
    _require(isinstance(case, dict))
    if event["type"].startswith("refund."):
        _require(metadata.get("paid_out_case_id") == case["case"]["id"]
                 and metadata.get("ledger_id") == scope.ledger_id)
    at = _now(None)
    inspection, operation, remote, _ = await _collect(backend, store, provider, order, case, scope, at)
    _require(operation is not None)
    if event["type"] == "charge.refunded":
        _require(obj.get("object") == "charge" and obj.get("id") == inspection.payload()["source"]["charge"]
                 and obj.get("livemode") is scope.livemode)
    else:
        _require(remote is not None and obj.get("object") == "refund" and obj.get("id") == remote["id"])
    case["last_provider_notice"] = {"event_id": event["id"], "type": event["type"],
                                    "provider_reference": obj["id"], "observed_at": at}
    if case["status"] != "buyer_refunded":
        case["status"] = "awaiting_review"
    store.audit(None, "finance.paid_out_refund_notification", case["case"]["id"])
    return True
