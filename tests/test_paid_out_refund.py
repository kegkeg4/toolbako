"""Paid-out refund planning with synthetic facts; no DB or provider calls."""
import asyncio
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

import pytest

from app.finance import FinanceConflict
from app.financial_scope import FinancialScope
from app.integrations import StripeIntegration
from app.paid_out_refund import (
    FUNDING_POLICY, capture_paid_payout, inspect_paid_out_refund,
    propose_paid_out_refund, review_paid_out_refund,
)


NOW = datetime(2026, 11, 2, 10, tzinfo=timezone.utc)
ADMINS = frozenset({"admin_one", "admin_two"})


def facts(mode="test"):
    scope = FinancialScope(mode, "acct_platform", "11111111-1111-4111-8111-111111111111")
    allocation = {"id": "allocation_one", "order_id": "order_one", "receipt_id": "pi_one",
                  "source_charge": "ch_one", "transfer_id": "tr_one", "amount": 800, "transfer_amount": 640}
    payout = {"id": "payout_one", "seller_id": "seller_one", "account_id": "acct_seller", "finance_v2": True,
              "amount": 800, "fee": 160, "net_amount": 640, "status": "paid", "provider_payout_id": "po_one",
              "paid_at": NOW - timedelta(days=1), "scheduled_for": NOW - timedelta(days=2),
              "request_key": "request_one", "allocations": [allocation]}
    case = {"id": "case_one", "order_id": "order_one", "payment_id": "pi_one", "payment_amount": 1000,
            "refund_amount": 1000, "seller_recovery_amount": 640, "seller_id": "seller_one",
            "payout_id": "payout_one", "allocation_id": "allocation_one"}
    payment = {"id": "pi_one", "object": "payment_intent", "livemode": scope.livemode,
               "amount": 1000, "amount_received": 1000, "currency": "jpy", "status": "succeeded",
               "metadata": {"order_id": "order_one"}, "transfer_group": "order-order_one",
               "client_secret": "private-secret-fixture", "customer": {"email": "private@example.invalid"},
               "latest_charge": {"id": "ch_one", "object": "charge", "livemode": scope.livemode,
                   "payment_intent": "pi_one", "amount": 1000, "amount_captured": 1000, "currency": "jpy",
                   "paid": True, "captured": True, "status": "succeeded", "disputed": False,
                   "transfer_group": "order-order_one", "amount_refunded": 0}}
    bank = {"id": "po_one", "object": "payout", "livemode": scope.livemode, "amount": 640,
            "currency": "jpy", "status": "paid", "automatic": False, "method": "standard", "type": "bank_account",
            "metadata": {"payout_id": "payout_one"}, "destination": {"account_number": "private-fixture"}}
    transfer = {"id": "tr_one", "object": "transfer", "livemode": scope.livemode, "amount": 640,
                "currency": "jpy", "destination": "acct_seller", "source_transaction": "ch_one",
                "transfer_group": "order-order_one", "amount_reversed": 0, "reversed": False,
                "metadata": {"payout_id": "payout_one", "allocation_id": "allocation_one"}}
    funding = {"policy": FUNDING_POLICY, "decision_id": "decision_one", "case_id": "case_one",
               "scope": scope.payload(), "refund_limit": 1000, "recovery_limit": 640, "reserve_id": "reserve_one",
               "available_amount": 1000, "approved_by": "admin_one", "approved_at": NOW - timedelta(minutes=10)}
    return {"case": case, "scope": scope, "bound_scope": scope,
            "paid_evidence": capture_paid_payout(payout, scope=scope), "payout": payout, "payment": payment,
            "platform_account": {"object": "account", "id": "acct_platform"},
            "platform_balance": {"object": "balance", "livemode": scope.livemode},
            "provider_payout": bank, "payout_account_id": "acct_seller", "transfer": transfer,
            "inventory": {"complete": True, "scope": scope.payload(), "case_id": "case_one",
                          "payment_id": "pi_one", "transfer_id": "tr_one", "refunds": [], "recoveries": []},
            "funding": funding, "observed_at": NOW, "at": NOW}


def inspect(bundle):
    args = dict(bundle)
    case = args.pop("case")
    return inspect_paid_out_refund(case, **args)


def outcome(bundle, kind, amount, *, status="succeeded", provider_status="succeeded", remote=True, suffix="one"):
    scope = bundle["scope"]
    provider = None
    if remote:
        provider = {"id": ("re_" if kind == "refund" else "trr_") + suffix,
                    "object": "refund" if kind == "refund" else "transfer_reversal",
                    "currency": "jpy", "amount": amount, "created": int((NOW - timedelta(hours=1)).timestamp()),
                    "description": "private-fixture"}
        if kind == "refund":
            provider.update(payment_intent="pi_one", charge="ch_one", status=provider_status,
                            metadata={"order_id": "order_one"})
            if provider_status == "succeeded":
                bundle["payment"]["latest_charge"]["amount_refunded"] += amount
        else:
            provider.update(transfer="tr_one", metadata={"allocation_id": "allocation_one"})
            bundle["transfer"]["amount_reversed"] += amount
            bundle["transfer"]["reversed"] = bundle["transfer"]["amount_reversed"] == 640
    operation = {"id": kind + "_" + suffix, "case_id": "case_one", "scope": scope.payload(),
                 "amount": amount, "status": status, "provider": provider}
    bundle["inventory"]["refunds" if kind == "refund" else "recoveries"].append(operation)
    return operation


def proposal(inspection, *, at=NOW):
    return propose_paid_out_refund(inspection, proposal_id="proposal_one", actor_id="admin_one", admin_ids=ADMINS, at=at)


def review(prop, inspection, **kwargs):
    args = {"actor_id": "admin_two", "admin_ids": ADMINS, "consumed_proposal_ids": frozenset(), "at": NOW}
    args.update(kwargs)
    return review_paid_out_refund(prop, inspection, **args)


@pytest.mark.parametrize("mode", ["test", "live"])
def test_review_plan_preserves_paid_records_and_returns_no_private_provider_payload(mode):
    bundle = facts(mode)
    before = deepcopy(bundle)
    result = inspect(bundle)
    payload = result.payload()
    assert payload["buyer_refund"] == {"target": 1000, "confirmed": 0, "remaining": 1000, "state": "not_started", "holds": []}
    assert payload["seller_recovery"] == {"target": 640, "confirmed": 0, "remaining": 640, "state": "not_started", "holds": []}
    assert not payload["execution_allowed"] and not payload["automatic_collection_allowed"]
    assert not payload["release_paid_reservation"]
    assert not payload["refund_eligibility_verified"] and not payload["durable_replay_protection"]
    assert "private" not in result.reviewed_json and bundle == before
    reviewed = review(proposal(result), result)
    assert reviewed.proposed_by != reviewed.reviewed_by and not reviewed.execution_allowed
    with pytest.raises(FrozenInstanceError):
        bundle["paid_evidence"].identity_json = "changed"
    with pytest.raises(FrozenInstanceError):
        reviewed.execution_allowed = True


def test_buyer_complete_does_not_mark_seller_recovered_or_release_paid_reservation():
    bundle = facts()
    outcome(bundle, "refund", 1000)
    bundle["funding"]["available_amount"] = 0
    result = inspect(bundle).payload()
    assert result["buyer_refund"]["state"] == "complete" and result["buyer_refund"]["remaining"] == 0
    assert result["seller_recovery"]["state"] == "not_started" and result["seller_recovery"]["remaining"] == 640
    assert result["paid_payout"]["status"] == "paid" and not result["release_paid_reservation"]


def test_seller_recovered_does_not_mark_buyer_refunded_or_replace_one_reserve():
    bundle = facts()
    outcome(bundle, "recovery", 640)
    bundle["funding"] = None
    inspected = inspect(bundle)
    result = inspected.payload()
    assert result["seller_recovery"]["state"] == "complete"
    assert result["buyer_refund"]["remaining"] == 1000
    assert "approved_reserve_required" in result["buyer_refund"]["holds"]
    with pytest.raises(FinanceConflict):
        proposal(inspected)


def test_explicit_zero_seller_recovery_decision_is_not_inferred_from_buyer_refund():
    bundle = facts()
    bundle["case"]["seller_recovery_amount"] = 0
    bundle["funding"]["recovery_limit"] = 0
    result = inspect(bundle).payload()
    assert result["seller_recovery"]["state"] == "complete"
    assert result["seller_recovery"]["target"] == 0 and result["buyer_refund"]["remaining"] == 1000


@pytest.mark.parametrize("kind,amount,remaining", [("refund", 400, 600), ("recovery", 240, 400)])
def test_partial_confirmed_amounts_are_exact_but_held(kind, amount, remaining):
    bundle = facts()
    outcome(bundle, kind, amount)
    result = inspect(bundle).payload()
    component = result["buyer_refund" if kind == "refund" else "seller_recovery"]
    assert component["confirmed"] == amount and component["remaining"] == remaining
    assert component["state"] == "held" and "partial_settlement" in component["holds"]
    assert not result["execution_allowed"]


@pytest.mark.parametrize("kind", ["refund", "recovery"])
@pytest.mark.parametrize("status", ["unknown", "pending", "rejected"])
def test_unknown_pending_rejected_intents_never_become_a_retry(kind, status):
    bundle = facts()
    amount = 1000 if kind == "refund" else 640
    outcome(bundle, kind, amount, status=status, remote=False)
    result = inspect(bundle).payload()
    component = result["buyer_refund" if kind == "refund" else "seller_recovery"]
    assert component["remaining"] == amount and component["state"] == "held"
    assert component["holds"] and not result["execution_allowed"]
    if kind == "recovery":
        assert result["buyer_refund"]["holds"] == []  # ONE reserve is independent.


@pytest.mark.parametrize("kind", ["refund", "recovery"])
def test_remote_success_does_not_erase_unknown_local_intent(kind):
    bundle = facts()
    outcome(bundle, kind, 1000 if kind == "refund" else 640, status="unknown")
    result = inspect(bundle).payload()["buyer_refund" if kind == "refund" else "seller_recovery"]
    assert result["remaining"] == 0 and result["state"] == "held"
    assert "unknown_operation" in result["holds"]


@pytest.mark.parametrize("status", ["pending", "requires_action", "failed", "canceled"])
def test_refund_creation_or_failure_is_not_buyer_refund_completion(status):
    bundle = facts()
    outcome(bundle, "refund", 1000, provider_status=status)
    result = inspect(bundle).payload()["buyer_refund"]
    assert result["confirmed"] == 0 and result["remaining"] == 1000 and result["state"] == "held"


@pytest.mark.parametrize("kind", ["refund", "recovery"])
def test_parent_totals_missing_from_case_inventory_force_reconciliation(kind):
    bundle = facts()
    if kind == "refund":
        bundle["payment"]["latest_charge"]["amount_refunded"] = 1000
    else:
        bundle["transfer"].update(amount_reversed=640, reversed=True)
    result = inspect(bundle).payload()["buyer_refund" if kind == "refund" else "seller_recovery"]
    assert result["state"] == "held" and result["confirmed"] == 0
    assert "unreconciled_" + kind + "_total" in result["holds"]


@pytest.mark.parametrize("kind,amount", [("refund", 600), ("recovery", 400)])
def test_multiple_distinct_unknown_intents_cannot_overcommit_the_target(kind, amount):
    bundle = facts()
    outcome(bundle, kind, amount, status="unknown", remote=False, suffix="one")
    outcome(bundle, kind, amount, status="unknown", remote=False, suffix="two")
    with pytest.raises(FinanceConflict):
        inspect(bundle)


def test_same_operation_cannot_be_counted_as_refund_and_recovery():
    bundle = facts()
    refund = outcome(bundle, "refund", 1000)
    recovery = outcome(bundle, "recovery", 640)
    recovery["id"] = refund["id"]
    with pytest.raises(FinanceConflict):
        inspect(bundle)


@pytest.mark.parametrize("field,value", [("payment_id", "pi_other"), ("transfer_id", "tr_other")])
def test_inventory_must_cover_the_original_parent_objects(field, value):
    bundle = facts()
    bundle["inventory"][field] = value
    with pytest.raises(FinanceConflict):
        inspect(bundle)


def test_older_case_unknown_intent_is_not_filtered_out_as_permission_to_retry():
    bundle = facts()
    existing = outcome(bundle, "refund", 1000, status="unknown", remote=False)
    existing["case_id"] = "previous_case"
    with pytest.raises(FinanceConflict):
        inspect(bundle)


@pytest.mark.parametrize("when", [NOW - timedelta(minutes=31), NOW + timedelta(seconds=1), NOW.replace(tzinfo=None)])
def test_stale_future_or_timezone_less_evidence_is_not_fresh(when):
    bundle = facts()
    bundle["observed_at"] = when
    with pytest.raises(FinanceConflict):
        inspect(bundle)


@pytest.mark.parametrize("mode", ["test", "live"])
@pytest.mark.parametrize("part", ["payment", "charge", "bank", "transfer", "balance", "binding", "platform", "evidence", "inventory"])
def test_cross_mode_wrong_account_or_ledger_is_rejected(mode, part):
    bundle = facts(mode)
    other = FinancialScope("live" if mode == "test" else "test", "acct_platform", bundle["scope"].ledger_id)
    if part in {"payment", "charge", "bank", "transfer", "balance"}:
        target = {"payment": bundle["payment"], "charge": bundle["payment"]["latest_charge"],
                  "bank": bundle["provider_payout"], "transfer": bundle["transfer"], "balance": bundle["platform_balance"]}[part]
        target["livemode"] = other.livemode
    if part == "binding": bundle["bound_scope"] = replace(bundle["scope"], ledger_id="22222222-2222-4222-8222-222222222222")
    if part == "platform": bundle["platform_account"]["id"] = "acct_other"
    if part == "evidence": bundle["paid_evidence"] = replace(bundle["paid_evidence"], scope=other)
    if part == "inventory": bundle["inventory"]["scope"] = other.payload()
    with pytest.raises(FinanceConflict):
        inspect(bundle)


@pytest.mark.parametrize("field,value", [
    ("status", "cancelled"), ("paid_at", NOW), ("provider_payout_id", "po_other"),
    ("seller_id", "seller_other"), ("account_id", "acct_other"), ("request_key", "other"),
    ("scheduled_for", NOW), ("cancellation_requested", True), ("amount", 999),
])
def test_paid_payout_history_cannot_be_relabelled_or_changed(field, value):
    bundle = facts()
    bundle["payout"][field] = value
    with pytest.raises(FinanceConflict):
        inspect(bundle)


@pytest.mark.parametrize("field,value", [("receipt_id", "pi_other"), ("source_charge", "ch_other"),
    ("transfer_id", "tr_other"), ("reversal_id", "trr_manual"), ("amount", 900), ("order_id", "order_other")])
def test_paid_allocation_history_is_immutable(field, value):
    bundle = facts()
    bundle["payout"]["allocations"][0][field] = value
    with pytest.raises(FinanceConflict):
        inspect(bundle)


@pytest.mark.parametrize("fault", ["multiple_allocations", "missing_paid_time", "incomplete_inventory", "partial_refund_policy", "extra_case_field"])
def test_unsupported_or_incomplete_cases_stop(fault):
    bundle = facts()
    if fault == "multiple_allocations": bundle["payout"]["allocations"] *= 2
    if fault == "missing_paid_time": del bundle["payout"]["paid_at"]
    if fault == "incomplete_inventory": bundle["inventory"]["complete"] = False
    if fault == "partial_refund_policy": bundle["case"]["refund_amount"] = 500
    if fault == "extra_case_field": bundle["case"]["automatic_netting"] = True
    with pytest.raises(FinanceConflict):
        inspect(bundle)


@pytest.mark.parametrize("kind", ["refund", "recovery"])
@pytest.mark.parametrize("fault", ["too_much", "duplicate_operation", "duplicate_remote", "wrong_scope", "wrong_case", "wrong_parent", "future", "missing_remote"])
def test_over_settlement_duplicate_or_wrong_parent_evidence_is_rejected(kind, fault):
    bundle = facts()
    amount = 1000 if kind == "refund" else 640
    operation = outcome(bundle, kind, amount)
    array = bundle["inventory"]["refunds" if kind == "refund" else "recoveries"]
    if fault == "too_much": operation["amount"] += 1; operation["provider"]["amount"] += 1
    if fault == "duplicate_operation": array.append(deepcopy(operation))
    if fault == "duplicate_remote": array.append({**deepcopy(operation), "id": "other_operation"})
    if fault == "wrong_scope": operation["scope"] = {**operation["scope"], "platform_account": "acct_other"}
    if fault == "wrong_case": operation["case_id"] = "other_case"
    if fault == "wrong_parent": operation["provider"]["charge" if kind == "refund" else "transfer"] = "other"
    if fault == "future": operation["provider"]["created"] = int(NOW.timestamp()) + 1
    if fault == "missing_remote": operation["provider"] = None
    with pytest.raises(FinanceConflict):
        inspect(bundle)


@pytest.mark.parametrize("field,value", [("policy", "automatic_seller_debit"), ("case_id", "other"),
    ("refund_limit", 999), ("recovery_limit", 639), ("approved_at", NOW + timedelta(seconds=1)),
    ("available_amount", True), ("reserve_id", "")])
def test_per_case_approved_funding_fields_are_required(field, value):
    bundle = facts()
    bundle["funding"][field] = value
    with pytest.raises(FinanceConflict):
        inspect(bundle)


def test_insufficient_reserve_cannot_be_two_person_reviewed_as_funded():
    bundle = facts()
    bundle["funding"]["available_amount"] = 999
    inspection = inspect(bundle)
    assert "approved_reserve_required" in inspection.payload()["buyer_refund"]["holds"]
    with pytest.raises(FinanceConflict):
        proposal(inspection)


@pytest.mark.parametrize("fault", ["same_actor", "outsider", "one_admin", "expired", "future", "consumed", "old_get", "wrong_case", "wrong_scope"])
def test_review_rejects_stale_replay_or_wrong_reviewers(fault):
    bundle = facts()
    inspection = inspect(bundle)
    prop = proposal(inspection)
    kwargs = {}
    if fault == "same_actor": kwargs["actor_id"] = "admin_one"
    if fault == "outsider": kwargs["actor_id"] = "outsider"
    if fault == "one_admin": kwargs["admin_ids"] = frozenset({"admin_two"})
    if fault == "expired": kwargs["at"] = NOW + timedelta(minutes=31)
    if fault == "future": kwargs["at"] = NOW - timedelta(seconds=1)
    if fault == "consumed": kwargs["consumed_proposal_ids"] = frozenset({prop.id})
    if fault == "old_get": prop = replace(prop, proposed_at=NOW + timedelta(seconds=1)); kwargs["at"] = NOW + timedelta(seconds=2)
    if fault == "wrong_case": prop = replace(prop, case_id="other")
    if fault == "wrong_scope": prop = replace(prop, scope=replace(prop.scope, mode="live"))
    with pytest.raises(FinanceConflict):
        review(prop, inspection, **kwargs)


@pytest.mark.parametrize("fault", ["reserve", "reserve_amount", "decision", "approver", "funding_time", "refund", "recovery", "unknown_intent", "recovery_target"])
def test_second_admin_must_review_exact_current_fields(fault):
    bundle = facts()
    prop = proposal(inspect(bundle))
    if fault == "reserve": bundle["funding"]["reserve_id"] = "reserve_other"
    if fault == "reserve_amount": bundle["funding"]["available_amount"] = 1001
    if fault == "decision": bundle["funding"]["decision_id"] = "decision_other"
    if fault == "approver": bundle["funding"]["approved_by"] = "admin_two"
    if fault == "funding_time": bundle["funding"]["approved_at"] -= timedelta(seconds=1)
    if fault == "refund": outcome(bundle, "refund", 1000)
    if fault == "recovery": outcome(bundle, "recovery", 640)
    if fault == "unknown_intent": outcome(bundle, "refund", 1000, status="unknown", remote=False)
    if fault == "recovery_target": bundle["case"]["seller_recovery_amount"] = 0; bundle["funding"]["recovery_limit"] = 0
    bundle["observed_at"] = bundle["at"] = NOW + timedelta(seconds=1)
    fresh = inspect(bundle)
    with pytest.raises(FinanceConflict):
        review(prop, fresh, at=bundle["at"])


def test_same_fresh_evidence_succeeds_but_review_is_not_durable_replay_protection():
    bundle = facts()
    first = inspect(bundle)
    prop = proposal(first)
    bundle["observed_at"] = bundle["at"] = NOW + timedelta(seconds=2)
    fresh = inspect(bundle)
    assert first.fingerprint == fresh.fingerprint
    reviewed = review(prop, fresh, at=bundle["at"])
    assert not reviewed.execution_allowed
    with pytest.raises(FinanceConflict):
        review(prop, fresh, consumed_proposal_ids=frozenset({reviewed.proposal_id}), at=bundle["at"])
    with pytest.raises(FinanceConflict):
        review(reviewed, fresh, at=bundle["at"])


def test_review_cannot_relabel_inspection_scope_without_matching_reviewed_payload():
    inspection = inspect(facts())
    relabelled = replace(inspection, scope=replace(inspection.scope, mode="live"))
    with pytest.raises(FinanceConflict):
        proposal(relabelled)


def test_reviewed_live_plan_cannot_enable_provider_post(monkeypatch):
    inspection = inspect(facts("live"))
    reviewed = review(proposal(inspection), inspection)
    assert not reviewed.execution_allowed
    def no_network(*args, **kwargs):
        pytest.fail("A pure review plan must not permit live network writes")
    monkeypatch.setattr("app.integrations.httpx.AsyncClient", no_network)
    stripe = StripeIntegration("rk_live_synthetic", "https://example.invalid", charge_mode="separate")
    with pytest.raises(RuntimeError, match="本番決済"):
        asyncio.run(stripe._post("refunds", {"payment_intent": "pi_one", "_idempotency_key": "synthetic"}))
