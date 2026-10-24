"""Two-person recovery of uncertain Sandbox transfers/reversals/payouts, using GET only.

No absence result authorizes a retry. Both reviews fetch the complete bounded
provider list and reject duplicates or mismatches before resolving the journal.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .database import content_hash
from .finance import FinanceConflict
from .payout_worker import sandbox_payouts_ready, verify_bank_payout


def _allowed(settings, stripe, backend, actor_id, store):
    if (not sandbox_payouts_ready(settings) or stripe.journal is not backend
            or stripe.secret_key != settings.stripe_secret_key or stripe.charge_mode != "separate"
            or actor_id not in settings.admin_user_ids or len(set(settings.admin_user_ids)) < 2
            or backend._store is not store):
        raise FinanceConflict("接続済みのテスト環境と、別々の管理者2名が必要です")


async def _target(backend, payout):
    bank_id = f"bank-payout-{payout['id']}"
    operation = await backend.operation_details(bank_id)
    if operation and operation["status"] in {"pending", "unknown"}:
        data = {"amount": str(payout["net_amount"]), "currency": "jpy", "method": "standard",
                "metadata[payout_id]": payout["id"], "_stripe_account": payout["account_id"]}
        if operation["endpoint"] != "payouts" or operation["request_hash"] != content_hash(data):
            raise FinanceConflict("銀行振込の要求内容が一致しません")
        return bank_id, operation, None
    for allocation in payout["allocations"]:
        operation_id = f"reverse-allocation-{allocation['id']}"
        operation = await backend.operation_details(operation_id)
        if not operation or operation["status"] not in {"pending", "unknown"}:
            continue
        if payout.get("provider_payout_id") or await backend.operation_details(bank_id):
            raise FinanceConflict("銀行振込開始済みの分配取消は復旧できません")
        transfer_id = allocation.get("transfer_id")
        data = {"amount": str(allocation["transfer_amount"]), "metadata[allocation_id]": allocation["id"]}
        if (not isinstance(transfer_id, str) or not transfer_id.startswith("tr_")
                or type(allocation["transfer_amount"]) is not int or allocation["transfer_amount"] <= 0
                or operation["endpoint"] != f"transfers/{transfer_id}/reversals"
                or operation["request_hash"] != content_hash(data)):
            raise FinanceConflict("分配取消の要求内容が一致しません")
        return operation_id, operation, allocation
    for allocation in payout["allocations"]:
        operation_id = f"allocation-{allocation['id']}"
        operation = await backend.operation_details(operation_id)
        if not operation or operation["status"] not in {"pending", "unknown"}:
            continue
        data = {"amount": str(allocation["transfer_amount"]), "currency": "jpy",
                "destination": payout["account_id"], "source_transaction": allocation.get("source_charge"),
                "transfer_group": f"order-{allocation['order_id']}",
                "metadata[payout_id]": payout["id"], "metadata[allocation_id]": allocation["id"]}
        if (not allocation.get("source_charge") or operation["endpoint"] != "transfers"
                or operation["request_hash"] != content_hash(data)):
            raise FinanceConflict("分配の要求内容が一致しません")
        return operation_id, operation, allocation
    raise FinanceConflict("照合対象の結果不明な分配・分配取消・銀行振込がありません")


async def _inspect(backend, stripe, payout, at):
    operation_id, operation, allocation = await _target(backend, payout)
    reversal = operation["endpoint"].endswith("/reversals")
    parent = None
    if reversal:
        transfer_operation = await backend.operation_details(f"allocation-{allocation['id']}")
        transfer_result = await backend.operation_result(f"allocation-{allocation['id']}")
        transfer_data = {"amount": str(allocation["transfer_amount"]), "currency": "jpy",
            "destination": payout["account_id"], "source_transaction": allocation.get("source_charge"),
            "transfer_group": f"order-{allocation['order_id']}",
            "metadata[payout_id]": payout["id"], "metadata[allocation_id]": allocation["id"]}
        if (not allocation.get("source_charge") or not transfer_operation or not transfer_result
                or transfer_operation["endpoint"] != "transfers"
                or transfer_operation["request_hash"] != content_hash(transfer_data)
                or transfer_result["status"] != "succeeded"
                or (transfer_result.get("response") or {}).get("id") != allocation["transfer_id"]):
            raise FinanceConflict("取消元の分配台帳を確認できません")
        parent = await stripe._get(f"transfers/{allocation['transfer_id']}")
        if (parent.get("object") != "transfer" or parent.get("id") != allocation["transfer_id"]
                or parent.get("livemode") is not False or type(parent.get("amount")) is not int
                or parent["amount"] != allocation["transfer_amount"] or parent.get("currency") != "jpy"
                or parent.get("destination") != payout["account_id"]
                or parent.get("source_transaction") != allocation["source_charge"]
                or parent.get("transfer_group") != f"order-{allocation['order_id']}"
                or not isinstance(parent.get("metadata"), dict)
                or parent["metadata"].get("allocation_id") != allocation["id"]
                or parent["metadata"].get("payout_id") != payout["id"]
                or parent.get("reversed") is not True or type(parent.get("amount_reversed")) is not int
                or parent["amount_reversed"] != allocation["transfer_amount"]):
            raise FinanceConflict("取消元の分配・テスト環境・全額回収を確認できません")
        # Embedded reversals only contain the latest 10. Always fetch all pages.
        objects = await stripe.list_financial_objects(operation["endpoint"])
    elif allocation is None:
        objects = await stripe.list_financial_objects("payouts", account=payout["account_id"],
            params={"created[gte]": str(int(operation["created_at"].timestamp()) - 60)})
    else:
        objects = await stripe.list_financial_objects("transfers", params={
            "destination": payout["account_id"], "transfer_group": f"order-{allocation['order_id']}"})
    if any(not isinstance(obj.get("metadata"), dict) for obj in objects):
        raise FinanceConflict("Stripeの照合記録の形式を確認できません。再送せず詳細確認してください")
    metadata_key, expected_id = ("payout_id", payout["id"]) if allocation is None else ("allocation_id", allocation["id"])
    matches = [obj for obj in objects if obj["metadata"].get(metadata_key) == expected_id]
    if len(matches) != 1:
        raise FinanceConflict("該当記録が0件または複数あります。再送せずStripeで詳細確認してください")
    remote = matches[0]
    if not reversal and remote.get("livemode") is not False:
        raise FinanceConflict("テスト環境の記録ではありません")
    if reversal:
        if (remote.get("object") != "transfer_reversal" or not str(remote.get("id", "")).startswith("trr_")
                or remote.get("transfer") != allocation["transfer_id"] or remote.get("currency") != "jpy"
                or type(remote.get("amount")) is not int or remote["amount"] != allocation["transfer_amount"]
                or type(remote.get("created")) is not int
                or not int(operation["created_at"].timestamp()) - 60 <= remote["created"] <= int(at.timestamp()) + 60
                or (allocation.get("reversal_id") and allocation["reversal_id"] != remote["id"])
                or len(objects) != 1):
            raise FinanceConflict("分配取消の元分配・全額・作成日時が一致しません")
        identity = {"reversal": {key: remote.get(key) for key in ("id", "object", "amount", "currency", "transfer", "metadata", "created")},
                    "transfer": {key: parent.get(key) for key in ("id", "amount", "currency", "destination", "source_transaction", "transfer_group", "metadata", "livemode", "reversed", "amount_reversed")}}
    elif allocation is None:
        verify_bank_payout({**payout, "provider_payout_id": remote.get("id")}, remote)
        if (remote.get("object") != "payout" or remote.get("automatic") is not False
                or remote.get("method") != "standard" or remote.get("status") not in {"pending", "in_transit", "paid", "failed", "canceled"}):
            raise FinanceConflict("銀行振込の種別・状態が一致しません")
        identity = (remote["id"], remote["amount"], remote["currency"], payout["account_id"], remote["metadata"],
                    remote.get("destination"), remote["method"], remote["automatic"], remote["livemode"])
    else:
        if (remote.get("object") != "transfer" or not str(remote.get("id", "")).startswith("tr_")
                or type(remote.get("amount")) is not int or remote["amount"] != allocation["transfer_amount"]
                or remote.get("currency") != "jpy" or remote.get("destination") != payout["account_id"]
                or remote.get("source_transaction") != allocation["source_charge"]
                or remote.get("transfer_group") != f"order-{allocation['order_id']}"
                or (remote.get("metadata") or {}).get("payout_id") != payout["id"]
                or remote.get("reversed") is not False or remote.get("amount_reversed") != 0):
            raise FinanceConflict("分配記録の金額・口座・元決済が一致しません")
        identity = (remote["id"], remote["amount"], remote["currency"], remote["destination"], remote["source_transaction"],
                    remote["transfer_group"], remote["metadata"], remote["livemode"], remote["amount_reversed"])
    return operation_id, operation, allocation, remote, content_hash({"provider": identity, "cancellation_requested": bool(payout.get("cancellation_requested"))})


async def _bounded_inspect(*args):
    try:
        async with asyncio.timeout(30):
            return await _inspect(*args)
    except TimeoutError:
        raise FinanceConflict("Stripeの照合がタイムアウトしました。再送せず再確認してください") from None


async def propose_recovery(backend, store, stripe, settings, payout, actor_id, *, at=None):
    _allowed(settings, stripe, backend, actor_id, store)
    if payout["status"] != "review":
        raise FinanceConflict("運営確認中の申請だけ照合できます")
    at = at or datetime.now(timezone.utc)
    operation_id, operation, _, remote, fingerprint = await _bounded_inspect(backend, stripe, payout, at)
    proposal = {"id": str(uuid4()), "status": "proposed", "proposed_by": actor_id, "proposed_at": at,
                "operation_id": operation_id, "endpoint": operation["endpoint"],
                "request_hash": operation["request_hash"], "provider_reference": remote["id"], "fingerprint": fingerprint}
    payout["reconciliation"] = proposal
    store.audit(actor_id, "payout.reconciliation_proposed", f"{payout['id']}:{operation_id}:{remote['id']}")
    await backend.save()
    return proposal


async def approve_recovery(backend, store, stripe, settings, payout, actor_id, proposal_id, *, at=None):
    _allowed(settings, stripe, backend, actor_id, store)
    proposal = payout.get("reconciliation") or {}
    at = at or datetime.now(timezone.utc)
    if (payout["status"] != "review" or proposal.get("status") != "proposed" or proposal.get("id") != proposal_id
            or proposal.get("proposed_by") == actor_id or proposal.get("proposed_by") not in settings.admin_user_ids
            or not isinstance(proposal.get("proposed_at"), datetime)
            or not proposal["proposed_at"].tzinfo
            or not timedelta(0) <= at - proposal["proposed_at"] <= timedelta(minutes=30)):
        raise FinanceConflict("別の管理者が30分以内の照合結果を確認してください。古い画面は再読み込みしてください")
    operation_id, operation, allocation, remote, fingerprint = await _bounded_inspect(backend, stripe, payout, at)
    if (operation_id != proposal["operation_id"] or operation["request_hash"] != proposal["request_hash"]
            or operation["endpoint"] != proposal["endpoint"] or remote["id"] != proposal["provider_reference"]
            or fingerprint != proposal["fingerprint"]):
        raise FinanceConflict("照合結果が変更されています。再確認が必要です")
    if operation["endpoint"].endswith("/reversals"):
        allocation["reversal_id"] = remote["id"]
        payout.update(status="reversing", cancellation_requested=True, hold_reason="")
    elif allocation is None:
        payout.update(provider_payout_id=remote["id"], status="bank_pending", hold_reason="")
    else:
        allocation["transfer_id"] = remote["id"]
        payout.update(status="reversing" if payout.get("cancellation_requested") else "transferring", hold_reason="")
    proposal.update(status="approved", approved_by=actor_id, approved_at=at)
    store.audit(actor_id, "payout.reconciliation_approved", f"{payout['id']}:{operation_id}:{remote['id']}:{proposal['proposed_by']}")
    await backend.reconcile_operation(operation_id, endpoint=operation["endpoint"],
                                    request_hash=operation["request_hash"], response=remote)
    return payout["status"]
