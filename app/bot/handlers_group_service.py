import logging

from aiogram import F, Router
from aiogram.types import Message, User

from app.config import Settings
from app.db.connection import open_database
from app.db.repositories import AccessEventsRepository
from app.services.admin_notify import notify_admins


logger = logging.getLogger(__name__)
router = Router(name="group_service")


def _user_details(user: User, message: Message) -> dict[str, object]:
    return {
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "message_id": message.message_id,
        "chat_id": message.chat.id,
    }


@router.message(F.new_chat_members)
async def delete_join_service_message(message: Message, settings: Settings) -> None:
    """Remove Telegram's automatic join notice without posting anything in the group."""
    if message.chat.id != settings.telegram_group_id:
        return

    participants = list(message.new_chat_members or [])
    if not participants:
        return

    delete_error: Exception | None = None
    try:
        await message.delete()
    except Exception as exc:
        delete_error = exc
        logger.warning(
            "group_join_service_message_delete_failed chat_id=%s message_id=%s error=%s",
            message.chat.id,
            message.message_id,
            exc,
        )

    async with open_database(settings.database_path) as db:
        events = AccessEventsRepository(db)
        for participant in participants:
            details = _user_details(participant, message)
            if delete_error is None:
                await events.add(
                    telegram_user_id=participant.id,
                    event_type="group_join_service_message_deleted",
                    details=details,
                )
            else:
                await events.add(
                    telegram_user_id=participant.id,
                    event_type="group_join_service_message_delete_failed",
                    details={
                        **details,
                        "error_type": type(delete_error).__name__,
                        "error": str(delete_error),
                    },
                )
        await db.commit()

    if delete_error is not None:
        await notify_admins(
            settings,
            message.bot,
            "Не удалось удалить сервисное сообщение о входе в группу. "
            "Проверьте у бота право Delete messages / Удаление сообщений.",
        )
