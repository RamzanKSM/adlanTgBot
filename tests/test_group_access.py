from datetime import UTC, datetime, timedelta
from json import loads

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetChatMember

from app.config import Settings
from app.db.connection import connect_database
from app.db.migrations import SCHEMA_SQL
from app.db.repositories import UsersRepository
from app.jobs.access import warn_and_expire_access
from app.services.group_access import can_remove_from_group


class FakeMember:
    def __init__(self, status: str, is_member: bool = False) -> None:
        self.status = status
        self.is_member = is_member


class FakeBot:
    def __init__(self, member: FakeMember | Exception) -> None:
        self.member = member
        self.get_chat_member_calls: list[tuple[int, int]] = []
        self.ban_calls: list[tuple[int, int]] = []
        self.unban_calls: list[tuple[int, int, bool]] = []
        self.sent_messages: list[tuple[int, str]] = []

    async def get_chat_member(self, chat_id: int, user_id: int) -> FakeMember:
        self.get_chat_member_calls.append((chat_id, user_id))
        if isinstance(self.member, Exception):
            raise self.member
        return self.member

    async def ban_chat_member(self, chat_id: int, user_id: int) -> None:
        self.ban_calls.append((chat_id, user_id))

    async def unban_chat_member(self, chat_id: int, user_id: int, only_if_banned: bool = False) -> None:
        self.unban_calls.append((chat_id, user_id, only_if_banned))

    async def send_message(self, chat_id: int, text: str) -> None:
        self.sent_messages.append((chat_id, text))


class FailingBanBot(FakeBot):
    async def ban_chat_member(self, chat_id: int, user_id: int) -> None:
        self.ban_calls.append((chat_id, user_id))
        raise RuntimeError("ban failed")


def _bad_request() -> TelegramBadRequest:
    return TelegramBadRequest(method=GetChatMember(chat_id=-100, user_id=123), message="chat member unavailable")


def _settings(tmp_path=None, admin_ids: str = "") -> Settings:
    return Settings(
        bot_token="test",
        telegram_group_id=-100,
        database_path=(tmp_path / "test.sqlite3") if tmp_path else "test.sqlite3",
        admin_ids_raw=admin_ids,
    )


async def _create_expired_user(settings: Settings, telegram_user_id: int = 123) -> None:
    db = await connect_database(settings.database_path)
    await db.executescript(SCHEMA_SQL)
    users = UsersRepository(db)
    user = await users.upsert_telegram_user(
        telegram_user_id=telegram_user_id,
        username=None,
        first_name=None,
        last_name=None,
    )
    await users.set_access_until(user.id, datetime.now(UTC) - timedelta(days=1))
    await users.set_is_in_group(user.telegram_user_id, True)
    await db.commit()
    await db.close()


async def _create_user_with_access_until(settings: Settings, access_until: datetime, telegram_user_id: int = 123) -> None:
    db = await connect_database(settings.database_path)
    await db.executescript(SCHEMA_SQL)
    users = UsersRepository(db)
    user = await users.upsert_telegram_user(
        telegram_user_id=telegram_user_id,
        username=None,
        first_name=None,
        last_name=None,
    )
    await users.set_access_until(user.id, access_until)
    await db.commit()
    await db.close()


async def _latest_user_and_event(settings: Settings, telegram_user_id: int = 123):
    db = await connect_database(settings.database_path)
    user = await UsersRepository(db).get_by_telegram_id(telegram_user_id)
    event = await (
        await db.execute(
            "SELECT * FROM access_events WHERE telegram_user_id = ? ORDER BY id DESC LIMIT 1",
            (telegram_user_id,),
        )
    ).fetchone()
    await db.close()
    return user, event


async def test_can_remove_from_group_protects_configured_admin() -> None:
    bot = FakeBot(FakeMember("member"))

    result = await can_remove_from_group(bot, _settings(admin_ids="123"), 123)

    assert result.can_remove is False
    assert result.reason == "configured_admin"
    assert bot.get_chat_member_calls == []


async def test_can_remove_from_group_protects_telegram_admin() -> None:
    bot = FakeBot(FakeMember("administrator"))

    result = await can_remove_from_group(bot, _settings(), 123)

    assert result.can_remove is False
    assert result.reason == "telegram_admin"
    assert result.status == "administrator"


async def test_can_remove_from_group_fails_closed_when_status_unverified() -> None:
    bot = FakeBot(_bad_request())

    result = await can_remove_from_group(bot, _settings(), 123)

    assert result.can_remove is False
    assert result.reason == "unverified"
    assert result.error is not None


async def test_can_remove_from_group_allows_regular_member() -> None:
    bot = FakeBot(FakeMember("member"))

    result = await can_remove_from_group(bot, _settings(), 123)

    assert result.can_remove is True
    assert result.reason == "removable"


async def test_expired_access_does_not_mark_user_out_of_group_when_ban_fails(tmp_path) -> None:
    settings = _settings(tmp_path)
    await _create_expired_user(settings)
    bot = FailingBanBot(FakeMember("member"))

    await warn_and_expire_access(settings, bot)

    updated_user, event = await _latest_user_and_event(settings)

    assert updated_user is not None
    assert updated_user.is_in_group is True
    assert event is not None
    assert event["event_type"] == "access_expired_removal_failed"
    assert loads(event["details"])["error_type"] == "RuntimeError"
    assert bot.sent_messages == []


async def test_expired_access_sends_private_message_after_successful_removal(tmp_path) -> None:
    settings = _settings(tmp_path)
    await _create_expired_user(settings)
    bot = FakeBot(FakeMember("member"))

    await warn_and_expire_access(settings, bot)

    updated_user, event = await _latest_user_and_event(settings)

    assert updated_user is not None
    assert updated_user.is_in_group is False
    assert event is not None
    assert event["event_type"] == "access_expired_removed"
    assert bot.sent_messages == [
        (123, "❌ Доступ закончился, вы удалены из группы.\n💳 Продлить доступ можно через кнопку «💳 Тарифы».")
    ]


async def test_access_warning_uses_human_moscow_datetime(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    now = datetime(2026, 7, 1, 22, 39, tzinfo=UTC)
    access_until = datetime(2026, 7, 1, 23, 39, 13, 768248, tzinfo=UTC)
    await _create_user_with_access_until(settings, access_until)
    bot = FakeBot(FakeMember("member"))
    monkeypatch.setattr("app.jobs.access.utc_now", lambda: now)

    await warn_and_expire_access(settings, bot)

    assert bot.sent_messages == [
        (123, "⏳ Доступ в группу закончится 02.07.2026 02:39 МСК.\n💳 Продлите доступ заранее.")
    ]


async def test_expired_access_skips_configured_admin_without_status_lookup_or_ban(tmp_path) -> None:
    settings = _settings(tmp_path, admin_ids="123")
    await _create_expired_user(settings)
    bot = FakeBot(FakeMember("member"))

    await warn_and_expire_access(settings, bot)

    updated_user, event = await _latest_user_and_event(settings)
    assert updated_user is not None
    assert updated_user.is_in_group is True
    assert bot.get_chat_member_calls == []
    assert bot.ban_calls == []
    assert bot.sent_messages == []
    assert event is not None
    assert event["event_type"] == "access_expired_removal_skipped_protected"


async def test_expired_access_skips_removal_when_status_unverified(tmp_path) -> None:
    settings = _settings(tmp_path)
    await _create_expired_user(settings)
    bot = FakeBot(_bad_request())

    await warn_and_expire_access(settings, bot)

    updated_user, event = await _latest_user_and_event(settings)
    assert updated_user is not None
    assert updated_user.is_in_group is True
    assert bot.ban_calls == []
    assert bot.sent_messages == []
    assert event is not None
    assert event["event_type"] == "access_expired_removal_skipped_unverified"
