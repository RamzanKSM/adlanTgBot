from datetime import timedelta
import os
from pathlib import Path

import pytest

from app.ai.chunking import RecursiveChunker, utf16_offset
from app.ai.repositories import AiRepository
from app.bot.handlers_ai import _is_bot_invocation, _persist
from app.bot.handlers_chat_member import onboarding_greeting
from app.ai.worker import ANSWER_SCHEMA, ROUTER_SCHEMA, CodexCliWorker, CLASSIFY_SCHEMA
from app.db.connection import connect_database
from app.db.migrations import run_migrations
from app.utils.datetime import utc_now


async def _repo(tmp_path):
    path = tmp_path / "ai.sqlite3"
    await run_migrations(str(path))
    db = await connect_database(path)
    return db, AiRepository(db)


async def test_ai_migration_creates_relational_state_without_vector_extension(tmp_path) -> None:
    db, _ = await _repo(tmp_path)
    try:
        tables = {row["name"] for row in await db.execute_fetchall("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        await db.close()
    assert {"telegram_messages", "knowledge_messages", "knowledge_chunks", "user_turns", "user_turn_messages", "conversational_states", "ai_audit_events"} <= tables
    assert "knowledge_chunk_vectors" not in tables


def test_chunker_preserves_raw_offsets_utf16_and_overlap() -> None:
    text = ("Первый абзац про Python и упражнения.\n\n" * 50) + "Финал 😊"
    chunks = RecursiveChunker().chunk(text)
    assert len(chunks) > 1
    assert chunks[0]["start_char"] == 0
    assert chunks[-1]["end_char"] <= len(text)
    assert all(0 < chunk["token_count"] <= 112 for chunk in chunks)
    assert all(text[chunk["start_char"]:chunk["end_char"]].strip() for chunk in chunks)
    emoji_start = text.index("😊")
    assert utf16_offset(text, emoji_start + 1) == utf16_offset(text, emoji_start) + 2
    assert chunks[1]["start_char"] < chunks[0]["end_char"]


@pytest.mark.skipif(__import__("os").environ.get("RUN_FASTEMBED_INTEGRATION") != "1", reason="uses real pinned FastEmbed tokenizer")
def test_real_offset_tokenizer_is_untruncated_and_chunks_cover_russian_emoji(tmp_path, monkeypatch) -> None:
    from app.ai.knowledge import FastEmbedder
    from app.ai.chunking import ChunkingProfile
    monkeypatch.chdir(tmp_path)
    cache = Path(os.environ.get("FASTEMBED_TEST_CACHE", str(tmp_path / "model-cache")))
    text = (("План обучения Python 😊 Практика и упражнения без структуры " * 90).strip())
    embedder = FastEmbedder(cache)
    spans = embedder.token_spans(text)
    assert len(spans) > 500
    assert spans[-1][1] == len(text.rstrip())
    assert all(0 <= start < end <= len(text) for start, end in spans)
    assert all(left[1] <= right[0] for left, right in zip(spans, spans[1:]))
    chunks = RecursiveChunker(profile=ChunkingProfile(tokenizer_profile=embedder.tokenizer_profile)).chunk_from_spans(text, spans)
    assert chunks[0]["start_char"] == spans[0][0]
    assert chunks[-1]["end_char"] == spans[-1][1]
    assert max(int(chunk["token_count"]) for chunk in chunks) <= 112
    assert len(chunks) > 5


async def test_candidate_and_edit_invalidation_are_durable(tmp_path) -> None:
    db, repo = await _repo(tmp_path)
    try:
        stored = await repo.store_message(chat_id=-100, telegram_message_id=10, sender_telegram_user_id=1, sender_chat_id=None,
            direction="incoming", message_kind="text", text="План курса Python", reply_to_telegram_message_id=None)
        await repo.make_knowledge_candidate(stored.id)
        await repo.replace_chunks(stored.id, [{"start_char": 0, "end_char": 17, "start_utf16": 0, "end_utf16": 17,
            "embedding_text": "План курса Python", "token_count": 3, "profile": "test"}])
        await repo.invalidate_knowledge(stored.id)
        await db.commit()
        candidate = await (await db.execute("SELECT candidate_state FROM knowledge_messages WHERE telegram_message_row_id = ?", (stored.id,))).fetchone()
        chunks = await db.execute_fetchall("SELECT id FROM knowledge_chunks WHERE telegram_message_row_id = ?", (stored.id,))
    finally:
        await db.close()
    assert candidate["candidate_state"] == "pending"
    assert chunks == []


async def test_candidate_policy_accepts_admins_and_any_sender_chat_but_not_ordinary_members(tmp_path) -> None:
    """Anonymous/channel posts use sender_chat; humans require ADMIN_IDS."""
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from app.config import Settings

    database_path = tmp_path / "ai.sqlite3"
    await run_migrations(str(database_path))
    settings = Settings(bot_token="test", telegram_group_id=-100, database_path=database_path, ai_enabled=True, admin_ids_raw="1")
    now = datetime.now(UTC)

    def incoming(
        message_id: int,
        *,
        text: str | None = None,
        caption: str | None = None,
        user_id: int | None = None,
        sender_chat_id: int | None = None,
        forward_origin=None,
    ):
        user = SimpleNamespace(id=user_id, username="member", first_name="Member", last_name=None) if user_id is not None else None
        return SimpleNamespace(
            chat=SimpleNamespace(id=-100), message_id=message_id, text=text, caption=caption,
            from_user=user,
            sender_chat=SimpleNamespace(id=sender_chat_id) if sender_chat_id is not None else None,
            forward_origin=forward_origin, reply_to_message=None, message_thread_id=None,
            date=now, edit_date=None, entities=None, caption_entities=None,
        )

    # A non-admin human (even a manual forward) is not a candidate. An admin,
    # a self sender_chat post, and an automatic foreign forward are candidates.
    await _persist(incoming(1, text="обычный участник", user_id=42), settings, SimpleNamespace(id=7), edited=False)
    await _persist(incoming(2, text="администратор", user_id=1), settings, SimpleNamespace(id=7), edited=False)
    await _persist(incoming(3, caption="от имени этой группы", sender_chat_id=-100), settings, SimpleNamespace(id=7), edited=False)
    await _persist(
        incoming(4, caption="автоматический форвард", sender_chat_id=-777, forward_origin=SimpleNamespace(type="channel")),
        settings,
        SimpleNamespace(id=7),
        edited=False,
    )

    db = await connect_database(database_path)
    try:
        rows = await db.execute_fetchall(
            """SELECT tm.telegram_message_id, tm.sender_telegram_user_id, tm.sender_chat_id,
                      tm.message_kind, km.candidate_state
                 FROM telegram_messages tm LEFT JOIN knowledge_messages km ON km.telegram_message_row_id = tm.id
                 ORDER BY tm.telegram_message_id"""
        )
    finally:
        await db.close()
    assert [(row["telegram_message_id"], row["sender_telegram_user_id"], row["sender_chat_id"], row["message_kind"], row["candidate_state"]) for row in rows] == [
        (1, 42, None, "text", None),
        (2, 1, None, "text", "pending"),
        (3, None, -100, "caption", "pending"),
        (4, None, -777, "caption", "pending"),
    ]


async def test_edited_message_recreates_candidate_after_invalidating_existing_chunks(tmp_path) -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from app.config import Settings

    database_path = tmp_path / "ai.sqlite3"
    await run_migrations(str(database_path))
    settings = Settings(bot_token="test", telegram_group_id=-100, database_path=database_path, ai_enabled=True, admin_ids_raw="42")
    now = datetime.now(UTC)
    user = SimpleNamespace(id=42, username="member", first_name="Member", last_name=None)
    original = SimpleNamespace(
        chat=SimpleNamespace(id=-100), message_id=5, text="старый текст", caption=None, from_user=user,
        sender_chat=None, reply_to_message=None, message_thread_id=None, date=now, edit_date=None,
        entities=None, caption_entities=None,
    )
    await _persist(original, settings, SimpleNamespace(id=7), edited=False)

    db = await connect_database(database_path)
    try:
        repo = AiRepository(db)
        stored = await (await db.execute("SELECT id FROM telegram_messages WHERE chat_id = -100 AND telegram_message_id = 5")).fetchone()
        await repo.set_candidate_result(stored["id"], state="include", reason="test", profile="test")
        await repo.replace_chunks(stored["id"], [{"start_char": 0, "end_char": 11, "start_utf16": 0, "end_utf16": 11,
            "embedding_text": "старый текст", "token_count": 2, "profile": "test"}])
        await db.commit()
    finally:
        await db.close()

    edited = SimpleNamespace(
        chat=SimpleNamespace(id=-100), message_id=5, text="новый текст", caption=None, from_user=user,
        sender_chat=None, reply_to_message=None, message_thread_id=None, date=now, edit_date=now,
        entities=None, caption_entities=None,
    )
    await _persist(edited, settings, SimpleNamespace(id=7), edited=True)

    db = await connect_database(database_path)
    try:
        row = await (await db.execute(
            """SELECT tm.text, km.candidate_state, COUNT(kc.id) AS chunk_count
                 FROM telegram_messages tm JOIN knowledge_messages km ON km.telegram_message_row_id = tm.id
                 LEFT JOIN knowledge_chunks kc ON kc.telegram_message_row_id = tm.id
                 WHERE tm.chat_id = -100 AND tm.telegram_message_id = 5 GROUP BY tm.id"""
        )).fetchone()
    finally:
        await db.close()
    assert (row["text"], row["candidate_state"], row["chunk_count"]) == ("новый текст", "pending", 0)


async def test_debounce_is_per_user_and_claim_is_durable(tmp_path) -> None:
    db, repo = await _repo(tmp_path)
    try:
        one = await repo.store_message(chat_id=-100, telegram_message_id=1, sender_telegram_user_id=10, sender_chat_id=None, direction="incoming", message_kind="text", text="@bot вопрос", reply_to_telegram_message_id=None)
        two = await repo.store_message(chat_id=-100, telegram_message_id=2, sender_telegram_user_id=11, sender_chat_id=None, direction="incoming", message_kind="text", text="@bot второй", reply_to_telegram_message_id=None)
        three = await repo.store_message(chat_id=-100, telegram_message_id=3, sender_telegram_user_id=10, sender_chat_id=None, direction="incoming", message_kind="text", text="@bot ещё вопрос", reply_to_telegram_message_id=None)
        await db.commit()
        first_turn = await repo.schedule_turn(-100, 10, one.id, 30)
        await repo.schedule_turn(-100, 10, one.id, 30)
        second_turn = await repo.schedule_turn(-100, 11, two.id, 30)
        await db.execute("UPDATE user_turns SET due_at = ?", ("2000-01-01T00:00:00+00:00",))
        await db.commit()
        claimed = await repo.claim_due_turns()
        follow_up_turn = await repo.schedule_turn(-100, 10, three.id, 30)
        await db.commit()
        active = await db.execute_fetchall("SELECT id, status FROM user_turns ORDER BY id")
    finally:
        await db.close()
    assert first_turn != second_turn
    assert follow_up_turn != first_turn
    assert {row["id"] for row in claimed} == {first_turn, second_turn}
    assert [(row["id"], row["status"]) for row in active] == [
        (first_turn, "processing"), (second_turn, "processing"), (follow_up_turn, "pending"),
    ]


def test_bot_gate_matches_only_own_mention_and_reply_with_utf16_offsets() -> None:
    from types import SimpleNamespace

    text = "😊 @other вопрос"
    other = SimpleNamespace(text=text, caption=None, entities=[SimpleNamespace(type="mention", offset=3, length=6)], caption_entities=None, reply_to_message=None)
    assert not _is_bot_invocation(other, bot_id=7, bot_username="ours", onboarding_pending=False, active_session=False)
    own_text = "😊 @ours вопрос"
    own = SimpleNamespace(text=own_text, caption=None, entities=[SimpleNamespace(type="mention", offset=3, length=5)], caption_entities=None, reply_to_message=None)
    assert _is_bot_invocation(own, bot_id=7, bot_username="ours", onboarding_pending=False, active_session=False)
    reply = SimpleNamespace(text="ok", caption=None, entities=None, caption_entities=None, reply_to_message=SimpleNamespace(from_user=SimpleNamespace(id=7)))
    assert _is_bot_invocation(reply, bot_id=7, bot_username=None, onboarding_pending=False, active_session=False)


async def test_session_expiry_and_turn_terminal_retry(tmp_path) -> None:
    db, repo = await _repo(tmp_path)
    try:
        await repo.set_state(-100, 10, "assistant.session", {"active": True, "expires_at": "2000-01-01T00:00:00+00:00"})
        assert not await repo.active_session(-100, 10)
        stored = await repo.store_message(chat_id=-100, telegram_message_id=90, sender_telegram_user_id=10, sender_chat_id=None, direction="incoming", message_kind="text", text="x", reply_to_telegram_message_id=None)
        await db.commit()
        turn = await repo.schedule_turn(-100, 10, stored.id, 0)
        await repo.claim_due_turns(lease_seconds=1, max_attempts=1)
        await db.execute("UPDATE user_turns SET lease_until = '2000-01-01T00:00:00+00:00' WHERE id = ?", (turn,))
        await db.commit()
        await repo.claim_due_turns(lease_seconds=1, max_attempts=1)
        row = await (await db.execute("SELECT status FROM user_turns WHERE id = ?", (turn,))).fetchone()
    finally:
        await db.close()
    assert row["status"] == "failed"


def test_worker_safe_env_excludes_application_secrets(monkeypatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "secret")
    monkeypatch.setenv("DATABASE_PATH", "secret")
    monkeypatch.setenv("CODEX_HOME", "/safe")
    assert "BOT_TOKEN" not in CodexCliWorker._safe_env()
    assert "DATABASE_PATH" not in CodexCliWorker._safe_env()
    assert CodexCliWorker._safe_env()["CODEX_HOME"] == "/safe"
    assert CLASSIFY_SCHEMA["additionalProperties"] is False


def test_onboarding_greeting_mentions_exact_telegram_participant() -> None:
    text, entities = onboarding_greeting(777, "Имя 😊")
    assert text.startswith("Имя 😊")
    assert entities[0].url == "tg://user?id=777"
    assert entities[0].length == len("Имя 😊".encode("utf-16-le")) // 2


async def test_worker_uses_official_argv_schema_and_result_file(monkeypatch) -> None:
    import json
    from pathlib import Path
    import app.ai.worker as worker_module

    captured: list[str] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self, payload):
            return b"", b""

        def kill(self):
            pass

        async def wait(self):
            return 0

    async def fake_exec(*argv, **kwargs):
        captured.extend(argv)
        result_path = Path(argv[argv.index("-o") + 1])
        result_path.write_text(json.dumps({"decision": "include", "reason": "test"}), encoding="utf-8")
        return FakeProcess()

    monkeypatch.setattr(worker_module.asyncio, "create_subprocess_exec", fake_exec)
    result = await CodexCliWorker("codex", 1, "gpt-5.6-luna", "medium").classify("content")
    assert result.decision == "include"
    for flag in ("--ephemeral", "--ignore-user-config", "--ignore-rules", "--output-schema", "-o"):
        assert flag in captured
    assert captured[captured.index("--model") + 1] == "gpt-5.6-luna"
    assert captured[captured.index("--config") + 1] == 'model_reasoning_effort="medium"'


def test_ai_worker_model_and_reasoning_effort_come_from_validated_settings() -> None:
    from pydantic import ValidationError

    from app.config import Settings

    settings = Settings(ai_worker_model="gpt-5.6-luna", ai_worker_reasoning_effort="MEDIUM")
    assert settings.ai_worker_model == "gpt-5.6-luna"
    assert settings.ai_worker_reasoning_effort == "medium"
    with pytest.raises(ValidationError, match="AI_WORKER_MODEL"):
        Settings(ai_worker_model="   ")
    with pytest.raises(ValidationError, match="AI_WORKER_REASONING_EFFORT"):
        Settings(ai_worker_reasoning_effort="ultra")


async def test_due_turn_aggregates_all_messages_and_no_response_skips_send(monkeypatch, tmp_path) -> None:
    import app.jobs.ai as jobs
    from app.ai.worker import Route
    from app.config import Settings

    db, repo = await _repo(tmp_path)
    try:
        first = await repo.store_message(chat_id=-100, telegram_message_id=201, sender_telegram_user_id=10, sender_chat_id=None, direction="incoming", message_kind="text", text="first", reply_to_telegram_message_id=None)
        second = await repo.store_message(chat_id=-100, telegram_message_id=202, sender_telegram_user_id=10, sender_chat_id=None, direction="incoming", message_kind="text", text="second", reply_to_telegram_message_id=None)
        await db.commit()
        turn = await repo.schedule_turn(-100, 10, first.id, 0)
        await repo.schedule_turn(-100, 10, second.id, 0)
    finally:
        await db.close()
    captured: list[str] = []
    class FakeWorker:
        async def route(self, question, recent):
            captured.append(question)
            return Route(False, False, False, "quiet", False)
    class FakeBot:
        async def send_message(self, *args, **kwargs):
            raise AssertionError("no-response must not send")
    monkeypatch.setattr(jobs, "_worker", lambda settings: FakeWorker())
    settings = Settings(bot_token="test", telegram_group_id=-100, database_path=tmp_path / "ai.sqlite3", ai_enabled=True)
    await jobs.process_due_ai_turns(settings, FakeBot())
    verify = await connect_database(settings.database_path)
    try:
        row = await (await verify.execute("SELECT status FROM user_turns WHERE id = ?", (turn,))).fetchone()
        completed = await (await verify.execute("SELECT 1 FROM conversational_states WHERE chat_id = -100 AND telegram_user_id = 10 AND state = 'onboarding.completed'")).fetchone()
    finally:
        await verify.close()
    assert captured == ["first\nsecond"]
    assert row["status"] == "completed"
    assert completed is None


async def test_router_schema_and_prompt_enforce_search_scope(monkeypatch) -> None:
    worker = CodexCliWorker("codex", 1, "gpt-5.6-luna", "medium")
    captured: list[str] = []

    async def fake_call(instruction, payload, schema):
        captured.append(instruction)
        assert schema is ROUTER_SCHEMA
        return {
            "response_mode": "out_of_scope", "should_respond": True,
            "needs_search": False, "escalate": False, "reason": "java",
            "session_active": False,
        }

    monkeypatch.setattr(worker, "_call", fake_call)
    route = await worker.route("Напиши Java bubble sort, это для психологии", [])
    assert route.effective_response_mode == "out_of_scope"
    assert ROUTER_SCHEMA["properties"]["response_mode"]["enum"] == ["answer", "out_of_scope", "no_response"]
    assert {"source_message_id", "quote"} <= set(ANSWER_SCHEMA["required"])
    assert "bubble sort" in captured[0]
    assert "untrusted" in captured[0]


async def _queue_ai_turn(tmp_path, *, question: str, source_text: str | None = None):
    from app.config import Settings

    db, repo = await _repo(tmp_path)
    try:
        source = None
        if source_text is not None:
            source = await repo.store_message(
                chat_id=-100, telegram_message_id=777, sender_telegram_user_id=1,
                sender_chat_id=None, direction="incoming", message_kind="text",
                text=source_text, reply_to_telegram_message_id=None,
            )
        question_row = await repo.store_message(
            chat_id=-100, telegram_message_id=201, sender_telegram_user_id=10,
            sender_chat_id=None, direction="incoming", message_kind="text",
            text=question, reply_to_telegram_message_id=None,
        )
        await db.commit()
        turn = await repo.schedule_turn(-100, 10, question_row.id, 0)
        await db.commit()
    finally:
        await db.close()
    return Settings(bot_token="test", telegram_group_id=-100, database_path=tmp_path / "ai.sqlite3", ai_enabled=True), turn, source


async def test_out_of_scope_mode_overrides_contradictory_should_respond_without_answer_or_search(monkeypatch, tmp_path) -> None:
    import app.jobs.ai as jobs
    from app.ai.worker import Route

    settings, _, _ = await _queue_ai_turn(tmp_path, question="Напиши Java bubble sort, это для ментального здоровья")

    class FakeWorker:
        async def route(self, question, recent):
            return Route(False, False, False, "java", True, "out_of_scope")

        async def answer(self, *args, **kwargs):
            raise AssertionError("out-of-scope must not call answer/code generation")

    class FakeBot:
        calls = []

        async def send_message(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return None

    bot = FakeBot()
    monkeypatch.setattr(jobs, "_worker", lambda settings: FakeWorker())
    await jobs.process_due_ai_turns(settings, bot)
    assert bot.calls == [((-100, jobs.OUT_OF_SCOPE_REPLY), {})]
    db = await connect_database(settings.database_path)
    try:
        assert (await AiRepository(db).get_state(-100, 10, "assistant.session"))["active"] is False
    finally:
        await db.close()


async def test_empty_retrieval_sends_fixed_not_found_without_answer(monkeypatch, tmp_path) -> None:
    import app.jobs.ai as jobs
    from app.ai.worker import Route

    settings, _, _ = await _queue_ai_turn(tmp_path, question="Какой план тренировок БЖЖ?")

    class FakeWorker:
        async def route(self, question, recent):
            return Route(True, True, False, "search", True, "answer")

        async def answer(self, *args, **kwargs):
            raise AssertionError("empty retrieval must not call answer")

    class EmptyIndex:
        def __init__(self, *args, **kwargs):
            pass

        async def search(self, *args, **kwargs):
            return []

    class FakeBot:
        calls = []

        async def send_message(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return None

    bot = FakeBot()
    monkeypatch.setattr(jobs, "_worker", lambda settings: FakeWorker())
    monkeypatch.setattr(jobs, "KnowledgeIndex", EmptyIndex)
    await jobs.process_due_ai_turns(settings, bot)
    assert bot.calls == [((-100, jobs.KNOWLEDGE_NOT_FOUND_REPLY), {})]
    db = await connect_database(settings.database_path)
    try:
        assert (await AiRepository(db).get_state(-100, 10, "assistant.session"))["active"] is False
    finally:
        await db.close()


async def test_answer_uses_native_quote_with_utf16_position(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace

    import app.jobs.ai as jobs
    from app.ai.knowledge import RetrievedChunk
    from app.ai.worker import Answer, Route

    source_text = "До 😊 фрагмент после"
    settings, _, source = await _queue_ai_turn(tmp_path, question="Найди фрагмент", source_text=source_text)
    hit = RetrievedChunk(1, source.id, 777, -100, 1, "фрагмент", 0.1)

    class FakeWorker:
        async def route(self, question, recent):
            return Route(True, True, False, "search", False, "answer")

        async def answer(self, *args, **kwargs):
            return Answer("Вот источник", False, 777, "фрагмент")

    class HitIndex:
        def __init__(self, *args, **kwargs):
            pass

        async def search(self, *args, **kwargs):
            return [hit]

    class FakeBot:
        calls = []

        async def send_message(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return SimpleNamespace(message_id=900, from_user=None, date=None)

    bot = FakeBot()
    monkeypatch.setattr(jobs, "_worker", lambda settings: FakeWorker())
    monkeypatch.setattr(jobs, "KnowledgeIndex", HitIndex)
    await jobs.process_due_ai_turns(settings, bot)
    reply = bot.calls[0][1]["reply_parameters"]
    assert reply.message_id == 777
    assert reply.quote == "фрагмент"
    assert reply.quote_position == len("До 😊 ".encode("utf-16-le")) // 2
    db = await connect_database(settings.database_path)
    try:
        stored = await (await db.execute(
            "SELECT reply_to_telegram_message_id FROM telegram_messages WHERE chat_id = -100 AND telegram_message_id = 900"
        )).fetchone()
    finally:
        await db.close()
    assert stored["reply_to_telegram_message_id"] == 777


async def test_native_reply_bad_request_retries_once_plain_and_persists_no_reference(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace

    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import SendMessage

    import app.jobs.ai as jobs
    from app.ai.knowledge import RetrievedChunk
    from app.ai.worker import Answer, Route

    settings, _, source = await _queue_ai_turn(tmp_path, question="Дай цитату", source_text="До 😊 фрагмент после")
    hit = RetrievedChunk(1, source.id, 777, -100, 1, "фрагмент", 0.1)

    class FakeWorker:
        async def route(self, question, recent):
            return Route(True, True, False, "search", False, "answer")

        async def answer(self, *args, **kwargs):
            return Answer("Ответ", False, 777, "фрагмент")

    class HitIndex:
        def __init__(self, *args, **kwargs):
            pass

        async def search(self, *args, **kwargs):
            return [hit]

    class FakeBot:
        def __init__(self):
            self.calls = []

        async def send_message(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            if "reply_parameters" in kwargs:
                raise TelegramBadRequest(SendMessage(chat_id=-100, text="Ответ"), "quote rejected")
            return SimpleNamespace(message_id=901, from_user=None, date=None)

    bot = FakeBot()
    monkeypatch.setattr(jobs, "_worker", lambda settings: FakeWorker())
    monkeypatch.setattr(jobs, "KnowledgeIndex", HitIndex)
    await jobs.process_due_ai_turns(settings, bot)
    assert len(bot.calls) == 2
    assert "reply_parameters" in bot.calls[0][1]
    assert bot.calls[1] == ((-100, "Ответ"), {})
    db = await connect_database(settings.database_path)
    try:
        rows = await db.execute_fetchall(
            "SELECT telegram_message_id, reply_to_telegram_message_id FROM telegram_messages WHERE chat_id = -100 AND direction = 'outgoing'"
        )
    finally:
        await db.close()
    assert [(row["telegram_message_id"], row["reply_to_telegram_message_id"]) for row in rows] == [(901, None)]


@pytest.mark.parametrize("source_message_id, quote", [(999, None), (777, "несуществующая цитата")])
async def test_hallucinated_source_or_quote_is_sent_without_telegram_reference(monkeypatch, tmp_path, source_message_id, quote) -> None:
    import app.jobs.ai as jobs
    from app.ai.knowledge import RetrievedChunk
    from app.ai.worker import Answer, Route

    settings, _, source = await _queue_ai_turn(tmp_path, question="Сошлись на источник", source_text="точный текст источника")
    hit = RetrievedChunk(1, source.id, 777, -100, 1, "точный текст", 0.1)

    class FakeWorker:
        async def route(self, question, recent):
            return Route(True, True, False, "search", False, "answer")

        async def answer(self, *args, **kwargs):
            return Answer("Ответ", False, source_message_id, quote)

    class HitIndex:
        def __init__(self, *args, **kwargs):
            pass

        async def search(self, *args, **kwargs):
            return [hit]

    class FakeBot:
        calls = []

        async def send_message(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return None

    bot = FakeBot()
    monkeypatch.setattr(jobs, "_worker", lambda settings: FakeWorker())
    monkeypatch.setattr(jobs, "KnowledgeIndex", HitIndex)
    await jobs.process_due_ai_turns(settings, bot)
    assert bot.calls == [((-100, "Ответ"), {})]


@pytest.mark.skipif(__import__("os").environ.get("RUN_FASTEMBED_INTEGRATION") != "1", reason="downloads/uses the real pinned FastEmbed model")
async def test_real_fastembed_semantics_rank_python_above_football(tmp_path, monkeypatch) -> None:
    pytest.importorskip("fastembed")
    from app.ai.knowledge import KnowledgeIndex

    monkeypatch.chdir(tmp_path)
    cache = Path(os.environ.get("FASTEMBED_TEST_CACHE", str(tmp_path / "model-cache")))

    db, repo = await _repo(tmp_path)
    try:
        python_message = await repo.store_message(chat_id=-100, telegram_message_id=101, sender_telegram_user_id=1, sender_chat_id=None, direction="incoming", message_kind="text", text="Курс Python для начинающих: уроки и упражнения", reply_to_telegram_message_id=None)
        football_message = await repo.store_message(chat_id=-100, telegram_message_id=102, sender_telegram_user_id=1, sender_chat_id=None, direction="incoming", message_kind="text", text="Новости футбола и результаты матчей", reply_to_telegram_message_id=None)
        for message in (python_message, football_message):
            await repo.make_knowledge_candidate(message.id)
            await repo.set_candidate_result(message.id, state="include", reason="test", profile="test")
        chunks = await repo.replace_chunks(python_message.id, [{"start_char": 0, "end_char": len(python_message.text), "start_utf16": 0, "end_utf16": len(python_message.text.encode("utf-16-le")) // 2, "embedding_text": python_message.text, "token_count": 7, "profile": "test"}])
        chunks += await repo.replace_chunks(football_message.id, [{"start_char": 0, "end_char": len(football_message.text), "start_utf16": 0, "end_utf16": len(football_message.text.encode("utf-16-le")) // 2, "embedding_text": football_message.text, "token_count": 6, "profile": "test"}])
        index = KnowledgeIndex(db, cache_dir=cache)
        await index.index_chunks(chunks)
        results = await index.search("Как начать курс по Питону?", top_k=2, context_char_budget=1000)
    finally:
        await db.close()
    assert results[0].source_message_id == python_message.id


async def test_vector_search_orders_python_above_football_for_russian_query(tmp_path) -> None:
    pytest.importorskip("sqlite_vec")
    from app.ai.knowledge import KnowledgeIndex
    from app.ai.repositories import KnowledgeChunk

    class FakeSemanticEmbedder:
        model_name = "test"

        def embed(self, texts):
            for text in texts:
                lowered = text.lower()
                first = 1.0 if "питон" in lowered or "python" in lowered or "курс" in lowered else 0.0
                second = 1.0 if "футбол" in lowered else 0.0
                yield [first, second] + [0.0] * 382

    db, repo = await _repo(tmp_path)
    try:
        python_message = await repo.store_message(chat_id=-100, telegram_message_id=1, sender_telegram_user_id=1, sender_chat_id=None, direction="incoming", message_kind="text", text="Курс Python для начинающих", reply_to_telegram_message_id=None)
        football_message = await repo.store_message(chat_id=-100, telegram_message_id=2, sender_telegram_user_id=1, sender_chat_id=None, direction="incoming", message_kind="text", text="Новости футбола", reply_to_telegram_message_id=None)
        for message in (python_message, football_message):
            await repo.make_knowledge_candidate(message.id)
            await repo.set_candidate_result(message.id, state="include", reason="test", profile="test")
        chunks = await repo.replace_chunks(python_message.id, [{"start_char": 0, "end_char": 27, "start_utf16": 0, "end_utf16": 27, "embedding_text": "Курс Python для начинающих", "token_count": 4, "profile": "test"}])
        chunks += await repo.replace_chunks(football_message.id, [{"start_char": 0, "end_char": 15, "start_utf16": 0, "end_utf16": 15, "embedding_text": "Новости футбола", "token_count": 2, "profile": "test"}])
        index = KnowledgeIndex(db, FakeSemanticEmbedder())
        await index.index_chunks(chunks)
        results = await index.search("Как начать курс по Питону?", top_k=2, context_char_budget=1000)
    finally:
        await db.close()
    assert results[0].source_message_id == python_message.id
