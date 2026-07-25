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


def test_message_catalog_contains_community_benefits_copy() -> None:
    assert message("button.user_benefits") == "Что я получаю❓"
    assert message("user.community_benefits") == (
        "**🔥 Что ты получишь в сообществе?**\n\n"
        "Это не просто закрытый канал, а полноценная система для тех, кто хочет стать сильнее, "
        "спортивнее и добиться реального результата.\n\n"
        "Внутри тебя ждут:\n\n"
        "💪 Программы тренировок для набора мышечной массы.\n\n"
        "🥋 ОФП и силовые тренировки для бойцов БЖЖ, грэпплинга, ММА и борьбы.\n\n"
        "⚖️ Полноценный марафон по похудению с пошаговой системой и гарантией результата при "
        "соблюдении всех рекомендаций.\n\n"
        "🍽️ Готовые планы питания для похудения и набора массы.\n\n"
        "🧠 Материалы по психологии, дисциплине и борьбе с выгоранием.\n\n"
        "💊 Раздел о витаминах, спортивных добавках и гормонах - только то, что действительно работает.\n\n"
        "📚 Новые тренировки, чек-листы и полезные материалы добавляются регулярно.\n\n"
        "**Одна подписка - вместо десятков отдельных курсов, программ тренировок, планов питания и "
        "консультаций. Всё самое важное уже собрано в одном месте.**"
    )


def test_message_catalog_rejects_unknown_key() -> None:
    with pytest.raises(KeyError, match="Unknown message key"):
        message("unknown.key")


def test_message_catalog_reports_missing_format_value() -> None:
    with pytest.raises(KeyError, match="Missing value 'link'"):
        message("payment.received_with_link")
