from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import aiosqlite

from app.utils.datetime import datetime_to_iso, iso_to_datetime, utc_now
from app.utils.json import dumps_compact, loads_object


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class StoredMessage:
    id: int
    chat_id: int
    telegram_message_id: int
    sender_telegram_user_id: int | None
    text: str
    created_at: datetime
    changed: bool


@dataclass(slots=True)
class KnowledgeChunk:
    id: int
    message_row_id: int
    ordinal: int
    embedding_text: str
    token_count: int


class AiRepository:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def store_message(
        self,
        *,
        chat_id: int,
        telegram_message_id: int,
        sender_telegram_user_id: int | None,
        sender_chat_id: int | None,
        direction: str,
        message_kind: str,
        text: str,
        reply_to_telegram_message_id: int | None,
        author_username: str | None = None,
        author_first_name: str | None = None,
        author_last_name: str | None = None,
        author_display_name: str | None = None,
        message_thread_id: int | None = None,
        created_at: datetime | None = None,
        edited_at: datetime | None = None,
    ) -> StoredMessage:
        now = created_at or utc_now()
        now_iso = datetime_to_iso(now)
        digest = content_hash(text)
        before = await (await self.db.execute(
            "SELECT content_hash FROM telegram_messages WHERE chat_id = ? AND telegram_message_id = ?",
            (chat_id, telegram_message_id),
        )).fetchone()
        await self.db.execute(
            """
            INSERT INTO telegram_messages (
                chat_id, telegram_message_id, sender_telegram_user_id, sender_chat_id,
                direction, message_kind, text, reply_to_telegram_message_id, created_at, content_hash,
                author_username, author_first_name, author_last_name, author_display_name, message_thread_id, edited_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, telegram_message_id) DO UPDATE SET
                sender_telegram_user_id = excluded.sender_telegram_user_id,
                sender_chat_id = excluded.sender_chat_id,
                direction = excluded.direction,
                message_kind = excluded.message_kind,
                reply_to_telegram_message_id = excluded.reply_to_telegram_message_id,
                text = excluded.text,
                content_hash = excluded.content_hash,
                author_username = excluded.author_username, author_first_name = excluded.author_first_name,
                author_last_name = excluded.author_last_name, author_display_name = excluded.author_display_name,
                message_thread_id = excluded.message_thread_id,
                edited_at = CASE WHEN telegram_messages.content_hash != excluded.content_hash THEN excluded.edited_at ELSE telegram_messages.edited_at END
            """,
            (chat_id, telegram_message_id, sender_telegram_user_id, sender_chat_id, direction, message_kind,
             text, reply_to_telegram_message_id, now_iso, digest, author_username, author_first_name,
             author_last_name, author_display_name, message_thread_id, datetime_to_iso(edited_at) if edited_at else None),
        )
        row = await (await self.db.execute(
            "SELECT id, chat_id, telegram_message_id, sender_telegram_user_id, text, created_at "
            "FROM telegram_messages WHERE chat_id = ? AND telegram_message_id = ?",
            (chat_id, telegram_message_id),
        )).fetchone()
        if row is None:
            raise RuntimeError("failed to store Telegram message")
        return StoredMessage(row["id"], row["chat_id"], row["telegram_message_id"], row["sender_telegram_user_id"], row["text"], iso_to_datetime(row["created_at"]) or now, before is None or before["content_hash"] != digest)

    async def invalidate_knowledge(self, message_row_id: int) -> None:
        # Delete vec rows before relational chunks. A connection without the
        # extension can still safely invalidate relational state; join-based
        # retrieval never exposes an orphan and the next vector connection
        # performs corrective cleanup.
        table = await (await self.db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'knowledge_chunk_vectors'")).fetchone()
        if table:
            try:
                await self.db.execute("DELETE FROM knowledge_chunk_vectors WHERE chunk_id IN (SELECT id FROM knowledge_chunks WHERE telegram_message_row_id = ?)", (message_row_id,))
            except Exception:
                pass
        await self.db.execute("DELETE FROM knowledge_chunks WHERE telegram_message_row_id = ?", (message_row_id,))
        await self.db.execute(
            """UPDATE knowledge_messages SET candidate_state = 'pending', classification_reason = NULL,
                   classifier_profile = NULL, classified_at = NULL, last_error = NULL, updated_at = ?
               WHERE telegram_message_row_id = ?""",
            (datetime_to_iso(utc_now()), message_row_id),
        )

    async def make_knowledge_candidate(self, message_row_id: int) -> None:
        now = datetime_to_iso(utc_now())
        await self.db.execute(
            """INSERT INTO knowledge_messages (telegram_message_row_id, candidate_state, updated_at)
               VALUES (?, 'pending', ?)
               ON CONFLICT(telegram_message_row_id) DO UPDATE SET
                   candidate_state = 'pending', classification_reason = NULL, classifier_profile = NULL,
                   classified_at = NULL, last_error = NULL, next_attempt_at = NULL, updated_at = excluded.updated_at""",
            (message_row_id, now),
        )

    async def pending_candidates(self, limit: int = 20) -> list[aiosqlite.Row]:
        now = datetime_to_iso(utc_now())
        return await self.db.execute_fetchall(
            """SELECT km.*, tm.text, tm.chat_id, tm.telegram_message_id, tm.sender_telegram_user_id
               FROM knowledge_messages km JOIN telegram_messages tm ON tm.id = km.telegram_message_row_id
               WHERE (km.candidate_state = 'pending' OR (km.candidate_state = 'failed' AND km.next_attempt_at <= ?))
               ORDER BY km.updated_at, km.telegram_message_row_id LIMIT ?""",
            (now, limit),
        )

    async def set_candidate_result(
        self, message_row_id: int, *, state: str, reason: str | None, profile: str, error: str | None = None
    ) -> None:
        if state not in {"include", "exclude", "review", "failed"}:
            raise ValueError("invalid candidate state")
        now = datetime_to_iso(utc_now())
        await self.db.execute(
            """UPDATE knowledge_messages SET candidate_state = ?, classification_reason = ?, classifier_profile = ?,
                   classifier_attempts = classifier_attempts + 1, classified_at = ?, last_error = ?, next_attempt_at = NULL, updated_at = ?
               WHERE telegram_message_row_id = ?""",
            (state, reason, profile, now if state != "failed" else None, error, now, message_row_id),
        )

    async def fail_candidate(self, message_row_id: int, profile: str, error: str, *, max_attempts: int, base_seconds: int) -> None:
        row = await (await self.db.execute("SELECT classifier_attempts FROM knowledge_messages WHERE telegram_message_row_id = ?", (message_row_id,))).fetchone()
        attempts = int(row["classifier_attempts"]) + 1 if row else 1
        delay = min(base_seconds * (2 ** max(0, attempts - 1)), 3600)
        next_at = datetime_to_iso(utc_now() + timedelta(seconds=delay)) if attempts < max_attempts else None
        await self.db.execute(
            """UPDATE knowledge_messages SET candidate_state = 'failed', classifier_profile = ?, classifier_attempts = ?,
               last_error = ?, next_attempt_at = ?, updated_at = ? WHERE telegram_message_row_id = ?""",
            (profile, attempts, error[:300], next_at, datetime_to_iso(utc_now()), message_row_id),
        )

    async def replace_chunks(self, message_row_id: int, chunks: list[dict[str, Any]]) -> list[KnowledgeChunk]:
        await self.db.execute("DELETE FROM knowledge_chunks WHERE telegram_message_row_id = ?", (message_row_id,))
        now = datetime_to_iso(utc_now())
        for ordinal, chunk in enumerate(chunks):
            await self.db.execute(
                """INSERT INTO knowledge_chunks (telegram_message_row_id, ordinal, raw_start_char, raw_end_char,
                   raw_start_utf16, raw_end_utf16, embedding_text, token_count, content_hash, chunk_profile, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (message_row_id, ordinal, chunk["start_char"], chunk["end_char"], chunk["start_utf16"], chunk["end_utf16"],
                 chunk["embedding_text"], chunk["token_count"], content_hash(chunk["embedding_text"]), chunk["profile"], now),
            )
        rows = await self.db.execute_fetchall(
            "SELECT id, telegram_message_row_id, ordinal, embedding_text, token_count FROM knowledge_chunks WHERE telegram_message_row_id = ? ORDER BY ordinal",
            (message_row_id,),
        )
        return [KnowledgeChunk(row["id"], row["telegram_message_row_id"], row["ordinal"], row["embedding_text"], row["token_count"]) for row in rows]

    async def recent_messages(self, chat_id: int, limit: int) -> list[aiosqlite.Row]:
        return await self.db.execute_fetchall(
            """SELECT * FROM telegram_messages WHERE chat_id = ? AND deleted_at IS NULL
               ORDER BY created_at DESC, id DESC LIMIT ?""", (chat_id, limit)
        )

    async def audit(self, event_type: str, status: str, *, chat_id: int | None = None,
                    telegram_user_id: int | None = None, message_row_id: int | None = None,
                    turn_id: int | None = None, counts: dict[str, int] | None = None,
                    duration_ms: int | None = None, safe_error: str | None = None) -> None:
        await self.db.execute(
            """INSERT INTO ai_audit_events (event_type, status, chat_id, telegram_user_id, telegram_message_row_id,
               turn_id, counts_json, duration_ms, safe_error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_type, status, chat_id, telegram_user_id, message_row_id, turn_id, dumps_compact(counts or {}),
             duration_ms, safe_error[:300] if safe_error else None, datetime_to_iso(utc_now())),
        )

    async def set_state(self, chat_id: int, telegram_user_id: int, state: str, value: dict[str, Any] | None = None) -> None:
        now = datetime_to_iso(utc_now())
        await self.db.execute(
            """INSERT INTO conversational_states (chat_id, telegram_user_id, state, value_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(chat_id, telegram_user_id, state) DO UPDATE SET value_json = excluded.value_json,
                   version = conversational_states.version + 1, updated_at = excluded.updated_at""",
            (chat_id, telegram_user_id, state, dumps_compact(value or {}), now, now),
        )

    async def get_state(self, chat_id: int, telegram_user_id: int, state: str) -> dict[str, Any] | None:
        row = await (await self.db.execute(
            "SELECT value_json FROM conversational_states WHERE chat_id = ? AND telegram_user_id = ? AND state = ?",
            (chat_id, telegram_user_id, state),
        )).fetchone()
        return loads_object(row["value_json"]) if row else None

    async def active_session(self, chat_id: int, telegram_user_id: int) -> bool:
        state = await self.get_state(chat_id, telegram_user_id, "assistant.session")
        if not state or not state.get("active"):
            return False
        expires_at = iso_to_datetime(state.get("expires_at")) if isinstance(state.get("expires_at"), str) else None
        if expires_at is not None and expires_at > utc_now():
            return True
        await self.set_state(chat_id, telegram_user_id, "assistant.session", {"active": False})
        return False

    async def schedule_turn(self, chat_id: int, telegram_user_id: int, message_row_id: int, debounce_seconds: int) -> int:
        """Atomically extend only this user's pending turn and link its message."""
        now = utc_now()
        due_at = now + timedelta(seconds=debounce_seconds)
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            row = await (await self.db.execute(
                "SELECT id FROM user_turns WHERE chat_id = ? AND telegram_user_id = ? AND status = 'pending'",
                (chat_id, telegram_user_id),
            )).fetchone()
            if row is None:
                cursor = await self.db.execute(
                    """INSERT INTO user_turns (chat_id, telegram_user_id, status, due_at, created_at, updated_at)
                       VALUES (?, ?, 'pending', ?, ?, ?)""",
                    (chat_id, telegram_user_id, datetime_to_iso(due_at), datetime_to_iso(now), datetime_to_iso(now)),
                )
                turn_id = int(cursor.lastrowid)
            else:
                turn_id = int(row["id"])
                await self.db.execute("UPDATE user_turns SET due_at = ?, updated_at = ? WHERE id = ?", (datetime_to_iso(due_at), datetime_to_iso(now), turn_id))
            await self.db.execute(
                "INSERT OR IGNORE INTO user_turn_messages (turn_id, telegram_message_row_id, created_at) VALUES (?, ?, ?)",
                (turn_id, message_row_id, datetime_to_iso(now)),
            )
        except BaseException:
            await self.db.rollback()
            raise
        else:
            await self.db.commit()
        return turn_id

    async def claim_due_turns(self, *, lease_seconds: int = 180, max_attempts: int = 5, limit: int = 10) -> list[aiosqlite.Row]:
        """Claim due work in one writer transaction so only polling owns each turn."""
        current = utc_now()
        now = datetime_to_iso(current)
        lease_until = datetime_to_iso(current + timedelta(seconds=lease_seconds))
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            await self.db.execute(
                """UPDATE user_turns SET status = 'pending', next_attempt_at = ?, lease_until = NULL, updated_at = ?
                   WHERE status = 'processing' AND lease_until IS NOT NULL AND lease_until <= ? AND attempts < ?""",
                (now, now, now, max_attempts),
            )
            await self.db.execute(
                """UPDATE user_turns SET status = 'failed', lease_until = NULL, last_error = 'lease_expired', updated_at = ?
                   WHERE status = 'processing' AND lease_until IS NOT NULL AND lease_until <= ? AND attempts >= ?""",
                (now, now, max_attempts),
            )
            rows = await self.db.execute_fetchall(
                """SELECT * FROM user_turns WHERE status = 'pending' AND due_at <= ?
                   AND (next_attempt_at IS NULL OR next_attempt_at <= ?) AND attempts < ? ORDER BY due_at, id LIMIT ?""",
                (now, now, max_attempts, limit)
            )
            for row in rows:
                await self.db.execute("UPDATE user_turns SET status = 'processing', claimed_at = ?, lease_until = ?, attempts = attempts + 1, updated_at = ? WHERE id = ? AND status = 'pending'", (now, lease_until, now, row["id"]))
        except BaseException:
            await self.db.rollback()
            raise
        else:
            await self.db.commit()
        return rows

    async def finish_turn(self, turn_id: int) -> None:
        await self.db.execute("UPDATE user_turns SET status = 'completed', lease_until = NULL, last_error = NULL, updated_at = ? WHERE id = ?", (datetime_to_iso(utc_now()), turn_id))

    async def retry_turn(self, turn_id: int, error: str, *, max_attempts: int, base_seconds: int) -> None:
        row = await (await self.db.execute("SELECT attempts FROM user_turns WHERE id = ?", (turn_id,))).fetchone()
        attempts = int(row["attempts"]) if row else max_attempts
        delay = min(base_seconds * (2 ** max(0, attempts - 1)), 3600)
        retry = attempts < max_attempts
        await self.db.execute(
            """UPDATE user_turns SET status = ?, next_attempt_at = ?, lease_until = NULL, last_error = ?, updated_at = ? WHERE id = ?""",
            ("pending" if retry else "failed", datetime_to_iso(utc_now() + timedelta(seconds=delay)) if retry else None,
             error[:300], datetime_to_iso(utc_now()), turn_id),
        )
