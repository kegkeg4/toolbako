from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient


@pytest.mark.parametrize("endpoint", ["/security/mfa", "/security/mfa/enroll", "/security/mfa/verify"])
@pytest.mark.parametrize("destination,expected", [("/seller?tab=tools", "/seller?tab=tools"), ("//evil.example", "/")])
def test_expired_provider_session_returns_to_login_with_safe_mfa_destination(monkeypatch, endpoint, destination, expected):
    from app.main import app, mfa, settings

    async def expired(*args):
        raise HTTPException(401, "expired")

    monkeypatch.setattr(mfa, "factors", expired)
    monkeypatch.setattr(mfa, "enroll", expired)
    monkeypatch.setattr(mfa, "verify", expired)
    client = TestClient(app)
    client.get("/auth/demo", follow_redirects=False)
    monkeypatch.setattr(type(settings), "supabase_ready", property(lambda self: True))
    if endpoint == "/security/mfa":
        response = client.get(endpoint, params={"next": destination}, follow_redirects=False)
    else:
        response = client.post(endpoint, data={"next": destination, "factor_id": "unused", "code": "123456"}, follow_redirects=False)
    assert response.status_code == 303
    location = urlsplit(response.headers["location"])
    assert location.path == "/login" and not location.netloc
    query = parse_qs(location.query)
    assert query["reauth"] == ["1"]
    mfa_return = urlsplit(query["next"][0])
    assert mfa_return.path == "/security/mfa" and not mfa_return.netloc
    assert parse_qs(mfa_return.query)["next"] == [expected]
    login = client.get(response.headers["location"])
    assert login.status_code == 200
    assert "安全のため、もう一度ログインしてください" in login.text


def test_session_expiring_while_rendering_wrong_code_reauthenticates(monkeypatch):
    from app.main import app, mfa

    async def wrong_code(*args):
        raise HTTPException(400, "認証コードを確認してください")

    async def expired(*args):
        raise HTTPException(401, "expired")

    monkeypatch.setattr(mfa, "verify", wrong_code)
    monkeypatch.setattr(mfa, "factors", expired)
    client = TestClient(app)
    client.get("/auth/demo", follow_redirects=False)
    response = client.post("/security/mfa/verify", data={"next": "/seller", "factor_id": "unused", "code": "123456"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?reauth=1&")


def test_logged_out_mfa_keeps_seller_destination():
    from app.main import app
    response = TestClient(app).get("/security/mfa?next=%2Fseller", follow_redirects=False)
    assert response.status_code == 303
    mfa_return = parse_qs(urlsplit(response.headers["location"]).query)["next"][0]
    assert parse_qs(urlsplit(mfa_return).query)["next"] == ["/seller"]
