from aiogram.enums import ChatType

from app.bot.filters import is_private_chat


def test_is_private_chat_accepts_private_enum_and_value() -> None:
    assert is_private_chat(ChatType.PRIVATE)
    assert is_private_chat("private")


def test_is_private_chat_rejects_group_chat_types() -> None:
    assert not is_private_chat(ChatType.GROUP)
    assert not is_private_chat("group")
    assert not is_private_chat("supergroup")
