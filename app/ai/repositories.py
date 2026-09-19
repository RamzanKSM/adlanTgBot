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


_INVOCATION_PRIORITY = {"ambient": 0, "active_session": 1, "onboarding": 2, "mention": 3, "reply_to_bot": 4}


def strongest_invocation(existing_kind: str | None, incoming_kind: str | None) -> str:
    """Keep the strongest signal across a debounced or merged turn."""
    candidates = (existing_kind or "ambient", incoming_kind or "ambient")
    return max(candidates, key=lambda kind: _INVOCATION_PRIORITY.get(kind, 0))


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

    async def schedule_turn(
        self, chat_id: int, telegram_user_id: int, message_row_id: int, debounce_seconds: int,
        *, invocation_kind: str = "ambient", invocation_explicit: bool = False,
    ) -> int:
        """Atomically extend only this user's pending turn and link its message."""
        now = utc_now()
        due_at = now + timedelta(seconds=debounce_seconds)
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            row = await (await self.db.execute(
                "SELECT id, invocation_kind, invocation_explicit FROM user_turns WHERE chat_id = ? AND telegram_user_id = ? AND status = 'pending'",
                (chat_id, telegram_user_id),
            )).fetchone()
            if row is None:
                cursor = await self.db.execute(
                    """INSERT INTO user_turns (chat_id, telegram_user_id, status, due_at, invocation_kind,
                       invocation_explicit, created_at, updated_at)
                       VALUES (?, ?, 'pending', ?, ?, ?, ?, ?)""",
                    (chat_id, telegram_user_id, datetime_to_iso(due_at), invocation_kind, int(invocation_explicit),
                     datetime_to_iso(now), datetime_to_iso(now)),
                )
                turn_id = int(cursor.lastrowid)
            else:
                turn_id = int(row["id"])
                merged_kind = strongest_invocation(row["invocation_kind"], invocation_kind)
                merged_explicit = bool(row["invocation_explicit"]) or invocation_explicit
                await self.db.execute(
                    "UPDATE user_turns SET due_at = ?, invocation_kind = ?, invocation_explicit = ?, updated_at = ? WHERE id = ?",
                    (datetime_to_iso(due_at), merged_kind, int(merged_explicit), datetime_to_iso(now), turn_id),
                )
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
            expired = await self.db.execute_fetchall(
                "SELECT id FROM user_turns WHERE status = 'processing' AND lease_until IS NOT NULL AND lease_until <= ? AND attempts < ? ORDER BY id",
                (now, max_attempts),
            )
            for expired_turn in expired:
                await self._requeue_processing_locked(
                    int(expired_turn["id"]), next_attempt_at=now, attempts=None, quota_reserved=None, quota_deferred=None,
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

    async def reserve_turn_quota(self, turn, *, max_turns: int, window_seconds: int) -> tuple[bool, datetime, bool]:
        """Reserve one user batch before any LLM call under SQLite's writer lock.

        A retry of a claimed batch has ``quota_reserved`` already set and must
        not consume a second admission.
        """
        now = utc_now()
        now_iso = datetime_to_iso(now)
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            fresh = await (await self.db.execute("SELECT quota_reserved FROM user_turns WHERE id = ?", (turn["id"],))).fetchone()
            if fresh is None:
                raise RuntimeError("turn disappeared before quota reservation")
            if int(fresh["quota_reserved"]):
                quota = await (await self.db.execute(
                    "SELECT window_ends_at FROM ai_user_quotas WHERE chat_id = ? AND telegram_user_id = ?",
                    (turn["chat_id"], turn["telegram_user_id"]),
                )).fetchone()
                if quota is None:
                    raise RuntimeError("reserved turn has no quota state")
                result = (True, iso_to_datetime(quota["window_ends_at"]) or now, False)
            else:
                quota = await (await self.db.execute(
                    "SELECT * FROM ai_user_quotas WHERE chat_id = ? AND telegram_user_id = ?",
                    (turn["chat_id"], turn["telegram_user_id"]),
                )).fetchone()
                if quota is None or (iso_to_datetime(quota["window_ends_at"]) or now) <= now:
                    window_end = now + timedelta(seconds=window_seconds)
                    await self.db.execute(
                        """INSERT INTO ai_user_quotas (chat_id, telegram_user_id, window_started_at, window_ends_at, admitted_turns, notice_sent)
                           VALUES (?, ?, ?, ?, 0, 0)
                           ON CONFLICT(chat_id, telegram_user_id) DO UPDATE SET
                             window_started_at = excluded.window_started_at, window_ends_at = excluded.window_ends_at,
                             admitted_turns = 0, notice_sent = 0""",
                        (turn["chat_id"], turn["telegram_user_id"], now_iso, datetime_to_iso(window_end)),
                    )
                    admitted = 0
                    notice_sent = False
                else:
                    window_end = iso_to_datetime(quota["window_ends_at"]) or now
                    admitted = int(quota["admitted_turns"])
                    notice_sent = bool(quota["notice_sent"])
                if admitted >= max_turns:
                    notify = not notice_sent
                    if notify:
                        await self.db.execute(
                            "UPDATE ai_user_quotas SET notice_sent = 1 WHERE chat_id = ? AND telegram_user_id = ?",
                            (turn["chat_id"], turn["telegram_user_id"]),
                        )
                    result = (False, window_end, notify)
                else:
                    await self.db.execute(
                        "UPDATE ai_user_quotas SET admitted_turns = admitted_turns + 1 WHERE chat_id = ? AND telegram_user_id = ?",
                        (turn["chat_id"], turn["telegram_user_id"]),
                    )
                    await self.db.execute("UPDATE user_turns SET quota_reserved = 1, updated_at = ? WHERE id = ?", (now_iso, turn["id"]))
                    result = (True, window_end, False)
        except BaseException:
            await self.db.rollback()
            raise
        else:
            await self.db.commit()
        return result

    async def defer_turn_for_quota(self, turn_id: int, window_ends_at: datetime) -> None:
        """Leave all linked text pending and undo the claim's retry accounting."""
        owns_transaction = not self.db.in_transaction
        if owns_transaction:
            await self.db.execute("BEGIN IMMEDIATE")
        try:
            row = await (await self.db.execute("SELECT attempts FROM user_turns WHERE id = ?", (turn_id,))).fetchone()
            attempts = max(0, int(row["attempts"]) - 1) if row else 0
            await self._requeue_processing_locked(
                turn_id, next_attempt_at=datetime_to_iso(window_ends_at), attempts=attempts, quota_reserved=0, quota_deferred=1,
            )
        except BaseException:
            if owns_transaction:
                await self.db.rollback()
            raise
        else:
            if owns_transaction:
                await self.db.commit()

    async def _requeue_processing_locked(
        self, turn_id: int, *, next_attempt_at: str | None, attempts: int | None,
        quota_reserved: int | None, quota_deferred: int | None,
    ) -> aiosqlite.Row:
        """Merge a concurrent pending turn before publishing this turn as pending.

        The caller owns ``BEGIN IMMEDIATE``.  Keeping cursor fields on the
        processing row makes partial-batch progress authoritative while moved
        links receive new ids after existing links in chronological order.
        """
        current = await (await self.db.execute("SELECT * FROM user_turns WHERE id = ? AND status = 'processing'", (turn_id,))).fetchone()
        if current is None:
            raise RuntimeError("processing turn missing during requeue")
        pending = await (await self.db.execute(
            "SELECT * FROM user_turns WHERE chat_id = ? AND telegram_user_id = ? AND status = 'pending' ORDER BY id LIMIT 1",
            (current["chat_id"], current["telegram_user_id"]),
        )).fetchone()
        due_at = current["due_at"]
        invocation_kind = current["invocation_kind"]
        invocation_explicit = bool(current["invocation_explicit"])
        if pending is not None:
            await self.db.execute(
                """INSERT OR IGNORE INTO user_turn_messages (turn_id, telegram_message_row_id, created_at)
                   SELECT ?, telegram_message_row_id, created_at FROM user_turn_messages WHERE turn_id = ? ORDER BY id""",
                (turn_id, pending["id"]),
            )
            due_at = max(due_at, pending["due_at"])
            invocation_kind = strongest_invocation(invocation_kind, pending["invocation_kind"])
            invocation_explicit = invocation_explicit or bool(pending["invocation_explicit"])
            await self.db.execute("DELETE FROM user_turns WHERE id = ?", (pending["id"],))
        await self.db.execute(
            """UPDATE user_turns SET status = 'pending', due_at = ?, next_attempt_at = ?, lease_until = NULL,
               attempts = COALESCE(?, attempts), quota_reserved = COALESCE(?, quota_reserved),
               quota_deferred = COALESCE(?, quota_deferred), invocation_kind = ?, invocation_explicit = ?,
               last_error = NULL, updated_at = ? WHERE id = ?""",
            (due_at, next_attempt_at, attempts, quota_reserved, quota_deferred, invocation_kind,
             int(invocation_explicit), datetime_to_iso(utc_now()), turn_id),
        )
        return await (await self.db.execute("SELECT * FROM user_turns WHERE id = ?", (turn_id,))).fetchone()

    async def current_batch(self, turn_id: int, max_chars: int) -> tuple[list[dict[str, Any]], int | None, int]:
        """Return a chronological, lossless slice; text itself never exceeds max_chars."""
        turn = await (await self.db.execute(
            "SELECT cursor_message_link_id, cursor_char_offset FROM user_turns WHERE id = ?", (turn_id,)
        )).fetchone()
        if turn is None:
            raise RuntimeError("turn not found")
        cursor_link = turn["cursor_message_link_id"]
        offset = int(turn["cursor_char_offset"])
        rows = await self.db.execute_fetchall(
            """SELECT utm.id AS link_id, tm.telegram_message_id, tm.created_at, tm.text, tm.author_username,
                      tm.author_display_name, tm.message_thread_id
               FROM user_turn_messages utm JOIN telegram_messages tm ON tm.id = utm.telegram_message_row_id
               WHERE utm.turn_id = ? ORDER BY utm.id""", (turn_id,)
        )
        started = cursor_link is None
        remaining = max_chars
        batch: list[dict[str, Any]] = []
        last_link: int | None = None
        last_offset = 0
        for row in rows:
            if not started:
                if int(row["link_id"]) != int(cursor_link):
                    continue
                started = True
            start = offset if cursor_link is not None and int(row["link_id"]) == int(cursor_link) else 0
            text = row["text"]
            if start >= len(text):
                continue
            separator = "\n" if batch else ""
            available = remaining - len(separator)
            if available <= 0:
                break
            fragment = text[start:start + available]
            if not fragment:
                break
            batch.append({"link_id": int(row["link_id"]), "telegram_message_id": int(row["telegram_message_id"]),
                          "created_at": row["created_at"], "text": fragment, "start_offset": start,
                          "end_offset": start + len(fragment), "author_username": row["author_username"],
                          "author_display_name": row["author_display_name"], "message_thread_id": row["message_thread_id"]})
            remaining -= len(separator) + len(fragment)
            last_link, last_offset = int(row["link_id"]), start + len(fragment)
            if remaining <= 0:
                break
        return batch, last_link, last_offset

    async def finish_batch(self, turn_id: int, *, last_link_id: int | None, last_char_offset: int, debounce_seconds: int) -> bool:
        """Advance only delivered input; return whether the turn was requeued."""
        now = utc_now()
        now_iso = datetime_to_iso(now)
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            if last_link_id is None:
                await self.db.execute("UPDATE user_turns SET status = 'completed', lease_until = NULL, updated_at = ? WHERE id = ?", (now_iso, turn_id))
            else:
                source = await (await self.db.execute(
                    "SELECT tm.text FROM user_turn_messages utm JOIN telegram_messages tm ON tm.id = utm.telegram_message_row_id WHERE utm.id = ? AND utm.turn_id = ?",
                    (last_link_id, turn_id),
                )).fetchone()
                if source is None:
                    raise RuntimeError("batch cursor source vanished")
                later = await (await self.db.execute("SELECT 1 FROM user_turn_messages WHERE turn_id = ? AND id > ? LIMIT 1", (turn_id, last_link_id))).fetchone()
                has_remainder = last_char_offset < len(source["text"]) or later is not None
                pending = await (await self.db.execute(
                    "SELECT id, due_at, invocation_kind, invocation_explicit FROM user_turns WHERE chat_id = (SELECT chat_id FROM user_turns WHERE id = ?) AND telegram_user_id = (SELECT telegram_user_id FROM user_turns WHERE id = ?) AND status = 'pending'",
                    (turn_id, turn_id),
                )).fetchone()
                if has_remainder:
                    latest = await (await self.db.execute(
                        "SELECT tm.created_at FROM user_turn_messages utm JOIN telegram_messages tm ON tm.id = utm.telegram_message_row_id WHERE utm.turn_id = ? ORDER BY tm.created_at DESC, utm.id DESC LIMIT 1",
                        (turn_id,),
                    )).fetchone()
                    latest_due = (iso_to_datetime(latest["created_at"]) or now) + timedelta(seconds=debounce_seconds) if latest else now
                    due_at = datetime_to_iso(max(now, latest_due))
                    if pending is not None:
                        await self.db.execute(
                            "INSERT OR IGNORE INTO user_turn_messages (turn_id, telegram_message_row_id, created_at) SELECT ?, telegram_message_row_id, created_at FROM user_turn_messages WHERE turn_id = ? ORDER BY id",
                            (turn_id, pending["id"]),
                        )
                        due_at = max(due_at, pending["due_at"])
                        current = await (await self.db.execute(
                            "SELECT invocation_kind, invocation_explicit FROM user_turns WHERE id = ?", (turn_id,)
                        )).fetchone()
                        merged_kind = strongest_invocation(current["invocation_kind"] if current else None, pending["invocation_kind"])
                        merged_explicit = bool(current["invocation_explicit"]) if current else False
                        merged_explicit = merged_explicit or bool(pending["invocation_explicit"])
                        await self.db.execute("DELETE FROM user_turns WHERE id = ?", (pending["id"],))
                    else:
                        merged_kind, merged_explicit = None, None
                    await self.db.execute(
                        """UPDATE user_turns SET status = 'pending', due_at = ?, next_attempt_at = NULL, lease_until = NULL,
                           attempts = 0, quota_reserved = 0, quota_deferred = 0, cursor_message_link_id = ?, cursor_char_offset = ?,
                           invocation_kind = COALESCE(?, invocation_kind), invocation_explicit = COALESCE(?, invocation_explicit), updated_at = ? WHERE id = ?""",
                        (due_at, last_link_id, last_char_offset, merged_kind, int(merged_explicit) if merged_explicit is not None else None, now_iso, turn_id),
                    )
                    requeued = True
                else:
                    await self.db.execute(
                        "UPDATE user_turns SET status = 'completed', lease_until = NULL, quota_reserved = 0, quota_deferred = 0, updated_at = ? WHERE id = ?",
                        (now_iso, turn_id),
                    )
                    requeued = False
            if last_link_id is None:
                requeued = False
        except BaseException:
            await self.db.rollback()
            raise
        else:
            await self.db.commit()
        return requeued

    async def retry_turn(self, turn_id: int, error: str, *, max_attempts: int, base_seconds: int) -> None:
        owns_transaction = not self.db.in_transaction
        if owns_transaction:
            await self.db.execute("BEGIN IMMEDIATE")
        try:
            row = await (await self.db.execute("SELECT attempts FROM user_turns WHERE id = ?", (turn_id,))).fetchone()
            attempts = int(row["attempts"]) if row else max_attempts
            retry = attempts < max_attempts
            if retry:
                delay = min(base_seconds * (2 ** max(0, attempts - 1)), 3600)
                await self._requeue_processing_locked(
                    turn_id, next_attempt_at=datetime_to_iso(utc_now() + timedelta(seconds=delay)), attempts=attempts,
                    quota_reserved=None, quota_deferred=None,
                )
                await self.db.execute("UPDATE user_turns SET last_error = ? WHERE id = ?", (error[:300], turn_id))
            else:
                # Terminal failure does not compete with the separate pending
                # turn; it remains available for the next worker claim.
                await self.db.execute("UPDATE user_turns SET status = 'failed', lease_until = NULL, last_error = ?, updated_at = ? WHERE id = ?", (error[:300], datetime_to_iso(utc_now()), turn_id))
        except BaseException:
            if owns_transaction:
                await self.db.rollback()
            raise
        else:
            if owns_transaction:
                await self.db.commit()
