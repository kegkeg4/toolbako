"""Pure review plans for one full refund after one paid bank-payout allocation.

Not wired to routes, the ledger or Stripe. A reviewed plan NEVER authorizes POST.
ONE's approved policy is to reserve buyer-refund funds independently of seller
recovery; this module neither creates that reserve nor debits/nets seller funds.

An adapter must authenticate fresh GETs, verify the bound dataset under its DB
lock, supply the complete parent-payment/transfer operation inventory across
all cases (not only this case) and a previously persisted paid
snapshot, enforce admin/MFA, and atomically persist/consume a review. Passing
objects here cannot prove those external facts or provide durable replay safety.
Order refund/cancellation eligibility must be checked before opening a case;
this planner does not decide a buyer's entitlement or a seller's legal liability.
Multiple allocations, extra payments and partial-refund policy are unsupported.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import re

from .finance import FinanceConflict
from .financial_scope import FinancialScope, FinancialScopeError
from .payment_source_proof import payment_source_proof


FUNDING_POLICY = "one_fronts_buyer_refund"
REVIEW_WINDOW = timedelta(minutes=30)  # Same upper bound as existing recovery.


def _fail():
    raise FinanceConflict("払出し後返金の証拠・審査内容が一致しません。実行せず確認してください")


def _require(condition):
    if not condition:
        _fail()


def _id(value, prefix=None):
    return (isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]{1,120}", value))
            and (prefix is None or value.startswith(prefix) and len(value) > len(prefix)))


def _yen(value, *, zero=False):
    return type(value) is int and value >= (0 if zero else 1)


def _time(value):
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _paid_identity(payout):
    _require(isinstance(payout, dict) and payout.get("finance_v2") is True
             and payout.get("status") == "paid" and _time(payout.get("paid_at"))
             and _time(payout.get("scheduled_for")) and not payout.get("cancellation_requested"))
    for key in ("id", "seller_id", "request_key"):
        _require(_id(payout.get(key)))
    _require(_id(payout.get("account_id"), "acct_")
             and _id(payout.get("provider_payout_id"), "po_")
             and _yen(payout.get("amount")) and _yen(payout.get("fee"), zero=True)
             and _yen(payout.get("net_amount"))
             and payout["amount"] == payout["fee"] + payout["net_amount"])
    allocations = payout.get("allocations")
    _require(isinstance(allocations, list) and len(allocations) == 1)
    a = allocations[0]
    _require(isinstance(a, dict) and _id(a.get("id")) and _id(a.get("order_id"))
             and _id(a.get("receipt_id"), "pi_") and _id(a.get("source_charge"), "ch_")
             and _id(a.get("transfer_id"), "tr_") and not a.get("reversal_id")
             and _yen(a.get("amount")) and _yen(a.get("transfer_amount"))
             and a["amount"] == payout["amount"] and a["transfer_amount"] == payout["net_amount"])
    result = {k: payout[k] for k in ("id", "seller_id", "account_id", "amount", "fee", "net_amount",
                                    "provider_payout_id", "status", "request_key")}
    result.update(paid_at=payout["paid_at"].isoformat(), scheduled_for=payout["scheduled_for"].isoformat(),
                  allocation={k: a[k] for k in ("id", "order_id", "receipt_id", "source_charge", "transfer_id",
                                                "amount", "transfer_amount")})
    return result


@dataclass(frozen=True)
class PaidPayoutEvidence:
    scope: FinancialScope
    identity_json: str


def capture_paid_payout(payout, *, scope):
    """Capture a baseline to persist BEFORE a case; never recapture to hide edits."""
    _require(isinstance(scope, FinancialScope))
    return PaidPayoutEvidence(scope, _json(_paid_identity(payout)))


@dataclass(frozen=True)
class PaidOutRefundInspection:
    scope: FinancialScope
    observed_at: datetime
    reviewed_json: str

    @property
    def fingerprint(self):
        return _digest(self.reviewed_json)

    def payload(self):
        return json.loads(self.reviewed_json)


def _outcomes(operations, *, kind, case, scope, source, allocation, paid_at, observed_at):
    """Journal + authenticated GET evidence; unknown intent is never a retry."""
    _require(isinstance(operations, (list, tuple)) and len(operations) <= 100)
    identities, ids, references, holds = [], set(), set(), set()
    confirmed = committed = 0
    target = case["refund_amount"] if kind == "refund" else case["seller_recovery_amount"]
    for operation in operations:
        _require(isinstance(operation, dict) and _id(operation.get("id"))
                 and operation["id"] not in ids and operation.get("case_id") == case["id"]
                 and operation.get("scope") == scope.payload() and _yen(operation.get("amount"))
                 and operation["amount"] <= target
                 and operation.get("status") in {"pending", "unknown", "rejected", "succeeded"})
        ids.add(operation["id"])
        status, amount = operation["status"], operation["amount"]
        if status != "rejected":
            committed += amount
        if status != "succeeded":
            holds.add("unknown_operation" if status in {"pending", "unknown"} else "rejected_operation")
        remote = operation.get("provider")
        minimal = None
        if remote is None:
            _require(status != "succeeded")
        else:
            _require(isinstance(remote, dict) and remote.get("currency") == "jpy"
                     and type(remote.get("amount")) is int and remote["amount"] == amount
                     and type(remote.get("created")) is int
                     and int(paid_at.timestamp()) <= remote["created"] <= int(observed_at.timestamp())
                     and isinstance(remote.get("metadata"), dict))
            if kind == "refund":
                _require(remote.get("object") == "refund" and _id(remote.get("id"), "re_")
                         and remote.get("payment_intent") == source["id"] and remote.get("charge") == source["charge"]
                         and remote["metadata"].get("order_id") == case["order_id"]
                         and "extra_id" not in remote["metadata"]
                         and remote.get("status") in {"succeeded", "pending", "requires_action", "failed", "canceled"})
                if remote["status"] == "succeeded":
                    confirmed += amount
                else:
                    holds.add("provider_pending" if remote["status"] in {"pending", "requires_action"} else "provider_failed")
                minimal = {k: remote[k] for k in ("id", "object", "amount", "currency", "created", "payment_intent", "charge", "status")}
                minimal["order_id"] = remote["metadata"]["order_id"]
            else:
                # Reversals have no livemode; their verified Transfer supplies it.
                _require(remote.get("object") == "transfer_reversal" and _id(remote.get("id"), "trr_")
                         and remote.get("transfer") == allocation["transfer_id"]
                         and remote["metadata"].get("allocation_id") == allocation["id"])
                confirmed += amount
                minimal = {k: remote[k] for k in ("id", "object", "amount", "currency", "created", "transfer")}
                minimal["allocation_id"] = remote["metadata"]["allocation_id"]
            _require(remote["id"] not in references and status != "rejected")
            references.add(remote["id"])
        identities.append({"id": operation["id"], "status": status, "amount": amount, "provider": minimal})
    _require(confirmed <= target and committed <= target)
    if 0 < confirmed < target:
        holds.add("partial_settlement")
    state = "complete" if confirmed == target else "partial" if confirmed else "not_started"
    return {"target": target, "confirmed": confirmed, "remaining": target - confirmed,
            "state": state, "holds": sorted(holds)}, sorted(identities, key=lambda x: x["id"]), ids


def inspect_paid_out_refund(case, *, scope, bound_scope, paid_evidence, payout, payment,
                            platform_account, platform_balance, provider_payout, payout_account_id,
                            transfer, inventory, funding, observed_at, at):
    """Evaluate supplied fresh evidence. No response is an execution permission."""
    _require(isinstance(scope, FinancialScope) and bound_scope == scope
             and isinstance(paid_evidence, PaidPayoutEvidence) and paid_evidence.scope == scope
             and _time(at) and _time(observed_at) and timedelta(0) <= at - observed_at <= REVIEW_WINDOW)
    try:
        scope.validate_provider(platform_account, platform_balance)
    except FinancialScopeError:
        _fail()
    paid = _paid_identity(payout)
    _require(_json(paid) == paid_evidence.identity_json and payout["paid_at"] <= observed_at)
    allocation = paid["allocation"]
    fields = {"id", "order_id", "payment_id", "payment_amount", "refund_amount", "seller_recovery_amount",
              "seller_id", "payout_id", "allocation_id"}
    _require(isinstance(case, dict) and set(case) == fields and _id(case.get("id"))
             and case["order_id"] == allocation["order_id"] and case["payment_id"] == allocation["receipt_id"]
             and case["seller_id"] == paid["seller_id"] and case["payout_id"] == paid["id"]
             and case["allocation_id"] == allocation["id"] and _yen(case["payment_amount"])
             and allocation["amount"] <= case["payment_amount"]
             and type(case["refund_amount"]) is int and case["refund_amount"] == case["payment_amount"]
             and _yen(case["seller_recovery_amount"], zero=True)
             and case["seller_recovery_amount"] <= allocation["transfer_amount"])
    source = payment_source_proof(payment, payment_id=case["payment_id"], amount=case["payment_amount"],
                                  order_id=case["order_id"], expected_mode=scope.mode, allow_refunded=True,
                                  source_charge=allocation["source_charge"])
    _require(isinstance(payment.get("metadata"), dict) and payment["metadata"].get("order_id") == case["order_id"]
             and "extra_id" not in payment["metadata"]
             and payment["latest_charge"].get("transfer_group") == f"order-{case['order_id']}")
    bank = provider_payout
    _require(payout_account_id == paid["account_id"] and isinstance(bank, dict)
             and bank.get("object") == "payout" and bank.get("id") == paid["provider_payout_id"]
             and bank.get("livemode") is scope.livemode and bank.get("status") == "paid"
             and type(bank.get("amount")) is int and bank["amount"] == paid["net_amount"]
             and bank.get("currency") == "jpy" and bank.get("automatic") is False
             and bank.get("method") == "standard" and bank.get("type") == "bank_account"
             and isinstance(bank.get("metadata"), dict) and bank["metadata"].get("payout_id") == paid["id"])
    _require(isinstance(transfer, dict) and transfer.get("object") == "transfer"
             and transfer.get("id") == allocation["transfer_id"] and transfer.get("livemode") is scope.livemode
             and type(transfer.get("amount")) is int and transfer["amount"] == allocation["transfer_amount"]
             and transfer.get("currency") == "jpy" and transfer.get("destination") == paid["account_id"]
             and transfer.get("source_transaction") == source["charge"]
             and transfer.get("transfer_group") == f"order-{case['order_id']}"
             and isinstance(transfer.get("metadata"), dict) and transfer["metadata"].get("payout_id") == paid["id"]
             and transfer["metadata"].get("allocation_id") == allocation["id"]
             and _yen(transfer.get("amount_reversed"), zero=True)
             and transfer["amount_reversed"] <= case["seller_recovery_amount"]
             and transfer.get("reversed") is (transfer["amount_reversed"] == transfer["amount"]))
    _require(isinstance(inventory, dict) and inventory.get("complete") is True
             and inventory.get("scope") == scope.payload() and inventory.get("case_id") == case["id"]
             and inventory.get("payment_id") == case["payment_id"]
             and inventory.get("transfer_id") == allocation["transfer_id"])
    buyer, refunds, refund_ids = _outcomes(inventory.get("refunds"), kind="refund", case=case, scope=scope,
        source=source, allocation=allocation, paid_at=payout["paid_at"], observed_at=observed_at)
    seller, recoveries, recovery_ids = _outcomes(inventory.get("recoveries"), kind="recovery", case=case, scope=scope,
        source=source, allocation=allocation, paid_at=payout["paid_at"], observed_at=observed_at)
    _require(not refund_ids & recovery_ids)
    if buyer["confirmed"] != source["amount_refunded"]:
        buyer["holds"] = sorted(set(buyer["holds"]) | {"unreconciled_refund_total"})
    if seller["confirmed"] != transfer["amount_reversed"]:
        seller["holds"] = sorted(set(seller["holds"]) | {"unreconciled_recovery_total"})
    for component in (buyer, seller):
        if component["holds"]:
            component["state"] = "held"
    funding_proof = None
    if funding is not None:
        required = {"policy", "decision_id", "case_id", "scope", "refund_limit", "recovery_limit", "reserve_id",
                    "available_amount", "approved_by", "approved_at"}
        _require(isinstance(funding, dict) and set(funding) == required
                 and funding["policy"] == FUNDING_POLICY and funding["case_id"] == case["id"]
                 and funding["scope"] == scope.payload() and _id(funding["decision_id"]) and _id(funding["reserve_id"])
                 and type(funding["refund_limit"]) is int and funding["refund_limit"] == case["refund_amount"]
                 and type(funding["recovery_limit"]) is int and funding["recovery_limit"] == case["seller_recovery_amount"]
                 and _yen(funding["available_amount"], zero=True) and _id(funding["approved_by"])
                 and _time(funding["approved_at"]) and funding["approved_at"] <= observed_at)
        funding_proof = {**funding, "approved_at": funding["approved_at"].isoformat()}
    if funding_proof is None or funding_proof["available_amount"] < buyer["remaining"]:
        buyer["holds"] = sorted(set(buyer["holds"]) | {"approved_reserve_required"})
        buyer["state"] = "held"
    reviewed = {"version": 1, "case": dict(case), "scope": scope.payload(), "paid_payout": paid,
                "source": source, "bank": {k: bank[k] for k in ("id", "object", "livemode", "amount", "currency",
                                                               "status", "automatic", "method", "type")},
                "transfer": {k: transfer[k] for k in ("id", "object", "livemode", "amount", "currency", "destination",
                    "source_transaction", "transfer_group", "amount_reversed", "reversed")},
                "refund_operations": refunds, "recovery_operations": recoveries, "funding": funding_proof,
                "buyer_refund": buyer, "seller_recovery": seller, "execution_allowed": False,
                "automatic_collection_allowed": False, "release_paid_reservation": False,
                "refund_eligibility_verified": False, "durable_replay_protection": False}
    return PaidOutRefundInspection(scope, observed_at, _json(reviewed))


@dataclass(frozen=True)
class PaidOutRefundProposal:
    id: str
    case_id: str
    scope: FinancialScope
    fingerprint: str
    proposed_by: str
    proposed_at: datetime


@dataclass(frozen=True)
class PaidOutRefundReview:
    proposal_id: str
    case_id: str
    fingerprint: str
    proposed_by: str
    reviewed_by: str
    reviewed_at: datetime
    reviewed_json: str
    execution_allowed: bool = False


def _review_guard(inspection, actor_id, admin_ids, at):
    _require(isinstance(inspection, PaidOutRefundInspection) and _time(at)
             and timedelta(0) <= at - inspection.observed_at <= REVIEW_WINDOW
             and isinstance(admin_ids, (set, frozenset)) and len(admin_ids) >= 2
             and all(_id(actor) for actor in admin_ids) and actor_id in admin_ids)
    payload = inspection.payload()
    _require(payload.get("scope") == inspection.scope.payload())
    funding = payload["funding"]
    _require(funding is not None and funding["approved_by"] in admin_ids
             and funding["available_amount"] >= payload["buyer_refund"]["remaining"])
    return payload


def propose_paid_out_refund(inspection, *, proposal_id, actor_id, admin_ids, at):
    """Review only, including explicit holds; never a refundable API request."""
    payload = _review_guard(inspection, actor_id, admin_ids, at)
    _require(_id(proposal_id))
    return PaidOutRefundProposal(proposal_id, payload["case"]["id"], inspection.scope,
                                  inspection.fingerprint, actor_id, at)


def review_paid_out_refund(proposal, inspection, *, actor_id, admin_ids, consumed_proposal_ids, at):
    """Require fresh identical evidence and two admins. Caller must persist use.

consumed_proposal_ids must come from the locked durable ledger, never a browser.
This pure function does not consume it or replace MFA/access enforcement.
"""
    payload = _review_guard(inspection, actor_id, admin_ids, at)
    _require(isinstance(proposal, PaidOutRefundProposal) and _time(proposal.proposed_at)
             and isinstance(consumed_proposal_ids, (set, frozenset))
             and proposal.id not in consumed_proposal_ids and proposal.proposed_by in admin_ids
             and proposal.proposed_by != actor_id and proposal.scope == inspection.scope
             and proposal.case_id == payload["case"]["id"] and proposal.fingerprint == inspection.fingerprint
             and timedelta(0) <= at - proposal.proposed_at <= REVIEW_WINDOW
             and inspection.observed_at >= proposal.proposed_at)
    return PaidOutRefundReview(proposal.id, proposal.case_id, proposal.fingerprint, proposal.proposed_by,
                                actor_id, at, inspection.reviewed_json)
