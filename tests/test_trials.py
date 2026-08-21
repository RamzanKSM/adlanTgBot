import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.bot.handlers_admin import (
    TrialSetUsageError,
    TrialSetValidationError,
    parse_trial_set_args,
)
from app.bot.handlers_user import trial_button
from app.config import Settings
from app.db.connection import connect_database
from app.db.migrations import run_migrations
from app.db.repositories import (
    PaymentsRepository,
    TariffsRepository,
    TrialAccessesRepository,
    TrialSettingsRepository,
    UsersRepository,
)
from app.jobs.access import warn_and_expire_access
from app.services.access import grant_manual_access
from app.services.lava import LavaPaymentNotification
from app.services.payments import PaymentService
from app.services.trials import TrialService


@pytest.fixture
async def db(tmp_path):
    path = tmp_path / "trial.sqlite3"
    await run_migrations(str(path))
    connection = await connect_database(path)
    try:
        yield connection
    finally:
        await connection.close()


def _settings(tmp_path) -> Settings:
    return Settings(bot_token="test", telegram_group_id=-100, database_path=tmp_path / "trial.sqlite3")


async def test_trial_settings_defaults_and_trial_is_issued_once(db) -> None:
    settings = TrialSettingsRepository(db)
    assert (await settings.get()).enabled is False
    assert (await settings.get()).duration_days == 3

    await settings.set(True, 7, updated_by_telegram_user_id=999)
    await db.commit()
    service = TrialService(db)

    first = await service.grant(123, "user", "First", None)
    second = await service.grant(123, "user", "First", None)
    user = await UsersRepository(db).get_by_telegram_id(123)
    trial = await TrialAccessesRepository(db).get_by_user_id(user.id)

    assert first.status == "issued"
    assert second.status == "active_access"
    assert user.access_kind == "trial"
    assert user.access_source_id == first.trial_id
    assert trial is not None
    assert trial.status == "active"
    assert trial.expires_at - trial.started_at == timedelta(days=7)


async def test_trial_rejects_used_trial_after_expiry(db, monkeypatch) -> None:
    settings = TrialSettingsRepository(db)
    await settings.set(True, 3, updated_by_telegram_user_id=999)
    await db.commit()
    service = TrialService(db)
    first = await service.grant(123, None, None, None)
    assert first.status == "issued"

    await UsersRepository(db).set_access_until(first.user.id, datetime(2026, 1, 1, tzinfo=UTC))
    await db.commit()
    monkeypatch.setattr("app.services.trials.utc_now", lambda: datetime(2026, 1, 2, tzinfo=UTC))
    second = await service.grant(123, None, None, None)

    assert second.status == "already_used"


async def test_concurrent_trial_grants_are_limited_by_sqlite_transaction(tmp_path) -> None:
    path = tmp_path / "concurrent-trial.sqlite3"
    await run_migrations(str(path))
    setup = await connect_database(path)
    await TrialSettingsRepository(setup).set(True, 3, updated_by_telegram_user_id=1)
    await setup.commit()
    await setup.close()

    async def grant_once():
        connection = await connect_database(path)
        try:
            return await TrialService(connection).grant(123, None, None, None)
        finally:
            await connection.close()

    results = await asyncio.gather(grant_once(), grant_once())
    assert [result.status for result in results].count("issued") == 1
    assert {result.status for result in results} <= {"issued", "active_access", "already_used"}


async def test_payment_replaces_trial_provenance_and_marks_history_converted(db, tmp_path) -> None:
    trial_settings = TrialSettingsRepository(db)
    await trial_settings.set(True, 3, updated_by_telegram_user_id=999)
    await db.commit()
    trial = await TrialService(db).grant(123, None, None, None)
    tariff = await TariffsRepository(db).upsert("m1", "Month", "", 1000, "RUB", 30)
    payment = await PaymentsRepository(db).create(
        user_id=trial.user.id,
        tariff_id=tariff.id,
        order_id="trial-payment",
        amount=1000,
        currency="RUB",
        payment_url="https://pay.example/1",
        invoice_id="invoice-1",
        expires_at=None,
    )
    await db.commit()
    service = PaymentService(db, _settings(tmp_path), lava_client=None)
    paid_at = datetime(2026, 1, 1, tzinfo=UTC)
    await service.handle_paid(
        LavaPaymentNotification(
            order_id=payment.order_id,
            invoice_id=payment.invoice_id,
            status="paid",
            amount=1000,
            currency="RUB",
            paid_at=paid_at,
            raw_payload={},
        )
    )

    user = await UsersRepository(db).get_by_telegram_id(123)
    trial_history = await TrialAccessesRepository(db).get_by_user_id(user.id)
    assert user.access_kind == "paid"
    assert user.access_source_id == payment.id
    assert trial_history.status == "converted"


async def test_manual_grant_sets_manual_provenance(db) -> None:
    grant = await grant_manual_access(db, 123, 7, granted_by_telegram_user_id=999)
    assert grant.user.access_kind == "manual"
    assert grant.user.access_source_id is None


async def test_trial_button_issues_access_and_personal_invite(tmp_path) -> None:
    path = tmp_path / "trial-button.sqlite3"
    await run_migrations(str(path))
    db = await connect_database(path)
    await TrialSettingsRepository(db).set(True, 3, updated_by_telegram_user_id=999)
    await db.commit()
    await db.close()

    class FakeUser:
        id = 123
        username = "user"
        first_name = "First"
        last_name = None

    class FakeMember:
        status = "left"
        is_member = False

    class FakeInvite:
        invite_link = "https://t.me/+personal"

    class FakeBot:
        async def get_chat_member(self, chat_id: int, user_id: int) -> FakeMember:
            return FakeMember()

        async def create_chat_invite_link(self, **kwargs) -> FakeInvite:
            return FakeInvite()

    class FakeMessage:
        from_user = FakeUser()
        bot = FakeBot()

        def __init__(self) -> None:
            self.answers: list[str] = []

        async def answer(self, body: str, **kwargs) -> None:
            self.answers.append(body)

    message = FakeMessage()
    settings = Settings(bot_token="test", telegram_group_id=-100, database_path=path)
    await trial_button(message, settings)  # type: ignore[arg-type]

    assert len(message.answers) == 1
    assert "Пробный доступ активирован" in message.answers[0]
    assert "https://t.me/+personal" in message.answers[0]

    check = await connect_database(path)
    try:
        user = await UsersRepository(check).get_by_telegram_id(123)
        trial = await TrialAccessesRepository(check).get_by_user_id(user.id)
    finally:
        await check.close()
    assert user.access_kind == "trial"
    assert trial is not None


@pytest.mark.parametrize("command", ["/trial_set", "/trial_set 0", "/trial_set 1001", "/trial_set abc", "/trial_set 3 extra"])
def test_parse_trial_set_args_rejects_invalid_values(command: str) -> None:
    with pytest.raises((TrialSetUsageError, TrialSetValidationError)):
        parse_trial_set_args(command)


def test_parse_trial_set_args_accepts_configured_range() -> None:
    assert parse_trial_set_args("/trial_set 1") == 1
    assert parse_trial_set_args("/trial_set 1000") == 1000


class WarningBot:
    def __init__(self) -> None:
        self.sent_messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, body: str) -> None:
        self.sent_messages.append((chat_id, body))


async def test_warning_window_and_text_depend_on_access_kind(tmp_path, monkeypatch) -> None:
    path = tmp_path / "warnings.sqlite3"
    await run_migrations(str(path))
    db = await connect_database(path)
    try:
        users = UsersRepository(db)
        now = datetime(2026, 1, 1, tzinfo=UTC)
        for telegram_id, kind, until in (
            (1, "trial", now + timedelta(days=1)),
            (2, "paid", now + timedelta(days=2)),
            (3, "manual", now + timedelta(days=2)),
            (4, "trial", now + timedelta(days=2)),
        ):
            user = await users.upsert_telegram_user(telegram_id, None, None, None)
            await users.set_access(user.id, until, kind, None)
        await db.commit()
    finally:
        await db.close()

    monkeypatch.setattr("app.jobs.access.utc_now", lambda: now)
    bot = WarningBot()
    await warn_and_expire_access(Settings(bot_token="test", telegram_group_id=-100, database_path=path), bot)  # type: ignore[arg-type]

    by_user = dict(bot.sent_messages)
    assert "Пробный доступ закончится" in by_user[1]
    assert "Подписка закончится" in by_user[2]
    assert "Доступ в группу закончится" in by_user[3]
    assert 4 not in by_user
