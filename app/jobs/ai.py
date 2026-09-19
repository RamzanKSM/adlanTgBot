from __future__ import annotations

import logging
from datetime import timedelta

from aiogram import Bot

from app.ai.knowledge import KnowledgeIndex, KnowledgeIngestionService
from app.ai.repositories import AiRepository
from app.ai.worker import CodexCliWorker
from app.bot.handlers_chat_member import onboarding_greeting
from app.services.admin_notify import notify_admins
from app.config import Settings
from app.db.connection import open_database
from app.utils.datetime import datetime_to_iso, utc_now
from app.utils.datetime import iso_to_datetime


logger = logging.getLogger(__name__)


def _worker(settings: Settings) -> CodexCliWorker:
    return CodexCliWorker(
        settings.ai_worker_executable,
        settings.ai_worker_timeout_seconds,
        settings.ai_worker_model,
        settings.ai_worker_reasoning_effort,
    )


async def process_knowledge_candidates(settings: Settings) -> None:
    if not settings.ai_enabled:
        return
    async with open_database(settings.database_path) as db:
        repo = AiRepository(db)
        for candidate in await repo.pending_candidates():
            try:
                decision = await _worker(settings).classify(candidate["text"])
                if decision.decision == "include":
                    count = await KnowledgeIngestionService(repo, KnowledgeIndex(db, cache_dir=settings.ai_embedding_cache_dir)).include(candidate["telegram_message_row_id"], candidate["text"], decision.reason, "codex-cli-json-v2")
                    await repo.audit("classify", "include", message_row_id=candidate["telegram_message_row_id"], counts={"chunks": count})
                else:
                    await repo.set_candidate_result(candidate["telegram_message_row_id"], state=decision.decision, reason=decision.reason, profile="codex-cli-json-v2")
                    await repo.audit("classify", decision.decision, message_row_id=candidate["telegram_message_row_id"])
            except Exception as exc:
                await repo.fail_candidate(candidate["telegram_message_row_id"], "codex-cli-json-v2", type(exc).__name__, max_attempts=settings.ai_retry_max_attempts, base_seconds=settings.ai_retry_base_seconds)
                await repo.audit("classify", "retry", message_row_id=candidate["telegram_message_row_id"], safe_error=type(exc).__name__)
            await db.commit()


async def retry_pending_onboarding(settings: Settings, bot: Bot) -> None:
    if not settings.ai_enabled:
        return
    async with open_database(settings.database_path) as db:
        repo = AiRepository(db)
        rows = await db.execute_fetchall("SELECT chat_id, telegram_user_id, value_json FROM conversational_states WHERE state = 'onboarding.awaiting'")
        for row in rows:
            state = await repo.get_state(row["chat_id"], row["telegram_user_id"], "onboarding.awaiting")
            if not state or state.get("greeting_sent") or state.get("terminal_failed") or await repo.get_state(row["chat_id"], row["telegram_user_id"], "onboarding.completed") is not None:
                continue
            next_attempt = iso_to_datetime(state.get("next_attempt_at")) if isinstance(state.get("next_attempt_at"), str) else None
            if next_attempt and next_attempt > utc_now():
                continue
            greeting, entities = onboarding_greeting(row["telegram_user_id"], str(state.get("display_name") or "Участник"))
            try:
                sent = await bot.send_message(row["chat_id"], greeting, entities=entities)
            except Exception as exc:
                await repo.audit("send", "retry", chat_id=row["chat_id"], telegram_user_id=row["telegram_user_id"], safe_error=type(exc).__name__)
                attempts = int(state.get("attempts", 0)) + 1
                if attempts < settings.ai_retry_max_attempts:
                    delay = min(settings.ai_retry_base_seconds * (2 ** (attempts - 1)), 3600)
                    await repo.set_state(row["chat_id"], row["telegram_user_id"], "onboarding.awaiting", {**state, "attempts": attempts, "next_attempt_at": datetime_to_iso(utc_now() + timedelta(seconds=delay))})
                else:
                    await repo.set_state(row["chat_id"], row["telegram_user_id"], "onboarding.awaiting", {**state, "attempts": attempts, "next_attempt_at": None, "terminal_failed": True})
                await db.commit()
                continue
            if sent is not None and hasattr(sent, "message_id"):
                await repo.store_message(chat_id=row["chat_id"], telegram_message_id=sent.message_id, sender_telegram_user_id=getattr(getattr(sent, "from_user", None), "id", None), sender_chat_id=None, direction="outgoing", message_kind="text", text=greeting, reply_to_telegram_message_id=None, created_at=getattr(sent, "date", None))
            await repo.set_state(row["chat_id"], row["telegram_user_id"], "onboarding.awaiting", {**state, "greeting_sent": True})
            await db.commit()


async def process_due_ai_turns(settings: Settings, bot: Bot) -> None:
    if not settings.ai_enabled:
        return
    async with open_database(settings.database_path) as db:
        repo = AiRepository(db)
        turns = await repo.claim_due_turns(lease_seconds=settings.ai_processing_lease_seconds, max_attempts=settings.ai_retry_max_attempts)
        for turn in turns:
            try:
                message_rows = await db.execute_fetchall("SELECT tm.* FROM user_turn_messages utm JOIN telegram_messages tm ON tm.id = utm.telegram_message_row_id WHERE utm.turn_id = ? ORDER BY utm.id", (turn["id"],))
                question = "\n".join(row["text"] for row in message_rows)
                user = message_rows[-1] if message_rows else None
                onboarding_pending = await repo.get_state(turn["chat_id"], turn["telegram_user_id"], "onboarding.awaiting") is not None and await repo.get_state(turn["chat_id"], turn["telegram_user_id"], "onboarding.completed") is None
                recent = [{"sender_telegram_user_id": row["sender_telegram_user_id"], "author_display_name": row["author_display_name"], "telegram_message_id": row["telegram_message_id"], "created_at": row["created_at"], "direction": row["direction"], "text": row["text"]} for row in reversed(await repo.recent_messages(turn["chat_id"], settings.ai_recent_context_limit))]
                await db.commit()
                route = await _worker(settings).route(question, [{"onboarding_pending": onboarding_pending, "user": {"id": turn["telegram_user_id"], "username": user["author_username"] if user else None, "display_name": user["author_display_name"] if user else None}, "messages": recent}])
                await repo.audit("router", "ok", chat_id=turn["chat_id"], telegram_user_id=turn["telegram_user_id"], turn_id=turn["id"])
                await db.commit()
                if route.escalate:
                    excerpt = question.replace("\n", " ")[:240]
                    await notify_admins(settings, bot, f"⚠️ AI escalation: chat={turn['chat_id']} user={turn['telegram_user_id']} turn={turn['id']}\n{excerpt}")
                    await repo.audit("router", "escalated", chat_id=turn["chat_id"], telegram_user_id=turn["telegram_user_id"], turn_id=turn["id"])
                    await db.commit()
                if not route.should_respond:
                    await repo.set_state(turn["chat_id"], turn["telegram_user_id"], "assistant.session", {"active": False})
                    await repo.finish_turn(turn["id"])
                    await db.commit()
                    continue
                retrieved = []
                if route.needs_search:
                    retrieved = await KnowledgeIndex(db, cache_dir=settings.ai_embedding_cache_dir).search(question, top_k=settings.ai_retrieval_top_k, context_char_budget=settings.ai_retrieval_context_chars)
                    await repo.audit("retrieve", "ok", chat_id=turn["chat_id"], telegram_user_id=turn["telegram_user_id"], turn_id=turn["id"], counts={"chunks": len(retrieved)})
                    await db.commit()
                answer = await _worker(settings).answer(question, [{"text": item.text, "source_message_id": item.source_telegram_message_id, "distance": item.distance} for item in retrieved], [{"onboarding_pending": onboarding_pending, "user_id": turn["telegram_user_id"], "messages": recent}])
                await repo.audit("agent", "ok", chat_id=turn["chat_id"], telegram_user_id=turn["telegram_user_id"], turn_id=turn["id"])
                await db.commit()
                sent = await bot.send_message(turn["chat_id"], answer.text)
                if sent is not None and hasattr(sent, "message_id"):
                    await repo.store_message(chat_id=turn["chat_id"], telegram_message_id=sent.message_id, sender_telegram_user_id=getattr(getattr(sent, "from_user", None), "id", None), sender_chat_id=None, direction="outgoing", message_kind="text", text=answer.text, reply_to_telegram_message_id=None, created_at=getattr(sent, "date", None))
                active = answer.session_active or route.session_active
                await repo.set_state(turn["chat_id"], turn["telegram_user_id"], "assistant.session", {"active": active, "expires_at": datetime_to_iso(utc_now() + timedelta(seconds=settings.ai_session_timeout_seconds)) if active else None})
                await repo.finish_turn(turn["id"])
                if onboarding_pending:
                    await repo.set_state(turn["chat_id"], turn["telegram_user_id"], "onboarding.completed", {"turn_id": turn["id"]})
                await repo.audit("send", "ok", chat_id=turn["chat_id"], telegram_user_id=turn["telegram_user_id"], turn_id=turn["id"], counts={"retrieved": len(retrieved)})
            except Exception as exc:
                await repo.retry_turn(turn["id"], type(exc).__name__, max_attempts=settings.ai_retry_max_attempts, base_seconds=settings.ai_retry_base_seconds)
                await repo.audit("turn", "retry", chat_id=turn["chat_id"], telegram_user_id=turn["telegram_user_id"], turn_id=turn["id"], safe_error=type(exc).__name__)
            await db.commit()
