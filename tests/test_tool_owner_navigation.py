"""Product owners manage their listing instead of entering self-transactions."""
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from app.data import DemoStore
import app.main as main


class Actions(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links = []
        self.posts = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            self.links.append(attrs.get("href"))
        if tag == "form" and attrs.get("method", "get").lower() == "post":
            self.posts.append(attrs.get("action"))


@pytest.fixture
def product_page(monkeypatch):
    store = DemoStore()
    owner = {"id":"owner-page-seller", "username":"owner_page_seller", "display_name":"同じ表示名"}
    customer = {"id":"owner-page-buyer", "username":"owner_page_buyer", "display_name":"同じ表示名"}
    tool = store.get("commit-senpai")
    tool.update(author_id=owner["id"], author_username=owner["username"],
                author_name=owner["display_name"], customization_available=True,
                fulfillment_type="custom", capacity=5)
    actor = {"user":owner}
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "database_store", None)
    monkeypatch.setattr(main, "current_user", lambda request: actor["user"])
    monkeypatch.setattr(main, "generate_og", lambda *args: "/static/og-default.png")
    with TestClient(main.app) as client:
        yield client, store, tool, owner, customer, actor


@pytest.mark.parametrize("state", ["paid", "free", "consultation", "sold_out", "paused", "draft"])
def test_owner_has_edit_navigation_on_desktop_and_mobile(product_page, state):
    client, _, tool, _, _, _ = product_page
    if state in {"free", "consultation"}:
        tool["price_type"] = state
    elif state == "sold_out":
        tool["capacity"] = 0
    elif state in {"paused", "draft"}:
        tool["status"] = state
    response = client.get(f'/tools/{tool["slug"]}')
    assert response.status_code == 200
    actions = Actions(response.text)
    # Both the side panel and the fixed mobile bar retain a useful route.
    assert actions.links.count(f'/seller/tools/{tool["slug"]}/edit') >= 2
    assert f'/checkout/{tool["slug"]}' not in actions.links
    for target in [f'/tools/{tool["slug"]}/customize',
                   f'/messages/start/{tool["slug"]}',
                   f'/tools/{tool["slug"]}/wait-reopen']:
        assert target not in actions.posts
    if state != "draft":
        assert "あなたが出品した商品です" in response.text
        assert 'aria-label="商品管理メニュー"' in response.text


@pytest.mark.parametrize("viewer", ["anonymous", "customer"])
def test_other_visitors_keep_purchase_and_contact_actions(product_page, viewer):
    client, _, tool, _, customer, actor = product_page
    actor["user"] = None if viewer == "anonymous" else customer
    response = client.get(f'/tools/{tool["slug"]}')
    assert response.status_code == 200
    actions = Actions(response.text)
    assert f'/checkout/{tool["slug"]}' in actions.links
    assert f'/tools/{tool["slug"]}/customize' in actions.posts
    assert f'/messages/start/{tool["slug"]}' in actions.posts
    assert f'/seller/tools/{tool["slug"]}/edit' not in actions.links
    tool["status"] = "draft"
    assert client.get(f'/tools/{tool["slug"]}').status_code == 404


def test_direct_self_transaction_requests_stay_rejected_without_mutation(product_page):
    client, store, tool, _, _, _ = product_page
    orders = list(store.orders)
    conversations = list(store.conversations)
    assert client.post(f'/checkout/{tool["slug"]}', data={"purchase_agreement":"yes"}).status_code == 403
    assert client.post(f'/tools/{tool["slug"]}/customize').status_code == 422
    assert client.post(f'/messages/start/{tool["slug"]}').status_code == 422
    assert store.orders == orders
    assert store.conversations == conversations
