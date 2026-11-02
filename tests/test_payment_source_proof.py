"""Pure source evidence cannot enable live financial operations."""
import asyncio
from copy import deepcopy

import pytest

from app.finance import FinanceConflict
from app.integrations import StripeIntegration
from app.payment_reconciliation import sandbox_payment_proof
from app.payment_source_proof import payment_source_proof
from app.payout_worker import verify_source_payment


def source(mode="test", *, refunded=0):
    return {"id": "pi_source", "object": "payment_intent", "livemode": mode == "live",
            "amount": 1000, "amount_received": 1000, "currency": "jpy", "status": "succeeded",
            "metadata": {"order_id": "order_source"}, "transfer_group": "order-order_source",
            "client_secret": "private-fixture", "customer": {"email": "private@example.invalid"},
            "latest_charge": {"id": "ch_source", "object": "charge", "livemode": mode == "live",
                "payment_intent": "pi_source", "amount": 1000, "amount_captured": 1000,
                "currency": "jpy", "paid": True, "captured": True, "status": "succeeded",
                "disputed": False, "transfer_group": "order-order_source",
                "amount_refunded": refunded, "refunded": refunded == 1000,
                "billing_details": {"name": "private-fixture"}}}


def prove(payment, mode="test", **kwargs):
    return payment_source_proof(payment, payment_id="pi_source", amount=1000,
                                order_id="order_source", expected_mode=mode, **kwargs)


@pytest.mark.parametrize("mode", ["test", "live"])
def test_pure_proof_preserves_source_and_retains_no_private_payload(mode):
    payment = source(mode)
    original = deepcopy(payment)
    proof = prove(payment, mode)
    assert proof == {"id": "pi_source", "amount": 1000, "charge": "ch_source",
                     "amount_refunded": 0, "status": "succeeded"}
    assert payment == original
    assert "private" not in repr(proof)


@pytest.mark.parametrize("mode", ["test", "live"])
@pytest.mark.parametrize("part", ["payment", "charge"])
@pytest.mark.parametrize("mode_fault", ["opposite", "missing", "integer", "string"])
def test_cross_mode_or_ambiguous_mode_never_proves_source(mode, part, mode_fault):
    payment = source(mode)
    target = payment if part == "payment" else payment["latest_charge"]
    if mode_fault == "missing":
        del target["livemode"]
    else:
        target["livemode"] = {"opposite": mode != "live", "integer": int(mode == "live"),
                            "string": str(mode == "live").lower()}[mode_fault]
    with pytest.raises(FinanceConflict):
        prove(payment, mode)


@pytest.mark.parametrize("mode", [None, "", "staging", "LIVE", False, 0, ["test"]])
def test_expected_mode_must_be_explicit_and_valid(mode):
    with pytest.raises(FinanceConflict):
        prove(source(), mode)


@pytest.mark.parametrize("mode", ["test", "live"])
@pytest.mark.parametrize("part,field,value", [
    ("payment", "id", "pi_other"), ("payment", "object", "charge"),
    ("payment", "amount", 999), ("payment", "amount_received", 999),
    ("payment", "amount_received", 1000.0), ("payment", "currency", "usd"),
    ("payment", "status", "processing"), ("payment", "transfer_group", "order-other"),
    ("payment", "transfer_data", {}), ("payment", "on_behalf_of", ""),
    ("payment", "application_fee_amount", 0),
    ("charge", "object", "payment_intent"), ("charge", "payment_intent", "pi_other"),
    ("charge", "currency", "usd"), ("charge", "amount", 999),
    ("charge", "amount_captured", 999), ("charge", "captured", 1),
    ("charge", "paid", False), ("charge", "status", "pending"),
    ("charge", "disputed", True), ("charge", "amount_refunded", 1),
])
def test_source_identity_and_capture_facts_are_strict_in_both_modes(mode, part, field, value):
    payment = source(mode)
    target = payment if part == "payment" else payment["latest_charge"]
    target[field] = value
    with pytest.raises(FinanceConflict):
        prove(payment, mode)


@pytest.mark.parametrize("mode", ["test", "live"])
@pytest.mark.parametrize("amount", [0, 400, 1000])
def test_refund_inspection_accepts_only_bounded_cumulative_refund_facts(mode, amount):
    assert prove(source(mode, refunded=amount), mode, allow_refunded=True)["amount_refunded"] == amount


@pytest.mark.parametrize("mode", ["test", "live"])
@pytest.mark.parametrize("amount", [-1, 1001, False, 0.0, None])
def test_refund_inspection_rejects_invalid_cumulative_amount(mode, amount):
    with pytest.raises(FinanceConflict):
        prove(source(mode, refunded=amount), mode, allow_refunded=True)


@pytest.mark.parametrize("mode", ["test", "live"])
def test_previous_transfer_charge_must_still_match(mode):
    assert prove(source(mode), mode, source_charge="ch_source")["charge"] == "ch_source"
    with pytest.raises(FinanceConflict):
        prove(source(mode), mode, source_charge="ch_changed")


class SourceProvider:
    def __init__(self, payment):
        self.payment = payment
        self.calls = []

    async def retrieve_payment(self, payment_id):
        self.calls.append(payment_id)
        return self.payment


def test_existing_payout_wrapper_still_rejects_live_source():
    with pytest.raises(FinanceConflict):
        verify_source_payment(source("live"), {"id": "pi_source", "order_id": "order_source", "amount": 1000})


def test_existing_reconciliation_wrapper_still_rejects_live_source():
    provider = SourceProvider(source("live"))
    with pytest.raises(FinanceConflict):
        asyncio.run(sandbox_payment_proof(provider, {"id": "order_source"}, None, "pi_source", 1000))
    assert provider.calls == ["pi_source"]


@pytest.mark.parametrize("fault", ["metadata", "extra_on_primary", "wrong_extra", "charge_group", "refunded_flag"])
def test_reconciliation_keeps_its_context_specific_checks(fault):
    payment = source()
    extra = None
    if fault == "metadata": payment["metadata"] = {"order_id": "other"}
    if fault == "extra_on_primary": payment["metadata"]["extra_id"] = "extra_other"
    if fault == "wrong_extra":
        extra = {"id": "expected_extra"}
        payment["metadata"]["extra_id"] = "other_extra"
    if fault == "charge_group": payment["latest_charge"]["transfer_group"] = "order-other"
    if fault == "refunded_flag": payment["latest_charge"]["refunded"] = True
    with pytest.raises(FinanceConflict):
        asyncio.run(sandbox_payment_proof(SourceProvider(payment), {"id": "order_source"}, extra, "pi_source", 1000))


@pytest.mark.parametrize("extra_id", [None, "extra_source"])
@pytest.mark.parametrize("refunded", [0, 1000])
def test_reconciliation_returns_same_minimal_proof_for_primary_or_extra(extra_id, refunded):
    payment = source(refunded=refunded)
    extra = {"id": extra_id} if extra_id else None
    if extra_id: payment["metadata"]["extra_id"] = extra_id
    result = asyncio.run(sandbox_payment_proof(SourceProvider(payment), {"id": "order_source"}, extra,
                                               "pi_source", 1000, refund=bool(refunded)))
    assert result == prove(payment, allow_refunded=bool(refunded))


def test_live_evidence_is_not_live_post_authorization(monkeypatch):
    proof = prove(source("live"), "live")
    assert proof["charge"] == "ch_source"

    def no_network(*args, **kwargs):
        pytest.fail("Live writes must be rejected before constructing a network client")

    monkeypatch.setattr("app.integrations.httpx.AsyncClient", no_network)
    stripe = StripeIntegration("rk_live_synthetic", "https://example.invalid", charge_mode="separate")
    with pytest.raises(RuntimeError, match="本番決済"):
        asyncio.run(stripe._post("refunds", {"_idempotency_key": "no-live-proof", "payment_intent": proof["id"]}))
