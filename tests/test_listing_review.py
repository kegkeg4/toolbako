from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from app import main
from app.data import DemoStore
from app.listing_review import REVIEW_CHECKS, review_token


@pytest.fixture
def review_setup(monkeypatch):
    store = DemoStore()
    tool = store.tools[0]
    tool.update(status="draft", is_published=False, safety_scan={"status": "pending", "label": "安全チェック待ち"})
    actor = {"user": {"id": "review-admin", "username": "demo_creator", "display_name": "審査担当"}}
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "current_user", lambda request: actor["user"])
    return TestClient(main.app), store, tool, actor


def decision(tool, **overrides):
    return {"token": review_token(tool), "checks": list(REVIEW_CHECKS), "note": "掲載内容と販売条件の説明を確認しました。", **overrides}


def save_payload(tool, **overrides):
    return {"name": tool["name"], "tagline": tool["tagline"], "description_md": tool["description_md"],
            "price": tool["price"], "capacity": tool["capacity"], "support_days": tool["support_days"],
            "estimated_delivery_days": tool.get("estimated_delivery_days", 1),
            "fulfillment_type": tool.get("fulfillment_type", "custom"), "purchase_notes": tool.get("purchase_notes", ""),
            "faq_question": [x["question"] for x in tool.get("faq", [])], "faq_answer": [x["answer"] for x in tool.get("faq", [])], **overrides}


def seller(actor, tool):
    actor["user"] = {"id": tool["author_id"], "username": tool["author_username"], "display_name": tool["author_name"]}


def test_manual_review_requires_evidence_and_records_actor_without_auto_publish(review_setup):
    client, store, tool, actor = review_setup
    path = f"/admin/tools/{tool['slug']}/safety/passed"
    original = deepcopy(tool)
    for data in ({}, decision(tool, checks=[]), decision(tool, note="短い"), decision(tool, token="あ")):
        assert client.post(path, data=data, follow_redirects=False).status_code in {409, 422}
        assert tool == original
    page = client.get(f"/admin/tools/{tool['slug']}/review")
    assert page.status_code == 200 and tool["description_md"] in page.text
    assert 'AIによるコード解析' in page.text
    assert client.post(path, data=decision(tool), follow_redirects=False).status_code == 303
    assert tool["safety_scan"]["method"] == "manual"
    assert tool["safety_scan"]["reviewer_id"] == "review-admin"
    assert tool["safety_scan"]["checks"] == list(REVIEW_CHECKS)
    assert tool["passport"]["security"] == "掲載審査承認済み"
    assert not tool["is_published"]
    assert store.notifications[0]["user_id"] == tool["author_id"]
    seller(actor, tool)
    assert client.post(f"/seller/tools/{tool['slug']}/status/published", follow_redirects=False).status_code == 303
    assert tool["is_published"]


def test_stale_content_or_prior_decision_cannot_be_approved(review_setup):
    client, _, tool, _ = review_setup
    path = f"/admin/tools/{tool['slug']}/safety/passed"
    old = decision(tool)
    tool["description_md"] += "変更"
    assert client.post(path, data=old, follow_redirects=False).status_code == 409
    current = decision(tool)
    assert client.post(path, data=current, follow_redirects=False).status_code == 303
    assert client.post(path, data=current, follow_redirects=False).status_code == 409


def test_private_review_is_admin_only_and_resubmission_owner_only(review_setup):
    client, _, tool, actor = review_setup
    actor["user"] = None
    assert client.get(f"/admin/tools/{tool['slug']}/review").status_code == 403
    seller(actor, tool)
    assert client.get(f"/admin/tools/{tool['slug']}/review").status_code == 403
    assert client.post(f"/admin/tools/{tool['slug']}/safety/passed", data=decision(tool)).status_code == 403
    actor["user"] = {"id": "other-seller", "username": "other", "display_name": "他の販売者"}
    assert client.post(f"/seller/tools/{tool['slug']}/review").status_code == 403


def test_rejection_and_resubmission_are_visible_and_remain_private(review_setup):
    client, _, tool, actor = review_setup
    note = "外部サービスに送るデータの説明を追記してください。<script>alert(1)</script>"
    assert client.post(f"/admin/tools/{tool['slug']}/safety/failed", data=decision(tool, note=note), follow_redirects=False).status_code == 303
    seller(actor, tool)
    page = client.get(f"/seller/tools/{tool['slug']}/edit")
    assert '再審査を申請' in page.text and '&lt;script&gt;' in page.text
    assert '<script>alert(1)</script>' not in page.text
    assert client.post(f"/seller/tools/{tool['slug']}/review", follow_redirects=False).status_code == 303
    assert tool["safety_scan"]["status"] == "pending" and not tool["is_published"]
    assert tool["safety_scan"]["previous_note"] == note
    token = review_token(tool)
    client.post(f"/seller/tools/{tool['slug']}/review", follow_redirects=False)
    assert review_token(tool) == token
    assert client.post(f"/seller/tools/{tool['slug']}/status/published", follow_redirects=False).status_code == 409


def test_content_change_reopens_review_but_capacity_change_does_not(review_setup):
    client, _, tool, actor = review_setup
    assert client.post(f"/admin/tools/{tool['slug']}/safety/passed", data=decision(tool), follow_redirects=False).status_code == 303
    seller(actor, tool)
    client.post(f"/seller/tools/{tool['slug']}/status/published", follow_redirects=False)
    path = f"/seller/tools/{tool['slug']}/edit"
    assert client.post(path, data=save_payload(tool, capacity=20), follow_redirects=False).status_code == 303
    assert tool["safety_scan"]["status"] == "passed" and tool["is_published"]
    assert client.post(path, data=save_payload(tool, description_md="新しい動作をする商品説明です。"), follow_redirects=False).status_code == 303
    assert tool["safety_scan"]["status"] == "pending" and tool["status"] == "paused"
    assert not tool["is_published"]
    assert 'AI安全チェック' not in client.get(path).text
