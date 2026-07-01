from datetime import timedelta

import aiosqlite
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

from app.config import Settings
from app.db.repositories import AccessEventsRepository, InviteLinksRepository, UsersRepository
from app.utils.datetime import utc_now


MEMBER_STATUSES = {"creator", "administrator", "member"}


class InviteService:
    def __init__(self, db: aiosqlite.Connection, settings: Settings, bot: Bot):
        self.db = db
        self.settings = settings
        self.bot = bot
        self.users = UsersRepository(db)
        self.invites = InviteLinksRepository(db)
        self.events = AccessEventsRepository(db)

    async def is_current_member(self, telegram_user_id: int) -> bool:
        try:
            member = await self.bot.get_chat_member(self.settings.telegram_group_id, telegram_user_id)
        except TelegramBadRequest:
            return False
        status = getattr(member, "status", "")
        if status in MEMBER_STATUSES:
            return True
        return bool(getattr(member, "is_member", False))

    async def ensure_personal_invite(self, telegram_user_id: int, payment_id: int | None = None) -> str | None:
        user = await self.users.get_by_telegram_id(telegram_user_id)
        if user is None or user.access_until is None or user.access_until <= utc_now():
            return None

        if await self.is_current_member(telegram_user_id):
            await self.users.set_is_in_group(telegram_user_id, True)
            await self.db.commit()
            return None

        active_invite = await self.invites.get_active_by_user(user.id, utc_now())
        if active_invite is not None:
            return active_invite.invite_link

        expires_at = utc_now() + timedelta(hours=24)
        link = await self.bot.create_chat_invite_link(
            chat_id=self.settings.telegram_group_id,
            name=f"user-{telegram_user_id}",
            expire_date=expires_at,
            member_limit=1,
            creates_join_request=False,
        )
        invite = await self.invites.create(
            user_id=user.id,
            payment_id=payment_id,
            invite_link=link.invite_link,
            telegram_invite_link_id=getattr(link, "invite_link", None),
            expires_at=expires_at,
        )
        await self.events.add(
            telegram_user_id=telegram_user_id,
            user_id=user.id,
            event_type="invite_created",
            details={"invite_id": invite.id, "payment_id": payment_id},
        )
        await self.db.commit()
        return invite.invite_link

    async def revoke_link(self, invite_link: str) -> None:
        invite = await self.invites.get_active_by_link(invite_link)
        try:
            await self.bot.revoke_chat_invite_link(self.settings.telegram_group_id, invite_link)
        finally:
            if invite is not None:
                await self.invites.mark_revoked(invite.id, utc_now())
                await self.db.commit()
