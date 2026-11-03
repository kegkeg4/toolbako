"""Configuration readiness only: no provider calls or delivery claims."""
import httpx
import pytest

from app.config import Settings
from app.production import readiness_checks
from test_live_financial_config import financial


SINCE = 1_800_000_000


def configured(**changes):
    return financial(**{
        "email_api_key": "synthetic-never-sent", "email_from": "sender@example.invalid",
        "live_transaction_emails_enabled": True, "live_transaction_emails_since": SINCE,
        "live_mail_policy_since": SINCE, "live_database_role": "runtime_v1",
        **changes,
    })


def email_check(settings, *, database_ready=True):
    return next(item for item in readiness_checks(settings, runtime_database_ready=database_ready)
                if item["key"] == "email")


@pytest.mark.parametrize("mode", ["live_read_only", "live_setup_only", "live_financial"])
def test_live_readiness_labels_configured_without_claiming_delivery(mode, monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: pytest.fail("Readiness cannot send or probe email"))
    check = email_check(configured(stripe_connection_mode=mode))
    assert check["ok"] is True
    assert "接続設定" in check["label"]
    assert "実配達" in check["requirement"] and "別途確認" in check["requirement"]
    assert "delivery_verified" not in check


@pytest.mark.parametrize("change", [
    {"email_api_key": ""}, {"email_from": ""},
    {"live_transaction_emails_enabled": False},
    {"live_mail_policy_since": 0},
    {"live_database_role": "migration_admin"},
])
def test_live_readiness_rejects_unready_mail_configuration(change):
    assert email_check(configured(**change))["ok"] is False


@pytest.mark.parametrize("ready", [False, 1, "true", None])
def test_live_readiness_requires_actual_boolean_database_role_readiness(ready):
    assert email_check(configured(), database_ready=ready)["ok"] is False


@pytest.mark.parametrize("name,value", [
    ("live_transaction_emails_enabled", 1),
    ("live_transaction_emails_enabled", "true"),
    ("live_transaction_emails_since", True),
    ("live_transaction_emails_since", 0),
    ("live_transaction_emails_since", "1800000000"),
    ("live_mail_policy_since", True),
    ("live_mail_policy_since", "1800000000"),
    ("live_mail_policy_since", SINCE - 1),
])
def test_runtime_object_cannot_bypass_strict_email_types_or_epoch_match(name, value):
    settings = configured()
    # Actual Settings rejects these inputs; also defend a future DI container.
    object.__setattr__(settings, name, value)
    assert email_check(settings)["ok"] is False


def test_live_key_cannot_keep_legacy_email_green_under_wrong_sandbox_mode():
    settings = configured(stripe_connection_mode="sandbox", live_transaction_emails_enabled=False)
    assert email_check(settings)["ok"] is False


@pytest.mark.parametrize("key", ["", "sk_test_synthetic", "rk_test_synthetic"])
@pytest.mark.parametrize("configured_mail", [False, True])
def test_legacy_and_sandbox_email_readiness_keeps_existing_requirement(key, configured_mail):
    settings = Settings(stripe_secret_key=key, stripe_connection_mode="sandbox",
        email_api_key="synthetic" if configured_mail else "", email_from="sender@example.invalid")
    assert email_check(settings, database_ready=False)["ok"] is configured_mail


def test_live_email_settings_do_not_mark_payments_or_delivery_verified():
    checks = readiness_checks(configured(), runtime_database_ready=True)
    assert next(item for item in checks if item["key"] == "email")["ok"] is True
    assert next(item for item in checks if item["key"] == "payments")["ok"] is False
    assert not any("delivery_verified" in item for item in checks)
