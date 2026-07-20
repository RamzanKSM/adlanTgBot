from datetime import UTC, datetime, timedelta, timezone


MOSCOW_TZ = timezone(timedelta(hours=3), "MSK")


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_moscow(value: datetime) -> datetime:
    return to_utc(value).astimezone(MOSCOW_TZ)


def to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def datetime_to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    # SQLite stores the operator-facing representation in Moscow time.  The
    # explicit offset keeps it unambiguous and lets internal UTC comparisons
    # preserve the exact same instant.
    return to_moscow(value).isoformat()


def format_datetime_moscow(value: datetime) -> str:
    return to_moscow(value).strftime("%d.%m.%Y %H:%M МСК")


def iso_to_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    return to_utc(datetime.fromisoformat(normalized))


def add_days_from_base(current_until: datetime | None, paid_at: datetime, duration_days: int) -> datetime:
    base = max(to_utc(current_until), to_utc(paid_at)) if current_until else to_utc(paid_at)
    return base + timedelta(days=duration_days)
