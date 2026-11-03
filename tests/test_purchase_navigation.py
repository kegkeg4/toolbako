"""Historical orders always retain their room, not a broken product DM action."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.data import DemoStore
from app.request_deadlines import request_today
import app.main as main


@pytest.mark.parametrize("visibility", ["public", "draft", "paused", "unpublished", "removed", "missing", "blocked", "custom_request"])
def test_purchase_contact_action_matches_product_visibility(monkeypatch, visibility):
    store = DemoStore()
    store.orders.clear()
    buyer = {"id":"purchase-nav-buyer", "username":"purchase_nav_buyer", "display_name":"Buyer"}
    tool = store.get("commit-senpai")
    tool["fulfillment_type"] = "custom"
    if visibility == "custom_request":
        item = store.create_request(buyer, {"title":"Synthetic custom job", "deadline":(request_today()+timedelta(days=7)).isoformat()})
        seller = {"id":"purchase-nav-seller", "username":"purchase_nav_seller", "display_name":"Seller"}
        store.submit_application(item["id"], seller, "Synthetic proposal", 3000, 7)
        _, _, order = store.contract_request(buyer,item["id"],item["application_list"][0]["id"],payment_pending=False)
    else:
        order = store.buy(buyer, tool["slug"])
        if visibility in {"draft", "paused"}: tool["status"] = visibility
        elif visibility == "unpublished": tool["is_published"] = False
        elif visibility == "removed": tool["is_removed"] = True
        elif visibility == "missing": store.tools.remove(tool)
        elif visibility == "blocked": store.blocks.add((buyer["id"], tool["author_username"]))
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "database_store", None)
    monkeypatch.setattr(main, "current_user", lambda request: buyer)
    async def no_sweep():
        pass
    monkeypatch.setattr(main, "sweep_due_orders", no_sweep)
    with TestClient(main.app) as client:
        response = client.get("/purchases")
        assert response.status_code == 200
        room = f'/orders/{order["id"]}'
        assert f'href="{room}"' in response.text
        assert client.get(room).status_code == 200
        action = f'/messages/start/{order["tool_slug"]}'
        assert (f'action="{action}"' in response.text) is (visibility == "public")
        if visibility == "public":
            assert client.post(action,follow_redirects=False).status_code == 303
        else:
            # Same eligibility as the receiving endpoint; no stale path exposed.
            assert client.post(action,follow_redirects=False).status_code == (403 if visibility == "blocked" else 404)
