"""A changed source payment must never authorize a bank payout."""
import asyncio

import pytest

from app.finance import balance
from app.payout_worker import run_step
from test_database import database
from test_finance import NOW, provider, seed


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
