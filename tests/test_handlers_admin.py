import pytest

from app.bot.handlers_admin import (
    TARIFF_SET_USAGE,
    TariffSetUsageError,
    TariffSetValidationError,
    parse_tariff_set_args,
)


def test_parse_tariff_set_args_accepts_required_args() -> None:
    args = parse_tariff_set_args('/tariff_set week "7 дней" 500 7')

    assert args.code == "week"
    assert args.title == "7 дней"
    assert args.price_amount == 500
    assert args.duration_days == 7
    assert args.currency == "RUB"
    assert args.sort_order == 100
    assert args.description == ""


def test_tariff_set_usage_contains_format_args_examples_and_quotes_hint() -> None:
    assert 'Формат: /tariff_set CODE "Название" PRICE DURATION_DAYS' in TARIFF_SET_USAGE
    assert "- CODE -" in TARIFF_SET_USAGE
    assert "- PRICE -" in TARIFF_SET_USAGE
    assert '/tariff_set week "7 дней" 500 7' in TARIFF_SET_USAGE
    assert '/tariff_set month "30 дней" 1500 30' in TARIFF_SET_USAGE
    assert "Если в названии есть пробелы, берите его в кавычки." in TARIFF_SET_USAGE


def test_parse_tariff_set_args_raises_usage_error_for_unparsed_input() -> None:
    with pytest.raises(TariffSetUsageError):
        parse_tariff_set_args('/tariff_set week "7 дней" 500')


@pytest.mark.parametrize(
    ("command", "message"),
    [
        ('/tariff_set week "7 дней" 0 7', "Цена тарифа должна быть больше 0."),
        ('/tariff_set week "7 дней" 500 0', "Длительность тарифа должна быть больше 0 дней."),
    ],
)
def test_parse_tariff_set_args_validates_positive_numbers(command: str, message: str) -> None:
    with pytest.raises(TariffSetValidationError, match=message):
        parse_tariff_set_args(command)
