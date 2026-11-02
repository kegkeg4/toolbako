from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.connect_status import account_status


def remote(**changes):
    return dict(object="account", id="acct_existing", charges_enabled=True,
                payouts_enabled=True, details_submitted=True, **changes)


@pytest.mark.parametrize("field", ["charges_enabled", "payouts_enabled", "details_submitted"])
@pytest.mark.parametrize("value", [None, 0, 1, "false", "true"])
def test_status_rejects_incomplete_or_bool_like_flags(field, value):
    payload = remote()
    payload[field] = value
    with pytest.raises(RuntimeError):
        account_status(payload, "acct_existing")


@pytest.mark.parametrize("changes", [{"id": "acct_other"}, {"object": "customer"}])
def test_status_rejects_wrong_account(changes):
    payload = remote()
    payload.update(changes)
    with pytest.raises(RuntimeError):
        account_status(payload, "acct_existing")


def test_status_only_projects_non_sensitive_flags_without_mutating():
    payload = remote(email="do-not-copy@example.invalid", secret="do-not-copy")
    result = account_status(payload, "acct_existing")
    assert result == dict(charges_enabled=True, payouts_enabled=True, details_submitted=True)
    assert payload["secret"] == "do-not-copy"


@pytest.fixture
def seller_page(monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, "settings", replace(
        main.settings, demo_mode=False, stripe_secret_key="sk_test_fake",
        stripe_webhook_secret="whsec_fake", stripe_connect_webhook_secret="whsec_connect_fake",
        stripe_connect_client_id="ca_fake"))
    monkeypatch.setattr(main, "current_user", lambda request: {"id": "seller", "username": "seller"})
    mapped = {"account_id": "acct_existing", "charges_enabled": True,
              "payouts_enabled": True, "details_submitted": True}
    monkeypatch.setattr(main.store, "connected_accounts", {"seller": mapped})
    return main, mapped, TestClient(main.app)


def test_refresh_get_does_not_create_account_or_link_and_uses_post_form(seller_page, monkeypatch):
    main, mapped, client = seller_page
    async def retrieve(account):
        assert account == "acct_existing"
        return remote()
    async def no_writes(*args, **kwargs):
        raise AssertionError("GET must not create account/link")
    monkeypatch.setattr(main.stripe, "retrieve_account", retrieve)
    monkeypatch.setattr(main.stripe, "create_express_account", no_writes)
    monkeypatch.setattr(main.stripe, "create_account_link", no_writes)
    response = client.get("/seller/payments?refresh=1&account=acct_attacker")
    assert response.status_code == 200
    assert "登録リンクを更新してください" in response.text
    assert "登録を再開する" in response.text
    assert 'action="/seller/payments/onboard" method="post"' in response.text
    assert mapped["account_id"] == "acct_existing"


def test_return_does_not_claim_success_when_provider_is_unavailable(seller_page, monkeypatch):
    main, mapped, client = seller_page
    async def fail(account):
        raise RuntimeError("unavailable")
    monkeypatch.setattr(main.stripe, "retrieve_account", fail)
    response = client.get("/seller/payments?returned=1")
    assert response.status_code == 200
    assert "受取設定の最新状態を確認できませんでした" in response.text
    assert "登録・審査の完了とはなりません" in response.text
    assert '>販売可能<' not in response.text
    assert mapped["charges_enabled"] is True  # Do not erase previous provider evidence.
    assert mapped["sync_error"] is True


def test_successful_recheck_clears_stale_sync_error(seller_page, monkeypatch):
    main, mapped, client = seller_page
    mapped["sync_error"] = True
    async def retrieve(account):
        return remote()
    monkeypatch.setattr(main.stripe, "retrieve_account", retrieve)
    response = client.get("/seller/payments")
    assert response.status_code == 200
    assert "sync_error" not in mapped
    assert '>販売可能<' in response.text


def test_wrong_provider_account_never_updates_current_seller(seller_page, monkeypatch):
    main, mapped, client = seller_page
    async def retrieve(account):
        payload = remote()
        payload["id"] = "acct_other"
        return payload
    monkeypatch.setattr(main.stripe, "retrieve_account", retrieve)
    response = client.get("/seller/payments")
    assert response.status_code == 200
    assert mapped["account_id"] == "acct_existing"
    assert mapped["sync_error"] is True
    assert '>販売可能<' not in response.text
