import pytest

from app.messages import message


def test_message_catalog_formats_dynamic_values() -> None:
    assert message("payment.received_with_link", link="https://t.me/+test") == (
        "✅ Оплата получена.\n🔗 Ваша ссылка в группу: https://t.me/+test"
    )


def test_message_catalog_contains_primary_user_copy() -> None:
    assert message("user.tariffs_prompt") == "💳 Выберите тариф:"
    assert "🕐" in message("user.support")
    assert "💳 Тарифы" in message("user.no_active_access")


def test_message_catalog_rejects_unknown_key() -> None:
    with pytest.raises(KeyError, match="Unknown message key"):
        message("unknown.key")


def test_message_catalog_reports_missing_format_value() -> None:
    with pytest.raises(KeyError, match="Missing value 'link'"):
        message("payment.received_with_link")
