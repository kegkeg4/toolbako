"""Setup permission and checkout isolation, with synthetic provider responses."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import deployment_check as checks
from app.integrations import StripeIntegration
from test_live_connection_stage import stage, Backend


def setup(**changes):
    return stage(**{"stripe_connection_mode": "live_setup_only", "stripe_live_seller_setup_enabled": True,
                    "stripe_webhook_secret": "whsec_platform", "stripe_connect_webhook_secret": "whsec_connected", **changes})


@pytest.mark.parametrize("mode,flag,ok", [
    ("live_setup_only", True, True), ("live_setup_only", False, False),
    ("live_read_only", True, False), ("live_read_only", False, True),
    ("sandbox", True, False), ("LIVE_SETUP_ONLY", True, False),
    ("live_setup_only ", True, False), ("live_setup_only", 1, False),
    ("live_setup_only", "true", False),
])
def test_two_settings_must_match_without_widening_read_only(mode, flag, ok):
    config = setup(stripe_connection_mode=mode, stripe_live_seller_setup_enabled=flag)
    assert all(checks.configuration_checks(config).values()) is ok
    assert config.stripe_ready is False


@pytest.mark.parametrize("changes", [
    {"stripe_webhook_secret": ""}, {"stripe_connect_webhook_secret": ""},
    {"stripe_webhook_secret": "whsec_connected"}, {"stripe_api_version": "unreviewed"},
    {"stripe_connect_accounts_v2": True},
])
def test_setup_requires_both_distinct_notification_signatures_and_reviewed_version(changes):
    assert not all(checks.configuration_checks(setup(**changes)).values())


@pytest.mark.parametrize("fault", ["unbound", "wrong_target", "no_tls", None])
def test_setup_health_still_requires_actual_tls_target_binding(fault):
    backend = Backend()
    if fault == "unbound": backend.conn.row = (*backend.conn.row[:2], None, None, None, None)
    if fault == "wrong_target": backend.conn.info.host = "wrong.example"
    if fault == "no_tls": backend.conn.pgconn.ssl_in_use = False
    assert asyncio.run(checks.deployment_database_check(setup(), backend)) is (fault is None)


@pytest.mark.parametrize("path", ["checkout/sessions", "refunds", "transfers", "payouts", "accounts", "account_links"])
def test_setup_flag_cannot_open_generic_stripe_post(path, monkeypatch):
    import httpx
    client = StripeIntegration("rk_live_synthetic", "https://toolbako.example")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: pytest.fail("Blocked before provider I/O"))
    with pytest.raises(RuntimeError):
        asyncio.run(client._post(path, {"_idempotency_key": "synthetic"}))


@pytest.mark.parametrize("mode", ["live_read_only", "live_setup_only"])
def test_checkout_cannot_reserve_or_reuse_cached_url_in_connection_stages(mode, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, "settings", stage(stripe_connection_mode=mode,
                                              stripe_live_seller_setup_enabled=mode == "live_setup_only"))
    monkeypatch.setattr(main, "current_user", lambda request: {"id": "buyer"})
    monkeypatch.setattr(main.store, "get", lambda slug: {"author_id": "seller"})
    monkeypatch.setattr(main.store, "buy", lambda *a, **k: pytest.fail("No reservation or cached handoff lookup"))
    request = Request({"type": "http", "headers": []})
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.checkout_complete(request, "tool", [], "", "one_time", "yes"))
    assert error.value.status_code == 503


def test_setup_page_does_not_claim_sales_enabled(monkeypatch):
    import app.main as main
    import app.live_seller_onboarding as onboarding
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main, "settings", setup())
    monkeypatch.setattr(main, "current_user", lambda request: {"id": "seller", "username": "seller"})
    monkeypatch.setattr(main.store, "connected_accounts", {})
    async def ready(*args, **kwargs):
        return SimpleNamespace(action="account_ready", account_id="acct_existing",
            account_status=(("charges_enabled", True), ("payouts_enabled", True), ("details_submitted", True)))
    monkeypatch.setattr(onboarding, "prepare_live_seller_onboarding", ready)
    response = TestClient(main.app).get("/seller/payments?returned=1")
    assert response.status_code == 200
    assert "決済開始前" in response.text and ">販売可能<" not in response.text
    assert 'action="/seller/payments/onboard"' in response.text
    assert main.store.connected_accounts == {}  # This GET cannot persist/adopt a mapping.


@pytest.mark.parametrize('account_action,link_action,expected', [
    ('outcome_unknown', None, 503), ('operator_review_required', None, 503),
    ('account_mapped', 'link_outcome_unknown', 503), ('account_mapped', 'link_unavailable', 409),
    ('account_ready', 'account_ready', 303), ('account_mapped', 'link_created', 200),
])
def test_route_preserves_executor_outcomes_and_safe_handoff(account_action, link_action, expected, monkeypatch):
    import app.main as main
    import app.live_seller_execution as execution
    calls = []
    monkeypatch.setattr(main, 'settings', setup())
    monkeypatch.setattr(main, 'current_user', lambda request: {'id':'seller', 'username':'seller'})
    async def account(*args, **kwargs):
        calls.append('account')
        assert kwargs == {'seller_id':'seller'}
        return execution.SetupResult(account_action, 'acct_synthetic')
    async def link(*args, **kwargs):
        calls.append('link')
        assert link_action is not None
        import time
        return execution.SetupResult(link_action, 'acct_synthetic', handoff=(
            execution.AccountLinkHandoff('https://connect.stripe.com/setup/synthetic', time.time()+300)
            if link_action == 'link_created' else None))
    monkeypatch.setattr(execution, 'execute_live_seller_account', account)
    monkeypatch.setattr(execution, 'execute_live_seller_account_link', link)
    request = Request({'type':'http','method':'POST','path':'/seller/payments/onboard','headers':[],
                       'session':{},'scheme':'https','server':('testserver',443),'query_string':b''})
    if expected >= 400:
        with pytest.raises(HTTPException) as error: asyncio.run(main.seller_payment_onboard(request))
        assert error.value.status_code == expected
    else:
        response = asyncio.run(main.seller_payment_onboard(request))
        assert response.status_code == expected and 'no-store' in response.headers['cache-control']
        if expected == 303: assert response.headers['location'] == '/seller/payments?connected=1'
        else:
            assert response.headers['referrer-policy'] == 'no-referrer'
            assert b'https://connect.stripe.com/setup/synthetic' in response.body
    assert calls == (['account'] if link_action is None else ['account','link'])
