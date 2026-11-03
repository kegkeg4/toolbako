"""Transfer inquiries show usable actions while preserving domain block guards."""
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from app.data import DemoStore
import app.main as main


class Actions(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links = []
        self.inquiry_forms = 0
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            self.links.append(attrs.get("href"))
        # The inquiry posts to the current URL; ignore the shared logout form.
        if (tag == "form" and attrs.get("method", "get").lower() == "post"
                and not attrs.get("action")):
            self.inquiry_forms += 1


INQUIRY = {
    "offer_amount": 470000,
    "intended_use": "自社の業務改善サービスとして継続運営します。",
    "message": "譲渡対象と引き継ぎの範囲について詳しく確認したいです。",
}


@pytest.fixture
def transfer_page(monkeypatch):
    store = DemoStore()
    tool = store.get("workflow-pocket")
    owner = {"id": tool["author_id"], "username": tool["author_username"],
             "display_name": tool["author_name"]}
    buyer = {"id": "transfer-page-buyer", "username": "transfer_page_buyer",
             "display_name": "譲渡購入者"}
    actor = {"user": buyer}
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "database_store", None)
    monkeypatch.setattr(main, "current_user", lambda request: actor["user"])
    monkeypatch.setattr(main, "generate_og", lambda *args: "/static/og-default.png")
    with TestClient(main.app) as client:
        yield client, store, tool, owner, buyer, actor


def block_relationship(store, owner, buyer, direction):
    if direction in {"buyer", "both"}:
        store.blocks.add((buyer["id"], owner["username"]))
    if direction in {"seller", "both"}:
        store.blocks.add((owner["id"], buyer["username"]))


@pytest.mark.parametrize("direction", ["buyer", "seller", "both"])
def test_blocked_transfer_has_no_dead_submit_and_direct_post_stays_forbidden(
    transfer_page, direction,
):
    client, store, tool, owner, buyer, _ = transfer_page
    block_relationship(store, owner, buyer, direction)
    path = f'/tools/{tool["slug"]}/transfer'
    before = (len(store.transfer_inquiries), len(store.conversations),
              len(store.notifications))
    response = client.get(path)
    assert response.status_code == 200
    actions = Actions(response.text)
    assert actions.inquiry_forms == 0
    assert "この販売者とは現在取引できません" in response.text
    assert "/settings/blocks" in actions.links
    assert "独占譲渡の相談を送る" not in response.text
    assert client.post(path, data=INQUIRY, follow_redirects=False).status_code == 403
    assert before == (len(store.transfer_inquiries), len(store.conversations),
                      len(store.notifications))


def test_unblocked_transfer_still_accepts_an_inquiry(transfer_page):
    client, store, tool, _, _, _ = transfer_page
    path = f'/tools/{tool["slug"]}/transfer'
    response = client.get(path)
    assert response.status_code == 200
    assert Actions(response.text).inquiry_forms == 1
    assert "独占譲渡の相談を送る" in response.text
    before = len(store.transfer_inquiries)
    created = client.post(path, data=INQUIRY, follow_redirects=False)
    assert created.status_code == 303
    assert created.headers["location"].startswith("/transfer-inquiries/")
    assert len(store.transfer_inquiries) == before + 1


@pytest.mark.parametrize("viewer", ["owner", "anonymous"])
def test_owner_and_anonymous_keep_their_existing_navigation(transfer_page, viewer):
    client, _, tool, owner, _, actor = transfer_page
    actor["user"] = owner if viewer == "owner" else None
    path = f'/tools/{tool["slug"]}/transfer'
    response = client.get(path)
    assert response.status_code == 200
    actions = Actions(response.text)
    assert actions.inquiry_forms == 0
    assert "この販売者とは現在取引できません" not in response.text
    if viewer == "owner":
        assert f'/seller/tools/{tool["slug"]}/edit' in actions.links
        assert "あなたの出品です" in response.text
    else:
        assert f"/login?next={path}" in actions.links
        assert f"/signup?next={path}" in actions.links


@pytest.mark.parametrize("direction", ["buyer", "seller"])
def test_existing_inquiry_navigation_takes_priority_after_a_block(
    transfer_page, direction,
):
    client, store, tool, owner, buyer, _ = transfer_page
    inquiry, _ = store.create_transfer_inquiry(
        buyer, tool["slug"], INQUIRY["offer_amount"],
        INQUIRY["intended_use"], INQUIRY["message"],
    )
    block_relationship(store, owner, buyer, direction)
    response = client.get(f'/tools/{tool["slug"]}/transfer')
    assert response.status_code == 200
    actions = Actions(response.text)
    assert actions.inquiry_forms == 0
    assert f'/transfer-inquiries/{inquiry["id"]}' in actions.links
    assert "相談を受付済みです" in response.text
    assert "この販売者とは現在取引できません" not in response.text


@pytest.mark.parametrize("rating, label", [(None, "新着"), (4.7, "4.7")])
def test_transfer_rating_matches_existing_product_card_fallback(
    transfer_page, rating, label,
):
    client, _, tool, _, _, _ = transfer_page
    tool["rating"] = rating
    response = client.get(f'/tools/{tool["slug"]}/transfer')
    assert response.status_code == 200
    assert f"<span>★ {label}</span>" in response.text
    assert "★ None" not in response.text
