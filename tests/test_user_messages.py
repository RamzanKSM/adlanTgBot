from aiogram.enums import ParseMode

from app.bot.handlers_user import (
    COMMUNITY_BENEFITS_TEXT,
    DOCUMENTS_PROMPT,
    NO_ACTIVE_ACCESS_TEXT,
    SUPPORT_TEXT,
    community_benefits_button,
)
from app.bot.keyboards import USER_BENEFITS_BUTTON


def test_user_messages_use_the_requested_concise_copy() -> None:
    assert SUPPORT_TEXT == "👤 Поддержка: @gymvash\n🕐 Обычно отвечаем в течение 12 часов."
    assert DOCUMENTS_PROMPT == "📄 Выберите документ:"
    assert NO_ACTIVE_ACCESS_TEXT == "❌ Активного доступа нет. Нажмите «💳 Тарифы», чтобы выбрать тариф."


def test_community_benefits_message_and_button_use_catalog_copy() -> None:
    assert USER_BENEFITS_BUTTON == "Что я получаю❓"
    assert COMMUNITY_BENEFITS_TEXT.startswith("**🔥 Что ты получишь в сообществе?**")
    assert COMMUNITY_BENEFITS_TEXT.endswith(
        "**Одна подписка - вместо десятков отдельных курсов, программ тренировок, "
        "планов питания и консультаций. Всё самое важное уже собрано в одном месте.**"
    )


async def test_community_benefits_button_sends_markdown_message() -> None:
    class FakeMessage:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def answer(self, body: str, **kwargs: object) -> None:
            self.calls.append((body, kwargs))

    fake_message = FakeMessage()

    await community_benefits_button(fake_message)  # type: ignore[arg-type]

    assert fake_message.calls == [(COMMUNITY_BENEFITS_TEXT, {"parse_mode": ParseMode.MARKDOWN})]
