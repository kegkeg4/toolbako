"""Configuration evidence must never turn a plan into live readiness."""
import json

import pytest

from app.live_setup_plan import (
    build_live_setup_plan, inspect_endpoint_configuration, SetupPlanError,
)


BASE = "https://toolbako.example"
ACCOUNT = "acct_synthetic"


def plan():
    return build_live_setup_plan(BASE, ACCOUNT)


def evidence(kind):
    expected = plan()["endpoints"][kind]
    return {"id": "we_synthetic", "object": "webhook_endpoint", "livemode": True,
            "status": "enabled", "url": expected["url"],
            "api_version": expected["api_version"],
            "enabled_events": list(expected["enabled_events"])}


def inspect(kind, endpoint, **overrides):
    context = {"observed_platform_account": ACCOUNT,
               "observed_scope": plan()["endpoints"][kind]["scope"]}
    context.update(overrides)
    return inspect_endpoint_configuration(plan(), kind, endpoint=endpoint, **context)


def test_future_receivers_cannot_reuse_active_test_route_or_secret():
    packet = plan()
    endpoints = packet["endpoints"]
    assert endpoints["platform"]["url"] == BASE + "/webhooks/stripe/live"
    assert endpoints["connect"]["url"] == BASE + "/webhooks/stripe/connect/live"
    assert endpoints["platform"]["pending_secret_variable"] == "STRIPE_LIVE_PENDING_WEBHOOK_SECRET"
    assert endpoints["connect"]["pending_secret_variable"] == "STRIPE_LIVE_PENDING_CONNECT_WEBHOOK_SECRET"
    assert not packet["receiver_deployed"] and not packet["webhooks_connected"]
    assert not packet["payments_activated"]
    assert packet["event_payload"] == "snapshot"
    assert packet["account_api"] == "v1" and packet["charge_mode"] == "separate"
    for expected in endpoints.values():
        assert "*" not in expected["enabled_events"]
        assert not any(e.startswith(("identity.", "invoice.", "customer.subscription."))
                       for e in expected["enabled_events"])


@pytest.mark.parametrize("kind", ["platform", "connect"])
def test_matching_readback_proves_only_configuration(kind):
    endpoint = evidence(kind)
    endpoint["enabled_events"].reverse()
    endpoint["secret"] = "whsec_doNotExpose"
    result = inspect(kind, endpoint)
    assert result["configuration_matches"]
    assert not result["delivery_verified"] and not result["ledger_isolation_verified"]
    assert not result["payments_activated"]
    assert "whsec_doNotExpose" not in json.dumps(result)
    assert "we_synthetic" not in json.dumps(result)


@pytest.mark.parametrize(("field", "value", "issue"), [
    ("url", BASE + "/webhooks/stripe", "receiver_url_mismatch"),
    ("url", BASE + "/webhooks/stripe/connect/live", "receiver_url_mismatch"),
    ("url", BASE + "/webhooks/stripe/live/", "receiver_url_mismatch"),
    ("livemode", False, "live_mode_unverified"),
    ("livemode", 1, "live_mode_unverified"),
    ("livemode", None, "live_mode_unverified"),
    ("status", "disabled", "endpoint_not_enabled"),
    ("api_version", None, "api_version_mismatch"),
    ("api_version", "2026-10-28.endive", "api_version_mismatch"),
    ("enabled_events", ["*"], "event_selection_mismatch"),
    ("enabled_events", ["account.updated"], "event_selection_mismatch"),
    ("enabled_events", None, "event_selection_mismatch"),
    ("enabled_events", [{}], "event_selection_mismatch"),
    ("id", "acct_other", "invalid_endpoint"),
    ("object", "event_destination", "invalid_endpoint"),
])
def test_wrong_mode_scope_shape_or_broad_event_selection_fails(field, value, issue):
    endpoint = evidence("platform")
    endpoint[field] = value
    result = inspect("platform", endpoint)
    assert not result["configuration_matches"]
    assert issue in result["issues"]


def test_missing_extra_and_duplicate_events_are_not_exact_configuration():
    events = evidence("platform")["enabled_events"]
    for observed in [events[:-1], events + ["identity.verification_session.verified"],
                     events + [events[0]]]:
        endpoint = evidence("platform")
        endpoint["enabled_events"] = observed
        assert inspect("platform", endpoint)["issues"] == ["event_selection_mismatch"]


@pytest.mark.parametrize(("overrides", "issue"), [
    ({"observed_platform_account": "acct_other"}, "platform_account_mismatch"),
    ({"observed_platform_account": None}, "platform_account_mismatch"),
    ({"observed_scope": "connected_accounts"}, "event_scope_mismatch"),
    ({"observed_scope": None}, "event_scope_mismatch"),
])
def test_provider_read_context_must_be_explicit(overrides, issue):
    result = inspect("platform", evidence("platform"), **overrides)
    assert not result["configuration_matches"]
    assert result["issues"] == [issue]


@pytest.mark.parametrize("base", [None, "http://toolbako.example", "https://localhost",
    "https://127.0.0.1", "https://user:credential@toolbako.example",
    "https://toolbako.example?secret=value", "https://toolbako.example#fragment",
    "https://toolbako.example/path", "https://toolbako.example:444",
    "https://toolbako.example:bad", "https://toolbako.example\\bad",
    "https://toolbako.example\n"])
def test_bad_base_rejected_without_echo(base):
    with pytest.raises(SetupPlanError, match="^invalid_live_setup_plan_input$"):
        build_live_setup_plan(base, ACCOUNT)


@pytest.mark.parametrize("account", [None, "", "acct_", "acct_test-other", "rk_live_secret"])
def test_bad_platform_rejected_without_echo(account):
    with pytest.raises(SetupPlanError, match="^invalid_live_setup_plan_input$"):
        build_live_setup_plan(BASE, account)


def test_invalid_readback_fails_closed():
    assert inspect("connect", None)["issues"] == ["invalid_endpoint"]
    with pytest.raises(SetupPlanError):
        inspect_endpoint_configuration(plan(), "other", observed_platform_account=ACCOUNT,
                                       observed_scope="your_account", endpoint={})
