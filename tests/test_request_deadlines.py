"""Expired recruitment remains readable but cannot accept new applications."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.data import DemoStore
import app.main as main

JST = timezone(timedelta(hours=9))


@pytest.fixture
def recruitment(monkeypatch):
    store = DemoStore(seed=False)
    owner = {"id":"request-owner", "username":"request_owner", "display_name":"Owner"}
    applicant = {"id":"request-applicant", "username":"request_applicant", "display_name":"Applicant"}
    store.registered_users = {u["username"]:dict(u) for u in (owner, applicant)}
    item = store.create_request(owner, {"title":"Expired recruitment sentinel", "category":"開発者ツール",
        "detail":"Synthetic recruitment details", "budget_min":1000, "budget_max":5000,
        "deadline":(datetime.now(JST).date()-timedelta(days=1)).isoformat()})
    actor = {"user":applicant}
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "database_store", None)
    monkeypatch.setattr(main, "current_user", lambda request: actor["user"])
    with TestClient(main.app) as client:
        yield client, store, actor, owner, applicant, item


def payload():
    return {"message":"対応方法を具体的にご説明いたします。", "amount":3000, "delivery_days":7}


def test_expired_request_is_not_advertised_as_open(recruitment):
    client, store, actor, owner, applicant, item = recruitment
    assert item["title"] not in client.get("/requests").text
    detail = client.get(f"/requests/{item['id']}")
    assert detail.status_code == 200
    assert item["title"] in detail.text
    assert "募集終了" in detail.text
    assert f'action="/requests/{item["id"]}/apply"' not in detail.text


def test_expired_application_post_does_not_mutate_or_notify(recruitment):
    client, store, actor, owner, applicant, item = recruitment
    response = client.post(f"/requests/{item['id']}/apply", data=payload(), follow_redirects=False)
    assert response.status_code == 403
    assert item["application_list"] == [] and item["applications"] == 0
    assert store.notifications == []


def test_expired_request_store_boundary_rejects_direct_application(recruitment):
    client, store, actor, owner, applicant, item = recruitment
    with pytest.raises(ValueError):
        store.submit_application(item["id"], applicant, "valid synthetic message", 3000, 7)
    assert item["application_list"] == []


def test_expired_request_keeps_existing_application_visible_to_owner(recruitment):
    client, store, actor, owner, applicant, item = recruitment
    item["application_list"].append({"id":"existing-application", "applicant_id":applicant["id"],
        "applicant_name":applicant["display_name"], "message":"PRIOR_APPLICATION_SENTINEL", "amount":3000,
        "delivery_days":7, "status":"submitted"})
    item["applications"] = 1
    actor["user"] = owner
    response = client.get(f"/requests/{item['id']}")
    assert response.status_code == 200 and "PRIOR_APPLICATION_SENTINEL" in response.text
    assert item["status"] == "open"  # Display rule does not rewrite business history.


def test_current_jst_day_accepts_application_and_html_matches_server_minimum(recruitment):
    client, store, actor, owner, applicant, item = recruitment
    item["deadline"] = datetime.now(JST).date().isoformat()
    page = client.get(f"/requests/{item['id']}")
    assert 'name="message" minlength="10"' in page.text
    assert "日本時間" in page.text
    assert client.post(f"/requests/{item['id']}/apply", data=payload(), follow_redirects=False).status_code == 303
    assert item["applications"] == 1


@pytest.mark.parametrize("at, expected", [
    (datetime(2026, 11, 3, 14, 59, 59, tzinfo=timezone.utc), True),
    (datetime(2026, 11, 3, 15, 0, 0, tzinfo=timezone.utc), False),
])
def test_deadline_boundary_is_jst_end_of_calendar_day(at, expected):
    from app.request_deadlines import accepting_applications
    assert accepting_applications({"status":"open", "deadline":"2026-11-03"}, at=at) is expected


@pytest.mark.parametrize("deadline", [None, "", "2026-02-30", "20261103", "unknown", 20261103])
def test_missing_or_malformed_deadline_never_opens_applications(deadline):
    from app.request_deadlines import accepting_applications
    assert not accepting_applications({"status":"open", "deadline":deadline})


def test_creation_uses_same_jst_day_as_expiry(recruitment, monkeypatch):
    from app.request_deadlines import request_today
    client, store, actor, owner, applicant, item = recruitment
    # UTC is still Nov 3 while Japan already has Nov 4.
    today = request_today(datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(main, "request_today", lambda: today)
    data = {"title":"Date boundary request", "category":"開発者ツール", "detail":"Synthetic date boundary check",
            "budget_min":1000, "budget_max":5000, "terms_agreement":"yes", "deadline":"2026-11-03"}
    assert client.post("/requests/new", data=data, follow_redirects=False).status_code == 422
    data["deadline"] = "2026-11-04"
    assert client.post("/requests/new", data=data, follow_redirects=False).status_code == 303
