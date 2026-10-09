"""Two-person recovery of uncertain Sandbox transfers/payouts, using GET only.

No absence result authorizes a retry. Both reviews fetch the complete bounded
provider list and reject duplicates or mismatches before resolving the journal.
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .database import content_hash
from .finance import FinanceConflict
from .payout_worker import sandbox_payouts_ready, verify_bank_payout


def _allowed(settings, stripe, backend, actor_id):
    if (not sandbox_payouts_ready(settings) or stripe.journal is not backend
            or stripe.secret_key != settings.stripe_secret_key or stripe.charge_mode != "separate"
            or actor_id not in settings.admin_user_ids or len(set(settings.admin_user_ids)) < 2):
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
    raise FinanceConflict("照合対象の結果不明な分配・銀行振込がありません")


async def _inspect(backend, stripe, payout):
    operation_id, operation, allocation = await _target(backend, payout)
    if allocation is None:
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
    if remote.get("livemode") is not False:
        raise FinanceConflict("テスト環境の記録ではありません")
    if allocation is None:
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
    return operation_id, operation, allocation, remote, content_hash(identity)


async def propose_recovery(backend, store, stripe, settings, payout, actor_id, *, at=None):
    _allowed(settings, stripe, backend, actor_id)
    if payout["status"] != "review":
        raise FinanceConflict("運営確認中の申請だけ照合できます")
    operation_id, operation, _, remote, fingerprint = await _inspect(backend, stripe, payout)
    at = at or datetime.now(timezone.utc)
    proposal = {"id": str(uuid4()), "status": "proposed", "proposed_by": actor_id, "proposed_at": at,
                "operation_id": operation_id, "endpoint": operation["endpoint"],
                "request_hash": operation["request_hash"], "provider_reference": remote["id"], "fingerprint": fingerprint}
    payout["reconciliation"] = proposal
    store.audit(actor_id, "payout.reconciliation_proposed", f"{payout['id']}:{operation_id}:{remote['id']}")
    await backend.save()
    return proposal


async def approve_recovery(backend, store, stripe, settings, payout, actor_id, proposal_id, *, at=None):
    _allowed(settings, stripe, backend, actor_id)
    proposal = payout.get("reconciliation") or {}
    at = at or datetime.now(timezone.utc)
    if (payout["status"] != "review" or proposal.get("status") != "proposed" or proposal.get("id") != proposal_id
            or proposal.get("proposed_by") == actor_id or proposal.get("proposed_by") not in settings.admin_user_ids
            or not isinstance(proposal.get("proposed_at"), datetime)
            or not timedelta(0) <= at - proposal["proposed_at"] <= timedelta(minutes=30)):
        raise FinanceConflict("別の管理者が30分以内の照合結果を確認してください。古い画面は再読み込みしてください")
    operation_id, operation, allocation, remote, fingerprint = await _inspect(backend, stripe, payout)
    if (operation_id != proposal["operation_id"] or operation["request_hash"] != proposal["request_hash"]
            or operation["endpoint"] != proposal["endpoint"] or remote["id"] != proposal["provider_reference"]
            or fingerprint != proposal["fingerprint"]):
        raise FinanceConflict("照合結果が変更されています。再確認が必要です")
    if allocation is None:
        payout.update(provider_payout_id=remote["id"], status="bank_pending", hold_reason="")
    else:
        allocation["transfer_id"] = remote["id"]
        payout.update(status="transferring", hold_reason="")
    proposal.update(status="approved", approved_by=actor_id, approved_at=at)
    store.audit(actor_id, "payout.reconciliation_approved", f"{payout['id']}:{operation_id}:{remote['id']}:{proposal['proposed_by']}")
    await backend.reconcile_operation(operation_id, endpoint=operation["endpoint"],
                                    request_hash=operation["request_hash"], response=remote)
    return payout["status"]
