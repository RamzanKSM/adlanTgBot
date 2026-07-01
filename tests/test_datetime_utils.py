from datetime import UTC, datetime

from app.utils.datetime import format_datetime_moscow


def test_format_datetime_moscow_converts_utc_to_moscow_time() -> None:
    value = datetime(2026, 7, 1, 23, 39, 13, 768248, tzinfo=UTC)

    assert format_datetime_moscow(value) == "02.07.2026 02:39 МСК"


def test_format_datetime_moscow_treats_naive_datetime_as_utc() -> None:
    value = datetime(2026, 7, 2, 0, 5)

    assert format_datetime_moscow(value) == "02.07.2026 03:05 МСК"
