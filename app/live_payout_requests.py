"""Live withdrawal reservations and administrative holds, never money movement.

All changes use the caller's bound Postgres request. A seller request reserves
only independently verified receipts. Pausing is immediate; releasing an
administrative pause consumes a durable review by a different active admin.
Stripe calls in this module are GET-only. Execution remains a separate review.
"""
import asyncio
from copy import copy
from datetime import datetime, timedelta, timezone
import json
import re
from uuid import uuid4

from .database import content_hash
from .finance import FinanceConflict, request_payout, receipts
from .live_payout_execution import (_actor, _local_service_context, _seller,
    _sources, _unresolved, service_context)
from .live_seller_onboarding import _mapping, _inspect_account
from .mfa import SupabaseMFA
from .persistence import encode_value
from .security import SessionService


RELEASE_FIELD = "live_payout_release_review"
RELEASE_ENDPOINT = "internal/live-payout-hold-release-v1"
HOLD_REASON = "運営による振込保留"
RESUMABLE = {"requested", "transferring", "awaiting_funds"}
WINDOW = timedelta(minutes=30)


def _require(value):
    if not value:
        raise FinanceConflict("振込申請・保留解除の本人確認、台帳または承認が一致しません")


def live_payout_requests_ready(settings):
    return (not settings.demo_mode and settings.stripe_connection_mode == "live_financial"
            and settings.stripe_charge_mode == "separate"
            and getattr(settings, "stripe_live_payouts_enabled", False) is True)


def _now(at):
    value = at or datetime.now(timezone.utc)
    _require(isinstance(value, datetime) and value.tzinfo is not None)
    return value


def _seller_actor(store, settings, mfa, request, seller_id):
    _require(isinstance(mfa, SupabaseMFA) and mfa.settings is settings
             and isinstance(mfa.sessions, SessionService) and mfa.sessions.store is store
             and request is not None and request.method == "POST")
    user = mfa.sessions.current(request)
    _require(user is not None and user.get("id") == seller_id and mfa.recent(request))
    accounts = [u for u in store.registered_users.values() if u.get("id") == seller_id]
    _require(len(accounts) == 1 and not any(accounts[0].get(k) for k in ("is_banned", "deleted_at", "erased_at")))


async def request_live_payout(backend, store, stripe, settings, seller_id, amount,
                              request_key, *, mfa, request, at=None):
    _require(live_payout_requests_ready(settings))
    _seller_actor(store, settings, mfa, request, seller_id)
    _require(type(amount) is int and amount >= settings.payout_minimum
             and isinstance(request_key, str) and re.fullmatch(r"[A-Za-z0-9_-]{32}", request_key))
    at = _now(at)
    async with asyncio.timeout(35):
        scope = await service_context(backend, store, stripe, settings)
        mapping = _mapping(store, scope, seller_id)
        _require(mapping is not None)
        await _seller(backend, store, stripe, scope,
                      {"seller_id": seller_id, "account_id": mapping["account_id"]})
        previous = [p for p in store.payouts if p.get("finance_v2") and p.get("seller_id") == seller_id
                    and p.get("request_key") == request_key]
        _require(len(previous) <= 1)
        if previous:
            _require(previous[0].get("amount") == amount and previous[0].get("scope") == scope.payload()
                     and previous[0].get("account_id") == mapping["account_id"])
            return previous[0]
        # Allocation preparation must not mutate the real snapshot before all
        # source GETs pass. Other copied fields are read-only in request_payout.
        candidate = copy(store)
        candidate.payouts = list(store.payouts)
        payout = request_payout(candidate, settings, seller_id, amount, at=at, request_key=request_key)
        payout["scope"] = scope.payload()
        await _unresolved(backend, store)
        await _sources(backend, candidate, stripe, scope, payout)
    # Fresh server session check after provider I/O; no request-supplied actor.
    _seller_actor(store, settings, mfa, request, seller_id)
    _require(live_payout_requests_ready(settings)
             and _local_service_context(backend, store, stripe, settings) == scope)
    store.payouts.insert(0, payout)
    store.audit(seller_id, "payout.requested", payout["id"], request.headers.get("x-request-id", ""))
    await backend.save()
    return payout


async def hold_live_seller_payouts(backend, store, stripe, settings, seller_id, actor_id, *, mfa, request, at=None):
    # A protective local hold must remain possible during provider outages.
    scope = _local_service_context(backend, store, stripe, settings)
    await backend.assert_financial_scope(scope)
    _actor(store, settings, mfa, request, actor_id)
    mapping = _mapping(store, scope, seller_id)
    _require(mapping is not None)
    at = _now(at)
    mapping.update(payouts_paused=True, payouts_paused_at=at)
    mapping.pop(RELEASE_FIELD, None)
    for payout in store.payouts:
        if payout.get("finance_v2") and payout.get("seller_id") == seller_id and payout.get("status") in RESUMABLE:
            payout.update(admin_hold_previous_status=payout["status"], status="held", hold_reason=HOLD_REASON)
    store.audit(actor_id, "admin.payout_hold", seller_id, request.headers.get("x-request-id", ""))
    await backend.save()
    return mapping


def _fingerprint(store, scope, seller_id, mapping):
    payouts = [p for p in store.payouts if p.get("seller_id") == seller_id]
    # Cover every reserved amount, provider reference and hold reason; activity
    # during the review requires a new review instead of releasing new holds.
    return content_hash({"scope": scope.payload(), "seller_id": seller_id,
        "account_id": mapping["account_id"], "paused_at": mapping.get("payouts_paused_at"),
        "payouts": [{k:v for k,v in p.items() if k not in {"live_payout_review", "live_payout_recovery"}}
                    for p in payouts]})


async def _inspect_release(backend, store, stripe, scope, seller_id):
    mapping = _mapping(store, scope, seller_id)
    _require(mapping is not None and mapping.get("payouts_paused") is True)
    await _seller(backend, store, stripe, scope,
                  {"seller_id":seller_id, "account_id":mapping["account_id"]}, require_ready=False)
    status, active = await _inspect_account(stripe, scope, seller_id, mapping["onboarding_operation_id"], mapping["account_id"])
    _require(active and all(value for _, value in status))
    await _unresolved(backend, store)
    for payout in store.payouts:
        if payout.get("seller_id") != seller_id or payout.get("status") != "held" or payout.get("hold_reason") != HOLD_REASON:
            continue
        _require(payout.get("finance_v2") is True and payout.get("admin_hold_previous_status", "requested") in RESUMABLE
                 and not payout.get("cancellation_requested") and not payout.get("provider_payout_id"))
        # Refunded/disputed sources are not revived by an administrative release.
        facts = receipts(store)
        _require(all(facts.get(a.get("receipt_id"), {}).get("eligible") is True for a in payout.get("allocations", [])))
        await _sources(backend, store, stripe, scope, payout, for_execution=False)
    return mapping, _fingerprint(store, scope, seller_id, mapping)


def _core(proposal):
    return {k:v for k,v in proposal.items() if k not in {"status", "approved_by", "approved_at"}}


async def _receipt(backend, proposal, *, create=False):
    key = "live-payout-release-review-" + proposal["id"]
    core = _core(proposal)
    response = {"id":key, "proof":encode_value(core)}
    if create:
        async with backend._connection.transaction():
            await backend._connection.execute(
                "insert into toolbako_runtime.stripe_operations(operation_id,endpoint,request_hash,status,response) "
                "values(%s,%s,%s,'succeeded',%s::jsonb)",
                (key, RELEASE_ENDPOINT, content_hash(core), json.dumps(response)))
            committed = await backend._save(backend._connection)
        backend._accept_commit(committed)
    else:
        details, result = await backend.operation_details(key), await backend.operation_result(key)
        _require(details is not None and details["endpoint"] == RELEASE_ENDPOINT
                 and details["request_hash"] == content_hash(core) and details["status"] == "succeeded"
                 and result == {"status":"succeeded", "response":response})


async def propose_live_seller_release(backend, store, stripe, settings, seller_id, actor_id, *, mfa, request, at=None):
    _require(live_payout_requests_ready(settings))
    _actor(store, settings, mfa, request, actor_id)
    at = _now(at)
    async with asyncio.timeout(35):
        scope = await service_context(backend, store, stripe, settings)
        mapping, fingerprint = await _inspect_release(backend, store, stripe, scope, seller_id)
    _actor(store, settings, mfa, request, actor_id)
    proposal = {"id":str(uuid4()), "status":"proposed", "proposed_by":actor_id,
                "proposed_at":at, "scope":scope.payload(), "seller_id":seller_id,
                "account_id":mapping["account_id"], "fingerprint":fingerprint}
    mapping[RELEASE_FIELD] = proposal
    store.audit(actor_id, "admin.payout_release_proposed", seller_id + ":" + proposal["id"])
    await _receipt(backend, proposal, create=True)
    return proposal


async def approve_live_seller_release(backend, store, stripe, settings, seller_id, actor_id, proposal_id, *, mfa, request, at=None):
    _require(live_payout_requests_ready(settings))
    admins = _actor(store, settings, mfa, request, actor_id)
    at = _now(at)
    async with asyncio.timeout(35):
        scope = await service_context(backend, store, stripe, settings)
        mapping, fingerprint = await _inspect_release(backend, store, stripe, scope, seller_id)
    proposal = mapping.get(RELEASE_FIELD) or {}
    _require(proposal.get("id") == proposal_id and proposal.get("status") == "proposed"
             and proposal.get("scope") == scope.payload() and proposal.get("seller_id") == seller_id
             and proposal.get("account_id") == mapping["account_id"] and proposal.get("fingerprint") == fingerprint
             and proposal.get("proposed_by") in admins and proposal["proposed_by"] != actor_id
             and isinstance(proposal.get("proposed_at"), datetime) and proposal["proposed_at"].tzinfo is not None
             and timedelta(0) <= at - proposal["proposed_at"] <= WINDOW)
    await _receipt(backend, proposal)
    admins = _actor(store, settings, mfa, request, actor_id)
    _require(proposal["proposed_by"] in admins and live_payout_requests_ready(settings)
             and timedelta(0) <= _now(None) - proposal["proposed_at"] <= WINDOW)
    mapping.update(payouts_paused=False, payouts_paused_at=None)
    for payout in store.payouts:
        if payout.get("finance_v2") and payout.get("seller_id") == seller_id and payout.get("status") == "held" and payout.get("hold_reason") == HOLD_REASON:
            payout.update(status=payout.pop("admin_hold_previous_status", "requested"), hold_reason="")
    proposal.update(status="approved", approved_by=actor_id, approved_at=at)
    store.audit(actor_id, "admin.payout_release", seller_id + ":" + proposal_id, request.headers.get("x-request-id", ""))
    await backend.save()
    return mapping


async def cancel_unstarted_live_payout(backend, store, stripe, settings, payout, actor_id, *, mfa, request):
    """Release only an untouched reservation; no reversal or second bank request."""
    scope = _local_service_context(backend, store, stripe, settings)
    await backend.assert_financial_scope(scope)
    _actor(store, settings, mfa, request, actor_id)
    _require(any(p is payout for p in store.payouts) and payout.get("finance_v2") is True
             and payout.get("scope") == scope.payload()
             and payout.get("status") in {"requested", "held", "cancelled"}
             and not payout.get("provider_payout_id") and not payout.get("paid_at"))
    _require(await backend.operation_details("bank-payout-" + payout["id"]) is None)
    for allocation in payout.get("allocations", []):
        _require(not allocation.get("transfer_id") and not allocation.get("reversal_id")
                 and await backend.operation_details("allocation-" + allocation["id"]) is None
                 and await backend.operation_details("reverse-allocation-" + allocation["id"]) is None)
    if payout["status"] == "cancelled":
        return payout
    payout.update(status="cancelled", hold_reason="分配開始前の振込申請を取り消しました")
    store.audit(actor_id, "payout.cancelled_before_transfer", payout["id"], request.headers.get("x-request-id", ""))
    await backend.save()
    return payout
