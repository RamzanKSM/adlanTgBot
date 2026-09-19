from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram import Bot
from aiogram.types import Message

from app.ai.repositories import AiRepository
from app.config import Settings
from app.db.connection import open_database


logger = logging.getLogger(__name__)
router = Router(name="ai_group_messages")


def _message_text(message: Message) -> tuple[str, str] | None:
    if message.text is not None:
        return message.text, "text"
    if message.caption is not None:
        return message.caption, "caption"
    return None


def _is_configured_admin(message: Message, settings: Settings) -> bool:
    # Knowledge permissions intentionally use immutable numeric IDs only.
    return message.from_user is not None and message.from_user.id in settings.admin_ids


def _entity_text_utf16(text: str, offset: int, length: int) -> str:
    raw = text.encode("utf-16-le")
    return raw[offset * 2:(offset + length) * 2].decode("utf-16-le")


def _is_bot_invocation(message: Message, *, bot_id: int, bot_username: str | None, onboarding_pending: bool, active_session: bool) -> bool:
    if onboarding_pending or active_session:
        return True
    if message.reply_to_message and message.reply_to_message.from_user and message.reply_to_message.from_user.id == bot_id:
        return True
    content = message.text or message.caption or ""
    entities = message.entities if message.text is not None else message.caption_entities
    return bool(bot_username) and any(
        getattr(entity.type, "value", entity.type) == "mention"
        and _entity_text_utf16(content, entity.offset, entity.length).lstrip("@").casefold() == bot_username.casefold()
        for entity in (entities or [])
    )


async def _persist(message: Message, settings: Settings, bot: Bot, *, edited: bool) -> None:
    if message.chat.id != settings.telegram_group_id:
        return
    content = _message_text(message)
    if content is None:
        return
    text, kind = content
    telegram_user_id = message.from_user.id if message.from_user else None
    async with open_database(settings.database_path) as db:
        repo = AiRepository(db)
        stored = await repo.store_message(
            chat_id=message.chat.id,
            telegram_message_id=message.message_id,
            sender_telegram_user_id=telegram_user_id,
            sender_chat_id=getattr(message.sender_chat, "id", None),
            direction="incoming",
            message_kind=kind,
            text=text,
            reply_to_telegram_message_id=getattr(message.reply_to_message, "message_id", None),
            author_username=message.from_user.username if message.from_user else None,
            author_first_name=message.from_user.first_name if message.from_user else None,
            author_last_name=message.from_user.last_name if message.from_user else None,
            author_display_name=" ".join(part for part in ((message.from_user.first_name if message.from_user else None), (message.from_user.last_name if message.from_user else None)) if part) or None,
            message_thread_id=message.message_thread_id,
            created_at=message.date,
            edited_at=message.edit_date if edited else None,
        )
        if edited and stored.changed:
            await repo.invalidate_knowledge(stored.id)
            await repo.audit("save", "edited", chat_id=message.chat.id, telegram_user_id=telegram_user_id, message_row_id=stored.id)
        elif stored.changed:
            await repo.audit("save", "ok", chat_id=message.chat.id, telegram_user_id=telegram_user_id, message_row_id=stored.id)
        if stored.changed and _is_configured_admin(message, settings):
            await repo.make_knowledge_candidate(stored.id)
            await repo.audit("classify", "pending", chat_id=message.chat.id, telegram_user_id=telegram_user_id, message_row_id=stored.id)
        onboarding_pending = telegram_user_id is not None and (
            await repo.get_state(message.chat.id, telegram_user_id, "onboarding.awaiting") is not None
            and await repo.get_state(message.chat.id, telegram_user_id, "onboarding.completed") is None
        )
        active_session = await repo.active_session(message.chat.id, telegram_user_id) if telegram_user_id is not None else False
        await db.commit()
        if telegram_user_id is not None and stored.changed and not edited:
            needs_username = any(getattr(entity.type, "value", entity.type) == "mention" for entity in ((message.entities if message.text is not None else message.caption_entities) or []))
            try:
                me = await bot.get_me() if needs_username else None
                invoked = _is_bot_invocation(message, bot_id=bot.id, bot_username=me.username if me else None, onboarding_pending=onboarding_pending, active_session=active_session)
            except Exception:
                # Never turn an arbitrary @mention into our invocation. The
                # durable row remains stored and the next real interaction can
                # route it after bot identity is available.
                invoked = onboarding_pending or active_session
                await repo.audit("turn", "identity_unavailable", chat_id=message.chat.id, telegram_user_id=telegram_user_id, message_row_id=stored.id)
        else:
            invoked = False
        if invoked:
            # Commit the message before the separate BEGIN IMMEDIATE debounce claim.
            turn_id = await repo.schedule_turn(message.chat.id, telegram_user_id, stored.id, settings.ai_turn_debounce_seconds)
            await repo.audit("turn", "scheduled", chat_id=message.chat.id, telegram_user_id=telegram_user_id, message_row_id=stored.id, turn_id=turn_id)
            await db.commit()
    logger.info("ai.message_saved chat_id=%s message_id=%s kind=%s edited=%s", message.chat.id, message.message_id, kind, edited)


@router.message(F.chat.type.in_({"group", "supergroup"}))
async def save_group_message(message: Message, settings: Settings) -> None:
    await _persist(message, settings, message.bot, edited=False)


@router.edited_message(F.chat.type.in_({"group", "supergroup"}))
async def save_edited_group_message(message: Message, settings: Settings) -> None:
    await _persist(message, settings, message.bot, edited=True)
