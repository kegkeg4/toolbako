"""Two-person recovery of a failed Sandbox bank payout.

Each approval performs at most one reversal, reconciles one uncertain reversal,
or releases the reservation after fresh GETs confirm all funds were returned.
Never creates another bank payout. Previously paid payouts require accounting
review and are deliberately excluded from this workflow.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .database import content_hash
from .finance import FinanceConflict
from .integrations import StripeOutcomeUnknown
from .payout_worker import verify_bank_payout
from .reconciliation import _allowed


def bank_data(payout):
    return {"amount": str(payout["net_amount"]), "currency": "jpy", "method": "standard",
            "metadata[payout_id]": payout["id"], "_stripe_account": payout["account_id"]}


def transfer_data(payout, allocation):
    return {"amount": str(allocation["transfer_amount"]), "currency": "jpy",
            "destination": payout["account_id"], "source_transaction": allocation.get("source_charge"),
            "transfer_group": f"order-{allocation['order_id']}",
            "metadata[payout_id]": payout["id"], "metadata[allocation_id]": allocation["id"]}


async def _journal(backend, key, endpoint, data, *, statuses=("succeeded",)):
    operation = await backend.operation_details(key)
    result = await backend.operation_result(key)
    if (not operation or not result or operation["status"] not in statuses
            or operation["endpoint"] != endpoint or operation["request_hash"] != content_hash(data)):
        raise FinanceConflict("元の金融要求と確定結果を確認できません。再送せず詳細確認してください")
    return operation, result.get("response") or {}


def _reversal(remote, allocation, since, at):
    if (remote.get("object") != "transfer_reversal" or not str(remote.get("id", "")).startswith("trr_")
            or remote.get("transfer") != allocation["transfer_id"] or remote.get("currency") != "jpy"
            or type(remote.get("amount")) is not int or remote["amount"] != allocation["transfer_amount"]
            or not isinstance(remote.get("metadata"), dict) or remote["metadata"].get("allocation_id") != allocation["id"]
            or type(remote.get("created")) is not int or not since <= remote["created"] <= int(at.timestamp()) + 60
            or (allocation.get("reversal_id") and allocation["reversal_id"] != remote["id"])):
        raise FinanceConflict("分配回収の金額・参照・作成時刻が一致しません")


async def _inspect(backend, store, stripe, payout, at):
    allocations = payout.get("allocations") or []
    if (payout.get("status") != "bank_failed" or not payout.get("finance_v2") or payout.get("paid_at")
            or not 1 <= len(allocations) <= 10 or not payout.get("provider_payout_id")
            or len({a["id"] for a in allocations}) != len(allocations)
            or any(p is not payout and p.get("provider_payout_id") == payout["provider_payout_id"] for p in store.payouts)):
        raise FinanceConflict("未着金の振込失敗だけ復旧できます。着金履歴・重複・多数の明細は個別照合が必要です")
    bank_operation, bank_result = await _journal(backend, f"bank-payout-{payout['id']}", "payouts", bank_data(payout))
    if bank_result.get("id") != payout["provider_payout_id"]:
        raise FinanceConflict("銀行振込の台帳参照が一致しません")
    remote = await stripe.retrieve_payout(payout["provider_payout_id"], payout["account_id"])
    verify_bank_payout(payout, remote)
    if (remote.get("object") != "payout" or remote.get("livemode") is not False
            or remote.get("automatic") is not False or remote.get("method") != "standard"
            or remote.get("type") != "bank_account" or remote.get("status") not in {"failed", "canceled"}
            or type(remote.get("amount")) is not int):
        raise FinanceConflict("テスト環境の手動銀行振込の失敗・取消を確認できません")
    returned_id = remote.get("failure_balance_transaction")
    if (not isinstance(returned_id, str) or not returned_id.startswith("txn_")
            or returned_id == remote.get("balance_transaction")):
        raise FinanceConflict("失敗した振込の資金が戻るまで保留します")
    returned = await stripe._get(f"balance_transactions/{returned_id}", account=payout["account_id"])
    expected_type = "payout_failure" if remote["status"] == "failed" else "payout_cancel"
    if (returned.get("object") != "balance_transaction" or returned.get("id") != returned_id
            or returned.get("source") != remote["id"] or returned.get("currency") != "jpy"
            or returned.get("type") != expected_type or returned.get("status") != "available"
            or type(returned.get("amount")) is not int or returned["amount"] != payout["net_amount"]
            or type(returned.get("net")) is not int or returned["net"] != payout["net_amount"]
            or type(returned.get("fee")) is not int or returned["fee"] != 0
            or type(returned.get("available_on")) is not int or returned["available_on"] > int(at.timestamp())
            or type(returned.get("created")) is not int
            or not int(bank_operation["created_at"].timestamp()) - 60 <= returned["created"] <= int(at.timestamp()) + 60):
        raise FinanceConflict("元の振込への全額返却・利用可能残高を確認できません")
    bank_proof = {"payout_id": remote["id"], "account_id": payout["account_id"], "amount": remote["amount"],
                  "status": remote["status"], "return_transaction": returned_id}
    identities, completed, missing, uncertain = [], [], [], []
    transfers = set()
    for allocation in allocations:
        amount = allocation.get("transfer_amount")
        if type(amount) is not int or amount < 0:
            raise FinanceConflict("分配金額を確認できません")
        if amount == 0:
            if allocation.get("transfer_id") or await backend.operation_details(f"allocation-{allocation['id']}"):
                raise FinanceConflict("0円明細に分配要求があります")
            continue
        _, result = await _journal(backend, f"allocation-{allocation['id']}", "transfers", transfer_data(payout, allocation))
        transfer_id = allocation.get("transfer_id")
        if (not isinstance(transfer_id, str) or not transfer_id.startswith("tr_") or transfer_id in transfers
                or result.get("id") != transfer_id or not allocation.get("source_charge")
                or any(a.get("transfer_id") == transfer_id for p in store.payouts if p is not payout for a in p.get("allocations", []))):
            raise FinanceConflict("元の分配が不明または重複しています")
        transfers.add(transfer_id)
        parent = await stripe._get(f"transfers/{transfer_id}")
        if (parent.get("object") != "transfer" or parent.get("id") != transfer_id or parent.get("livemode") is not False
                or type(parent.get("amount")) is not int or parent["amount"] != amount or parent.get("currency") != "jpy"
                or parent.get("destination") != payout["account_id"] or parent.get("source_transaction") != allocation["source_charge"]
                or parent.get("transfer_group") != f"order-{allocation['order_id']}"
                or not isinstance(parent.get("metadata"), dict) or parent["metadata"].get("payout_id") != payout["id"]
                or parent["metadata"].get("allocation_id") != allocation["id"]):
            raise FinanceConflict("元の分配・口座・決済が一致しません")
        key, endpoint = f"reverse-allocation-{allocation['id']}", f"transfers/{transfer_id}/reversals"
        data = stripe.reversal_data(allocation)
        data.pop("_idempotency_key")
        operation = await backend.operation_details(key)
        objects = await stripe.list_financial_objects(endpoint)
        if not operation:
            if (objects or parent.get("reversed") is not False or type(parent.get("amount_reversed")) is not int
                    or parent["amount_reversed"] != 0 or allocation.get("reversal_id")):
                raise FinanceConflict("台帳外の分配回収があります。追加回収せず詳細確認してください")
            missing.append(allocation)
        else:
            operation, cached = await _journal(backend, key, endpoint, data, statuses=("succeeded", "pending", "unknown"))
            if (len(objects) != 1 or parent.get("reversed") is not True
                    or type(parent.get("amount_reversed")) is not int or parent["amount_reversed"] != amount):
                raise FinanceConflict("分配回収の全額成功が不明です。再送せず保留します")
            _reversal(objects[0], allocation, int(operation["created_at"].timestamp()) - 60, at)
            if operation["status"] == "succeeded":
                if cached.get("id") != objects[0]["id"]:
                    raise FinanceConflict("分配回収の台帳参照が一致しません")
                completed.append((allocation, objects[0]))
            else:
                uncertain.append((allocation, operation, objects[0]))
        identities.append({"allocation": allocation, "parent": parent, "reversals": objects, "operation": operation})
    if sum(a["transfer_amount"] for a in allocations) != payout["net_amount"]:
        raise FinanceConflict("回収金額が振込金額と一致しません")
    # Resolve unknown requests before allowing another financial mutation.
    action = "reconcile" if uncertain else "reverse" if missing else "release"
    target = uncertain[0][0] if uncertain else missing[0] if missing else None
    fingerprint = content_hash({"bank": bank_proof, "return": returned, "allocations": identities})
    return {"action": action, "target": target, "fingerprint": fingerprint, "bank": bank_proof,
            "completed": completed, "uncertain": uncertain, "remaining": len(missing) + len(uncertain)}


async def _bounded_inspect(*args):
    try:
        async with asyncio.timeout(30):
            return await _inspect(*args)
    except TimeoutError:
        raise FinanceConflict("振込失敗の照合がタイムアウトしました。資金の予約は維持されています") from None


async def propose_failed_payout(backend, store, stripe, settings, payout, actor_id, *, at=None):
    _allowed(settings, stripe, backend, actor_id, store)
    at = at or datetime.now(timezone.utc)
    inspection = await _bounded_inspect(backend, store, stripe, payout, at)
    proposal = {"id": str(uuid4()), "status": "proposed", "proposed_by": actor_id, "proposed_at": at,
                "action": inspection["action"], "fingerprint": inspection["fingerprint"], "bank": inspection["bank"],
                "remaining": inspection["remaining"], "allocation_id": inspection["target"]["id"] if inspection["target"] else None}
    payout["failed_payout_recovery"] = proposal
    store.audit(actor_id, "payout.failure_recovery_proposed", f"{payout['id']}:{proposal['id']}:{proposal['action']}")
    await backend.save()
    return proposal


async def approve_failed_payout(backend, store, stripe, settings, payout, actor_id, proposal_id, *, at=None):
    _allowed(settings, stripe, backend, actor_id, store)
    at = at or datetime.now(timezone.utc)
    proposal = payout.get("failed_payout_recovery") or {}
    if (proposal.get("id") != proposal_id or proposal.get("status") != "proposed"
            or proposal.get("proposed_by") == actor_id or proposal.get("proposed_by") not in settings.admin_user_ids
            or not isinstance(proposal.get("proposed_at"), datetime) or not proposal["proposed_at"].tzinfo
            or not timedelta(0) <= at - proposal["proposed_at"] <= timedelta(minutes=30)):
        raise FinanceConflict("別の管理者が30分以内の照合結果を確認してください")
    inspection = await _bounded_inspect(backend, store, stripe, payout, at)
    if inspection["fingerprint"] != proposal["fingerprint"] or inspection["action"] != proposal["action"]:
        raise FinanceConflict("資金回収の状態が変わりました。最初から照合してください")
    proposal.update(status="approved", approved_by=actor_id, approved_at=at)
    action, target = inspection["action"], inspection["target"]
    store.audit(actor_id, "payout.failure_recovery_approved", f"{payout['id']}:{proposal_id}:{action}:{proposal['proposed_by']}")
    if action == "reconcile":
        allocation, operation, remote = inspection["uncertain"][0]
        allocation["reversal_id"] = remote["id"]
        await backend.reconcile_operation(f"reverse-allocation-{allocation['id']}", endpoint=operation["endpoint"],
                                          request_hash=operation["request_hash"], response=remote)
        return action
    if action == "reverse":
        try:
            remote = await stripe.reverse_allocation(target)
            _reversal(remote, target, int(at.timestamp()) - 60, datetime.now(timezone.utc))
            target["reversal_id"] = remote["id"]
            payout["hold_reason"] = "分配回収の要求を記録しました。全額回収の再照合までは再申請できません"
        except (StripeOutcomeUnknown, RuntimeError, FinanceConflict):
            proposal["status"] = "review"
            payout["hold_reason"] = "分配回収の結果確認が必要です。再送せず、二名で照合してください"
    else:
        for allocation, remote in inspection["completed"]:
            allocation["reversal_id"] = remote["id"]
        proposal["status"] = "released"
        payout.update(status="cancelled", hold_reason="銀行振込の失敗と全額回収を確認し、申請を取り消しました")
    await backend.save()
    return action


async def validate_failed_release(conn, payout):
    """DB commit guard: cancellation cannot bypass confirmed bank/reversal intents."""
    proof = payout.get("failed_payout_recovery") or {}
    bank = proof.get("bank") or {}
    if (proof.get("status") != "released" or proof.get("action") != "release" or payout.get("paid_at")
            or not proof.get("approved_by") or not proof.get("proposed_by") or proof["approved_by"] == proof["proposed_by"]
            or bank.get("payout_id") != payout.get("provider_payout_id") or bank.get("account_id") != payout["account_id"]
            or bank.get("amount") != payout["net_amount"] or bank.get("status") not in {"failed", "canceled"}
            or not str(bank.get("return_transaction", "")).startswith("txn_")):
        raise FinanceConflict("Bank payout requires verified failed-payout recovery before cancellation")
    checks = [(f"bank-payout-{payout['id']}", "payouts", bank_data(payout), payout["provider_payout_id"])]
    for a in payout["allocations"]:
        if a["transfer_amount"]:
            checks.append((f"reverse-allocation-{a['id']}", f"transfers/{a.get('transfer_id')}/reversals",
                           {"amount": str(a["transfer_amount"]), "metadata[allocation_id]": a["id"]}, a.get("reversal_id")))
    for key, endpoint, data, reference in checks:
        row = await (await conn.execute("select endpoint,request_hash,status,response from toolbako_runtime.stripe_operations where operation_id=%s", (key,))).fetchone()
        if not reference or not row or row[:3] != (endpoint, content_hash(data), "succeeded") or (row[3] or {}).get("id") != reference:
            raise FinanceConflict("Unconfirmed bank payout or transfer reversal cannot release funds")
