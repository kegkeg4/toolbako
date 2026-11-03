"""Pure proof and synthetic worker checks: no PostgreSQL or provider traffic."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.database import content_hash
from app.finance import FinanceConflict
from app.integrations import StripeOutcomeUnknown
from app.payout_worker import _step
from app.stripe_bank_proof import allocation_transfer_proof, bank_payout_proof


def bank(mode="test", **changes):
    return {"id": "po_fixture", "object": "payout", "livemode": mode == "live", "amount": 690,
            "currency": "jpy", "metadata": {"payout_id": "payout1"}, "status": "pending",
            "automatic": False, "method": "standard", "type": "bank_account", **changes}


def bank_context(mode="test", **changes):
    return {"expected_mode": mode, "account_id": "acct_seller", "response_account_id": "acct_seller",
            "amount": 690, "currency": "jpy", "metadata": {"payout_id": "payout1"},
            "payout_id": "po_fixture", **changes}


def transfer(mode="test", **changes):
    return {"id": "tr_fixture", "object": "transfer", "livemode": mode == "live", "amount": 690,
            "currency": "jpy", "destination": "acct_seller", "source_transaction": "ch_source",
            "transfer_group": "order-order1", "metadata": {"payout_id": "payout1", "allocation_id": "allocation1"},
            "reversed": False, "amount_reversed": 0, **changes}


def transfer_context(mode="test", **changes):
    return {"expected_mode": mode, "account_id": "acct_seller", "source_charge": "ch_source",
            "amount": 690, "currency": "jpy", "transfer_group": "order-order1",
            "metadata": {"payout_id": "payout1", "allocation_id": "allocation1"},
            "transfer_id": "tr_fixture", **changes}


@pytest.mark.parametrize("mode", ["test", "live"])
@pytest.mark.parametrize("status", ["pending", "in_transit", "paid", "failed", "canceled"])
def test_bank_proof_preserves_all_recognized_states_without_mutating(mode, status):
    remote = bank(mode, status=status, arrival_date=1793458800, description="private unretained value")
    original = deepcopy(remote)
    proof = bank_payout_proof(remote, **bank_context(mode))
    assert proof == {"id": "po_fixture", "account_id": "acct_seller", "amount": 690,
                     "currency": "jpy", "status": status, "arrival_date": 1793458800}
    assert remote == original


@pytest.mark.parametrize("field", list(bank()))
def test_bank_proof_rejects_missing_fields(field):
    remote = bank()
    del remote[field]
    with pytest.raises(FinanceConflict):
        bank_payout_proof(remote, **bank_context())


@pytest.mark.parametrize("field,value", [
    ("object", "transfer"), ("id", "po_wrong"), ("id", "po_fixture/else"), ("id", "po_"),
    ("livemode", True), ("livemode", 0), ("amount", True), ("amount", 690.0), ("amount", "690"),
    ("amount", 689), ("currency", "usd"), ("automatic", 0), ("automatic", True),
    ("method", "instant"), ("type", "card"), ("status", "unknown"), ("status", []),
    ("metadata", []), ("metadata", {"payout_id": "someoneelse"}),
    ("arrival_date", True), ("arrival_date", 1.5), ("arrival_date", 0),
    ("arrival_date", -1), ("arrival_date", 253402214401),
])
def test_bank_proof_rejects_incomplete_or_wrong_provider_evidence(field, value):
    with pytest.raises(FinanceConflict):
        bank_payout_proof(bank(**{field: value}), **bank_context())


@pytest.mark.parametrize("field,value", [
    ("expected_mode", "staging"), ("expected_mode", False), ("expected_mode", "live"),
    ("account_id", "acct_"), ("account_id", "acct_other"), ("response_account_id", "acct_other"),
    ("response_account_id", None), ("amount", True), ("amount", 690.0), ("amount", 0),
    ("currency", "usd"), ("metadata", {}), ("metadata", {"payout_id": None}),
    ("payout_id", "po_other"), ("payout_id", ""),
])
def test_bank_proof_rejects_wrong_expected_context(field, value):
    with pytest.raises(FinanceConflict):
        bank_payout_proof(bank(), **bank_context(**{field: value}))


@pytest.mark.parametrize("mode", ["test", "live"])
def test_transfer_proof_is_mode_explicit_and_keeps_only_bound_evidence(mode):
    remote = transfer(mode, description="private unretained value")
    original = deepcopy(remote)
    assert allocation_transfer_proof(remote, **transfer_context(mode)) == {
        "id": "tr_fixture", "account_id": "acct_seller", "source_charge": "ch_source",
        "amount": 690, "currency": "jpy", "transfer_group": "order-order1"}
    assert remote == original


@pytest.mark.parametrize("field", list(transfer()))
def test_transfer_proof_rejects_every_missing_required_field(field):
    remote = transfer()
    del remote[field]
    with pytest.raises(FinanceConflict):
        allocation_transfer_proof(remote, **transfer_context())


@pytest.mark.parametrize("field,value", [
    ("object", "payout"), ("id", "tr_other"), ("id", "tr_"), ("id", "tr_fixture?else"),
    ("livemode", True), ("livemode", 0), ("amount", True), ("amount", 690.0), ("amount", 689),
    ("currency", "usd"), ("destination", "acct_other"), ("destination", {"id": "acct_seller"}),
    ("source_transaction", "ch_other"), ("transfer_group", "order-other"),
    ("metadata", {"payout_id": "other", "allocation_id": "allocation1"}),
    ("metadata", {"payout_id": "payout1", "allocation_id": "other"}), ("metadata", []),
    ("reversed", 0), ("reversed", True), ("amount_reversed", False),
    ("amount_reversed", 0.0), ("amount_reversed", -1), ("amount_reversed", 1), ("amount_reversed", 690),
])
def test_transfer_proof_rejects_wrong_ownership_and_partial_or_full_reversal(field, value):
    with pytest.raises(FinanceConflict):
        allocation_transfer_proof(transfer(**{field: value}), **transfer_context())


@pytest.mark.parametrize("field,value", [
    ("expected_mode", None), ("expected_mode", "live"), ("account_id", "acct_other"),
    ("source_charge", None), ("source_charge", "ch_other"), ("source_charge", "ch_"),
    ("amount", True), ("amount", 690.0), ("amount", 0), ("currency", "usd"),
    ("metadata", {}), ("metadata", {"payout_id": "payout1"}),
    ("transfer_group", "order-"), ("transfer_group", "order-other"), ("transfer_id", "tr_other"),
])
def test_transfer_proof_requires_complete_expected_identity(field, value):
    with pytest.raises(FinanceConflict):
        allocation_transfer_proof(transfer(), **transfer_context(**{field: value}))


@pytest.mark.parametrize("remote", [None, [], "wrong", 0])
@pytest.mark.parametrize("kind", ["bank", "transfer"])
def test_non_object_provider_evidence_is_a_safe_conflict(remote, kind):
    helper, context = (bank_payout_proof, bank_context()) if kind == "bank" else (allocation_transfer_proof, transfer_context())
    with pytest.raises(FinanceConflict):
        helper(remote, **context)


def payout_record(**changes):
    return {"id": "payout1", "seller_id": "seller", "account_id": "acct_seller", "net_amount": 690,
            "status": "requested", "allocations": [{"id": "allocation1", "order_id": "order1",
                "receipt_id": "receipt1", "transfer_amount": 690, "source_charge": "ch_source"}], **changes}


class CacheBoundary:
    def __init__(self, remote=None, *, kind="bank", state="succeeded", account="acct_seller"):
        self.result = None if remote is None else {"status": state, "response": remote}
        self.kind, self.details_calls = kind, 0
        data = {"amount": "690", "currency": "jpy", "method": "standard", "metadata[payout_id]": "payout1",
                "_stripe_account": account}
        self.details = {"status": state, "endpoint": "payouts", "request_hash": content_hash(data)}

    async def operation_result(self, key):
        prefix = "bank-payout-" if self.kind == "bank" else "allocation-"
        return self.result if key.startswith(prefix) else None

    async def operation_details(self, key):
        assert key == "bank-payout-payout1"
        self.details_calls += 1
        return self.details


class NoProviderCalls:
    def __getattr__(self, name):
        raise AssertionError("Cached/unknown proof must not call provider: " + name)


def test_cached_bank_adoption_requires_original_account_request_and_never_resends():
    payout = payout_record()
    backend = CacheBoundary(bank())
    asyncio.run(_step(backend, None, NoProviderCalls(), payout))
    assert payout["status"] == "bank_pending" and payout["provider_payout_id"] == "po_fixture"
    assert backend.details_calls == 1


@pytest.mark.parametrize("fault", ["account", "endpoint", "request_hash", "status", "missing", "remote_mode", "remote_amount", "remote_type"])
def test_cached_bank_wrong_proof_cannot_adopt_or_resend(fault):
    payout = payout_record()
    backend = CacheBoundary(bank(), account="acct_other" if fault == "account" else "acct_seller")
    if fault in {"endpoint", "request_hash", "status"}:
        backend.details[fault] = "wrong"
    elif fault == "missing":
        backend.details = None
    elif fault == "remote_mode":
        backend.result["response"]["livemode"] = True
    elif fault == "remote_amount":
        backend.result["response"]["amount"] = 690.0
    elif fault == "remote_type":
        backend.result["response"]["type"] = "card"
    with pytest.raises(FinanceConflict):
        asyncio.run(_step(backend, None, NoProviderCalls(), payout))
    assert payout["status"] == "requested" and "provider_payout_id" not in payout


@pytest.mark.parametrize("kind", ["bank", "transfer"])
@pytest.mark.parametrize("state", ["pending", "unknown", "rejected"])
def test_unknown_operation_still_stops_without_lookup_or_resend(kind, state):
    payout = payout_record()
    backend = CacheBoundary(bank() if kind == "bank" else transfer(), kind=kind, state=state)
    with pytest.raises(StripeOutcomeUnknown):
        asyncio.run(_step(backend, None, NoProviderCalls(), payout))
    assert not backend.details_calls and "provider_payout_id" not in payout
    assert "transfer_id" not in payout["allocations"][0]


@pytest.mark.parametrize("fault", [None, "mode", "metadata", "partial", "full", "bool_amount"])
def test_cached_transfer_checks_unreversed_bound_evidence_before_adoption(fault):
    payout = payout_record()
    remote = transfer()
    if fault == "mode": remote["livemode"] = True
    elif fault == "metadata": remote["metadata"]["allocation_id"] = "other"
    elif fault == "partial": remote["amount_reversed"] = 1
    elif fault == "full": remote.update(reversed=True, amount_reversed=690)
    elif fault == "bool_amount": remote["amount"] = True
    backend = CacheBoundary(remote, kind="transfer")
    store = SimpleNamespace(connected_accounts={"seller": {"payouts_paused": True}})
    if fault:
        with pytest.raises(FinanceConflict):
            asyncio.run(_step(backend, store, NoProviderCalls(), payout))
        assert "transfer_id" not in payout["allocations"][0]
    else:
        asyncio.run(_step(backend, store, NoProviderCalls(), payout))
        assert payout["status"] == "held" and payout["allocations"][0]["transfer_id"] == "tr_fixture"


@pytest.mark.parametrize("fault", [None, "mode", "amount", "automatic", "arrival"])
def test_bank_pending_fresh_get_proves_test_context_before_marking_paid(fault):
    payout = payout_record(status="bank_pending", provider_payout_id="po_fixture")
    remote = bank(status="paid")
    if fault == "mode": remote["livemode"] = True
    elif fault == "amount": remote["amount"] = 690.0
    elif fault == "automatic": remote["automatic"] = True
    elif fault == "arrival": remote["arrival_date"] = True
    class ReadOnlyProvider:
        async def retrieve_payout(self, payout_id, account_id):
            assert (payout_id, account_id) == ("po_fixture", "acct_seller")
            return remote
    if fault:
        with pytest.raises(FinanceConflict):
            asyncio.run(_step(None, None, ReadOnlyProvider(), payout))
        assert payout["status"] == "bank_pending" and "paid_at" not in payout
    else:
        asyncio.run(_step(None, None, ReadOnlyProvider(), payout))
        assert payout["status"] == "paid" and payout["paid_at"]


@pytest.mark.parametrize("kind", ["transfer", "bank"])
@pytest.mark.parametrize("fault", [None, "mode", "float_amount", "metadata", "object"])
def test_new_response_cannot_commit_wrong_identity_or_claim_bank_paid(monkeypatch, kind, fault):
    # A provider-shaped test double exercises the actual one-step caller. It
    # proves call order/state protection, not durable Postgres transactions.
    from app import payout_worker
    from test_payout_source_proof import complete_source_payment
    payout = payout_record(status="transferring" if kind == "bank" else "requested")
    allocation = payout["allocations"][0]
    if kind == "bank": allocation["transfer_id"] = "tr_fixture"
    store = SimpleNamespace(connected_accounts={"seller": {"account_id": "acct_seller"}})
    fact = {"id": "pi_finance", "order_id": "order1", "amount": 1000, "eligible": True}
    monkeypatch.setattr(payout_worker, "receipts", lambda _: {"receipt1": fact})
    remote = transfer() if kind == "transfer" else bank(status="paid")
    if fault == "mode": remote["livemode"] = True
    elif fault == "float_amount": remote["amount"] = 690.0
    elif fault == "metadata": remote["metadata"]["payout_id"] = "someoneelse"
    elif fault == "object": remote["object"] = "incorrect"
    class SyntheticProvider:
        def __init__(self): self.calls = []
        async def retrieve_account(self, account):
            assert account == "acct_seller"
            return {"id": account, "country": "JP", "default_currency": "jpy", "payouts_enabled": True,
                    "details_submitted": True, "capabilities": {"transfers": "active"},
                    "settings": {"payouts": {"schedule": {"interval": "manual"}}}}
        async def retrieve_payment(self, payment_id):
            assert payment_id == "pi_finance"
            payment = complete_source_payment()
            payment["transfer_group"] = "order-order1"
            payment["latest_charge"]["id"] = "ch_source"
            return payment
        async def create_allocation_transfer(self, current, part, source):
            assert current is payout and part is allocation and source == "ch_source"
            self.calls.append("transfer")
            return remote
        async def retrieve_balance(self, account):
            assert account == "acct_seller"
            return {"available": [{"currency": "jpy", "amount": 690}]}
        async def create_bank_payout(self, current):
            assert current is payout
            self.calls.append("bank")
            return remote
    provider = SyntheticProvider()
    if fault:
        with pytest.raises(FinanceConflict):
            asyncio.run(_step(CacheBoundary(), store, provider, payout))
        assert "provider_payout_id" not in payout
        if kind == "transfer": assert "transfer_id" not in allocation
    else:
        asyncio.run(_step(CacheBoundary(), store, provider, payout))
        if kind == "bank":
            assert payout["provider_payout_id"] == "po_fixture" and payout["status"] == "bank_pending"
            assert "paid_at" not in payout
        else:
            assert allocation["transfer_id"] == "tr_fixture" and payout["status"] == "transferring"
    assert provider.calls == [kind]


@pytest.fixture
def scoped_live_webhook():
    """Real pure validator with explicit synthetic scope; no journal/network."""
    from app.config import Settings
    from app.data import DemoStore
    from app.financial_scope import FinancialScope
    from app.integrations import StripeIntegration
    from app.live_setup_plan import STRIPE_API_VERSION
    from app.live_webhooks import LiveStripeWebhookService

    scope = FinancialScope("live", "acct_LivePlatform", "11111111-1111-4111-8111-111111111111")
    settings = Settings(stripe_secret_key="rk_live_syntheticOnly", stripe_api_version=STRIPE_API_VERSION,
                        stripe_platform_account_id=scope.platform_account, stripe_ledger_id=scope.ledger_id)
    provider = StripeIntegration(settings.stripe_secret_key, "https://synthetic.invalid",
                                 api_version=STRIPE_API_VERSION, financial_scope=scope)
    service = LiveStripeWebhookService(settings, DemoStore(seed=False), backend=None, provider=provider, kind="connect")

    def evidence(mode):
        remote = bank(mode)
        remote["metadata"].update(ledger_id=scope.ledger_id, platform_account_id=scope.platform_account)
        return remote

    return service, evidence


@pytest.mark.parametrize("mode", ["test", "live"])
@pytest.mark.parametrize("field,value", [("amount", 690.0), ("automatic", 0), ("object", "transfer"),
                                       ("arrival_date", True), ("metadata", {"payout_id": "other"})])
def test_live_webhook_retains_strict_proof_and_mode_rejection(scoped_live_webhook, mode, field, value):
    from fastapi import HTTPException
    service, evidence = scoped_live_webhook
    payout = payout_record(provider_payout_id="po_fixture")
    # Prove the fixture meets the full current contract before corrupting one
    # field; a missing ledger must not make every negative case pass by accident.
    assert service._validate_payout(evidence("live"), {"payout": payout}, {"account": "acct_seller"}) is None
    remote = evidence(mode)
    if field == "metadata":
        remote["metadata"].update(value)
    else:
        remote[field] = value
    with pytest.raises(HTTPException) as error:
        service._validate_payout(remote, {"payout": payout}, {"account": "acct_seller"})
    assert error.value.status_code == 400


@pytest.mark.parametrize("mode", ["test", "live"])
def test_live_webhook_accepts_only_live_bank_evidence(scoped_live_webhook, mode):
    from fastapi import HTTPException
    service, evidence = scoped_live_webhook
    args = (evidence(mode), {"payout": payout_record(provider_payout_id="po_fixture")}, {"account": "acct_seller"})
    if mode == "test":
        with pytest.raises(HTTPException): service._validate_payout(*args)
    else:
        assert service._validate_payout(*args) is None
