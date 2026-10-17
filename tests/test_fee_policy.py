"""Approved standard fee and preservation of already quoted order terms."""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import data
from app.config import Settings
from app.data import DemoStore
from app.finance import balance, receipts
from app.integrations import StripeIntegration
from app.payment_state import apply_checkout_payment


@pytest.fixture
def purchase(monkeypatch):
    config = Settings(platform_fee_rate=0.20, custom_fee_rate=0.18)
    monkeypatch.setattr(data, "settings", config)
    store = DemoStore()
    store.orders.clear()
    tool = store.get("minutes-magic")
    tool.update(price=1000, fulfillment_type="custom")
    buyer = {"id": "fee-test-buyer", "username": "fee_test", "display_name": "Fee test"}
    return config, store, buyer


def test_default_standard_fee_is_twenty_percent():
    assert Settings().platform_fee_rate == 0.20
    assert Settings(platform_fee_rate=-1).platform_fee_rate == 0.20


def test_new_purchase_quotes_twenty_percent_and_only_releases_after_completion(purchase):
    config, store, buyer = purchase
    order = store.buy(buyer, "minutes-magic", payment_pending=True)
    assert (order["amount"], order["platform_fee"], order["platform_fee_rate"]) == (1000, 200, 0.20)
    stripe = StripeIntegration("sk_test_fee", "https://example.test", charge_mode="destination")
    payload = stripe.checkout_data(order, "acct_fee_test")
    assert payload["line_items[0][price_data][unit_amount]"] == "1000"
    assert payload["payment_intent_data[application_fee_amount]"] == "200"
    apply_checkout_payment(store, config, order, None, {"payment_intent": "pi_fee_test"})
    assert balance(store, order["seller_id"]) == 0
    store.deliver(order, "Test delivery")
    store.transition(order, "accept", actor_id=buyer["id"])
    assert receipts(store)["pi_fee_test"]["net"] == 800
    assert balance(store, order["seller_id"]) == 800


@pytest.mark.parametrize("quoted_rate,primary_fee,extra_fee,net", [
    (0.15, 150, 75, 1275),
    (0.20, 200, 100, 1200),
])
def test_existing_quotes_and_extra_payments_keep_the_recorded_rate(
    purchase, monkeypatch, quoted_rate, primary_fee, extra_fee, net,
):
    config, store, buyer = purchase
    monkeypatch.setattr(data, "settings", replace(config, platform_fee_rate=quoted_rate))
    order = store.buy(buyer, "minutes-magic", payment_pending=True)
    monkeypatch.setattr(data, "settings", config)
    # Retrying a checkout does not reprice an existing order.
    retried, created = store.buy(buyer, "minutes-magic", payment_pending=True, return_created=True)
    assert not created and retried is order
    assert order["platform_fee"] == primary_fee
    stripe = StripeIntegration("sk_test_fee", "https://example.test", charge_mode="destination")
    assert stripe.checkout_data(order, "acct_fee_test")["payment_intent_data[application_fee_amount]"] == str(primary_fee)
    subscription = {**order, "billing_type": "subscription"}
    assert stripe.checkout_data(subscription, "acct_fee_test")["subscription_data[application_fee_percent]"] == str(quoted_rate * 100)
    apply_checkout_payment(store, config, order, None, {"payment_intent": "pi_primary_fee_test"})
    extra, _ = store.reserve_extra_payment(order, 500, "Additional work")
    assert stripe.extra_checkout_data(order, extra, "acct_fee_test")["payment_intent_data[application_fee_amount]"] == str(extra_fee)
    apply_checkout_payment(store, config, order, extra, {"payment_intent": "pi_extra_fee_test"})
    apply_checkout_payment(store, config, order, extra, {"payment_intent": "pi_extra_fee_test"})
    assert order["platform_fee_rate"] == quoted_rate
    assert order["platform_fee"] == primary_fee + extra_fee
    assert receipts(store)["pi_primary_fee_test"]["fee"] == primary_fee
    assert receipts(store)["pi_extra_fee_test"]["fee"] == extra_fee
    store.deliver(order, "Test delivery")
    store.transition(order, "accept", actor_id=buyer["id"])
    assert balance(store, order["seller_id"]) == net


def test_public_standard_fee_disclosure_matches_new_order_rate():
    from app.main import app
    client = TestClient(app)
    page = client.get("/tokushoho")
    assert page.status_code == 200
    assert "通常販売の販売者手数料は20.0%（税込・通常のカード決済費込み）" in page.text
    assert "銀行振込の手数料は別途定めます" in page.text
    assert "納品承諾・取引完了後" in page.text
