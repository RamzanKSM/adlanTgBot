from app.bot.handlers_user import DOCUMENTS_PROMPT, NO_ACTIVE_ACCESS_TEXT, SUPPORT_TEXT


def test_user_messages_use_the_requested_concise_copy() -> None:
    assert SUPPORT_TEXT == "👤 Поддержка: @gymvash\n🕐 Обычно отвечаем в течение 12 часов."
    assert DOCUMENTS_PROMPT == "📄 Выберите документ:"
    assert NO_ACTIVE_ACCESS_TEXT == "❌ Активного доступа нет. Нажмите «💳 Тарифы», чтобы выбрать тариф."
