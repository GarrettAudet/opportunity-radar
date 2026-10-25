from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


DOW_ALIASES = {
    "MON": 0,
    "TUE": 1,
    "WED": 2,
    "THU": 3,
    "FRI": 4,
    "SAT": 5,
    "SUN": 6,
}


def local_now(now: datetime | None, timezone_name: str) -> datetime:
    base = now or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return base.astimezone(ZoneInfo(timezone_name))


def week_key_for(now: datetime | None, timezone_name: str) -> str:
    local = local_now(now, timezone_name)
    year, week, _day = local.isocalendar()
    return f"{year}-W{week:02d}"


def day_key_for(now: datetime | None, timezone_name: str) -> str:
    return local_now(now, timezone_name).date().isoformat()


def should_send_now(now: datetime | None, *, timezone_name: str, send_dow: str, send_hour: int, frequency: str = "weekly") -> bool:
    """Allow same-day catch-up; the pipeline enforces daily or weekly suppression."""
    local = local_now(now, timezone_name)
    wanted_dow = DOW_ALIASES.get(send_dow.upper()[:3])
    return (
        frequency in {"daily", "weekly"}
        and 0 <= send_hour <= 23
        and (frequency == "daily" or (wanted_dow is not None and local.weekday() == wanted_dow))
        and local.hour >= send_hour
    )
