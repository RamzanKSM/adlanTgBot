from datetime import timedelta
from json import loads
from types import SimpleNamespace

from app.bot.handlers_chat_member import on_chat_member
from app.bot.handlers_group_service import delete_join_service_message
from app.config import Settings
from app.db.connection import connect_database
from app.db.migrations import SCHEMA_SQL
from app.db.repositories import InviteLinksRepository, UsersRepository
from app.utils.datetime import utc_now


class FakeMember:
    def __init__(self, status: str = "member", is_member: bool = True) -> None:
        self.status = status
        self.is_member = is_member


class FakeBot:
    def __init__(self, member: FakeMember | None = None) -> None:
        self.member = member or FakeMember()
        self.ban_calls: list[tuple[int, int]] = []
        self.unban_calls: list[tuple[int, int, bool]] = []
        self.revoke_calls: list[tuple[int, str]] = []
        self.sent_messages: list[tuple[int, str]] = []

    async def get_chat_member(self, chat_id: int, user_id: int) -> FakeMember:
        return self.member

    async def ban_chat_member(self, chat_id: int, user_id: int) -> None:
        self.ban_calls.append((chat_id, user_id))

    async def unban_chat_member(self, chat_id: int, user_id: int, only_if_banned: bool = False) -> None:
        self.unban_calls.append((chat_id, user_id, only_if_banned))

    async def revoke_chat_invite_link(self, chat_id: int, invite_link: str) -> None:
        self.revoke_calls.append((chat_id, invite_link))

    async def send_message(self, chat_id: int, text: str) -> None:
        self.sent_messages.append((chat_id, text))


class FailingDeleteMessage:
    def __init__(self, bot: FakeBot, participants: list[SimpleNamespace]) -> None:
        self.bot = bot
        self.chat = SimpleNamespace(id=-100)
        self.message_id = 77
        self.new_chat_members = participants

    async def delete(self) -> None:
        raise RuntimeError("missing permission")


class DeletableMessage(FailingDeleteMessage):
    def __init__(self, bot: FakeBot, participants: list[SimpleNamespace]) -> None:
        super().__init__(bot, participants)
        self.deleted = False

    async def delete(self) -> None:
        self.deleted = True


def _settings(tmp_path, admin_ids: str = "900") -> Settings:
    return Settings(
        bot_token="test",
        telegram_group_id=-100,
        database_path=tmp_path / "group-entry.sqlite3",
        admin_ids_raw=admin_ids,
    )


def _participant(telegram_user_id: int, username: str = "member") -> SimpleNamespace:
    return SimpleNamespace(id=telegram_user_id, username=username, first_name="Test", last_name="User")


def _join_event(bot: FakeBot, participant: SimpleNamespace, invite_link: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        bot=bot,
        chat=SimpleNamespace(id=-100),
        old_chat_member=SimpleNamespace(status="left"),
        new_chat_member=SimpleNamespace(status="member", user=participant),
        invite_link=SimpleNamespace(invite_link=invite_link) if invite_link else None,
    )


async def _prepare_database(settings: Settings) -> None:
    db = await connect_database(settings.database_path)
    await db.executescript(SCHEMA_SQL)
    await db.commit()
    await db.close()


async def _create_user(settings: Settings, telegram_user_id: int, active: bool = True):
    db = await connect_database(settings.database_path)
    users = UsersRepository(db)
    user = await users.upsert_telegram_user(telegram_user_id, "payer", "Payer", "User")
    await users.set_access_until(user.id, utc_now() + timedelta(days=5) if active else utc_now() - timedelta(days=1))
    await db.commit()
    user = await users.get_by_telegram_id(telegram_user_id)
    await db.close()
    return user


async def _create_personal_invite(settings: Settings, user_id: int, invite_link: str):
    db = await connect_database(settings.database_path)
    invite = await InviteLinksRepository(db).create(
        user_id=user_id,
        payment_id=None,
        invite_link=invite_link,
        telegram_invite_link_id=invite_link,
        expires_at=utc_now() + timedelta(hours=1),
    )
    await db.commit()
    await db.close()
    return invite


async def _events(settings: Settings, telegram_user_id: int) -> list[tuple[str, dict[str, object]]]:
    db = await connect_database(settings.database_path)
    rows = await (
        await db.execute(
            "SELECT event_type, details FROM access_events WHERE telegram_user_id = ? ORDER BY id",
            (telegram_user_id,),
        )
    ).fetchall()
    await db.close()
    return [(row["event_type"], loads(row["details"])) for row in rows]


async def test_wrong_personal_invite_removes_intruder_revokes_link_and_preserves_payer(tmp_path) -> None:
    settings = _settings(tmp_path)
    await _prepare_database(settings)
    payer = await _create_user(settings, 101)
    invite = await _create_personal_invite(settings, payer.id, "https://t.me/+personal")
    bot = FakeBot()

    await on_chat_member(_join_event(bot, _participant(202, "intruder"), invite.invite_link), settings)

    db = await connect_database(settings.database_path)
    payer_after = await UsersRepository(db).get_by_telegram_id(101)
    intruder = await UsersRepository(db).get_by_telegram_id(202)
    invite_status = await (await db.execute("SELECT status FROM invite_links WHERE id = ?", (invite.id,))).fetchone()
    await db.close()
    events = await _events(settings, 202)

    assert payer_after is not None and payer_after.access_until == payer.access_until
    assert intruder is None
    assert invite_status["status"] == "revoked"
    assert bot.revoke_calls == [(-100, invite.invite_link)]
    assert bot.ban_calls == [(-100, 202)]
    assert bot.unban_calls == [(-100, 202, True)]
    assert events[0][0] == "group_join_wrong_personal_invite"
    assert events[0][1]["expected_user_id"] == 101
    assert events[0][1]["username"] == "intruder"
    assert events[1][0] == "group_join_removed"
    assert bot.sent_messages and "чужой персональной" in bot.sent_messages[0][1]


async def test_expected_personal_invite_marks_only_payer_as_group_member_and_notifies_admin(tmp_path) -> None:
    settings = _settings(tmp_path)
    await _prepare_database(settings)
    payer = await _create_user(settings, 101)
    invite = await _create_personal_invite(settings, payer.id, "https://t.me/+expected")
    bot = FakeBot()

    await on_chat_member(_join_event(bot, _participant(101, "payer"), invite.invite_link), settings)

    db = await connect_database(settings.database_path)
    payer_after = await UsersRepository(db).get_by_telegram_id(101)
    invite_status = await (await db.execute("SELECT status FROM invite_links WHERE id = ?", (invite.id,))).fetchone()
    await db.close()
    events = await _events(settings, 101)

    assert payer_after is not None and payer_after.is_in_group is True
    assert invite_status["status"] == "used"
    assert bot.ban_calls == []
    assert events == [("group_join_expected_user", events[0][1])]
    assert events[0][1]["invite_id"] == invite.id
    assert bot.sent_messages and "Штатный вход" in bot.sent_messages[0][1]


async def test_join_without_access_is_logged_and_removed_without_creating_user(tmp_path) -> None:
    settings = _settings(tmp_path)
    await _prepare_database(settings)
    bot = FakeBot()

    await on_chat_member(_join_event(bot, _participant(303, "unknown")), settings)

    db = await connect_database(settings.database_path)
    unknown = await UsersRepository(db).get_by_telegram_id(303)
    await db.close()
    events = await _events(settings, 303)

    assert unknown is None
    assert bot.ban_calls == [(-100, 303)]
    assert [event_type for event_type, _ in events] == ["group_join_no_active_access", "group_join_removed"]
    assert events[0][1]["has_active_access"] is False


async def test_expired_payer_personal_invite_is_revoked_and_user_is_removed(tmp_path) -> None:
    settings = _settings(tmp_path)
    await _prepare_database(settings)
    payer = await _create_user(settings, 350, active=False)
    invite = await _create_personal_invite(settings, payer.id, "https://t.me/+expired")
    bot = FakeBot()

    await on_chat_member(_join_event(bot, _participant(350, "expired"), invite.invite_link), settings)

    db = await connect_database(settings.database_path)
    invite_status = await (await db.execute("SELECT status FROM invite_links WHERE id = ?", (invite.id,))).fetchone()
    await db.close()
    events = await _events(settings, 350)

    assert invite_status["status"] == "revoked"
    assert bot.ban_calls == [(-100, 350)]
    assert [event_type for event_type, _ in events] == ["group_join_no_active_access", "group_join_removed"]
    assert events[0][1]["tracked_invite"] is True


async def test_configured_admin_is_never_removed_when_joining_without_access(tmp_path) -> None:
    settings = _settings(tmp_path, admin_ids="404,900")
    await _prepare_database(settings)
    bot = FakeBot()

    await on_chat_member(_join_event(bot, _participant(404, "admin")), settings)

    events = await _events(settings, 404)
    assert bot.ban_calls == []
    assert [event_type for event_type, _ in events] == ["group_join_no_active_access", "group_join_removal_skipped_protected"]
    assert events[1][1]["reason"] == "configured_admin"


async def test_untracked_invite_allows_known_user_with_active_access(tmp_path) -> None:
    settings = _settings(tmp_path)
    await _prepare_database(settings)
    user = await _create_user(settings, 505)
    bot = FakeBot()

    await on_chat_member(_join_event(bot, _participant(505, "known"), "https://t.me/+untracked"), settings)

    db = await connect_database(settings.database_path)
    user_after = await UsersRepository(db).get_by_telegram_id(505)
    await db.close()
    events = await _events(settings, 505)

    assert user_after is not None and user_after.id == user.id and user_after.is_in_group is True
    assert bot.ban_calls == []
    assert events[0][0] == "group_join_untracked_invite"
    assert events[0][1]["action"] == "allowed_active_access"


async def test_join_service_message_is_deleted_and_delete_failure_is_recorded_and_notified(tmp_path) -> None:
    settings = _settings(tmp_path)
    await _prepare_database(settings)
    bot = FakeBot()
    participant = _participant(606, "service")
    deletable_message = DeletableMessage(bot, [participant])

    await delete_join_service_message(deletable_message, settings)
    successful_events = await _events(settings, 606)

    failing_message = FailingDeleteMessage(bot, [participant])
    await delete_join_service_message(failing_message, settings)
    all_events = await _events(settings, 606)

    assert deletable_message.deleted is True
    assert successful_events[0][0] == "group_join_service_message_deleted"
    assert all_events[1][0] == "group_join_service_message_delete_failed"
    assert all_events[1][1]["error_type"] == "RuntimeError"
    assert bot.sent_messages and "Delete messages" in bot.sent_messages[-1][1]
