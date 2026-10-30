from copy import deepcopy

import pytest
from app.config import Settings
from app.subscription_state import apply_invoice
from app.data import DemoStore
from test_webhook_security import webhook, deliver, account_event


def billing():
    store = DemoStore(seed=False)
    order = {"payment_status": "pending", "buyer_email": "synthetic@example.invalid"}
    subscription = {"buyer_id": "buyer", "tool_name": "Synthetic", "status": "pending"}
    return store, order, subscription


def invoice(identifier="in_current", period=200):
    return {"id": identifier, "amount_paid": 500, "period_end": period, "payment_intent": "pi_test", "charge": "ch_test"}


@pytest.mark.parametrize("first_paid", [True, False])
def test_paid_invoice_wins_same_invoice_failure_in_both_delivery_orders(first_paid):
    store, order, subscription = billing()
    events = ["invoice.paid", "invoice.payment_failed"] if first_paid else ["invoice.payment_failed", "invoice.paid"]
    for kind in events: apply_invoice(store, Settings(), order, subscription, invoice(), kind)
    assert subscription["status"] == "active" and order["payment_status"] == "paid"
    assert len(subscription["payments"]) == 1


def test_duplicate_failure_with_another_event_id_does_not_notify_again():
    store, order, subscription = billing()
    assert apply_invoice(store, Settings(), order, subscription, invoice(), "invoice.payment_failed")
    assert not apply_invoice(store, Settings(), order, subscription, invoice(), "invoice.payment_failed")
    assert len(store.notifications) == len(subscription["billing_events"]) == 1


@pytest.mark.parametrize("status", ["cancelled", "expired"])
def test_late_paid_invoice_keeps_cancelled_contract_closed_but_preserves_receipt(status):
    store, order, subscription = billing()
    subscription["status"] = order["payment_status"] = status
    apply_invoice(store, Settings(), order, subscription, invoice(), "invoice.paid")
    assert subscription["status"] == order["payment_status"] == status
    assert subscription["payments"][0]["charge"] == "ch_test"


def test_prior_period_events_cannot_override_newer_invoice_state():
    store, order, subscription = billing()
    apply_invoice(store, Settings(), order, subscription, invoice(period=200), "invoice.paid")
    assert not apply_invoice(store, Settings(), order, subscription, invoice("in_old", 100), "invoice.payment_failed")
    assert subscription["status"] == "active"
    apply_invoice(store, Settings(), order, subscription, invoice("in_next", 300), "invoice.payment_failed")
    apply_invoice(store, Settings(), order, subscription, invoice("in_old", 100), "invoice.paid")
    assert subscription["status"] == "past_due" and subscription["latest_invoice_period_end"] == 300
    assert len(subscription["payments"]) == 2


def test_invoice_fills_missing_checkout_receipt_references_without_duplicate():
    store, order, subscription = billing()
    subscription["payments"] = [{"provider_invoice_id": "in_current", "amount": 500, "status": "paid"}]
    apply_invoice(store, Settings(), order, subscription, invoice(), "invoice.paid")
    assert len(subscription["payments"]) == 1 and subscription["payments"][0]["payment_intent"] == "pi_test"


def test_deployed_account_webhook_uses_current_provider_state_not_event_order(webhook):
    client, store, service = webhook
    service.settings = Settings(environment="staging", stripe_secret_key="rk_test_fixture",
                                stripe_webhook_secret="platform-fixture", stripe_connect_webhook_secret="connect-fixture")
    class Provider:
        calls = 0
        async def _get(self, path):
            assert path == "accounts/acct_seller"
            self.calls += 1
            return {"id": "acct_seller", "object": "account", "charges_enabled": False,
                    "payouts_enabled": False, "details_submitted": True}
    service.provider = Provider()
    event = account_event()  # The older snapshot says payouts are enabled.
    assert deliver(client, event, secret="connect-fixture").status_code == 200
    assert not store.connected_accounts["seller"]["payouts_enabled"]
    assert deliver(client, event, secret="connect-fixture").json()["duplicate"]
    assert service.provider.calls == 1


@pytest.mark.parametrize("failure", ["unavailable", "wrong_object"])
def test_reconciliation_failure_is_retriable_and_never_claimed(webhook, failure):
    client, store, service = webhook
    service.settings = Settings(environment="staging", stripe_secret_key="rk_test_fixture",
                                stripe_connect_webhook_secret="connect-fixture")
    class Provider:
        async def _get(self, path):
            if failure == "unavailable": raise RuntimeError("private provider error")
            return {"id": "acct_other", "object": "account"}
    service.provider = Provider()
    response = deliver(client, account_event(), secret="connect-fixture")
    assert response.status_code == 503 and "private" not in response.text
    assert not store.processed_webhook_events and not store.connected_accounts["seller"]["payouts_enabled"]
