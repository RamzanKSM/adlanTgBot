from aiogram import F
from aiogram.enums import ChatType


PRIVATE_CHAT_FILTER = F.chat.type == ChatType.PRIVATE


def is_private_chat(chat_type: object) -> bool:
    return chat_type == ChatType.PRIVATE or chat_type == ChatType.PRIVATE.value
