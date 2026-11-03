"""Dashboard amounts/actions follow confirmed payment, not order reservations."""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.data import DemoStore
import app.main as main


@pytest.fixture
def dashboard(monkeypatch):
    store = DemoStore()
    store.orders.clear()
    buyer = {"id": "dashboard-buyer", "username": "dashboard_buyer", "display_name": "Buyer"}
    seller = {"id": "dashboard-seller", "username": "dashboard_seller", "display_name": "Seller"}
    tool = store.get("commit-senpai")
    tool.update(author_id=seller["id"], author_username=seller["username"],
                author_name=seller["display_name"], fulfillment_type="custom")
    order = store.buy(buyer, tool["slug"], payment_pending=True)
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "database_store", None)
    actor = {"user": buyer}
    monkeypatch.setattr(main, "current_user", lambda request: actor["user"])
    # Page read must not run unrelated account/order lifecycle work in this fixture.
    async def no_sweep():
        pass
    monkeypatch.setattr(main, "sweep_due_orders", no_sweep)
    captured = {}
    real_template_response = main.templates.TemplateResponse
    def capture_template(request, name, context, **kwargs):
        captured.clear()
        captured.update(context)
        return real_template_response(request, name, context, **kwargs)
    monkeypatch.setattr(main.templates, "TemplateResponse", capture_template)
    with TestClient(main.app) as client:
        real_get = client.get
        def rendered_get(path):
            response = real_get(path)
            response.context = dict(captured)
            return response
        monkeypatch.setattr(client, "get", rendered_get)
        yield client, actor, order, buyer, seller


@pytest.mark.parametrize("payment_status", ["pending", "expired", "cancelled", "past_due", "unknown", ""])
def test_unconfirmed_reservation_is_not_spending_or_delivery_work(dashboard, payment_status):
    client, actor, order, buyer, seller = dashboard
    order["payment_status"] = payment_status
    assert order["status"] == "in_progress"
    result = client.get("/mypage")
    assert result.status_code == 200
    assert result.context["metrics"]["spent"] == 0
    actor["user"] = seller
    result = client.get("/mypage")
    assert result.context["next_actions"] == []
    # Preserve the reservation in history and never change its financial facts.
    assert result.context["selling"] == [order]
    assert order["payment_status"] == payment_status


def test_confirmed_order_counts_and_prompts_delivery(dashboard):
    client, actor, order, buyer, seller = dashboard
    order["payment_status"] = "paid"
    assert client.get("/mypage").context["metrics"]["spent"] == order["amount"]
    actor["user"] = seller
    result = client.get("/mypage")
    assert result.context["next_actions"] == [order]
    order["status"] = "completed"
    result = client.get("/mypage")
    assert result.context["metrics"]["earned"] == order["amount"] - order["platform_fee"]
    assert result.context["metrics"]["sales"] == 1


@pytest.mark.parametrize("payment_status", ["pending", "unknown", ""])
def test_unconfirmed_completion_does_not_claim_earned_revenue(dashboard, payment_status):
    client, actor, order, buyer, seller = dashboard
    actor["user"] = seller
    order.update(status="completed", payment_status=payment_status)
    result = client.get("/mypage")
    assert result.context["metrics"]["earned"] == 0
    assert result.context["metrics"]["sales"] == 0


@pytest.mark.parametrize("demo_mode, expected_paid", [(True, True), (False, False)])
def test_missing_payment_status_is_legacy_demo_only(dashboard, monkeypatch, demo_mode, expected_paid):
    client, actor, order, buyer, seller = dashboard
    order.pop("payment_status")
    monkeypatch.setattr(main, "settings", replace(main.settings, demo_mode=demo_mode))
    assert client.get("/mypage").context["metrics"]["spent"] == (order["amount"] if expected_paid else 0)
    actor["user"] = seller
    assert client.get("/mypage").context["next_actions"] == ([order] if expected_paid else [])
