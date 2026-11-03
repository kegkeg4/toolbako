"""Offline mode/flag boundaries; no database/provider access or process launch."""
import asyncio

import httpx
import pytest

from app.config import Settings, _strict_operation_flag
from app import deployment_check as checks
from app.integrations import StripeIntegration
from app.live_runtime import RuntimeLaunchError, prepare_runtime_launch
from app.live_setup_plan import STRIPE_API_VERSION, WEBHOOK_API_VERSIONS
from app.production import readiness_summary
from test_live_connection_stage import stage, Backend
from test_live_runtime import fixture_env


def financial(**changes):
    return stage(**{"stripe_connection_mode": "live_financial",
        "stripe_api_version": STRIPE_API_VERSION,
        "stripe_webhook_secret": "whsec_platform", "stripe_connect_webhook_secret": "whsec_connect",
        **changes})


@pytest.mark.parametrize("refunds,payouts", [(False, False), (True, False), (False, True), (True, True)])
@pytest.mark.parametrize("version", sorted(WEBHOOK_API_VERSIONS))
def test_financial_mode_keeps_independent_flags_checkout_and_readiness_off(refunds, payouts, version):
    settings = financial(stripe_live_refunds_enabled=refunds, stripe_live_payouts_enabled=payouts,
                         stripe_webhook_api_version=version)
    assert all(checks.configuration_checks(settings).values())
    assert settings.stripe_ready is False
    assert not next(c for c in readiness_summary(settings)["checks"] if c["key"] == "payments")["ok"]


@pytest.mark.parametrize("name", ["stripe_live_refunds_enabled", "stripe_live_payouts_enabled"])
@pytest.mark.parametrize("value", [0, 1, None, "false", "true", "", [], {}])
def test_direct_flag_constructor_rejects_truthy_and_falsy_non_booleans(name, value):
    with pytest.raises(ValueError, match="Invalid financial operation boolean configuration"):
        Settings(**{name: value})


@pytest.mark.parametrize("name", ["STRIPE_LIVE_REFUNDS_ENABLED", "STRIPE_LIVE_PAYOUTS_ENABLED"])
@pytest.mark.parametrize("value", ["TRUE", "False", "1", "0", "yes", "", " true"])
def test_env_flag_is_exact_not_truthiness(name, value, monkeypatch):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError) as error: _strict_operation_flag(name)
    assert value not in str(error.value) or value == ""


def test_absent_flags_default_to_false(monkeypatch):
    for name in ("STRIPE_LIVE_REFUNDS_ENABLED", "STRIPE_LIVE_PAYOUTS_ENABLED"):
        monkeypatch.delenv(name, raising=False)
        assert _strict_operation_flag(name) is False


@pytest.mark.parametrize("changes", [
    {"stripe_webhook_secret": ""}, {"stripe_connect_webhook_secret": "whsec_platform"},
    {"stripe_api_version": "2026-06-24.dahlia"}, {"stripe_connect_accounts_v2": True},
    {"stripe_webhook_api_version": "2026-01-01.unsupported"},
    {"stripe_platform_account_id": "acct_wrong"}, {"stripe_ledger_id": ""},
    {"stripe_secret_key": "rk_test_synthetic"}, {"stripe_secret_key": "sk_live_synthetic"},
])
def test_financial_configuration_still_requires_target_scope_and_notifications(changes, monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: pytest.fail("Invalid config must not connect"))
    assert not all(asyncio.run(checks.connection_checks(financial(**changes))).values())


@pytest.mark.parametrize("mode", ["live_read_only", "live_setup_only", "sandbox"])
@pytest.mark.parametrize("name", ["stripe_live_refunds_enabled", "stripe_live_payouts_enabled"])
def test_other_modes_cannot_inherit_financial_flags(mode, name):
    settings = financial(stripe_connection_mode=mode, stripe_live_seller_setup_enabled=mode == "live_setup_only",
                         **{name: True})
    assert not all(checks.configuration_checks(settings).values())


@pytest.mark.parametrize("binding", ["correct", "absent", "other_ledger"])
def test_financial_operational_probe_requires_fresh_binding_without_provider(binding, monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: pytest.fail("Liveness never calls provider"))
    backend = Backend()
    if binding == "absent": backend.conn.row = backend.conn.row[:2] + (None, None, None, None)
    if binding == "other_ledger":
        row = list(backend.conn.row)
        row[-1] = {**row[-1], "ledger_id": "6317da82-f331-4f3b-9aa6-b01ae3b3449e"}
        backend.conn.row = tuple(row)
    assert asyncio.run(checks.deployment_database_check(financial(), backend)) is (binding == "correct")


@pytest.mark.parametrize("refunds,payouts", [("false", "false"), ("true", "false"), ("false", "true"), ("true", "true")])
@pytest.mark.parametrize("version", sorted(WEBHOOK_API_VERSIONS))
def test_isolated_projection_preserves_exact_flags_and_separate_webhook_version(refunds, payouts, version, monkeypatch):
    captured = []
    monkeypatch.setattr('os.execve', lambda exe, argv, env: captured.append(env))
    env = fixture_env(STRIPE_CONNECTION_MODE="live_financial", STRIPE_LIVE_REFUNDS_ENABLED=refunds,
        STRIPE_LIVE_PAYOUTS_ENABLED=payouts, STRIPE_LIVE_PENDING_WEBHOOK_SECRET="whsec_platform",
        STRIPE_LIVE_PENDING_CONNECT_WEBHOOK_SECRET="whsec_connect", STRIPE_WEBHOOK_API_VERSION=version,
        STRIPE_API_VERSION=STRIPE_API_VERSION)
    prepare_runtime_launch("preflight", env).execute()
    child = captured[0]
    assert child["STRIPE_WEBHOOK_API_VERSION"] == version and child["STRIPE_API_VERSION"] == STRIPE_API_VERSION
    assert child["STRIPE_LIVE_REFUNDS_ENABLED"] == refunds and child["STRIPE_LIVE_PAYOUTS_ENABLED"] == payouts
    assert child["STRIPE_SECRET_KEY"] == "rk_live_synthetic"


@pytest.mark.parametrize("change", [
    {"STRIPE_LIVE_REFUNDS_ENABLED": "1"}, {"STRIPE_LIVE_PAYOUTS_ENABLED": "TRUE"},
    {"STRIPE_LIVE_SELLER_SETUP_ENABLED": "yes"}, {"STRIPE_WEBHOOK_API_VERSION": "unknown"},
    {"STRIPE_API_VERSION": "2026-06-24.dahlia"}, {"STRIPE_LIVE_PENDING_WEBHOOK_SECRET": ""},
    {"STRIPE_LIVE_PENDING_CONNECT_WEBHOOK_SECRET": "whsec_platform"},
])
def test_isolated_financial_launch_fails_closed_on_ambiguous_configuration(change):
    env = fixture_env(**{"STRIPE_CONNECTION_MODE": "live_financial", "STRIPE_LIVE_PENDING_WEBHOOK_SECRET": "whsec_platform",
                        "STRIPE_LIVE_PENDING_CONNECT_WEBHOOK_SECRET": "whsec_connect", **change})
    with pytest.raises(RuntimeLaunchError): prepare_runtime_launch("preflight", env)


def test_independent_financial_flags_never_enable_generic_live_post(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: pytest.fail("No generic live POST"))
    settings = financial(stripe_live_refunds_enabled=True, stripe_live_payouts_enabled=True)
    provider = StripeIntegration(settings.stripe_secret_key, settings.site_base_url, charge_mode="separate")
    for endpoint in ("checkout/sessions", "refunds", "transfers", "payouts", "transfers/tr_fixture/reversals"):
        with pytest.raises(RuntimeError):
            asyncio.run(provider._post(endpoint, {"_idempotency_key": "synthetic"}))


@pytest.mark.parametrize("mode", ["live_read_only", "live_setup_only", "live_financial"])
def test_separate_dahlia_receiver_never_changes_outbound_clover(mode):
    settings = financial(stripe_connection_mode=mode, stripe_live_seller_setup_enabled=mode == "live_setup_only",
                         stripe_webhook_api_version="2026-06-24.dahlia", stripe_api_version="2026-06-24.dahlia")
    assert not all(checks.configuration_checks(settings).values())
    env = fixture_env(STRIPE_CONNECTION_MODE=mode, STRIPE_LIVE_SELLER_SETUP_ENABLED="true" if mode == "live_setup_only" else "false",
        STRIPE_API_VERSION="2026-06-24.dahlia", STRIPE_WEBHOOK_API_VERSION="2026-06-24.dahlia",
        STRIPE_LIVE_PENDING_WEBHOOK_SECRET="whsec_platform", STRIPE_LIVE_PENDING_CONNECT_WEBHOOK_SECRET="whsec_connect")
    with pytest.raises(RuntimeLaunchError): prepare_runtime_launch("preflight", env)
