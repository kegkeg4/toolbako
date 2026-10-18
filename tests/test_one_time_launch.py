import asyncio

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import main
from app.config import Settings
from app.data import DemoStore


@pytest.fixture
def launch(monkeypatch):
    store = DemoStore()
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "current_user", lambda _: {"id": "launch-buyer", "username": "launch-buyer"})
    request = Request({"type": "http", "method": "GET", "path": "/checkout/minutes-magic",
                       "headers": [], "query_string": b"", "scheme": "https",
                       "server": ("testserver", 443), "session": {}})
    return request, store


@pytest.mark.parametrize("mode", ["destination", "separate"])
def test_deferred_subscription_rejected_before_order_coupon_or_provider_mutation(monkeypatch, launch, mode):
    request, store = launch
    monkeypatch.setattr(main, "settings", Settings(environment="staging", demo_mode=False, stripe_charge_mode=mode))
    def forbidden(*a, **k): pytest.fail("Deferred plan must not mutate orders or consume coupons")
    monkeypatch.setattr(store, "buy", forbidden)
    monkeypatch.setattr(store, "coupon_discount", forbidden)
    with pytest.raises(HTTPException) as rejected:
        asyncio.run(main.checkout_complete(request, "minutes-magic", [], "", "subscription", "yes"))
    assert rejected.value.status_code == 409
    with pytest.raises(HTTPException) as preview:
        asyncio.run(main.coupon_preview(request, "minutes-magic", "", [], "subscription"))
    assert preview.value.status_code == 422
    page = asyncio.run(main.checkout(request, "minutes-magic")).body.decode()
    assert 'value="subscription"' not in page
    assert 'value="one_time"' in page
    assert "毎月自動更新" not in page
    assert "買い切りのみ" in page


def test_demo_can_still_preview_subscription_without_provider_contact(monkeypatch, launch):
    request, _ = launch
    monkeypatch.setattr(main, "settings", Settings(environment="development", demo_mode=True, database_url=""))
    page = asyncio.run(main.checkout(request, "minutes-magic")).body.decode()
    assert 'value="subscription"' in page
    assert "毎月自動更新" in page


def test_seller_cannot_enable_deferred_plan(monkeypatch, launch):
    request, store = launch
    tool = store.get("minutes-magic")
    original = dict(tool)
    monkeypatch.setattr(main, "settings", Settings(environment="staging", demo_mode=False))
    monkeypatch.setattr(main, "own_tool", lambda *a: tool)
    with pytest.raises(HTTPException) as rejected:
        asyncio.run(main.seller_tool_save(request, tool["slug"], name=tool["name"],
                    tagline=tool["tagline"], description_md="Valid description", price=1000,
                    capacity=5, support_days=30, estimated_delivery_days=1,
                    purchase_notes="", fulfillment_type="custom", supports_subscription="yes",
                    subscription_price=500))
    assert rejected.value.status_code == 409
    assert tool == original
