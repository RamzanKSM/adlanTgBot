from aiogram import Router
from aiogram.types import ChatMemberUpdated, User

from app.config import Settings
from app.db.connection import open_database
from app.db.repositories import AccessEventsRepository, InviteLinksRepository, UsersRepository
from app.services.admin_notify import notify_admins
from app.services.group_access import can_remove_from_group
from app.utils.datetime import utc_now


router = Router(name="chat_member")
MEMBER_STATUSES = {"member", "administrator", "creator"}


def _status_value(value: object) -> str:
    return str(getattr(value, "value", value))


def _user_details(user: User) -> dict[str, object]:
    return {
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
    }


def _format_user(user: User) -> str:
    username = f" @{user.username}" if user.username else ""
    name = " ".join(part for part in (user.first_name, user.last_name) if part).strip()
    return f"{name or 'Без имени'}{username} (ID: {user.id})"


def _has_active_access(user: object) -> bool:
    access_until = getattr(user, "access_until", None)
    return access_until is not None and access_until > utc_now()


async def _remove_participant(
    event: ChatMemberUpdated,
    settings: Settings,
    events: AccessEventsRepository,
    telegram_user_id: int,
    details: dict[str, object],
) -> str:
    safety = await can_remove_from_group(event.bot, settings, telegram_user_id)
    if not safety.can_remove:
        event_type = "group_join_removal_skipped_protected" if safety.is_protected else "group_join_removal_skipped_unverified"
        await events.add(
            telegram_user_id=telegram_user_id,
            event_type=event_type,
            details={**details, "reason": safety.reason, "status": safety.status, "error": safety.error},
        )
        return "removal_skipped"

    try:
        await event.bot.ban_chat_member(settings.telegram_group_id, telegram_user_id)
        await event.bot.unban_chat_member(settings.telegram_group_id, telegram_user_id, only_if_banned=True)
    except Exception as exc:
        await events.add(
            telegram_user_id=telegram_user_id,
            event_type="group_join_removal_failed",
            details={**details, "error_type": type(exc).__name__, "error": str(exc)},
        )
        return "removal_failed"

    await events.add(
        telegram_user_id=telegram_user_id,
        event_type="group_join_removed",
        details=details,
    )
    return "removed"


async def _revoke_personal_invite(
    event: ChatMemberUpdated,
    settings: Settings,
    invites: InviteLinksRepository,
    invite_id: int,
    invite_link: str,
) -> str | None:
    try:
        await event.bot.revoke_chat_invite_link(settings.telegram_group_id, invite_link)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"

    await invites.mark_revoked(invite_id, utc_now())
    return None


@router.chat_member()
async def on_chat_member(event: ChatMemberUpdated, settings: Settings) -> None:
    if event.chat.id != settings.telegram_group_id:
        return

    new_status = _status_value(event.new_chat_member.status)
    old_status = _status_value(event.old_chat_member.status)
    participant = event.new_chat_member.user
    telegram_user_id = participant.id

    async with open_database(settings.database_path) as db:
        users = UsersRepository(db)
        invites = InviteLinksRepository(db)
        events = AccessEventsRepository(db)

        if new_status in {"left", "kicked"}:
            await users.set_is_in_group(telegram_user_id, False)
            await events.add(
                telegram_user_id=telegram_user_id,
                event_type="group_left",
                details={"old_status": old_status, "new_status": new_status, **_user_details(participant)},
            )
            await db.commit()
            return

        if new_status not in MEMBER_STATUSES or old_status in MEMBER_STATUSES:
            return

        invite_link_value = getattr(getattr(event, "invite_link", None), "invite_link", None)
        known_user = await users.get_by_telegram_id(telegram_user_id)
        details: dict[str, object] = {
            "old_status": old_status,
            "new_status": new_status,
            "invite_link": invite_link_value,
            "has_active_access": _has_active_access(known_user),
            **_user_details(participant),
        }

        if invite_link_value:
            invite = await invites.get_active_by_link(invite_link_value)
            expected_user = await users.get_by_id(invite.user_id) if invite else None
            if invite is not None and expected_user is not None and expected_user.telegram_user_id != telegram_user_id:
                wrong_details = {
                    **details,
                    "invite_id": invite.id,
                    "expected_user_id": expected_user.telegram_user_id,
                    "expected_user_db_id": expected_user.id,
                }
                revoke_error = await _revoke_personal_invite(event, settings, invites, invite.id, invite.invite_link)
                if revoke_error:
                    wrong_details["invite_revoke_error"] = revoke_error
                await events.add(
                    telegram_user_id=telegram_user_id,
                    event_type="group_join_wrong_personal_invite",
                    details=wrong_details,
                )
                action = await _remove_participant(event, settings, events, telegram_user_id, wrong_details)
                await db.commit()
                await notify_admins(
                    settings,
                    event.bot,
                    f"Вход по чужой персональной ссылке: {_format_user(participant)}. "
                    f"Ожидаемый плательщик ID: {expected_user.telegram_user_id}. Действие: {action}.",
                )
                return

            if invite is not None and expected_user is not None and _has_active_access(expected_user):
                await users.set_is_in_group(telegram_user_id, True)
                await invites.mark_used(invite.id, utc_now())
                await events.add(
                    telegram_user_id=telegram_user_id,
                    user_id=expected_user.id,
                    event_type="group_join_expected_user",
                    details={**details, "invite_id": invite.id, "expected_user_id": expected_user.telegram_user_id},
                )
                await db.commit()
                await notify_admins(
                    settings,
                    event.bot,
                    f"Штатный вход в группу: {_format_user(participant)} по персональной ссылке.",
                )
                return

            if invite is not None and expected_user is not None:
                expired_details = {
                    **details,
                    "invite_id": invite.id,
                    "expected_user_id": expected_user.telegram_user_id,
                    "tracked_invite": True,
                }
                revoke_error = await _revoke_personal_invite(event, settings, invites, invite.id, invite.invite_link)
                if revoke_error:
                    expired_details["invite_revoke_error"] = revoke_error
                await events.add(
                    telegram_user_id=telegram_user_id,
                    user_id=expected_user.id,
                    event_type="group_join_no_active_access",
                    details=expired_details,
                )
                action = await _remove_participant(event, settings, events, telegram_user_id, expired_details)
                await db.commit()
                await notify_admins(
                    settings,
                    event.bot,
                    f"Вход по персональной ссылке после окончания доступа: {_format_user(participant)}. "
                    f"Действие: {action}.",
                )
                return

            event_type = "group_join_untracked_invite" if _has_active_access(known_user) else "group_join_untracked_invite_no_active_access"
            untracked_details = {**details, "tracked_invite": False}
            if _has_active_access(known_user):
                await users.set_is_in_group(telegram_user_id, True)
                await events.add(
                    telegram_user_id=telegram_user_id,
                    user_id=known_user.id if known_user else None,
                    event_type=event_type,
                    details={**untracked_details, "action": "allowed_active_access"},
                )
                action = "allowed_active_access"
            else:
                await events.add(telegram_user_id=telegram_user_id, event_type=event_type, details=untracked_details)
                action = await _remove_participant(event, settings, events, telegram_user_id, untracked_details)
            await db.commit()
            await notify_admins(
                settings,
                event.bot,
                f"Вход по неотслеживаемой ссылке: {_format_user(participant)}. Действие: {action}.",
            )
            return

        if _has_active_access(known_user):
            await users.set_is_in_group(telegram_user_id, True)
            await events.add(
                telegram_user_id=telegram_user_id,
                user_id=known_user.id if known_user else None,
                event_type="group_join_active_access_without_invite",
                details={**details, "action": "allowed_active_access"},
            )
            action = "allowed_active_access"
        else:
            await events.add(
                telegram_user_id=telegram_user_id,
                event_type="group_join_no_active_access",
                details=details,
            )
            action = await _remove_participant(event, settings, events, telegram_user_id, details)
        await db.commit()
        await notify_admins(
            settings,
            event.bot,
            f"Вход в группу без invite-ссылки: {_format_user(participant)}. Действие: {action}.",
        )
