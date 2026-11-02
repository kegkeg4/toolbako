"""Pure source evidence checks, not permission to send a financial request.

Callers must separately verify platform/dataset binding, journal and approvals.
The existing workers deliberately pass test mode even though this validator can
inspect either mode. No settings, network I/O, provider mutation or persistence.
"""
from .finance import FinanceConflict


def payment_source_proof(payment, *, payment_id, amount, order_id, expected_mode,
                         allow_refunded=False, source_charge=None):
    """Return only the identity/amount facts safe to retain in a proposal.

Order/extra metadata and context-specific Charge checks remain with the caller.
Refund objects have no mode field; callers can use these parent PI/Charge facts.
"""
    if (not isinstance(expected_mode, str) or expected_mode not in {"test", "live"}
            or type(allow_refunded) is not bool
            or not isinstance(payment_id, str) or not payment_id.startswith("pi_")
            or type(amount) is not int or amount <= 0
            or not isinstance(order_id, str) or not order_id
            or (source_charge is not None
                and (not isinstance(source_charge, str) or not source_charge.startswith("ch_")))):
        raise FinanceConflict("元決済の識別情報を確認できません")
    expected_live = expected_mode == "live"
    if (not isinstance(payment, dict) or payment.get("object") != "payment_intent"
            or payment.get("id") != payment_id or payment.get("livemode") is not expected_live
            or payment.get("status") != "succeeded" or payment.get("currency") != "jpy"
            or type(payment.get("amount")) is not int or payment["amount"] != amount
            or type(payment.get("amount_received")) is not int or payment["amount_received"] != amount
            or payment.get("transfer_group") != f"order-{order_id}"
            or payment.get("transfer_data") is not None or payment.get("on_behalf_of") is not None
            or payment.get("application_fee_amount") is not None):
        raise FinanceConflict("元決済の金額・環境・注文が一致しません")
    charge = payment.get("latest_charge")
    if (not isinstance(charge, dict) or charge.get("object") != "charge"
            or not isinstance(charge.get("id"), str) or not charge["id"].startswith("ch_")
            or charge.get("livemode") is not expected_live or charge.get("payment_intent") != payment_id
            or type(charge.get("amount")) is not int or charge["amount"] != amount
            or type(charge.get("amount_captured")) is not int or charge["amount_captured"] != amount
            or charge.get("currency") != "jpy" or charge.get("paid") is not True
            or charge.get("captured") is not True or charge.get("status") != "succeeded"
            or charge.get("disputed") is not False
            or type(charge.get("amount_refunded")) is not int or not 0 <= charge["amount_refunded"] <= amount
            or (not allow_refunded and charge["amount_refunded"] != 0)
            or (source_charge is not None and charge["id"] != source_charge)):
        raise FinanceConflict("元請求の入金・返金・異議申し立てを確認できません")
    return {"id": payment_id, "amount": amount, "charge": charge["id"],
            "amount_refunded": charge["amount_refunded"], "status": payment["status"]}
