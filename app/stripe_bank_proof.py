"""Pure bank/allocation evidence checks; never authority to move money.

The caller authenticates platform/dataset binding and the response's connected
account context. A Payout object does not identify its owning account itself.
An account context must come from the signed envelope, authenticated GET/POST,
or a matching durable operation request, never from an untrusted payload.
"""
import re

from .finance import FinanceConflict


def _require(condition):
    if not condition:
        raise FinanceConflict("銀行振込・売上分配の証拠が一致しません")


def _reference(value, prefix):
    return isinstance(value, str) and re.fullmatch(prefix + r"_[A-Za-z0-9]+", value) is not None


def _common(remote, *, object_type, prefix, expected_mode, amount, currency, metadata, required_metadata, reference):
    _require(isinstance(expected_mode, str) and expected_mode in {"test", "live"}
             and type(amount) is int and amount > 0 and currency == "jpy"
             and isinstance(metadata, dict) and set(required_metadata) <= metadata.keys()
             and all(isinstance(key, str) and key and isinstance(value, str) and value
                     for key, value in metadata.items())
             and (reference is None or _reference(reference, prefix)))
    _require(isinstance(remote, dict) and remote.get("object") == object_type
             and _reference(remote.get("id"), prefix)
             and (reference is None or remote["id"] == reference)
             and remote.get("livemode") is (expected_mode == "live")
             and type(remote.get("amount")) is int and remote["amount"] == amount
             and remote.get("currency") == currency
             and isinstance(remote.get("metadata"), dict)
             and all(remote["metadata"].get(key) == value for key, value in metadata.items()))


def bank_payout_proof(remote, *, expected_mode, account_id, response_account_id,
                      amount, currency, metadata, payout_id=None):
    """Validate a manual bank Payout in an authenticated account context.

All recognized statuses remain evidence, not permission to mark funds paid.
State transitions and fresh-GET/event ordering stay with the caller.
"""
    _require(_reference(account_id, "acct") and response_account_id == account_id)
    _common(remote, object_type="payout", prefix="po", expected_mode=expected_mode,
            amount=amount, currency=currency, metadata=metadata,
            required_metadata=("payout_id",), reference=payout_id)
    _require(remote.get("status") in ("pending", "in_transit", "paid", "failed", "canceled")
             and remote.get("automatic") is False and remote.get("method") == "standard"
             and remote.get("type") == "bank_account")
    arrival = remote.get("arrival_date")
    _require(arrival is None or (type(arrival) is int and 0 < arrival <= 253402214400))
    return {"id": remote["id"], "account_id": account_id, "amount": amount,
            "currency": currency, "status": remote["status"], "arrival_date": arrival}


def allocation_transfer_proof(remote, *, expected_mode, account_id, source_charge,
                              amount, currency, metadata, transfer_group, transfer_id=None):
    """Validate a new/unreversed allocation, not a refund/reversal recovery.

Any partial or full reversal is rejected here. Recovery requires its separate
fresh parent/reversal evidence, existing journal and two-person approval.
"""
    _require(_reference(account_id, "acct") and _reference(source_charge, "ch")
             and isinstance(transfer_group, str) and transfer_group.startswith("order-")
             and len(transfer_group) > len("order-"))
    _common(remote, object_type="transfer", prefix="tr", expected_mode=expected_mode,
            amount=amount, currency=currency, metadata=metadata,
            required_metadata=("payout_id", "allocation_id"), reference=transfer_id)
    _require(remote.get("destination") == account_id and remote.get("source_transaction") == source_charge
             and remote.get("transfer_group") == transfer_group and remote.get("reversed") is False
             and type(remote.get("amount_reversed")) is int and remote["amount_reversed"] == 0)
    return {"id": remote["id"], "account_id": account_id, "source_charge": source_charge,
            "amount": amount, "currency": currency, "transfer_group": transfer_group}
