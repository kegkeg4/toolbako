import asyncio
import json

import httpx
import pytest

from app.stripe_live_connection_check import inspect_connection, LiveConnectionUnverified, main


KEY = "rk_live_syntheticOnly"
ACCOUNT = "acct_synthetic"


def account():
    return {"object": "account", "id": ACCOUNT, "charges_enabled": True,
            "payouts_enabled": True, "email": "must-not-print@example.invalid",
            "capabilities": {"card_payments": "active", "transfers": "active"}}


def transport_for(a=None, b=None, status=200):
    calls = []
    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.url.host == "api.stripe.com"
        assert "stripe-account" not in request.headers
        assert request.headers["authorization"] == f"Bearer {KEY}"
        assert request.url.path in {"/v1/account", "/v1/balance"}
        payload = (account() if a is None else a) if request.url.path == "/v1/account" else (
            {"object": "balance", "livemode": True, "available": [{"amount": 987654321}]} if b is None else b)
        return httpx.Response(status, json=payload)
    return httpx.MockTransport(handler), calls


def test_read_only_connection_is_not_activation_or_write_permission_proof():
    transport, calls = transport_for()
    result = asyncio.run(inspect_connection(KEY, ACCOUNT, transport=transport))
    assert result["connection_verified"] and result["platform_matches"]
    assert result["charges_enabled"] and result["payouts_enabled"]
    assert not result["payments_activated"] and not result["write_permissions_verified"]
    assert not result["ledger_binding_verified"] and not result["webhooks_verified"]
    assert [r.url.path for r in calls] == ["/v1/account", "/v1/balance"]
    assert KEY not in json.dumps(result)
    assert "must-not-print" not in json.dumps(result)
    assert "987654321" not in json.dumps(result)


@pytest.mark.parametrize("key", ["", "rk_test_example", "sk_live_example", "rk_live_x\n", None])
def test_invalid_key_never_reaches_network(key):
    transport, calls = transport_for()
    with pytest.raises(LiveConnectionUnverified):
        asyncio.run(inspect_connection(key, ACCOUNT, transport=transport))
    assert calls == []


@pytest.mark.parametrize("a", [{"object": "account", "id": "acct_other"}, [], {"id": ACCOUNT}])
def test_wrong_account_stops_before_balance(a):
    transport, calls = transport_for(a=a)
    with pytest.raises(LiveConnectionUnverified):
        asyncio.run(inspect_connection(KEY, ACCOUNT, transport=transport))
    assert len(calls) == 1


@pytest.mark.parametrize("b", [{"object": "balance", "livemode": False},
                                 {"object": "balance", "livemode": 1},
                                 {"object": "balance"}, [], {"livemode": True}])
def test_mixed_mode_or_invalid_response_denied(b):
    transport, _ = transport_for(b=b)
    with pytest.raises(LiveConnectionUnverified):
        asyncio.run(inspect_connection(KEY, ACCOUNT, transport=transport))


@pytest.mark.parametrize("status", [302, 401, 403, 429, 500])
def test_provider_failures_do_not_leak_response(status):
    transport, calls = transport_for(a={"secret": KEY}, status=status)
    with pytest.raises(LiveConnectionUnverified, match="^live_connection_unverified$"):
        asyncio.run(inspect_connection(KEY, ACCOUNT, transport=transport))
    assert len(calls) == 1


def test_timeout_sanitized():
    def fail(request):
        raise httpx.ReadTimeout(KEY, request=request)
    with pytest.raises(LiveConnectionUnverified, match="^live_connection_unverified$"):
        asyncio.run(inspect_connection(KEY, ACCOUNT, transport=httpx.MockTransport(fail)))


def test_cli_never_falls_back_to_active_key(monkeypatch, capsys):
    monkeypatch.delenv("STRIPE_LIVE_PENDING_SECRET_KEY", raising=False)
    monkeypatch.delenv("STRIPE_LIVE_PENDING_ACCOUNT_ID", raising=False)
    monkeypatch.setenv("STRIPE_SECRET_KEY", KEY)
    assert main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result["connection_verified"] is False and result["payments_activated"] is False


def test_connected_but_charges_disabled_is_reported_honestly():
    a = account()
    a["charges_enabled"] = False
    a["capabilities"]["card_payments"] = "pending"
    transport, _ = transport_for(a=a)
    result = asyncio.run(inspect_connection(KEY, ACCOUNT, transport=transport))
    assert result["connection_verified"]
    assert not result["charges_enabled"] and not result["card_payments_active"]
