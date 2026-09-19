from __future__ import annotations

import json
import logging
from datetime import timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters

from app.ai.knowledge import KnowledgeIndex, KnowledgeIngestionService
from app.ai.repositories import AiRepository
from app.ai.worker import CodexCliWorker, redact_debug_data
from app.bot.handlers_chat_member import onboarding_greeting
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


def _turn_summary(settings: Settings, turn, batch: list[dict] | None, **fields: object) -> None:
    """One complete debug event per completed, deferred, or requeued batch."""
    summary = {
        "turn_id": turn["id"], "invocation_kind": turn["invocation_kind"],
        "invocation_explicit": bool(turn["invocation_explicit"]),
        "batch": [{"telegram_message_id": item["telegram_message_id"], "start_offset": item["start_offset"], "end_offset": item["end_offset"]} for item in (batch or [])],
        "response_mode": None, "search_query": None, "retrieved": [], "model_source_message_id": None,
        "effective_source_message_id": None, "source_validation": None,
        "quote_validation": None, "source_url_result": None, "delivery": None,
        "session_active": None, "requeued": False, "deferred": False,
    }
    summary.update(fields)
    _debug_log(settings, "turn.summary", **summary)


async def _effective_source(db, *, chat_id: int, retrieved, model_source_id: int) -> tuple[object | None, str]:
    allowed_source_ids = {
        int(item.source_telegram_message_id)
        for item in retrieved
        if int(item.source_chat_id) == chat_id
    }
    if not allowed_source_ids:
        return None, "no_retrieved_source_for_chat"
    ordered = [item for item in retrieved if int(item.source_chat_id) == chat_id]
    if model_source_id in allowed_source_ids:
        ordered.sort(key=lambda item: 0 if int(item.source_telegram_message_id) == model_source_id else 1)
    for hit in ordered:
        effective_id = int(hit.source_telegram_message_id)
        row = await (await db.execute(
            "SELECT text, message_thread_id FROM telegram_messages WHERE chat_id = ? AND telegram_message_id = ?",
            (chat_id, effective_id),
        )).fetchone()
        if row is not None:
            return (effective_id, row, hit), "model_source_valid" if effective_id == model_source_id else "model_source_replaced_with_best_hit"
    return None, "all_retrieved_sources_missing"


def _exact_quote(source_text: str, proposed: str | None, candidate: str) -> tuple[str | None, str]:
    if proposed and len(proposed) <= 1024 and proposed in source_text:
        return proposed, "model_quote_valid"
    fallback = candidate[:1024]
    if fallback and fallback in source_text:
        return fallback, "quote_candidate_fallback"
    return source_text[:1024] or None, "source_prefix_fallback"


def _source_url(chat, chat_id: int, message_id: int, message_thread_id: int | None) -> str | None:
    username = getattr(chat, "username", None)
    if isinstance(username, str) and username and username.replace("_", "a").isalnum():
        base = f"https://t.me/{username}/{message_id}"
    elif str(chat_id).startswith("-100"):
        base = f"https://t.me/c/{-chat_id - 1000000000000}/{message_id}"
    else:
        return None
    return f"{base}?single&thread={message_thread_id}" if message_thread_id is not None else base


async def _build_source_url(bot: Bot, *, chat_id: int, message_id: int, message_thread_id: int | None) -> tuple[str | None, str]:
    try:
        return _source_url(await bot.get_chat(chat_id), chat_id, message_id, message_thread_id), "source_url_built"
    except Exception as exc:
        return None, f"source_url_failed_{type(exc).__name__}"


async def _send_response(bot: Bot, chat_id: int, text: str, reply_parameters: ReplyParameters | None, *, reply_markup=None, fallback_reply: ReplyParameters | None = None, debug_logging: bool = False):
    """A rejected native quote must not make a valid answer/turn retry forever."""
    if reply_parameters is None:
        return await bot.send_message(chat_id, text, reply_markup=reply_markup), None, "sent_without_reference"
    try:
        return await bot.send_message(chat_id, text, reply_parameters=reply_parameters, reply_markup=reply_markup), reply_parameters.message_id, "sent_with_native_reference"
    except TelegramBadRequest:
        if debug_logging:
            logger.info("%s", json.dumps({"event": "answer.reference_delivery", "chat_id": chat_id, "result": "sent_without_reference", "reason": "telegram_bad_request"}, separators=(",", ":")))
        if fallback_reply is not None:
            try:
                return await bot.send_message(chat_id, text, reply_parameters=fallback_reply, reply_markup=reply_markup), fallback_reply.message_id, "telegram_bad_request_source_reply_fallback"
            except TelegramBadRequest:
                pass
        return await bot.send_message(chat_id, text, reply_markup=reply_markup), None, "telegram_bad_request_fallback"


async def _persist_successful_response(
    repo: AiRepository,
    *,
    turn,
    onboarding_pending: bool,
    text: str,
    sent,
    reply_to_telegram_message_id: int | None,
    session_active: bool,
    settings: Settings, last_link_id: int | None, last_char_offset: int,
    status: str,
    retrieved_count: int,
) -> bool:
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
    # finish_batch takes BEGIN IMMEDIATE to merge a concurrently scheduled
    # pending turn, so persist the delivered outgoing row/state first.
    await repo.db.commit()
    requeued = await repo.finish_batch(turn["id"], last_link_id=last_link_id, last_char_offset=last_char_offset, debounce_seconds=settings.ai_turn_debounce_seconds)
    if onboarding_pending:
        await repo.set_state(turn["chat_id"], turn["telegram_user_id"], "onboarding.completed", {"turn_id": turn["id"]})
    await repo.audit(
        "send",
        status,
        chat_id=turn["chat_id"],
        telegram_user_id=turn["telegram_user_id"],
        turn_id=turn["id"],
        counts={"retrieved": retrieved_count, "requeued": int(requeued)},
    )
    return requeued


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
            batch: list[dict] | None = None
            try:
                admitted, window_end, send_notice = await repo.reserve_turn_quota(
                    turn, max_turns=settings.ai_user_max_turns_per_window, window_seconds=settings.ai_user_limit_window_seconds,
                )
                if not admitted:
                    await repo.defer_turn_for_quota(turn["id"], window_end)
                    notice_result = "not_needed"
                    if send_notice:
                        try:
                            notice = "Я запомнил сообщения и вернусь к ним, когда восстановится лимит."
                            sent = await bot.send_message(turn["chat_id"], notice)
                            if sent is not None and hasattr(sent, "message_id"):
                                await repo.store_message(
                                    chat_id=turn["chat_id"], telegram_message_id=sent.message_id,
                                    sender_telegram_user_id=getattr(getattr(sent, "from_user", None), "id", None),
                                    sender_chat_id=None, direction="outgoing", message_kind="text", text=notice,
                                    reply_to_telegram_message_id=None, created_at=getattr(sent, "date", None),
                                )
                            notice_result = "sent"
                            _debug_log(settings, "turn.quota_notice", turn_id=turn["id"], result=notice_result)
                        except Exception as notice_error:
                            notice_result = "failed"
                            _debug_log(settings, "turn.quota_notice", turn_id=turn["id"], result=notice_result, error_type=type(notice_error).__name__)
                    fresh_turn = await (await db.execute("SELECT * FROM user_turns WHERE id = ?", (turn["id"],))).fetchone()
                    _turn_summary(settings, fresh_turn or turn, None, status="quota_deferred", deferred=True, deferred_until=datetime_to_iso(window_end), notice=notice_result)
                    await repo.audit("turn", "quota_deferred", chat_id=turn["chat_id"], telegram_user_id=turn["telegram_user_id"], turn_id=turn["id"])
                    await db.commit()
                    continue
                batch, last_link_id, last_char_offset = await repo.current_batch(turn["id"], settings.ai_deferred_batch_max_chars)
                if not batch:
                    await repo.finish_batch(turn["id"], last_link_id=None, last_char_offset=0, debounce_seconds=settings.ai_turn_debounce_seconds)
                    _turn_summary(settings, turn, None, status="empty_completed")
                    continue
                batch_debug = []
                for item in batch:
                    original = await (await db.execute(
                        "SELECT tm.text FROM user_turn_messages utm JOIN telegram_messages tm ON tm.id = utm.telegram_message_row_id WHERE utm.id = ?",
                        (item["link_id"],),
                    )).fetchone()
                    batch_debug.append({**item, "original_text": original["text"] if original else None})
                user = batch[-1]
                onboarding_pending = await repo.get_state(turn["chat_id"], turn["telegram_user_id"], "onboarding.awaiting") is not None and await repo.get_state(turn["chat_id"], turn["telegram_user_id"], "onboarding.completed") is None
                recent = [{"sender_telegram_user_id": row["sender_telegram_user_id"], "author_display_name": row["author_display_name"], "telegram_message_id": row["telegram_message_id"], "created_at": row["created_at"], "direction": row["direction"], "text": row["text"]} for row in reversed(await repo.recent_messages(turn["chat_id"], settings.ai_recent_context_limit))]
                await db.commit()
                trace = {"turn_id": turn["id"]}
                context = {"recent_group_context": recent, "user": {"id": turn["telegram_user_id"], "username": user["author_username"], "display_name": user["author_display_name"], "author_is_admin": turn["telegram_user_id"] in settings.admin_ids}, "invocation": {"kind": turn["invocation_kind"], "explicit": bool(turn["invocation_explicit"])}, "onboarding_pending": onboarding_pending, "deferred": bool(turn["cursor_message_link_id"]) or bool(turn["quota_deferred"]), "deferred_since": turn["created_at"] if (turn["cursor_message_link_id"] or turn["quota_deferred"]) else None}
                route = await _worker(settings).route(batch, context, trace=trace)
                _debug_log(
                    settings,
                    "route.decision",
                    chat_id=turn["chat_id"],
                    telegram_user_id=turn["telegram_user_id"],
                    turn_id=turn["id"],
                    response_mode=route.response_mode,
                    search_query=route.search_query,
                    reference_mode=route.reference_mode,
                    invocation_kind=turn["invocation_kind"],
                    invocation_explicit=bool(turn["invocation_explicit"]),
                    batch=batch_debug,
                    reason=route.reason,
                )
                await repo.audit("router", "ok", chat_id=turn["chat_id"], telegram_user_id=turn["telegram_user_id"], turn_id=turn["id"])
                await db.commit()
                target_reply = ReplyParameters(message_id=user["telegram_message_id"], allow_sending_without_reply=True)
                if route.response_mode == "no_response":
                    await repo.set_state(turn["chat_id"], turn["telegram_user_id"], "assistant.session", {"active": False})
                    await db.commit()
                    requeued = await repo.finish_batch(turn["id"], last_link_id=last_link_id, last_char_offset=last_char_offset, debounce_seconds=settings.ai_turn_debounce_seconds)
                    _turn_summary(settings, turn, batch, status="no_response", response_mode=route.response_mode, session_active=False, requeued=requeued)
                    await db.commit()
                    continue
                if route.response_mode in {"conversation", "out_of_scope"}:
                    text = OUT_OF_SCOPE_REPLY if route.response_mode == "out_of_scope" else await _worker(settings).converse(batch, context, trace=trace)
                    sent, reply_to, delivery_result = await _send_response(bot, turn["chat_id"], text, target_reply, debug_logging=settings.ai_debug_logging)
                    requeued = await _persist_successful_response(repo, turn=turn, onboarding_pending=onboarding_pending, text=text, sent=sent, reply_to_telegram_message_id=reply_to, session_active=route.response_mode == "conversation", settings=settings, status=route.response_mode, retrieved_count=0, last_link_id=last_link_id, last_char_offset=last_char_offset)
                    _turn_summary(settings, turn, batch, status=route.response_mode, response_mode=route.response_mode, delivery=delivery_result, session_active=route.response_mode == "conversation", requeued=requeued)
                    await db.commit()
                    continue
                query = (route.search_query or "").strip()
                if not query:
                    retrieved = []
                else:
                    retrieved = await KnowledgeIndex(db, cache_dir=settings.ai_embedding_cache_dir).search(query, top_k=settings.ai_retrieval_top_k, context_char_budget=settings.ai_retrieval_context_chars)
                _debug_log(
                    settings,
                    "retrieve.result",
                    chat_id=turn["chat_id"],
                    telegram_user_id=turn["telegram_user_id"],
                    turn_id=turn["id"],
                    retrieved_count=len(retrieved),
                    search_query=query,
                    sources=[{"id": item.source_telegram_message_id, "distance": item.distance} for item in retrieved],
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
                    sent, reply_to, delivery_result = await _send_response(bot, turn["chat_id"], KNOWLEDGE_NOT_FOUND_REPLY, target_reply, debug_logging=settings.ai_debug_logging)
                    requeued = await _persist_successful_response(repo, turn=turn, onboarding_pending=onboarding_pending, text=KNOWLEDGE_NOT_FOUND_REPLY, sent=sent, reply_to_telegram_message_id=reply_to, session_active=False, settings=settings, status="not_found", retrieved_count=0, last_link_id=last_link_id, last_char_offset=last_char_offset)
                    _turn_summary(settings, turn, batch, status="not_found", response_mode=route.response_mode, search_query=query or None, delivery=delivery_result, session_active=False, requeued=requeued)
                    await db.commit()
                    continue
                answer = await _worker(settings).answer(batch, [{"text": item.text, "quote_candidate": item.quote_candidate, "source_message_id": item.source_telegram_message_id, "distance": item.distance} for item in retrieved], {**context, "reference_mode": route.reference_mode}, trace=trace)
                await repo.audit("agent", "ok", chat_id=turn["chat_id"], telegram_user_id=turn["telegram_user_id"], turn_id=turn["id"])
                await db.commit()
                effective, source_validation = await _effective_source(db, chat_id=turn["chat_id"], retrieved=retrieved, model_source_id=answer.source_message_id)
                if effective is None:
                    raise RuntimeError("retrieved source vanished")
                source_id, source_row, hit = effective
                source_url, url_result = await _build_source_url(bot, chat_id=turn["chat_id"], message_id=source_id, message_thread_id=source_row["message_thread_id"])
                source_reply = ReplyParameters(message_id=source_id, allow_sending_without_reply=True)
                if route.reference_mode == "quote":
                    quote, reference_validation = _exact_quote(source_row["text"], answer.quote, hit.quote_candidate)
                    position = len(source_row["text"][:source_row["text"].find(quote)].encode("utf-16-le")) // 2 if quote else 0
                    reply_parameters = ReplyParameters(message_id=source_id, allow_sending_without_reply=True, quote=quote, quote_position=position) if quote else source_reply
                    reply_markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Источник", url=source_url)]]) if source_url else None
                    fallback_reply = source_reply
                else:
                    reference_validation = "link_source" if route.reference_mode == "link" else "no_requested_reference"
                    reply_parameters = target_reply if source_url else source_reply
                    reply_markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Источник", url=source_url)]]) if source_url else None
                    fallback_reply = source_reply if source_url else None
                _debug_log(
                    settings,
                    "answer.reference_validation",
                    chat_id=turn["chat_id"],
                    telegram_user_id=turn["telegram_user_id"],
                    turn_id=turn["id"],
                    model_source_message_id=answer.source_message_id,
                    effective_source_message_id=source_id,
                    quote_present=answer.quote is not None,
                    quote_chars=len(answer.quote) if answer.quote is not None else 0,
                    result=source_validation,
                    quote_validation=reference_validation,
                    source_url_result=url_result,
                )
                sent, reply_to, delivery_result = await _send_response(
                    bot,
                    turn["chat_id"],
                    answer.text,
                    reply_parameters,
                    reply_markup=reply_markup,
                    fallback_reply=fallback_reply,
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
                requeued = await _persist_successful_response(
                    repo,
                    turn=turn,
                    onboarding_pending=onboarding_pending,
                    text=answer.text,
                    sent=sent,
                    reply_to_telegram_message_id=reply_to,
                    session_active=True,
                    settings=settings,
                    status="ok",
                    retrieved_count=len(retrieved),
                    last_link_id=last_link_id,
                    last_char_offset=last_char_offset,
                )
                _turn_summary(settings, turn, batch, status="knowledge_answer", response_mode=route.response_mode, search_query=query, retrieved=[{"id": item.source_telegram_message_id, "distance": item.distance} for item in retrieved], model_source_message_id=answer.source_message_id, effective_source_message_id=source_id, source_validation=source_validation, quote_validation=reference_validation, source_url_result=url_result, delivery=delivery_result, session_active=True, requeued=requeued)
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
                _turn_summary(settings, turn, batch, status="retry", error_type=type(exc).__name__, deferred=True)
            await db.commit()
