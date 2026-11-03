"""Default-off, four-eyes live payout steps; no route or scheduler activation.

One consumed review sends at most one fixed Transfer/Payout request. A private
journal receipt authenticates the review; the request intent and consumption
commit before transport. Unknown outcomes can only be reconciled by fresh GETs
and a second independent server-authenticated admin. No absence permits retry.
Failed payout reversals remain outside this execution lane.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import httpx

from .config import Settings
from .data import DemoStore
from .database import PostgresStateStore, content_hash
from .finance import FinanceConflict, receipts
from .financial_scope import FinancialScope, configured_scope, key_mode
from .integrations import StripeIntegration, StripeOutcomeUnknown
from .live_seller_onboarding import _mapping, _inspect_account, _parameters, _metadata as onboarding_metadata
from .live_setup_plan import STRIPE_API_VERSION
from .mfa import SupabaseMFA
from .payment_source_proof import payment_source_proof
from .persistence import encode_value
from .security import SessionService
from .stripe_bank_proof import allocation_transfer_proof, bank_payout_proof


REVIEW_ENDPOINT = "internal/live-payout-review-v1"
FIELD = "live_payout_review"
RECOVERY_FIELD = "live_payout_recovery"
WINDOW = timedelta(minutes=30)


def _require(ok):
    if not ok:
        raise FinanceConflict("本番分配・振込の台帳・口座・承認を確認できません。再送せず確認してください")


def _now(at=None):
    value = at or datetime.now(timezone.utc)
    _require(isinstance(value, datetime) and value.tzinfo is not None)
    return value


def _enabled(settings):
    _require(settings.stripe_connection_mode == "live_financial"
             and getattr(settings, "stripe_live_payouts_enabled", False) is True)


def _local_service_context(backend, store, stripe, settings):
    _require(isinstance(settings, Settings) and isinstance(store, DemoStore)
             and isinstance(backend, PostgresStateStore) and backend._store is store
             and backend._connection is not None and backend.dsn == settings.database_url
             and not settings.demo_mode and settings.stripe_charge_mode == "separate"
             and settings.stripe_connection_mode in {"live_read_only", "live_setup_only", "live_financial"}
             and settings.stripe_api_version == STRIPE_API_VERSION
             and isinstance(stripe, StripeIntegration) and stripe.journal is backend
             and stripe.secret_key == settings.stripe_secret_key and stripe.charge_mode == "separate"
             and stripe.api_version == STRIPE_API_VERSION)
    scope = configured_scope(settings)
    _require(isinstance(scope, FinancialScope) and scope.mode == "live" and stripe.financial_scope == scope)
    return scope


async def service_context(backend, store, stripe, settings):
    scope = _local_service_context(backend, store, stripe, settings)
    await backend.assert_financial_scope(scope)
    await stripe.verify_financial_scope()
    return scope


async def _context(backend, store, stripe, settings, mfa, request, actor_id, *, execute=False):
    if execute:
        _enabled(settings)
    scope = await service_context(backend, store, stripe, settings)
    return scope, _actor(store, settings, mfa, request, actor_id)


def _actor(store, settings, mfa, request, actor_id):
    _require(isinstance(mfa, SupabaseMFA) and mfa.settings is settings
             and isinstance(mfa.sessions, SessionService) and mfa.sessions.store is store
             and request is not None and request.method == "POST")
    admins = {u["id"] for u in store.registered_users.values()
              if u.get("id") in settings.admin_user_ids and not u.get("is_banned")
              and not u.get("deleted_at") and not u.get("erased_at")}
    user = mfa.sessions.current(request)
    _require(user is not None and user["id"] == actor_id and actor_id in admins
             and len(admins) >= 2 and mfa.recent(request))
    return admins


def _payout(store, payout):
    _require(isinstance(payout, dict) and any(p is payout for p in store.payouts)
             and sum(p.get("id") == payout.get("id") for p in store.payouts) == 1
             and payout.get("finance_v2") is True)
    allocations = payout.get("allocations")
    _require(isinstance(allocations, list) and 1 <= len(allocations) <= 10
             and len({a.get("id") for a in allocations}) == len(allocations)
             and len({a.get("receipt_id") for a in allocations}) == len(allocations)
             and all(type(payout.get(k)) is int and payout[k] > 0 for k in ("amount", "net_amount"))
             and type(payout.get("fee")) is int and payout["fee"] >= 0
             and payout["net_amount"] + payout["fee"] == payout["amount"]
             and all(type(a.get("amount")) is int and a["amount"] > 0
                     and type(a.get("transfer_amount")) is int and 0 <= a["transfer_amount"] <= a["amount"]
                     for a in allocations)
             and sum(a["amount"] for a in allocations) == payout["amount"]
             and sum(a["transfer_amount"] for a in allocations) == payout["net_amount"])


def _metadata(scope, payout, allocation=None):
    result = {"payout_id": payout["id"], "ledger_id": scope.ledger_id,
              "platform_account_id": scope.platform_account}
    if allocation is not None:
        result["allocation_id"] = allocation["id"]
    return result


def request_data(scope, payout, allocation=None, charge=None):
    if allocation is None:
        key, path = f"bank-payout-{payout['id']}", "payouts"
        data = {"amount": str(payout["net_amount"]), "currency": "jpy", "method": "standard",
                "_stripe_account": payout["account_id"]}
    else:
        key, path = f"allocation-{allocation['id']}", "transfers"
        data = {"amount": str(allocation["transfer_amount"]), "currency": "jpy",
                "destination": payout["account_id"], "source_transaction": charge or allocation.get("source_charge"),
                "transfer_group": f"order-{allocation['order_id']}"}
    data.update({f"metadata[{k}]": v for k, v in _metadata(scope, payout, allocation).items()})
    return key, path, data


def remote_proof(scope, payout, allocation, remote, *, since=None, at=None):
    if allocation is None:
        proof = bank_payout_proof(remote, expected_mode="live", account_id=payout["account_id"],
            response_account_id=payout["account_id"], amount=payout["net_amount"], currency="jpy",
            metadata=_metadata(scope, payout), payout_id=payout.get("provider_payout_id"))
    else:
        proof = allocation_transfer_proof(remote, expected_mode="live", account_id=payout["account_id"],
            source_charge=allocation.get("source_charge"), amount=allocation["transfer_amount"], currency="jpy",
            metadata=_metadata(scope, payout, allocation), transfer_group=f"order-{allocation['order_id']}",
            transfer_id=allocation.get("transfer_id"))
    if since is not None:
        _require(type(remote.get("created")) is int
                 and since - 60 <= remote["created"] <= int(_now(at).timestamp()) + 60)
    return proof


async def _seller(backend, store, stripe, scope, payout, *, require_ready=True):
    mapping = _mapping(store, scope, payout["seller_id"])
    _require(mapping is not None and mapping["account_id"] == payout["account_id"]
             and (not require_ready or not mapping.get("payouts_paused")))
    key = mapping["onboarding_operation_id"]
    details, result = await backend.operation_details(key), await backend.operation_result(key)
    _require(details is not None and details["endpoint"] == "accounts" and details["status"] == "succeeded"
             and details["request_hash"] == content_hash(_parameters(scope, payout["seller_id"], key))
             and result is not None and result["status"] == "succeeded"
             and (result.get("response") or {}).get("id") == payout["account_id"])
    if require_ready:
        status, active = await _inspect_account(stripe, scope, payout["seller_id"], key, payout["account_id"])
        _require(active and all(value for _, value in status))
    else:
        remote = await stripe.retrieve_account(payout["account_id"])
        expected = onboarding_metadata(scope, payout["seller_id"], key)
        _require(isinstance(remote, dict) and remote.get("object") == "account"
                 and remote.get("id") == payout["account_id"] and remote.get("country") == "JP"
                 and remote.get("type") == "express" and remote.get("default_currency") == "jpy"
                 and isinstance(remote.get("metadata"), dict)
                 and all(remote["metadata"].get(k) == v for k, v in expected.items())
                 and ("livemode" not in remote or remote["livemode"] is True))
        connected = await stripe.retrieve_balance(payout["account_id"])
        _require(isinstance(connected, dict) and connected.get("object") == "balance"
                 and connected.get("livemode") is True)


async def _sources(backend, store, stripe, scope, payout, *, for_execution=True):
    _payout(store, payout)
    await _seller(backend, store, stripe, scope, payout, require_ready=for_execution)
    facts = receipts(store)
    proofs = {}
    for allocation in payout["allocations"]:
        fact = facts.get(allocation["receipt_id"])
        _require(fact is not None and (not for_execution or fact["eligible"]) and fact["seller_id"] == payout["seller_id"]
                 and fact["order_id"] == allocation["order_id"])
        orders = [o for o in store.orders if o.get("id") == fact["order_id"]]
        _require(len(orders) == 1 and (not for_execution or not orders[0].get("paid_out_refund_case")))
        order = orders[0]
        parts = [(None, order), *((e, e) for e in order.get("pending_extras", []) if e.get("status") == "paid")]
        matches = [(e, part) for e, part in parts if part.get("payment_reference") == fact["id"]]
        _require(len(matches) == 1)
        extra, _ = matches[0]
        payment = await stripe.retrieve_payment(fact["id"])
        proof = payment_source_proof(payment, payment_id=fact["id"], amount=fact["amount"],
            order_id=fact["order_id"], expected_mode="live", source_charge=allocation.get("source_charge"),
            allow_refunded=not for_execution)
        for remote in (payment, payment["latest_charge"]):
            metadata = remote.get("metadata")
            _require(isinstance(metadata, dict) and metadata.get("order_id") == fact["order_id"]
                     and metadata.get("ledger_id") == scope.ledger_id
                     and (metadata.get("extra_id") == extra["id"] if extra else "extra_id" not in metadata))
        _require(payment["latest_charge"].get("transfer_group") == f"order-{fact['order_id']}")
        proofs[allocation["id"]] = proof
    return proofs


async def _unresolved(backend, store, current=None):
    row = await (await backend._connection.execute(
        "select operation_id from toolbako_runtime.stripe_operations where status in ('pending','unknown') "
        "and (endpoint in ('refunds','transfers','payouts') or endpoint like 'transfers/%%/reversals') "
        "and operation_id<>%s limit 1", (current or "",),
    )).fetchone()
    _require(row is None)
    for order in store.orders:
        case = order.get("paid_out_refund_case")
        _require(not case or case.get("buyer_refund", {}).get("state") == "complete")
        _require(order.get("primary_refund_status") not in {"pending", "partial"}
                 and not any(e.get("refund_status") in {"pending", "partial"}
                             for e in order.get("pending_extras", [])))


async def _existing_transfers(backend, store, stripe, scope, payout, proofs):
    for allocation in payout["allocations"]:
        if not allocation["transfer_amount"]:
            _require(not allocation.get("transfer_id") and await backend.operation_details(f"allocation-{allocation['id']}") is None)
            continue
        objects = await stripe.list_financial_objects("transfers", params={
            "destination": payout["account_id"], "transfer_group": f"order-{allocation['order_id']}"})
        seen = set()
        for remote in objects:
            _require(remote.get("id") not in seen)
            seen.add(remote.get("id"))
            known = [(p, a) for p in store.payouts for a in p.get("allocations", [])
                     if a.get("transfer_id") == remote.get("id")]
            _require(len(known) == 1)
            owner, item = known[0]
            _require(owner["account_id"] == payout["account_id"] and owner["seller_id"] == payout["seller_id"])
            remote_proof(scope, owner, item, remote)
            key, path, data = request_data(scope, owner, item)
            details, result = await backend.operation_details(key), await backend.operation_result(key)
            _require(details is not None and details["status"] == "succeeded" and details["endpoint"] == path
                     and details["request_hash"] == content_hash(data) and result is not None
                     and (result.get("response") or {}).get("id") == remote["id"])
        if allocation.get("transfer_id"):
            _require(allocation["transfer_id"] in seen)


async def _inspect_next(backend, store, stripe, scope, payout, at, *, pending_key=None):
    _require(payout.get("status") in {"requested", "transferring", "awaiting_funds"}
             and not payout.get("cancellation_requested") and not payout.get("provider_payout_id")
             and isinstance(payout.get("scheduled_for"), datetime) and payout["scheduled_for"].tzinfo
             and payout["scheduled_for"] <= at)
    await _unresolved(backend, store, pending_key)
    proofs = await _sources(backend, store, stripe, scope, payout)
    await _existing_transfers(backend, store, stripe, scope, payout, proofs)
    allocation = next((a for a in payout["allocations"] if a["transfer_amount"] and not a.get("transfer_id")), None)
    key, path, data = request_data(scope, payout, allocation, proofs[allocation["id"]]["charge"] if allocation else None)
    for item in payout["allocations"]:
        if item.get("transfer_id"):
            continue
        details = await backend.operation_details(f"allocation-{item['id']}")
        if details:
            _require(pending_key == key == f"allocation-{item['id']}" and details["status"] == "pending"
                     and details["endpoint"] == path and details["request_hash"] == content_hash(data))
    bank = await backend.operation_details(f"bank-payout-{payout['id']}")
    if bank:
        _require(pending_key == key == f"bank-payout-{payout['id']}" and bank["status"] == "pending"
                 and bank["endpoint"] == path and bank["request_hash"] == content_hash(data))
    if allocation is None:
        existing = await stripe.list_financial_objects("payouts", account=payout["account_id"])
        _require(all(isinstance(x.get("metadata"), dict) for x in existing)
                 and not any(x["metadata"].get("payout_id") == payout["id"] for x in existing))
        balance = await stripe.retrieve_balance(payout["account_id"])
        values = balance.get("available")
        yen = [x for x in values if isinstance(x, dict) and x.get("currency") == "jpy"] if isinstance(values, list) else []
        _require(balance.get("object") == "balance" and balance.get("livemode") is True and len(yen) == 1
                 and type(yen[0].get("amount")) is int and yen[0]["amount"] >= payout["net_amount"])
    identity = {k: payout.get(k) for k in ("id", "seller_id", "account_id", "amount", "fee", "net_amount", "request_key", "scheduled_for")}
    identity["allocations"] = [{k: a.get(k) for k in ("id", "order_id", "receipt_id", "amount", "transfer_amount", "transfer_id", "reversal_id")} for a in payout["allocations"]]
    fingerprint = content_hash({"scope": scope.payload(), "payout": identity, "sources": proofs, "request": data})
    return key, path, data, allocation, fingerprint


def _core(proposal):
    return {k: v for k, v in proposal.items() if k not in {"status", "approved_by", "approved_at"}}


async def _review_receipt(backend, proposal, *, create=False):
    key = "live-payout-review-" + proposal["id"]
    proof = _core(proposal)
    response = {"id": key, "proof": encode_value(proof)}
    if create:
        async with backend._connection.transaction():
            await backend._connection.execute(
                "insert into toolbako_runtime.stripe_operations(operation_id,endpoint,request_hash,status,response) "
                "values(%s,%s,%s,'succeeded',%s::jsonb)",
                (key, REVIEW_ENDPOINT, content_hash(proof), json.dumps(response)))
            committed = await backend._save(backend._connection)
        backend._accept_commit(committed)
    else:
        details, result = await backend.operation_details(key), await backend.operation_result(key)
        _require(details is not None and result is not None and details["endpoint"] == REVIEW_ENDPOINT
                 and details["request_hash"] == content_hash(proof) and details["status"] == "succeeded"
                 and result == {"status": "succeeded", "response": response})


async def propose_live_payout(backend, store, stripe, settings, payout, actor_id, *, mfa, request, at=None):
    scope, _ = await _context(backend, store, stripe, settings, mfa, request, actor_id, execute=True)
    at = _now(at)
    async with asyncio.timeout(30):
        key, path, data, allocation, fingerprint = await _inspect_next(backend, store, stripe, scope, payout, at)
    proposal = {"id": str(uuid4()), "status": "proposed", "proposed_by": actor_id, "proposed_at": at,
                "scope": scope.payload(), "payout_id": payout["id"], "operation_id": key,
                "endpoint": path, "request_hash": content_hash(data), "fingerprint": fingerprint,
                "amount": int(data["amount"]), "currency": "jpy"}
    payout[FIELD] = proposal
    store.audit(actor_id, "payout.live_proposed", f"{payout['id']}:{proposal['id']}:{key}")
    await _review_receipt(backend, proposal, create=True)
    return proposal


async def _approval(backend, payout, field, scope, admins, actor, proposal_id, at):
    proposal = payout.get(field) or {}
    _require(proposal.get("id") == proposal_id and proposal.get("status") == "proposed"
             and proposal.get("scope") == scope.payload() and proposal.get("payout_id") == payout["id"]
             and proposal.get("proposed_by") in admins and proposal["proposed_by"] != actor
             and isinstance(proposal.get("proposed_at"), datetime) and proposal["proposed_at"].tzinfo
             and timedelta(0) <= at - proposal["proposed_at"] <= WINDOW)
    await _review_receipt(backend, proposal)
    return proposal


async def _hold(backend, store, payout, proposal, key, *, rejected=False):
    details = await backend.operation_details(key)
    if details and details["status"] == "pending":
        await backend.finish_operation(key, "rejected" if rejected else "unknown")
    payout.update(status="review", hold_reason="本番振込の結果確認が必要です。再送せず二名で照合してください")
    proposal["status"] = "review"
    await backend.save()


def _receipt(remote, allocation):
    keys = ("id", "object", "livemode", "amount", "currency", "metadata", "created",
            "destination", "source_transaction", "transfer_group", "reversed", "amount_reversed") if allocation else (
            "id", "object", "livemode", "amount", "currency", "metadata", "created", "status",
            "automatic", "method", "type", "arrival_date")
    return {k: remote[k] for k in keys if k in remote}


async def approve_live_payout(backend, store, stripe, settings, payout, actor_id, proposal_id, *, mfa, request, at=None):
    scope, admins = await _context(backend, store, stripe, settings, mfa, request, actor_id, execute=True)
    at = _now(at)
    _payout(store, payout)
    proposal = await _approval(backend, payout, FIELD, scope, admins, actor_id, proposal_id, at)
    async with asyncio.timeout(30):
        key, path, data, allocation, fingerprint = await _inspect_next(backend, store, stripe, scope, payout, at)
    _require((key, path, content_hash(data), fingerprint) ==
             (proposal["operation_id"], proposal["endpoint"], proposal["request_hash"], proposal["fingerprint"]))
    _require(type(proposal.get("amount")) is int and proposal["amount"] == int(data["amount"])
             and proposal.get("currency") == data["currency"] == "jpy")
    if allocation is not None:
        allocation["source_charge"] = data["source_transaction"]
    proposal.update(status="executing", approved_by=actor_id, approved_at=at)
    store.audit(actor_id, "payout.live_approved", f"{payout['id']}:{key}:{proposal['proposed_by']}")
    # This atomic checkpoint saves approval consumption + source identity +
    # pending exact intent. A failed/unknown commit MUST NOT reach transport.
    _require(await backend.begin_operation(key, path, data) is None)
    try:
        scope_again, current_admins = await _context(backend, store, stripe, settings, mfa, request, actor_id, execute=True)
        _require(scope_again == scope and proposal["proposed_by"] in current_admins
                 and timedelta(0) <= _now() - proposal["proposed_at"] <= WINDOW)
        await _review_receipt(backend, proposal)
        async with asyncio.timeout(30):
            fresh = await _inspect_next(backend, store, stripe, scope, payout, _now(), pending_key=key)
        _require((fresh[0], fresh[1], fresh[2], fresh[4]) == (key, path, data, fingerprint))
        headers = {"Authorization": f"Bearer {stripe.secret_key}", "Idempotency-Key": key,
                   "Stripe-Version": STRIPE_API_VERSION}
        body = dict(data)
        account = body.pop("_stripe_account", None)
        if account:
            headers["Stripe-Account"] = account
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            # External GETs/client entry can consume the remaining MFA/review
            # window. No await belongs between this final local check and POST.
            _enabled(settings)
            _require(_local_service_context(backend, store, stripe, settings) == scope)
            current_admins = _actor(store, settings, mfa, request, actor_id)
            _require(proposal["proposed_by"] in current_admins and proposal["proposed_by"] != actor_id
                     and proposal.get("approved_by") == actor_id and proposal.get("status") == "executing"
                     and proposal.get("scope") == scope.payload() and proposal.get("operation_id") == key
                     and proposal.get("request_hash") == content_hash(data)
                     and type(proposal.get("amount")) is int and proposal["amount"] == int(data["amount"])
                     and proposal.get("currency") == data["currency"] == "jpy"
                     and timedelta(0) <= _now() - proposal["proposed_at"] <= WINDOW)
            response = await client.post("https://api.stripe.com/v1/" + path, data=body, headers=headers)
        if response.status_code in {400, 401, 403, 404, 422}:
            await _hold(backend, store, payout, proposal, key, rejected=True)
            raise FinanceConflict("本番振込要求は拒否されています。再送せず確認してください")
        _require(response.status_code in {200, 201})
        remote = response.json()
        details = await backend.operation_details(key)
        _require(details is not None and details["status"] == "pending" and details["request_hash"] == content_hash(data))
        remote_proof(scope, payout, allocation, remote, since=int(details["created_at"].timestamp()))
    except Exception:
        details = await backend.operation_details(key)
        if details and details["status"] == "rejected":
            raise FinanceConflict("本番振込要求は拒否されています。再送せず確認してください") from None
        await _hold(backend, store, payout, proposal, key)
        raise StripeOutcomeUnknown(key) from None
    if allocation is not None:
        allocation["transfer_id"] = remote["id"]
        payout["status"] = "transferring"
    else:
        payout.update(provider_payout_id=remote["id"], status="bank_pending")
    proposal["status"] = "executed"
    # Never label bank-paid from the create response. Facts + consumed review +
    # response are one commit; a later authenticated GET confirms settlement.
    await backend.reconcile_operation(key, endpoint=path, request_hash=content_hash(data), response=_receipt(remote, allocation))
    return payout["status"]


async def _recover_inspect(backend, store, stripe, scope, payout, at):
    # A process can die after committing the consumed approval/intent but before
    # recording review status. The original pending intent, not a manual status
    # edit, is the recovery anchor. Nothing here permits a replacement POST.
    _require(payout.get("status") in {"review", "requested", "transferring", "awaiting_funds"}
             and not payout.get("cancellation_requested"))
    # This records a past request's verified outcome, not permission for another
    # payment. Pausing/limiting a seller, a later refund, or another unknown
    # operation must not deadlock GET-only accounting of this original intent.
    await _sources(backend, store, stripe, scope, payout, for_execution=False)
    targets = []
    for allocation in [None, *payout["allocations"]]:
        key, path, data = request_data(scope, payout, allocation)
        details = await backend.operation_details(key)
        if details and details["status"] in {"pending", "unknown"}:
            _require(details["endpoint"] == path and details["request_hash"] == content_hash(data))
            targets.append((key, path, data, allocation, details))
    _require(len(targets) == 1)
    key, path, data, allocation, details = targets[0]
    original = payout.get(FIELD) or {}
    _require(original.get("status") in {"executing", "review"} and original.get("operation_id") == key
             and original.get("scope") == scope.payload() and original.get("request_hash") == content_hash(data)
             and original.get("approved_by") and original.get("approved_by") != original.get("proposed_by"))
    await _review_receipt(backend, original)
    objects = await stripe.list_financial_objects(path, account=payout["account_id"] if allocation is None else None,
        params=None if allocation is None else {"destination": payout["account_id"], "transfer_group": f"order-{allocation['order_id']}"})
    _require(all(isinstance(r.get("metadata"), dict) for r in objects))
    field, identifier = ("payout_id", payout["id"]) if allocation is None else ("allocation_id", allocation["id"])
    matches = [r for r in objects if r["metadata"].get(field) == identifier]
    _require(len(matches) == 1)
    remote = matches[0]
    remote_proof(scope, payout, allocation, remote, since=int(details["created_at"].timestamp()), at=at)
    fingerprint = content_hash({"scope": scope.payload(), "operation": key, "request": data,
                                "provider": _receipt(remote, allocation)})
    return key, path, data, allocation, remote, fingerprint


async def propose_live_recovery(backend, store, stripe, settings, payout, actor_id, *, mfa, request, at=None):
    scope, _ = await _context(backend, store, stripe, settings, mfa, request, actor_id)
    at = _now(at)
    async with asyncio.timeout(30):
        key, path, data, allocation, remote, fingerprint = await _recover_inspect(backend, store, stripe, scope, payout, at)
    proposal = {"id": str(uuid4()), "status": "proposed", "proposed_by": actor_id, "proposed_at": at,
                "scope": scope.payload(), "payout_id": payout["id"], "operation_id": key,
                "endpoint": path, "request_hash": content_hash(data), "fingerprint": fingerprint,
                "amount": int(data["amount"]), "currency": "jpy"}
    payout[RECOVERY_FIELD] = proposal
    payout["status"] = "review"
    store.audit(actor_id, "payout.live_recovery_proposed", f"{payout['id']}:{key}")
    await _review_receipt(backend, proposal, create=True)
    return proposal


async def approve_live_recovery(backend, store, stripe, settings, payout, actor_id, proposal_id, *, mfa, request, at=None):
    scope, admins = await _context(backend, store, stripe, settings, mfa, request, actor_id)
    at = _now(at)
    _payout(store, payout)
    proposal = await _approval(backend, payout, RECOVERY_FIELD, scope, admins, actor_id, proposal_id, at)
    async with asyncio.timeout(30):
        key, path, data, allocation, remote, fingerprint = await _recover_inspect(backend, store, stripe, scope, payout, at)
    _require((key, path, content_hash(data), fingerprint) ==
             (proposal["operation_id"], proposal["endpoint"], proposal["request_hash"], proposal["fingerprint"]))
    _require(type(proposal.get("amount")) is int and proposal["amount"] == int(data["amount"])
             and proposal.get("currency") == data["currency"] == "jpy")
    if allocation is None:
        payout.update(provider_payout_id=remote["id"], status="bank_pending")
    else:
        allocation["transfer_id"] = remote["id"]
        payout["status"] = "transferring"
    payout["hold_reason"] = ""
    proposal.update(status="approved", approved_by=actor_id, approved_at=at)
    payout[FIELD]["status"] = "reconciled"
    store.audit(actor_id, "payout.live_recovery_approved", f"{payout['id']}:{key}:{proposal['proposed_by']}")
    await backend.reconcile_operation(key, endpoint=path, request_hash=content_hash(data), response=_receipt(remote, allocation))
    return payout["status"]


async def validate_live_bank_notification(backend, stripe, scope, payout, remote):
    """Read-only binding/request/receipt proof after an authenticated fresh GET.

    The caller must obtain ``remote`` under the signed event's connected account,
    already matched to payout.account_id. A payload alone proves no account.
    Unknown/pending original intents stay in the two-person GET recovery lane.
    This helper performs no provider request or database/state mutation.
    """
    _require(isinstance(backend, PostgresStateStore) and backend._connection is not None
             and isinstance(backend._store, DemoStore) and isinstance(scope, FinancialScope)
             and scope.mode == "live" and isinstance(stripe, StripeIntegration)
             and stripe.journal is backend and stripe.financial_scope == scope
             and key_mode(stripe.secret_key) == "live" and stripe.charge_mode == "separate"
             and stripe.api_version == STRIPE_API_VERSION)
    await backend.assert_financial_scope(scope)
    _payout(backend._store, payout)
    mapping = _mapping(backend._store, scope, payout["seller_id"])
    _require(mapping is not None and mapping["account_id"] == payout["account_id"]
             and payout.get("provider_payout_id"))
    key, path, data = request_data(scope, payout)
    details, result = await backend.operation_details(key), await backend.operation_result(key)
    _require(details is not None and details["status"] == "succeeded" and details["endpoint"] == path
             and details["request_hash"] == content_hash(data) and result is not None
             and result.get("status") == "succeeded")
    receipt = result.get("response")
    remote_proof(scope, payout, None, receipt, since=int(details["created_at"].timestamp()))
    return remote_proof(scope, payout, None, remote, since=int(details["created_at"].timestamp()))


async def poll_live_bank(backend, store, stripe, settings, payout):
    """GET-only worker step; never consumes stored approval or sends money."""
    scope = await service_context(backend, store, stripe, settings)
    _payout(store, payout)
    _require(payout.get("status") == "bank_pending" and payout.get("provider_payout_id"))
    # Restriction of further payouts after submission must not prevent reading
    # the outcome of the already matched bank request.
    await _seller(backend, store, stripe, scope, payout, require_ready=False)
    remote = await stripe.retrieve_payout(payout["provider_payout_id"], payout["account_id"])
    await validate_live_bank_notification(backend, stripe, scope, payout, remote)
    from .payout_worker import apply_bank_status
    apply_bank_status(payout, remote)
    store.audit(None, "payout.live_polled", f"{payout['id']}:{payout['status']}")
    await backend.save()
    return payout["status"]
