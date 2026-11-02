"""A changed source payment must never authorize a bank payout."""
import asyncio

import pytest

from app.finance import FinanceConflict, balance
from app.payout_worker import run_step, verify_source_payment
from test_database import database
from test_finance import NOW, provider, seed


def complete_source_payment():
    return {"id": "pi_finance", "object": "payment_intent", "livemode": False,
            "status": "succeeded", "currency": "jpy", "amount": 1000, "amount_received": 1000,
            "transfer_group": "order-order_finance", "latest_charge": {
                "id": "ch_finance", "object": "charge", "livemode": False,
                "payment_intent": "pi_finance", "paid": True, "captured": True,
                "status": "succeeded", "currency": "jpy", "amount": 1000,
                "amount_captured": 1000, "amount_refunded": 0, "disputed": False}}


SOURCE_FAULTS = [
    ("payment", "object", "charge"), ("payment", "amount", 999),
    ("payment", "amount", 1000.0), ("charge", "object", "payment_intent"),
    ("charge", "amount", 999), ("charge", "amount", 1000.0),
    ("charge", "amount_captured", 999), ("charge", "amount_captured", 1000.0),
    ("charge", "currency", "usd"), ("charge", "captured", False),
    ("charge", "captured", 1), ("charge", "status", "pending"),
    ("charge", "amount_refunded", False), ("charge", "amount_refunded", 0.0),
    ("charge", "disputed", 0),
]
MISSING_SOURCE_FIELDS = [("payment", "object"), ("payment", "amount"),
                         *(("charge", key) for key in ("object", "amount", "amount_captured",
                            "currency", "captured", "status", "amount_refunded", "disputed"))]


def source_fact():
    return {"id": "pi_finance", "order_id": "order_finance", "amount": 1000}


def test_complete_source_proof_accepts_original_charge_only():
    payment = complete_source_payment()
    assert verify_source_payment(payment, source_fact(), source_charge="ch_finance") is payment["latest_charge"]


@pytest.mark.parametrize("part,field,value", SOURCE_FAULTS)
def test_inconsistent_source_proof_is_rejected(part, field, value):
    payment = complete_source_payment()
    target = payment if part == "payment" else payment["latest_charge"]
    target[field] = value
    with pytest.raises(FinanceConflict):
        verify_source_payment(payment, source_fact())


@pytest.mark.parametrize("part,field", MISSING_SOURCE_FIELDS)
def test_incomplete_source_proof_is_rejected(part, field):
    payment = complete_source_payment()
    target = payment if part == "payment" else payment["latest_charge"]
    del target[field]
    with pytest.raises(FinanceConflict):
        verify_source_payment(payment, source_fact())


@pytest.mark.parametrize("part,field,value", SOURCE_FAULTS)
def test_inconsistent_source_after_transfer_keeps_reservation(database, provider, part, field, value):
    backend, dsn = database

    async def run():
        store, settings, stripe, payout_id = await seed(backend, dsn)
        assert await run_step(backend, store, stripe, settings, payout_id=payout_id, at=NOW) == "transferring"
        retrieve = stripe.retrieve_payment

        async def changed_source(reference):
            payment = await retrieve(reference)
            target = payment if part == "payment" else payment["latest_charge"]
            target[field] = value
            return payment

        stripe.retrieve_payment = changed_source
        assert await run_step(backend, store, stripe, settings, payout_id=payout_id, at=NOW) == "review"
        assert provider.transfers == 1 and provider.payouts == 0
        assert balance(store, "seller") == 0

    asyncio.run(run())


@pytest.mark.parametrize("part,field", MISSING_SOURCE_FIELDS)
def test_incomplete_source_after_transfer_keeps_reservation(database, provider, part, field):
    backend, dsn = database

    async def run():
        store, settings, stripe, payout_id = await seed(backend, dsn)
        assert await run_step(backend, store, stripe, settings, payout_id=payout_id, at=NOW) == "transferring"
        retrieve = stripe.retrieve_payment

        async def changed_source(reference):
            payment = await retrieve(reference)
            target = payment if part == "payment" else payment["latest_charge"]
            del target[field]
            return payment

        stripe.retrieve_payment = changed_source
        assert await run_step(backend, store, stripe, settings, payout_id=payout_id, at=NOW) == "review"
        assert provider.transfers == 1 and provider.payouts == 0
        assert balance(store, "seller") == 0

    asyncio.run(run())


@pytest.mark.parametrize("fault", [
    "payment_id", "charge_id", "charge_payment", "unpaid", "destination_charge",
    "on_behalf_of", "transfer_group", "live_payment", "live_charge", "amount_type",
])
def test_source_identity_rechecked_after_transfer(database, provider, fault):
    backend, dsn = database

    async def run():
        store, settings, stripe, payout_id = await seed(backend, dsn)
        assert await run_step(backend, store, stripe, settings, payout_id=payout_id, at=NOW) == "transferring"
        retrieve = stripe.retrieve_payment

        async def changed_source(reference):
            payment = await retrieve(reference)
            if fault == "payment_id": payment["id"] = "pi_unrelated"
            if fault == "charge_id": payment["latest_charge"]["id"] = "ch_unrelated"
            if fault == "charge_payment": payment["latest_charge"]["payment_intent"] = "pi_unrelated"
            if fault == "unpaid": payment["latest_charge"]["paid"] = False
            if fault == "destination_charge": payment["transfer_data"] = {"destination": "acct_other"}
            if fault == "on_behalf_of": payment["on_behalf_of"] = "acct_other"
            if fault == "transfer_group": payment["transfer_group"] = "order-unrelated"
            if fault == "live_payment": payment["livemode"] = True
            if fault == "live_charge": payment["latest_charge"]["livemode"] = True
            if fault == "amount_type": payment["amount_received"] = float(payment["amount_received"])
            return payment

        stripe.retrieve_payment = changed_source
        assert await run_step(backend, store, stripe, settings, payout_id=payout_id, at=NOW) == "review"
        assert provider.transfers == 1 and provider.payouts == 0
        assert balance(store, "seller") == 0

    asyncio.run(run())
