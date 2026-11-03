"""Recruitment closes after its displayed calendar date in Japan."""
from datetime import date, datetime, timedelta, timezone
import re

JST = timezone(timedelta(hours=9))


def request_today(at: datetime | None = None) -> date:
    return (at or datetime.now(timezone.utc)).astimezone(JST).date()


def accepting_applications(item: dict, *, at: datetime | None = None) -> bool:
    if item.get("status") != "open":
        return False
    value = item.get("deadline")
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        deadline = date.fromisoformat(value)
    except ValueError:
        return False
    return deadline >= request_today(at)
