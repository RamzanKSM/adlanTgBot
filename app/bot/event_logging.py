import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message, TelegramObject, User


logger = logging.getLogger(__name__)


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))


def _safe_username(user: User | None) -> str | None:
    return user.username if user is not None else None


def _message_command(message: Message) -> str | None:
    if not message.text or not message.text.startswith("/"):
        return None
    return message.text.split(maxsplit=1)[0]


def _message_kind(message: Message) -> str:
    if _message_command(message):
        return "command"
    content_type = _enum_value(message.content_type)
    return "text" if content_type == "text" else content_type


def _callback_data_prefix(data: str | None) -> str | None:
    if not data:
        return None
    return data.split(":", maxsplit=1)[0][:32]


def _status_value(value: object) -> str:
    return _enum_value(getattr(value, "status", value))


class TelegramEventLoggingMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message):
            self._log_message(event)
        elif isinstance(event, CallbackQuery):
            self._log_callback_query(event)
        elif isinstance(event, ChatMemberUpdated):
            self._log_chat_member(event)
        return await handler(event, data)

    def _log_message(self, message: Message) -> None:
        command = _message_command(message)
        logger.info(
            "telegram.message chat_id=%s chat_type=%s user_id=%s username=%s message_type=%s command=%s",
            message.chat.id,
            _enum_value(message.chat.type),
            message.from_user.id if message.from_user else None,
            _safe_username(message.from_user),
            _message_kind(message),
            command,
        )

    def _log_callback_query(self, callback: CallbackQuery) -> None:
        callback_message = callback.message
        chat = getattr(callback_message, "chat", None)
        logger.info(
            "telegram.callback_query chat_id=%s chat_type=%s user_id=%s username=%s data_prefix=%s data_len=%s",
            chat.id if chat else None,
            _enum_value(chat.type) if chat else None,
            callback.from_user.id,
            _safe_username(callback.from_user),
            _callback_data_prefix(callback.data),
            len(callback.data) if callback.data else 0,
        )

    def _log_chat_member(self, event: ChatMemberUpdated) -> None:
        user = event.new_chat_member.user
        logger.info(
            "telegram.chat_member chat_id=%s chat_type=%s user_id=%s username=%s old_status=%s new_status=%s",
            event.chat.id,
            _enum_value(event.chat.type),
            user.id,
            _safe_username(user),
            _status_value(event.old_chat_member),
            _status_value(event.new_chat_member),
        )
