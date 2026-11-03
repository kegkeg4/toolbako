"""Pure preparation for future live Connect webhook configuration.

This module does not read settings, credentials or the database, make requests,
or install routes. Its URLs are a plan, not evidence of a deployed receiver.
The current /webhooks/stripe route and financial dataset remain test-only.
"""
from urllib.parse import urlsplit
import re


STRIPE_API_VERSION = "2026-02-25.clover"
WEBHOOK_API_VERSIONS = frozenset({STRIPE_API_VERSION, "2026-06-24.dahlia"})
PLATFORM_EVENTS = (
    "checkout.session.completed",
    "checkout.session.async_payment_succeeded",
    "checkout.session.expired",
    "refund.updated",
    "charge.refunded",
    "charge.dispute.created",
)
CONNECT_EVENTS = (
    "account.updated",
    "payout.created",
    "payout.updated",
    "payout.paid",
    "payout.failed",
    "payout.canceled",
)


class SetupPlanError(ValueError):
    def __init__(self):
        # Never copy caller input, which might accidentally contain credentials.
        super().__init__("invalid_live_setup_plan_input")


def build_live_setup_plan(site_base_url, platform_account, *, webhook_api_version=STRIPE_API_VERSION):
    """Return a configuration packet; never claim external readiness."""
    try:
        parsed = urlsplit(site_base_url)
        valid = (
            isinstance(site_base_url, str)
            and parsed.scheme == "https"
            and parsed.hostname
            and "." in parsed.hostname
            and parsed.hostname not in {"127.0.0.1", "0.0.0.0"}
            and parsed.port in {None, 443}
            and not parsed.username and not parsed.password
            and parsed.path in {"", "/"}
            and not parsed.query and not parsed.fragment
            and not any(ord(c) <= 32 or c == "\\" for c in site_base_url)
            and isinstance(platform_account, str)
            and re.fullmatch(r"acct_[A-Za-z0-9]+", platform_account)
        )
    except (AttributeError, TypeError, ValueError):
        valid = False
    if (not valid or not isinstance(webhook_api_version, str)
            or webhook_api_version not in WEBHOOK_API_VERSIONS):
        raise SetupPlanError()
    base = site_base_url.rstrip("/")
    return {
        "platform_account": platform_account,
        "mode": "live",
        "account_api": "v1",
        "account_type": "express",
        "charge_mode": "separate",
        "event_payload": "snapshot",
        "receiver_deployed": False,
        "webhooks_connected": False,
        "payments_activated": False,
        "endpoints": {
            "platform": {
                "scope": "your_account",
                "connect": False,
                "url": base + "/webhooks/stripe/live",
                "api_version": webhook_api_version,
                "enabled_events": list(PLATFORM_EVENTS),
                "pending_secret_variable": "STRIPE_LIVE_PENDING_WEBHOOK_SECRET",
            },
            "connect": {
                "scope": "connected_accounts",
                "connect": True,
                "url": base + "/webhooks/stripe/connect/live",
                "api_version": webhook_api_version,
                "enabled_events": list(CONNECT_EVENTS),
                "pending_secret_variable": "STRIPE_LIVE_PENDING_CONNECT_WEBHOOK_SECRET",
            },
        },
    }


def inspect_endpoint_configuration(plan, kind, *, observed_platform_account,
                                   observed_scope, endpoint):
    """Compare one explicitly scoped provider readback with the packet.

The endpoint object alone does not prove its platform or event scope. The
caller must supply both from the authenticated Dashboard/API read context.
No successful comparison proves delivery, ledger isolation, or activation.
Raw endpoint fields and secrets are deliberately absent from the result.
"""
    if kind not in {"platform", "connect"}:
        raise SetupPlanError()
    expected = plan["endpoints"][kind]
    issues = []
    if observed_platform_account != plan["platform_account"]:
        issues.append("platform_account_mismatch")
    if observed_scope != expected["scope"]:
        issues.append("event_scope_mismatch")
    if not isinstance(endpoint, dict):
        issues.append("invalid_endpoint")
    else:
        endpoint_id = endpoint.get("id")
        if (endpoint.get("object") != "webhook_endpoint"
                or not isinstance(endpoint_id, str)
                or not re.fullmatch(r"we_[A-Za-z0-9]+", endpoint_id)):
            issues.append("invalid_endpoint")
        if endpoint.get("livemode") is not True:
            issues.append("live_mode_unverified")
        if endpoint.get("status") != "enabled":
            issues.append("endpoint_not_enabled")
        if endpoint.get("url") != expected["url"]:
            issues.append("receiver_url_mismatch")
        if endpoint.get("api_version") != expected["api_version"]:
            issues.append("api_version_mismatch")
        events = endpoint.get("enabled_events")
        if (not isinstance(events, list)
                or any(not isinstance(event, str) for event in events)
                or len(events) != len(set(events))
                or set(events) != set(expected["enabled_events"])):
            issues.append("event_selection_mismatch")
    return {
        "configuration_matches": not issues,
        "issues": issues,
        "delivery_verified": False,
        "ledger_isolation_verified": False,
        "payments_activated": False,
    }
