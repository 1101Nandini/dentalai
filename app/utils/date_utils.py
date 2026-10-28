from datetime import datetime, timezone, date
from typing import Optional


def utcnow() -> datetime:
    """Return current UTC datetime (timezone-aware)."""
    return datetime.now(timezone.utc)


def format_date(dt: datetime, fmt: str = "%Y-%m-%d") -> str:
    return dt.strftime(fmt)


def date_to_str(d: date) -> str:
    return d.isoformat()


def days_since(dt: datetime) -> int:
    delta = datetime.now(timezone.utc) - dt
    return delta.days
