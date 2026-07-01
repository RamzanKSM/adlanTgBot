from aiogram import Router
from aiogram.types import ChatMemberUpdated

from app.config import Settings
from app.db.connection import open_database
from app.db.repositories import AccessEventsRepository, InviteLinksRepository, UsersRepository
from app.services.admin_notify import notify_admins
from app.services.group_access import can_remove_from_group
from app.utils.datetime import utc_now


router = Router(name="chat_member")


def _status_value(value: object) -> str:
    return str(getattr(value, "value", value))


@router.chat_member()
async def on_chat_member(event: ChatMemberUpdated, settings: Settings) -> None:
    if event.chat.id != settings.telegram_group_id:
        return

    new_status = _status_value(event.new_chat_member.status)
    old_status = _status_value(event.old_chat_member.status)
    telegram_user_id = event.new_chat_member.user.id

    async with open_database(settings.database_path) as db:
        users = UsersRepository(db)
        invites = InviteLinksRepository(db)
        events = AccessEventsRepository(db)

        if new_status in {"left", "kicked"}:
            await users.set_is_in_group(telegram_user_id, False)
            await events.add(telegram_user_id=telegram_user_id, event_type="group_left")
            await db.commit()
            return

        if new_status not in {"member", "administrator", "creator"} or old_status in {"member", "administrator", "creator"}:
            return

        invite_link_value = getattr(getattr(event, "invite_link", None), "invite_link", None)
        if not invite_link_value:
            await users.set_is_in_group(telegram_user_id, True)
            await events.add(telegram_user_id=telegram_user_id, event_type="group_join_without_tracked_invite")
            await db.commit()
            return

        invite = await invites.get_active_by_link(invite_link_value)
        expected_user = await users.get_by_id(invite.user_id) if invite else None
        if invite is None or expected_user is None or expected_user.telegram_user_id != telegram_user_id:
            safety = await can_remove_from_group(event.bot, settings, telegram_user_id)
            if not safety.can_remove:
                event_type = (
                    "wrong_invite_removal_skipped_protected"
                    if safety.is_protected
                    else "wrong_invite_removal_skipped_unverified"
                )
                await events.add(
                    telegram_user_id=telegram_user_id,
                    event_type=event_type,
                    details={
                        "invite_link": invite_link_value,
                        "expected_user_id": expected_user.telegram_user_id if expected_user else None,
                        "reason": safety.reason,
                        "status": safety.status,
                        "error": safety.error,
                    },
                )
                await db.commit()
                await notify_admins(
                    settings,
                    event.bot,
                    (
                        f"Пользователь {telegram_user_id} вошел по чужой персональной ссылке, "
                        f"но удаление пропущено из-за admin/protection/status check ({safety.reason})."
                    ),
                )
                return

            await event.bot.ban_chat_member(settings.telegram_group_id, telegram_user_id)
            await event.bot.unban_chat_member(settings.telegram_group_id, telegram_user_id, only_if_banned=True)
            if invite is not None:
                await event.bot.revoke_chat_invite_link(settings.telegram_group_id, invite.invite_link)
                await invites.mark_revoked(invite.id, utc_now())
            await events.add(
                telegram_user_id=telegram_user_id,
                event_type="wrong_invite_user_removed",
                details={"invite_link": invite_link_value, "expected_user_id": expected_user.telegram_user_id if expected_user else None},
            )
            await db.commit()
            await notify_admins(
                settings,
                event.bot,
                f"Удален чужой пользователь {telegram_user_id}, вошедший по персональной ссылке.",
            )
            return

        await users.set_is_in_group(telegram_user_id, True)
        await invites.mark_used(invite.id, utc_now())
        await events.add(
            telegram_user_id=telegram_user_id,
            user_id=expected_user.id,
            event_type="group_joined_by_personal_invite",
            details={"invite_id": invite.id},
        )
        await db.commit()
