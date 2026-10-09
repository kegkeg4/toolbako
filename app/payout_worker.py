"""One durable payout step per invocation; sandbox only until launch approval.

Never mark a bank payout paid merely because creating it returned HTTP 200.
The runtime DB advisory lock serializes this worker with webhooks and requests.
"""
import argparse
import asyncio
from datetime import datetime, timezone

from .finance import FinanceConflict, receipts
from .integrations import StripeIntegration, StripeOutcomeUnknown


def sandbox_payouts_ready(settings):
    return (not settings.is_production and not settings.demo_mode and bool(settings.database_url)
            and settings.stripe_charge_mode == "separate"
            and settings.stripe_secret_key.startswith(("sk_test_", "rk_test_")))


def verify_bank_payout(payout, remote):
    if (not str(remote.get("id", "")).startswith("po_") or remote.get("id") != payout.get("provider_payout_id")
            or remote.get("currency") != "jpy" or remote.get("amount") != payout["net_amount"]
            or (remote.get("metadata") or {}).get("payout_id") != payout["id"]):
        raise FinanceConflict("銀行振込の参照・金額が一致しません")


def apply_bank_status(payout, remote):
    verify_bank_payout(payout, remote)
    status = remote.get("status")
    if status in {"failed", "canceled"}:
        payout["status"] = "bank_failed"
        payout["failure_code"] = str(remote.get("failure_code") or status)[:80]
    elif status == "paid" and payout["status"] != "bank_failed":
        payout["status"] = "paid"
        payout.setdefault("paid_at", datetime.now(timezone.utc))
    elif status not in {"pending", "in_transit", "paid"}:
        raise FinanceConflict("銀行振込の状態を確認できません")
    if type(remote.get("arrival_date")) is int:
        payout["estimated_arrival_at"] = datetime.fromtimestamp(remote["arrival_date"], timezone.utc)


async def run_step(backend, store, stripe, settings, *, payout_id=None, at=None):
    if not sandbox_payouts_ready(settings) or stripe.journal is not backend or stripe.secret_key != settings.stripe_secret_key or stripe.charge_mode != "separate":
        raise FinanceConflict("振込workerはPostgres接続済みのseparate方式・テスト環境のみ実行できます")
    at = at or datetime.now(timezone.utc)
    async with backend.request(store):
        candidates = sorted((p for p in store.payouts if p.get("finance_v2")
                             and p["status"] in {"requested", "transferring", "awaiting_funds", "bank_pending", "reversing"}
                             and (not payout_id or p["id"] == payout_id)), key=lambda p: p["created_at"])
        # A future-dated request must not starve already-due work.
        payout = next((p for p in candidates if p["status"] in {"bank_pending", "reversing"} or p["scheduled_for"] <= at), None)
        if not payout:
            return "idle"
        try:
            await _step(backend, store, stripe, payout)
        except StripeOutcomeUnknown:
            payout["status"] = "review"
            payout["hold_reason"] = "Stripeの処理結果が不明です。再送せず取引照合が必要です"
        except (FinanceConflict, RuntimeError):
            # Never release a reservation after an API timeout or failure.
            payout["status"] = "review"
            payout["hold_reason"] = "口座・決済・送金状態を確認してください。残高の予約は維持されています"
        store.audit(None, "payout.worker", f"{payout['id']}:{payout['status']}")
        await backend.save()
        return payout["status"]


async def _step(backend, store, stripe, payout):
    if payout["status"] == "bank_pending":
        remote = await stripe.retrieve_payout(payout["provider_payout_id"], payout["account_id"])
        apply_bank_status(payout, remote)
        return
    # Recover a crash between Stripe's successful response and snapshot commit.
    # Do this before checking a now-decreased balance or a newly applied hold.
    bank_attempt = await backend.operation_result(f"bank-payout-{payout['id']}")
    if bank_attempt:
        if bank_attempt["status"] != "succeeded":
            raise StripeOutcomeUnknown(f"bank-payout-{payout['id']}")
        remote = bank_attempt["response"]
        payout["provider_payout_id"] = remote["id"]
        verify_bank_payout(payout, remote)
        payout["status"] = "bank_pending"
        return
    for allocation in payout["allocations"]:
        if allocation.get("transfer_id") or not allocation["transfer_amount"]:
            continue
        attempt = await backend.operation_result(f"allocation-{allocation['id']}")
        if attempt:
            if attempt["status"] != "succeeded":
                raise StripeOutcomeUnknown(f"allocation-{allocation['id']}")
            result = attempt["response"]
            if (not str(result.get("id", "")).startswith("tr_") or result.get("amount") != allocation["transfer_amount"]
                    or result.get("currency") != "jpy" or result.get("destination") != payout["account_id"]
                    or not allocation.get("source_charge") or result.get("source_transaction") != allocation["source_charge"]):
                raise FinanceConflict("分配結果の復元に失敗しました")
            allocation["transfer_id"] = result["id"]
    if payout["status"] == "reversing":
        allocation = next((a for a in payout["allocations"] if a.get("transfer_id") and not a.get("reversal_id")), None)
        if allocation:
            result = await stripe.reverse_allocation(allocation)
            if (not str(result.get("id", "")).startswith("trr_") or result.get("transfer") != allocation["transfer_id"]
                    or result.get("amount") != allocation["transfer_amount"] or result.get("currency") != "jpy"):
                raise FinanceConflict("分配の取消結果が一致しません")
            allocation["reversal_id"] = result["id"]
            return
        payout.update(status="cancelled", hold_reason="分配の取消を確認し、振込申請を取り消しました")
        return
    local = store.connected_accounts.get(payout["seller_id"], {})
    if local.get("payouts_paused"):
        payout.update(status="held", hold_reason="運営による振込保留")
        return
    if local.get("account_id") != payout["account_id"]:
        raise FinanceConflict("振込先口座が変更されました")
    facts = receipts(store)
    if any(not facts.get(a["receipt_id"], {}).get("eligible") for a in payout["allocations"]):
        # In particular: refund/dispute after a transfer must NOT free the same
        # funds for another withdrawal. Keep the reservation for reconciliation.
        raise FinanceConflict("返金・紛争などにより振込対象外になりました")
    remote_account = await stripe.retrieve_account(payout["account_id"])
    payout_schedule = ((remote_account.get("settings") or {}).get("payouts") or {}).get("schedule") or {}
    if (remote_account.get("id") != payout["account_id"] or remote_account.get("country") != "JP"
            or remote_account.get("default_currency") != "jpy" or not remote_account.get("payouts_enabled")
            or not remote_account.get("details_submitted")
            or (remote_account.get("capabilities") or {}).get("transfers") != "active"
            or payout_schedule.get("interval") != "manual"):
        raise FinanceConflict("日本円・手動振込・本人確認済みの口座が必要です")

    allocation = next((a for a in payout["allocations"] if a["transfer_amount"] > 0 and not a.get("transfer_id")), None)
    if allocation:
        fact = facts[allocation["receipt_id"]]
        payment = await stripe.retrieve_payment(fact["id"])
        charge = payment.get("latest_charge") or {}
        if (payment.get("id") != fact["id"] or payment.get("status") != "succeeded"
                or payment.get("currency") != "jpy" or payment.get("amount_received") != fact["amount"]
                or payment.get("transfer_data") or payment.get("on_behalf_of")
                or payment.get("transfer_group") != f"order-{fact['order_id']}"
                or not isinstance(charge, dict) or not str(charge.get("id", "")).startswith("ch_")
                or charge.get("payment_intent") != fact["id"] or not charge.get("paid")
                or charge.get("amount_refunded", 0) or charge.get("disputed")):
            raise FinanceConflict("分配元の決済を確認できません")
        payout["status"] = "transferring"
        allocation["source_charge"] = charge["id"]
        result = await stripe.create_allocation_transfer(payout, allocation, charge["id"])
        if (not str(result.get("id", "")).startswith("tr_") or result.get("amount") != allocation["transfer_amount"]
                or result.get("currency") != "jpy" or result.get("destination") != payout["account_id"]
                or result.get("source_transaction") != charge["id"]):
            raise FinanceConflict("Stripe分配結果が一致しません")
        allocation["transfer_id"] = result["id"]
        return  # One financial mutation per invocation, durable checkpoint.

    # Recheck EVERY source before bank payout, even after the transfer step.
    for allocation in payout["allocations"]:
        fact = facts[allocation["receipt_id"]]
        payment = await stripe.retrieve_payment(fact["id"])
        charge = payment.get("latest_charge") or {}
        if not isinstance(charge, dict) or payment.get("status") != "succeeded" or payment.get("amount_received") != fact["amount"] or payment.get("currency") != "jpy" or charge.get("amount_refunded", 0) or charge.get("disputed"):
            raise FinanceConflict("分配後の返金・紛争を確認してください")
    remote_balance = await stripe.retrieve_balance(payout["account_id"])
    available = sum(x["amount"] for x in remote_balance.get("available", []) if x.get("currency") == "jpy" and type(x.get("amount")) is int)
    if available < payout["net_amount"]:
        payout["status"] = "awaiting_funds"
        return
    result = await stripe.create_bank_payout(payout)
    payout["provider_payout_id"] = result["id"]
    verify_bank_payout(payout, result)
    payout["status"] = "bank_pending"
    # Only a subsequent signed event or authenticated GET confirms settlement.
    if type(result.get("arrival_date")) is int:
        payout["estimated_arrival_at"] = datetime.fromtimestamp(result["arrival_date"], timezone.utc)


def main():
    parser = argparse.ArgumentParser(description="Sandbox-only Toolbako payout worker (one step)")
    parser.add_argument("--payout-id")
    args = parser.parse_args()
    from .config import settings
    from .data import DemoStore
    from .database import PostgresStateStore
    backend = PostgresStateStore(settings.database_url, production=settings.is_production)
    stripe = StripeIntegration(settings.stripe_secret_key, settings.site_base_url, api_version=settings.stripe_api_version,
                               charge_mode=settings.stripe_charge_mode, journal=backend)
    try:
        status = asyncio.run(run_step(backend, DemoStore(seed=False), stripe, settings, payout_id=args.payout_id))
        print(f"Sandbox payout step: {status}. No live payment operations are allowed.")
        raise SystemExit(2 if status in {"review", "bank_failed"} else 0)
    except Exception:
        print("Payout worker stopped safely. Check the private ledger and environment; do not retry an unknown operation.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
