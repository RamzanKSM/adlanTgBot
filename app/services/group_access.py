from dataclasses import dataclass
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

from app.config import Settings


logger = logging.getLogger(__name__)

GROUP_ADMIN_STATUSES = {"administrator", "creator"}
GROUP_MEMBER_STATUSES = {"administrator", "creator", "member"}
PROTECTED_REASONS = {"configured_admin", "telegram_admin"}


@dataclass(slots=True)
class GroupRemovalSafety:
    can_remove: bool
    reason: str
    status: str | None = None
    error: str | None = None

    @property
    def is_protected(self) -> bool:
        return self.reason in PROTECTED_REASONS


def _status_value(value: object) -> str:
    return str(getattr(value, "value", value))


async def can_remove_from_group(bot: Bot, settings: Settings, telegram_user_id: int) -> GroupRemovalSafety:
    if telegram_user_id in settings.admin_ids:
        return GroupRemovalSafety(can_remove=False, reason="configured_admin")

    try:
        member = await bot.get_chat_member(settings.telegram_group_id, telegram_user_id)
    except TelegramBadRequest as exc:
        logger.warning("group_member_status_unverified user_id=%s error=%s", telegram_user_id, exc)
        return GroupRemovalSafety(can_remove=False, reason="unverified", error=str(exc))

    status = _status_value(getattr(member, "status", ""))
    if status in GROUP_ADMIN_STATUSES:
        return GroupRemovalSafety(can_remove=False, reason="telegram_admin", status=status)
    if status in GROUP_MEMBER_STATUSES or bool(getattr(member, "is_member", False)):
        return GroupRemovalSafety(can_remove=True, reason="removable", status=status)
    return GroupRemovalSafety(can_remove=False, reason="not_member", status=status)
