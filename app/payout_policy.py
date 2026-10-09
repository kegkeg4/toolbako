"""Shared calculation policy; a schedule is not evidence of a bank transfer."""
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
RESERVED_PAYOUT_STATUSES = frozenset({"requested", "approved", "processing", "completed", "paid"})


def scheduled_payout_date(requested_at: datetime) -> datetime:
    if requested_at.tzinfo is None:
        raise ValueError("timezone-aware timestamp required")
    local = requested_at.astimezone(JST)
    monday = local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=local.weekday())
    return monday + timedelta(days=10)  # following week's Thursday, JST


def payout_fee(amount: int, settings) -> int:
    if isinstance(amount, bool) or not isinstance(amount, int) or amount < settings.payout_minimum:
        raise ValueError("invalid payout amount")
    fee = 0 if amount >= settings.payout_fee_free_threshold else settings.payout_fee
    if fee < 0 or amount <= fee:
        raise ValueError("payout must remain positive after fees")
    return fee
