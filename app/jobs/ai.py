from __future__ import annotations

import json
import logging
from datetime import timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import ReplyParameters

from app.ai.knowledge import KnowledgeIndex, KnowledgeIngestionService
from app.ai.repositories import AiRepository
from app.ai.worker import CodexCliWorker, redact_debug_data
from app.bot.handlers_chat_member import onboarding_greeting
from app.services.admin_notify import notify_admins
from app.config import Settings
from app.db.connection import open_database
from app.utils.datetime import datetime_to_iso, utc_now
from app.utils.datetime import iso_to_datetime


logger = logging.getLogger(__name__)


OUT_OF_SCOPE_REPLY = (
    "Я отвечаю только по утверждённой базе канала: психология, тренировки, "
    "питание и БАДы. По этому вопросу помочь не смогу."
)
KNOWLEDGE_NOT_FOUND_REPLY = "В базе канала не найдено подходящего материала."


def _worker(settings: Settings) -> CodexCliWorker:
    return CodexCliWorker(
        settings.ai_worker_executable,
        settings.ai_worker_timeout_seconds,
        settings.ai_worker_model,
        settings.ai_worker_reasoning_effort,
        settings.ai_debug_logging,
    )


def _debug_log(settings: Settings, event: str, **fields: object) -> None:
    """Keep sensitive LLM diagnostics strictly behind AI_DEBUG_LOGGING."""
    if settings.ai_debug_logging:
        logger.info("%s", json.dumps(redact_debug_data({"event": event, **fields}), ensure_ascii=False, separators=(",", ":"), default=str))


async def _validated_reply_parameters(db, *, chat_id: int, retrieved, source_message_id: int | None, quote: str | None) -> tuple[ReplyParameters | None, str]:
    """Build a Telegram-native source reference only from this turn's hits."""
    if source_message_id is None:
        return None, "no_source_message_id"
    allowed_source_ids = {
        int(item.source_telegram_message_id)
        for item in retrieved
        if int(item.source_chat_id) == chat_id
    }
    if source_message_id not in allowed_source_ids:
        return None, "source_not_retrieved_for_chat"
    row = await (await db.execute(
        "SELECT text FROM telegram_messages WHERE chat_id = ? AND telegram_message_id = ?",
        (chat_id, source_message_id),
    )).fetchone()
    if row is None:
        return None, "source_message_not_found"
    if quote is None:
        return ReplyParameters(message_id=source_message_id, allow_sending_without_reply=True), "source_reply_valid"
    if not quote or len(quote) > 1024:
        return None, "quote_empty_or_too_long"
    char_position = row["text"].find(quote)
    if char_position < 0:
        return None, "quote_not_exact_substring"
    utf16_position = len(row["text"][:char_position].encode("utf-16-le")) // 2
    return ReplyParameters(
        message_id=source_message_id,
        allow_sending_without_reply=True,
        quote=quote,
        quote_position=utf16_position,
    ), "native_quote_valid"


async def _send_response(bot: Bot, chat_id: int, text: str, reply_parameters: ReplyParameters | None, *, debug_logging: bool = False):
    """A rejected native quote must not make a valid answer/turn retry forever."""
    if reply_parameters is None:
        return await bot.send_message(chat_id, text), None, "sent_without_reference"
    try:
        return await bot.send_message(chat_id, text, reply_parameters=reply_parameters), reply_parameters.message_id, "sent_with_native_reference"
    except TelegramBadRequest:
        if debug_logging:
            logger.info("%s", json.dumps({"event": "answer.reference_delivery", "chat_id": chat_id, "result": "sent_without_reference", "reason": "telegram_bad_request"}, separators=(",", ":")))
        return await bot.send_message(chat_id, text), None, "telegram_bad_request_fallback"


async def _persist_successful_response(
    repo: AiRepository,
    *,
    turn,
    onboarding_pending: bool,
    text: str,
    sent,
    reply_to_telegram_message_id: int | None,
    session_active: bool,
    settings: Settings,
    status: str,
    retrieved_count: int,
) -> None:
    if sent is not None and hasattr(sent, "message_id"):
        await repo.store_message(
            chat_id=turn["chat_id"],
            telegram_message_id=sent.message_id,
            sender_telegram_user_id=getattr(getattr(sent, "from_user", None), "id", None),
            sender_chat_id=None,
            direction="outgoing",
            message_kind="text",
            text=text,
            reply_to_telegram_message_id=reply_to_telegram_message_id,
            created_at=getattr(sent, "date", None),
        )
    await repo.set_state(
        turn["chat_id"],
        turn["telegram_user_id"],
        "assistant.session",
        {
            "active": session_active,
            "expires_at": datetime_to_iso(utc_now() + timedelta(seconds=settings.ai_session_timeout_seconds)) if session_active else None,
        },
    )
    await repo.finish_turn(turn["id"])
    if onboarding_pending:
        await repo.set_state(turn["chat_id"], turn["telegram_user_id"], "onboarding.completed", {"turn_id": turn["id"]})
    await repo.audit(
        "send",
        status,
        chat_id=turn["chat_id"],
        telegram_user_id=turn["telegram_user_id"],
        turn_id=turn["id"],
        counts={"retrieved": retrieved_count},
    )


async def process_knowledge_candidates(settings: Settings) -> None:
    if not settings.ai_enabled:
        return
    async with open_database(settings.database_path) as db:
        repo = AiRepository(db)
        for candidate in await repo.pending_candidates():
            try:
                decision = await _worker(settings).classify(candidate["text"])
                _debug_log(
                    settings,
                    "classify.decision",
                    message_row_id=candidate["telegram_message_row_id"],
                    decision=decision.decision,
                    reason=decision.reason,
                )
                if decision.decision == "include":
                    count = await KnowledgeIngestionService(repo, KnowledgeIndex(db, cache_dir=settings.ai_embedding_cache_dir)).include(candidate["telegram_message_row_id"], candidate["text"], decision.reason, "codex-cli-json-v2")
                    await repo.audit("classify", "include", message_row_id=candidate["telegram_message_row_id"], counts={"chunks": count})
                else:
                    await repo.set_candidate_result(candidate["telegram_message_row_id"], state=decision.decision, reason=decision.reason, profile="codex-cli-json-v2")
                    await repo.audit("classify", decision.decision, message_row_id=candidate["telegram_message_row_id"])
            except Exception as exc:
                _debug_log(
                    settings,
                    "classify.error",
                    message_row_id=candidate["telegram_message_row_id"],
                    error_type=type(exc).__name__,
                )
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
                _debug_log(
                    settings,
                    "route.decision",
                    chat_id=turn["chat_id"],
                    telegram_user_id=turn["telegram_user_id"],
                    turn_id=turn["id"],
                    response_mode=route.effective_response_mode,
                    should_respond=route.should_respond,
                    needs_search=route.needs_search,
                    escalate=route.escalate,
                    session_active=route.session_active,
                    reason=route.reason,
                )
                await repo.audit("router", "ok", chat_id=turn["chat_id"], telegram_user_id=turn["telegram_user_id"], turn_id=turn["id"])
                await db.commit()
                if route.escalate:
                    excerpt = question.replace("\n", " ")[:240]
                    await notify_admins(settings, bot, f"⚠️ AI escalation: chat={turn['chat_id']} user={turn['telegram_user_id']} turn={turn['id']}\n{excerpt}")
                    await repo.audit("router", "escalated", chat_id=turn["chat_id"], telegram_user_id=turn["telegram_user_id"], turn_id=turn["id"])
                    await db.commit()
                # response_mode is the authoritative decision. should_respond
                # remains in the schema for backward-compatible telemetry.
                if route.effective_response_mode == "no_response":
                    _debug_log(
                        settings,
                        "turn.silenced",
                        chat_id=turn["chat_id"],
                        telegram_user_id=turn["telegram_user_id"],
                        turn_id=turn["id"],
                        reason="router_no_response",
                        router_reason=route.reason,
                    )
                    await repo.set_state(turn["chat_id"], turn["telegram_user_id"], "assistant.session", {"active": False})
                    await repo.finish_turn(turn["id"])
                    await db.commit()
                    continue
                if route.effective_response_mode == "out_of_scope":
                    _debug_log(
                        settings,
                        "turn.response_mode",
                        chat_id=turn["chat_id"],
                        telegram_user_id=turn["telegram_user_id"],
                        turn_id=turn["id"],
                        mode="out_of_scope",
                        reason=route.reason,
                    )
                    sent, reply_to, delivery_result = await _send_response(
                        bot,
                        turn["chat_id"],
                        OUT_OF_SCOPE_REPLY,
                        None,
                        debug_logging=settings.ai_debug_logging,
                    )
                    _debug_log(settings, "turn.delivery", turn_id=turn["id"], result=delivery_result)
                    await _persist_successful_response(
                        repo,
                        turn=turn,
                        onboarding_pending=onboarding_pending,
                        text=OUT_OF_SCOPE_REPLY,
                        sent=sent,
                        reply_to_telegram_message_id=reply_to,
                        session_active=False,
                        settings=settings,
                        status="out_of_scope",
                        retrieved_count=0,
                    )
                    await db.commit()
                    continue
                if not route.needs_search:
                    # A substantive reply without retrieval is never allowed,
                    # even if an invalid router result attempts to request it.
                    await repo.audit("router", "forced_search", chat_id=turn["chat_id"], telegram_user_id=turn["telegram_user_id"], turn_id=turn["id"])
                    _debug_log(
                        settings,
                        "route.forced_search",
                        chat_id=turn["chat_id"],
                        telegram_user_id=turn["telegram_user_id"],
                        turn_id=turn["id"],
                        reason="response_mode_answer_requires_retrieval",
                    )
                    await db.commit()
                retrieved = await KnowledgeIndex(db, cache_dir=settings.ai_embedding_cache_dir).search(question, top_k=settings.ai_retrieval_top_k, context_char_budget=settings.ai_retrieval_context_chars)
                _debug_log(
                    settings,
                    "retrieve.result",
                    chat_id=turn["chat_id"],
                    telegram_user_id=turn["telegram_user_id"],
                    turn_id=turn["id"],
                    retrieved_count=len(retrieved),
                    retrieved_source_telegram_message_ids=[item.source_telegram_message_id for item in retrieved],
                )
                await repo.audit("retrieve", "ok", chat_id=turn["chat_id"], telegram_user_id=turn["telegram_user_id"], turn_id=turn["id"], counts={"chunks": len(retrieved)})
                await db.commit()
                if not retrieved:
                    _debug_log(
                        settings,
                        "turn.response_mode",
                        chat_id=turn["chat_id"],
                        telegram_user_id=turn["telegram_user_id"],
                        turn_id=turn["id"],
                        mode="knowledge_not_found",
                        reason="no_retrieved_knowledge",
                    )
                    sent, reply_to, delivery_result = await _send_response(
                        bot,
                        turn["chat_id"],
                        KNOWLEDGE_NOT_FOUND_REPLY,
                        None,
                        debug_logging=settings.ai_debug_logging,
                    )
                    _debug_log(settings, "turn.delivery", turn_id=turn["id"], result=delivery_result)
                    await _persist_successful_response(
                        repo,
                        turn=turn,
                        onboarding_pending=onboarding_pending,
                        text=KNOWLEDGE_NOT_FOUND_REPLY,
                        sent=sent,
                        reply_to_telegram_message_id=reply_to,
                        session_active=False,
                        settings=settings,
                        status="not_found",
                        retrieved_count=0,
                    )
                    await db.commit()
                    continue
                answer = await _worker(settings).answer(question, [{"text": item.text, "source_message_id": item.source_telegram_message_id, "distance": item.distance} for item in retrieved], [{"onboarding_pending": onboarding_pending, "user_id": turn["telegram_user_id"], "messages": recent}])
                await repo.audit("agent", "ok", chat_id=turn["chat_id"], telegram_user_id=turn["telegram_user_id"], turn_id=turn["id"])
                await db.commit()
                reply_parameters, reference_validation = await _validated_reply_parameters(
                    db,
                    chat_id=turn["chat_id"],
                    retrieved=retrieved,
                    source_message_id=answer.source_message_id,
                    quote=answer.quote,
                )
                _debug_log(
                    settings,
                    "answer.reference_validation",
                    chat_id=turn["chat_id"],
                    telegram_user_id=turn["telegram_user_id"],
                    turn_id=turn["id"],
                    source_message_id=answer.source_message_id,
                    quote_present=answer.quote is not None,
                    quote_chars=len(answer.quote) if answer.quote is not None else 0,
                    result="valid" if reply_parameters is not None else "fallback_without_reference",
                    reason=reference_validation,
                )
                sent, reply_to, delivery_result = await _send_response(
                    bot,
                    turn["chat_id"],
                    answer.text,
                    reply_parameters,
                    debug_logging=settings.ai_debug_logging,
                )
                _debug_log(
                    settings,
                    "answer.delivery",
                    chat_id=turn["chat_id"],
                    telegram_user_id=turn["telegram_user_id"],
                    turn_id=turn["id"],
                    result=delivery_result,
                    reply_to_telegram_message_id=reply_to,
                )
                active = answer.session_active or route.session_active
                _debug_log(
                    settings,
                    "answer.session",
                    chat_id=turn["chat_id"],
                    telegram_user_id=turn["telegram_user_id"],
                    turn_id=turn["id"],
                    answer_session_active=answer.session_active,
                    route_session_active=route.session_active,
                    effective_session_active=active,
                )
                await _persist_successful_response(
                    repo,
                    turn=turn,
                    onboarding_pending=onboarding_pending,
                    text=answer.text,
                    sent=sent,
                    reply_to_telegram_message_id=reply_to,
                    session_active=active,
                    settings=settings,
                    status="ok",
                    retrieved_count=len(retrieved),
                )
            except Exception as exc:
                _debug_log(
                    settings,
                    "turn.retry",
                    chat_id=turn["chat_id"],
                    telegram_user_id=turn["telegram_user_id"],
                    turn_id=turn["id"],
                    error_type=type(exc).__name__,
                )
                await repo.retry_turn(turn["id"], type(exc).__name__, max_attempts=settings.ai_retry_max_attempts, base_seconds=settings.ai_retry_base_seconds)
                await repo.audit("turn", "retry", chat_id=turn["chat_id"], telegram_user_id=turn["telegram_user_id"], turn_id=turn["id"], safe_error=type(exc).__name__)
            await db.commit()
