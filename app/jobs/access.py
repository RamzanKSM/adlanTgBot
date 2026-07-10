import logging
from datetime import timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from app.config import Settings
from app.db.connection import open_database
from app.db.repositories import AccessEventsRepository, UsersRepository
from app.services.group_access import can_remove_from_group
from app.utils.datetime import datetime_to_iso, format_datetime_moscow, utc_now


logger = logging.getLogger(__name__)


async def warn_and_expire_access(settings: Settings, bot: Bot) -> None:
    now = utc_now()
    warning_until = now + timedelta(days=3)
    async with open_database(settings.database_path) as db:
        users = UsersRepository(db)
        events = AccessEventsRepository(db)

        for user in await users.list_warning_due(now, warning_until):
            if user.access_until is None:
                continue
            try:
                await bot.send_message(
                    user.telegram_user_id,
                    f"Доступ в группу закончится {format_datetime_moscow(user.access_until)}. "
                    "Продлите доступ заранее.",
                )
            finally:
                await users.mark_warned(user.id, user.access_until)
                await events.add(
                    telegram_user_id=user.telegram_user_id,
                    user_id=user.id,
                    event_type="access_warning_sent",
                    details={"access_until": datetime_to_iso(user.access_until)},
                )

        for user in await users.list_expired_in_group(now):
            safety = await can_remove_from_group(bot, settings, user.telegram_user_id)
            safety_details = {
                "access_until": datetime_to_iso(user.access_until),
                "reason": safety.reason,
                "status": safety.status,
                "error": safety.error,
            }
            if not safety.can_remove:
                if safety.reason == "not_member":
                    await users.set_is_in_group(user.telegram_user_id, False)
                    event_type = "access_expired_already_not_in_group"
                elif safety.is_protected:
                    event_type = "access_expired_removal_skipped_protected"
                else:
                    event_type = "access_expired_removal_skipped_unverified"
                await events.add(
                    telegram_user_id=user.telegram_user_id,
                    user_id=user.id,
                    event_type=event_type,
                    details=safety_details,
                )
                continue

            try:
                await bot.ban_chat_member(settings.telegram_group_id, user.telegram_user_id)
                await bot.unban_chat_member(settings.telegram_group_id, user.telegram_user_id, only_if_banned=True)
            except Exception as exc:
                await events.add(
                    telegram_user_id=user.telegram_user_id,
                    user_id=user.id,
                    event_type="access_expired_removal_failed",
                    details={**safety_details, "error": str(exc), "error_type": type(exc).__name__},
                )
            else:
                await users.set_is_in_group(user.telegram_user_id, False)
                await events.add(
                    telegram_user_id=user.telegram_user_id,
                    user_id=user.id,
                    event_type="access_expired_removed",
                    details={"access_until": datetime_to_iso(user.access_until)},
                )
                try:
                    await bot.send_message(
                        user.telegram_user_id,
                        "Доступ закончился, вы удалены из группы. Продлить доступ можно через кнопку «💳 Тарифы».",
                    )
                except (TelegramBadRequest, TelegramForbiddenError) as exc:
                    logger.info(
                        "access_expired_removal_notice_failed user_id=%s error=%s",
                        user.telegram_user_id,
                        exc,
                    )

        await db.commit()
