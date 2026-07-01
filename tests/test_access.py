from datetime import UTC, datetime, timedelta

from app.services.access import calculate_access_extension


def test_access_extension_starts_from_paid_at_without_current_access() -> None:
    paid_at = datetime(2026, 1, 1, tzinfo=UTC)

    extension = calculate_access_extension(None, 30, paid_at)

    assert extension.new_access_until == paid_at + timedelta(days=30)


def test_access_extension_extends_from_current_access_when_it_is_later() -> None:
    paid_at = datetime(2026, 1, 1, tzinfo=UTC)
    current_until = datetime(2026, 1, 20, tzinfo=UTC)

    extension = calculate_access_extension(current_until, 10, paid_at)

    assert extension.new_access_until == datetime(2026, 1, 30, tzinfo=UTC)
