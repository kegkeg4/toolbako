"""Offline configuration boundaries for the separately approved mail role."""
import pytest

from app.config import Settings, _strict_notification_epoch


@pytest.mark.parametrize("value", ["", "-1", "1.0", " 1", "+1", "true", "１２３", "1\n"])
def test_mail_policy_epoch_rejects_non_ascii_integer_without_echo(monkeypatch, value):
    monkeypatch.setenv("LIVE_MAIL_POLICY_SINCE", value)
    with pytest.raises(ValueError, match="^Invalid transaction notification start time$"):
        _strict_notification_epoch("LIVE_MAIL_POLICY_SINCE")


def test_mail_policy_is_absent_by_default_and_can_stay_when_delivery_paused(monkeypatch):
    monkeypatch.delenv("LIVE_MAIL_POLICY_SINCE", raising=False)
    assert _strict_notification_epoch("LIVE_MAIL_POLICY_SINCE") == 0
    s = Settings(live_mail_policy_since=1793664000,
                 live_transaction_emails_since=1793664000,
                 live_transaction_emails_enabled=False)
    assert s.live_mail_policy_since == s.live_transaction_emails_since
    assert s.live_transaction_emails_enabled is False


@pytest.mark.parametrize("value", [True, False, None, "1", -1, 1.0])
def test_constructor_rejects_non_integer_or_negative_policy_epoch(value):
    with pytest.raises(ValueError, match="^Invalid live mail permission start time$"):
        Settings(live_mail_policy_since=value)


@pytest.mark.parametrize("enabled", [False, True])
def test_config_does_not_allow_policy_start_and_event_start_to_drift(enabled):
    with pytest.raises(ValueError, match="^Invalid live mail permission start time$"):
        Settings(live_mail_policy_since=1793664000,
                 live_transaction_emails_since=1793663999,
                 live_transaction_emails_enabled=enabled)
