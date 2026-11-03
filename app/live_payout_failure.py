"""Failed live bank payouts: verified return, one reversal per fresh review.

This lane never retries a bank payout or an uncertain reversal. It can return
only a proven failed bank payout's existing transfers; it cannot collect a
seller debt, debit a bank, or fund a buyer refund. Every reversal and the final
local reservation release require distinct, current, server-authenticated
administrators. Intent plus consumed review commit before provider transport.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import httpx

from .database import content_hash
from .finance import FinanceConflict
from .financial_scope import FinancialScope, SCOPE_OPERATION_ID, validate_binding
from .integrations import StripeOutcomeUnknown
from .live_payout_execution import (_actor, _context, _enabled, _local_service_context,
    _metadata, _now, _payout, _sources, _unresolved, remote_proof, request_data,
    validate_live_bank_notification)
from .live_setup_plan import STRIPE_API_VERSION
from .persistence import encode_value
from .stripe_bank_proof import _reference


FIELD = "failed_payout_recovery"
APPROVALS = "live_failed_reversal_approvals"
ENDPOINT = "internal/live-failed-payout-review-v1"
WINDOW = timedelta(minutes=30)


def _require(value):
    if not value:
        raise FinanceConflict("銀行振込の失敗・全額返却・分配回収を確認できません。再送せず保留します")


def reversal_data(scope, payout, allocation):
    key = "reverse-allocation-" + allocation["id"]
    endpoint = "transfers/" + allocation["transfer_id"] + "/reversals"
    data = {"amount": str(allocation["transfer_amount"])}
    data.update({f"metadata[{k}]": v for k, v in _metadata(scope, payout, allocation).items()})
    return key, endpoint, data


def _core(proposal):
    return {k: v for k, v in proposal.items() if k not in {"status", "approved_by", "approved_at"}}


async def _review_receipt(backend, proposal, *, create=False):
    key = "live-failed-payout-review-" + proposal["id"]
    core = _core(proposal)
    response = {"id": key, "proof": encode_value(core)}
    if create:
        async with backend._connection.transaction():
            await backend._connection.execute(
                "insert into toolbako_runtime.stripe_operations(operation_id,endpoint,request_hash,status,response) "
                "values(%s,%s,%s,'succeeded',%s::jsonb)",
                (key, ENDPOINT, content_hash(core), json.dumps(response)))
            committed = await backend._save(backend._connection)
        backend._accept_commit(committed)
    else:
        details, result = await backend.operation_details(key), await backend.operation_result(key)
        _require(details is not None and details["endpoint"] == ENDPOINT and details["status"] == "succeeded"
                 and details["request_hash"] == content_hash(core)
                 and result == {"status": "succeeded", "response": response})


def _reversal(scope, payout, allocation, remote, since, at):
    expected = _metadata(scope, payout, allocation)
    _require(isinstance(remote, dict) and remote.get("object") == "transfer_reversal"
             and _reference(remote.get("id"), "trr")
             and remote.get("transfer") == allocation["transfer_id"] and remote.get("currency") == "jpy"
             and type(remote.get("amount")) is int and remote["amount"] == allocation["transfer_amount"]
             and isinstance(remote.get("metadata"), dict)
             and all(remote["metadata"].get(k) == v for k, v in expected.items())
             and type(remote.get("created")) is int and since - 60 <= remote["created"] <= int(at.timestamp()) + 60
             and ("livemode" not in remote or remote["livemode"] is True)
             and (not allocation.get("reversal_id") or allocation["reversal_id"] == remote["id"]))


def _receipt(remote):
    result = {k: remote[k] for k in ("id", "object", "amount", "currency", "transfer", "created", "livemode") if k in remote}
    result["metadata"] = {k: remote["metadata"][k] for k in ("payout_id", "allocation_id", "ledger_id", "platform_account_id")}
    return result


def _returned(remote, returned, payout, created_at, at):
    returned_id = remote.get("failure_balance_transaction")
    _require(_reference(returned_id, "txn")
             and returned_id != remote.get("balance_transaction"))
    _require(isinstance(returned, dict) and returned.get("object") == "balance_transaction"
             and returned.get("id") == returned_id and returned.get("source") == remote["id"]
             and returned.get("currency") == "jpy" and returned.get("status") == "available"
             and returned.get("type") == ("payout_failure" if remote["status"] == "failed" else "payout_cancel")
             and type(returned.get("amount")) is int and returned["amount"] == payout["net_amount"]
             and type(returned.get("net")) is int and returned["net"] == payout["net_amount"]
             and type(returned.get("fee")) is int and returned["fee"] == 0
             and type(returned.get("available_on")) is int and returned["available_on"] <= int(at.timestamp())
             and type(returned.get("created")) is int
             and int(created_at.timestamp()) - 60 <= returned["created"] <= int(at.timestamp()) + 60
             and ("livemode" not in returned or returned["livemode"] is True))


async def _journal(backend, key, path, data, statuses=("succeeded",)):
    details, result = await backend.operation_details(key), await backend.operation_result(key)
    _require(details is not None and result is not None and details["status"] in statuses
             and details["endpoint"] == path and details["request_hash"] == content_hash(data))
    return details, result.get("response") or {}


async def _inspect(backend, store, stripe, scope, payout, at, *, pending_key=None):
    _payout(store, payout)
    _require(payout.get("scope") == scope.payload() and payout.get("status") == "bank_failed"
             and not payout.get("paid_at") and _reference(payout.get("provider_payout_id"), "po")
             and sum(p.get("provider_payout_id") == payout["provider_payout_id"] for p in store.payouts) == 1)
    await _sources(backend, store, stripe, scope, payout, for_execution=False)
    bank_key, bank_path, bank_data = request_data(scope, payout)
    bank_op, _ = await _journal(backend, bank_key, bank_path, bank_data)
    remote = await stripe.retrieve_payout(payout["provider_payout_id"], payout["account_id"])
    await validate_live_bank_notification(backend, stripe, scope, payout, remote)
    _require(remote.get("status") in {"failed", "canceled"})
    returned_id = remote.get("failure_balance_transaction")
    _require(_reference(returned_id, "txn")
             and returned_id != remote.get("balance_transaction"))
    returned = await stripe._get("balance_transactions/" + returned_id, account=payout["account_id"])
    _returned(remote, returned, payout, bank_op["created_at"], at)
    bank = {"payout_id": remote["id"], "account_id": payout["account_id"], "amount": remote["amount"],
            "status": remote["status"], "return_transaction": returned_id, "scope": scope.payload()}
    completed, uncertain, missing, identities, seen = [], [], [], [], set()
    for a in payout["allocations"]:
        if not a["transfer_amount"]:
            _require(not a.get("transfer_id") and not a.get("reversal_id")
                     and await backend.operation_details("allocation-" + a["id"]) is None
                     and await backend.operation_details("reverse-allocation-" + a["id"]) is None)
            continue
        key, path, data = request_data(scope, payout, a)
        transfer_op, transfer_receipt = await _journal(backend, key, path, data)
        transfer_id = a.get("transfer_id")
        _require(_reference(transfer_id, "tr") and transfer_id not in seen
                 and transfer_receipt.get("id") == transfer_id
                 and sum(other.get("transfer_id") == transfer_id for p in store.payouts
                         for other in p.get("allocations", [])) == 1)
        seen.add(transfer_id)
        # Original succeeded receipt must be an unreversed, scoped transfer.
        remote_proof(scope, payout, a, transfer_receipt, since=int(transfer_op["created_at"].timestamp()), at=at)
        parent = await stripe._get("transfers/" + transfer_id)
        _require(isinstance(parent, dict) and type(parent.get("amount_reversed")) is int
                 and parent["amount_reversed"] in {0, a["transfer_amount"]}
                 and parent.get("reversed") is (parent["amount_reversed"] == a["transfer_amount"]))
        # Reuse the exact source/mode/metadata proof, permitting only full or
        # zero reversal after the current parent fields above are checked.
        remote_proof(scope, payout, a, {**parent, "reversed": False, "amount_reversed": 0},
                     since=int(transfer_op["created_at"].timestamp()), at=at)
        key, path, data = reversal_data(scope, payout, a)
        op = await backend.operation_details(key)
        objects = await stripe.list_financial_objects(path)
        if op is None or key == pending_key:
            if op is not None:
                _require(op["status"] == "pending" and op["endpoint"] == path and op["request_hash"] == content_hash(data))
            _require(not objects and parent["amount_reversed"] == 0 and not a.get("reversal_id"))
            missing.append(a)
        else:
            op, cached = await _journal(backend, key, path, data, ("succeeded", "pending", "unknown"))
            # No absent object can authorize a replacement POST.
            _require(len(objects) == 1 and parent["reversed"] is True and parent["amount_reversed"] == a["transfer_amount"])
            _reversal(scope, payout, a, objects[0], int(op["created_at"].timestamp()), at)
            original = (payout.get(APPROVALS) or {}).get(key) or {}
            _require(original.get("scope") == scope.payload() and original.get("payout_id") == payout["id"]
                     and original.get("operation_id") == key and original.get("action") == "reverse"
                     and original.get("request_hash") == content_hash(data)
                     and original.get("status") in {"executing", "review", "executed", "reconciled"}
                     and original.get("approved_by") and original["approved_by"] != original.get("proposed_by"))
            await _review_receipt(backend, original)
            if op["status"] == "succeeded":
                _require(cached.get("id") == objects[0]["id"])
                _reversal(scope, payout, a, cached, int(op["created_at"].timestamp()), at)
                completed.append((a, objects[0]))
            else:
                uncertain.append((a, op, objects[0]))
        # Exclude the exact pending checkpoint from the immutable review facts.
        identities.append({"id": a["id"], "transfer": parent, "reversals": objects,
                           "operation": None if key == pending_key else op})
    action = "reconcile" if uncertain else "reverse" if missing else "release"
    target = uncertain[0][0] if uncertain else missing[0] if missing else None
    if action == "reverse":
        # Do not use another seller's or an unfunded balance to recover money.
        funds = await stripe.retrieve_balance(payout["account_id"])
        values = funds.get("available") if isinstance(funds, dict) else None
        yen = [x for x in values if isinstance(x, dict) and x.get("currency") == "jpy"] if isinstance(values, list) else []
        _require(funds.get("object") == "balance" and funds.get("livemode") is True and len(yen) == 1
                 and type(yen[0].get("amount")) is int and yen[0]["amount"] >= target["transfer_amount"])
        await _unresolved(backend, store, pending_key)
    elif action == "release":
        await _unresolved(backend, store)
    fingerprint = content_hash({"scope": scope.payload(), "bank": bank, "return": returned,
                                "allocations": identities})
    return {"action": action, "target": target, "fingerprint": fingerprint, "bank": bank,
            "completed": completed, "uncertain": uncertain, "remaining": len(missing) + len(uncertain)}


async def propose_live_failed_payout(backend, store, stripe, settings, payout, actor_id, *, mfa, request, at=None):
    scope, _ = await _context(backend, store, stripe, settings, mfa, request, actor_id)
    at = _now(at)
    async with asyncio.timeout(35):
        facts = await _inspect(backend, store, stripe, scope, payout, at)
    if facts["action"] == "reverse": _enabled(settings)
    _actor(store, settings, mfa, request, actor_id)
    target = facts["target"]
    key, path, data = reversal_data(scope, payout, target) if target else ("", "", {})
    proposal = {"id": str(uuid4()), "status": "proposed", "proposed_by": actor_id, "proposed_at": at,
        "scope": scope.payload(), "payout_id": payout["id"], "action": facts["action"],
        "fingerprint": facts["fingerprint"], "bank": facts["bank"], "remaining": facts["remaining"],
        "operation_id": key, "endpoint": path, "request_hash": content_hash(data),
        "amount": target["transfer_amount"] if target else payout["net_amount"], "currency": "jpy"}
    payout[FIELD] = proposal
    store.audit(actor_id, "payout.live_failure_proposed", f"{payout['id']}:{proposal['id']}:{facts['action']}")
    await _review_receipt(backend, proposal, create=True)
    return proposal


def _approval(proposal, scope, payout, admins, actor_id, proposal_id, at):
    _require(proposal.get("id") == proposal_id and proposal.get("status") == "proposed"
             and proposal.get("scope") == scope.payload() and proposal.get("payout_id") == payout["id"]
             and proposal.get("proposed_by") in admins and proposal["proposed_by"] != actor_id
             and isinstance(proposal.get("proposed_at"), datetime) and proposal["proposed_at"].tzinfo is not None
             and timedelta(0) <= at - proposal["proposed_at"] <= WINDOW)


async def _hold(backend, payout, proposal, key, *, rejected=False):
    details = await backend.operation_details(key)
    if details and details["status"] == "pending":
        await backend.finish_operation(key, "rejected" if rejected else "unknown")
    proposal["status"] = "review"
    payout["hold_reason"] = "分配回収の結果が未確定です。再送せず二名で照合してください"
    await backend.save()


async def approve_live_failed_payout(backend, store, stripe, settings, payout, actor_id, proposal_id, *, mfa, request, at=None):
    scope, admins = await _context(backend, store, stripe, settings, mfa, request, actor_id)
    at = _now(at)
    proposal = payout.get(FIELD) or {}
    _approval(proposal, scope, payout, admins, actor_id, proposal_id, at)
    await _review_receipt(backend, proposal)
    async with asyncio.timeout(35):
        facts = await _inspect(backend, store, stripe, scope, payout, at)
    action, target = facts["action"], facts["target"]
    key, path, data = reversal_data(scope, payout, target) if target else ("", "", {})
    _require((action, facts["fingerprint"], key, path, content_hash(data)) ==
             (proposal["action"], proposal["fingerprint"], proposal["operation_id"], proposal["endpoint"], proposal["request_hash"])
             and type(proposal.get("amount")) is int and proposal["amount"] == (target["transfer_amount"] if target else payout["net_amount"])
             and proposal.get("currency") == "jpy")
    admins = _actor(store, settings, mfa, request, actor_id)
    _approval(proposal, scope, payout, admins, actor_id, proposal_id, _now())
    proposal.update(status="executing", approved_by=actor_id, approved_at=at)
    store.audit(actor_id, "payout.live_failure_approved", f"{payout['id']}:{proposal_id}:{action}:{proposal['proposed_by']}")
    if action == "reconcile":
        a, operation, remote = facts["uncertain"][0]
        a["reversal_id"] = remote["id"]
        payout[APPROVALS][key]["status"] = "reconciled"
        proposal["status"] = "reconciled"
        await backend.reconcile_operation(key, endpoint=path, request_hash=content_hash(data), response=_receipt(remote))
        return action
    if action == "release":
        for a, remote in facts["completed"]: a["reversal_id"] = remote["id"]
        proposal["status"] = "released"
        payout.update(status="cancelled", hold_reason="銀行振込の失敗と全額回収を確認し、申請を取り消しました")
        await backend.save()
        return action
    _enabled(settings)
    payout.setdefault(APPROVALS, {})[key] = proposal
    _require(await backend.begin_operation(key, path, data) is None)
    try:
        scope_again, admins = await _context(backend, store, stripe, settings, mfa, request, actor_id, execute=True)
        _require(scope_again == scope and proposal["proposed_by"] in admins)
        await _review_receipt(backend, proposal)
        async with asyncio.timeout(35):
            fresh = await _inspect(backend, store, stripe, scope, payout, _now(), pending_key=key)
        _require(fresh["action"] == action and fresh["fingerprint"] == facts["fingerprint"])
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            _enabled(settings)
            _require(_local_service_context(backend, store, stripe, settings) == scope)
            admins = _actor(store, settings, mfa, request, actor_id)
            _require(proposal["proposed_by"] in admins and proposal["proposed_by"] != actor_id
                     and proposal.get("approved_by") == actor_id and proposal.get("status") == "executing"
                     and proposal.get("scope") == scope.payload() and proposal.get("operation_id") == key
                     and proposal.get("request_hash") == content_hash(data)
                     and proposal.get("action") == "reverse" and proposal.get("endpoint") == path
                     and type(proposal.get("amount")) is int and proposal["amount"] == int(data["amount"])
                     and proposal.get("currency") == "jpy"
                     and timedelta(0) <= _now() - proposal["proposed_at"] <= WINDOW)
            response = await client.post("https://api.stripe.com/v1/" + path, data=data, headers={
                "Authorization": "Bearer " + stripe.secret_key, "Idempotency-Key": key, "Stripe-Version": STRIPE_API_VERSION})
        if response.status_code in {400, 401, 403, 404, 422}:
            await _hold(backend, payout, proposal, key, rejected=True)
            raise FinanceConflict("分配回収要求は拒否されました。残高を保持して個別確認してください")
        _require(response.status_code in {200, 201})
        remote = response.json()
        op = await backend.operation_details(key)
        _require(op is not None and op["status"] == "pending" and op["request_hash"] == content_hash(data))
        _reversal(scope, payout, target, remote, int(op["created_at"].timestamp()), _now())
    except Exception:
        op = await backend.operation_details(key)
        if op and op["status"] == "rejected":
            raise FinanceConflict("分配回収要求は拒否されました。再送せず確認してください") from None
        await _hold(backend, payout, proposal, key)
        raise StripeOutcomeUnknown(key) from None
    target["reversal_id"] = remote["id"]
    proposal["status"] = "executed"
    payout["hold_reason"] = "分配回収を記録しました。全額回収を別の承認で照合するまで再申請できません"
    await backend.reconcile_operation(key, endpoint=path, request_hash=content_hash(data), response=_receipt(remote))
    return action


async def validate_live_failed_release(conn, payout):
    """Atomic ledger guard: durable scoped review and every exact receipt."""
    raw = payout.get("scope") or {}
    scope = FinancialScope(raw.get("mode"), raw.get("platform_account"), raw.get("ledger_id"))
    _require(scope.mode == "live")
    binding = await (await conn.execute("select endpoint,request_hash,status,response from toolbako_runtime.stripe_operations where operation_id=%s", (SCOPE_OPERATION_ID,))).fetchone()
    validate_binding(binding, scope)
    proposal = payout.get(FIELD) or {}
    bank = proposal.get("bank") or {}
    _require(proposal.get("status") == "released" and proposal.get("action") == "release"
             and proposal.get("scope") == scope.payload() and proposal.get("payout_id") == payout["id"]
             and type(proposal.get("amount")) is int and proposal["amount"] == payout["net_amount"]
             and proposal.get("currency") == "jpy"
             and not payout.get("paid_at") and proposal.get("approved_by") and proposal.get("proposed_by")
             and proposal["approved_by"] != proposal["proposed_by"]
             and bank.get("scope") == scope.payload() and bank.get("payout_id") == payout.get("provider_payout_id")
             and bank.get("account_id") == payout["account_id"] and bank.get("amount") == payout["net_amount"]
             and bank.get("status") in {"failed", "canceled"} and _reference(bank.get("return_transaction"), "txn"))
    review_key = "live-failed-payout-review-" + proposal["id"]
    checks = [(review_key, ENDPOINT, _core(proposal), review_key)]
    key, path, data = request_data(scope, payout)
    checks.append((key, path, data, payout["provider_payout_id"]))
    for a in payout["allocations"]:
        if a["transfer_amount"]:
            key, path, data = reversal_data(scope, payout, a)
            checks.append((key, path, data, a.get("reversal_id")))
    for key, path, data, reference in checks:
        row = await (await conn.execute("select endpoint,request_hash,status,response from toolbako_runtime.stripe_operations where operation_id=%s", (key,))).fetchone()
        _require(reference and row and row[:3] == (path, content_hash(data), "succeeded")
                 and (row[3] or {}).get("id") == reference)
        if path == ENDPOINT:
            _require(row[3] == {"id": review_key, "proof": encode_value(_core(proposal))})
