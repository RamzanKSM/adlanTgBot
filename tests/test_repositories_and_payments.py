from datetime import UTC, datetime, timedelta
from json import loads

import pytest

from app.config import Settings
from app.db.connection import connect_database
from app.db.migrations import SCHEMA_SQL
from app.db.repositories import PaymentsRepository, TariffsRepository, UsersRepository
from app.services.access import grant_manual_access
from app.services.lava import LavaInvoice, LavaPaymentNotification
from app.services.payments import PaymentService


@pytest.fixture
async def db(tmp_path):
    connection = await connect_database(tmp_path / "test.sqlite3")
    await connection.executescript(SCHEMA_SQL)
    await connection.commit()
    try:
        yield connection
    finally:
        await connection.close()


async def test_repositories_create_user_tariff_and_payment(db) -> None:
    user = await UsersRepository(db).upsert_telegram_user(telegram_user_id=123, username="user", first_name="A", last_name="B")
    tariff = await TariffsRepository(db).upsert(
        code="m1",
        title="Month",
        description="",
        price_amount=1000,
        currency="RUB",
        duration_days=30,
    )
    payment = await PaymentsRepository(db).create(
        user_id=user.id,
        tariff_id=tariff.id,
        internal_invoice_id="invoice-1",
        amount=1000,
        currency="RUB",
        payment_url="https://pay.example/1",
        provider_invoice_id="lava-1",
        expires_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    await db.commit()

    assert payment.internal_invoice_id == "invoice-1"
    assert (await UsersRepository(db).get_by_telegram_id(123)).id == user.id
    assert (await TariffsRepository(db).get_by_code("m1")).id == tariff.id


async def test_payment_handle_paid_is_idempotent(db, tmp_path) -> None:
    users = UsersRepository(db)
    tariffs = TariffsRepository(db)
    payments = PaymentsRepository(db)
    user = await users.upsert_telegram_user(telegram_user_id=123, username=None, first_name=None, last_name=None)
    tariff = await tariffs.upsert(
        code="m1",
        title="Month",
        description="",
        price_amount=1000,
        currency="RUB",
        duration_days=30,
    )
    await payments.create(
        user_id=user.id,
        tariff_id=tariff.id,
        internal_invoice_id="invoice-1",
        amount=1000,
        currency="RUB",
        payment_url="https://pay.example/1",
        provider_invoice_id=None,
        expires_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    await db.commit()

    settings = Settings(bot_token="test", telegram_group_id=-100, database_path=tmp_path / "test.sqlite3")
    service = PaymentService(db, settings, lava_client=None)
    paid_at = datetime(2026, 1, 10, tzinfo=UTC)
    notification = LavaPaymentNotification(
        internal_invoice_id="invoice-1",
        provider_invoice_id="lava-1",
        status="paid",
        amount=1000,
        currency="RUB",
        paid_at=paid_at,
        raw_payload={"status": "paid"},
    )

    first = await service.handle_paid(notification)
    second = await service.handle_paid(notification)
    updated_user = await users.get_by_telegram_id(123)
    updated_payment = await payments.get_by_internal_invoice_id("invoice-1")

    assert first.already_applied is False
    assert second.already_applied is True
    assert updated_user.access_until == paid_at + timedelta(days=30)
    assert updated_payment.applied_at is not None
    assert updated_payment.status == "applied"


class RecordingLavaClient:
    def __init__(self) -> None:
        self.create_invoice_calls = 0

    async def create_invoice(self, **kwargs) -> LavaInvoice:
        self.create_invoice_calls += 1
        return LavaInvoice(
            provider_invoice_id="lava-1",
            payment_url="https://pay.example/1",
            raw_payload={"provider": "lava", "kwargs": kwargs},
        )


async def test_mock_payment_provider_creates_local_invoice_without_lava_call(db, tmp_path) -> None:
    user = await UsersRepository(db).upsert_telegram_user(telegram_user_id=123, username=None, first_name=None, last_name=None)
    await TariffsRepository(db).upsert(
        code="m1",
        title="Month",
        description="",
        price_amount=1000,
        currency="RUB",
        duration_days=30,
    )
    await db.commit()

    settings = Settings(
        bot_token="test",
        telegram_group_id=-100,
        database_path=tmp_path / "test.sqlite3",
        app_base_url="http://localhost:8000",
        payment_provider="mock",
    )
    lava_client = RecordingLavaClient()
    service = PaymentService(db, settings, lava_client)

    created = await service.create_payment_for_tariff(user.telegram_user_id, "m1")

    assert lava_client.create_invoice_calls == 0
    assert created.payment.provider == "mock"
    assert created.payment.payment_url == f"http://localhost:8000/mock/payments/{created.payment.internal_invoice_id}/pay"
    assert created.payment.provider_invoice_id == f"mock-{created.payment.internal_invoice_id}"


async def test_lava_payment_provider_keeps_lava_invoice_creation(db, tmp_path) -> None:
    user = await UsersRepository(db).upsert_telegram_user(telegram_user_id=123, username=None, first_name=None, last_name=None)
    await TariffsRepository(db).upsert(
        code="m1",
        title="Month",
        description="",
        price_amount=1000,
        currency="RUB",
        duration_days=30,
    )
    await db.commit()

    settings = Settings(
        bot_token="test",
        telegram_group_id=-100,
        database_path=tmp_path / "test.sqlite3",
        app_base_url="http://localhost:8000",
        payment_provider="lava",
    )
    lava_client = RecordingLavaClient()
    service = PaymentService(db, settings, lava_client)

    created = await service.create_payment_for_tariff(user.telegram_user_id, "m1")

    assert lava_client.create_invoice_calls == 1
    assert created.payment.provider == "lava"
    assert created.payment.payment_url == "https://pay.example/1"


async def test_confirm_mock_payment_is_idempotent(db, tmp_path) -> None:
    user = await UsersRepository(db).upsert_telegram_user(telegram_user_id=123, username=None, first_name=None, last_name=None)
    await TariffsRepository(db).upsert(
        code="m1",
        title="Month",
        description="",
        price_amount=1000,
        currency="RUB",
        duration_days=30,
    )
    await db.commit()

    settings = Settings(
        bot_token="test",
        telegram_group_id=-100,
        database_path=tmp_path / "test.sqlite3",
        payment_provider="mock",
    )
    service = PaymentService(db, settings, RecordingLavaClient())
    created = await service.create_payment_for_tariff(user.telegram_user_id, "m1")

    first = await service.confirm_mock_payment(created.payment.internal_invoice_id)
    second = await service.confirm_mock_payment(created.payment.internal_invoice_id)
    updated_user = await UsersRepository(db).get_by_telegram_id(user.telegram_user_id)
    updated_payment = await PaymentsRepository(db).get_by_internal_invoice_id(created.payment.internal_invoice_id)

    assert first.already_applied is False
    assert second.already_applied is True
    assert updated_user.access_until == first.access_extension.new_access_until
    assert updated_payment.status == "applied"


async def test_manual_access_grant_extends_from_current_access_and_writes_event(db) -> None:
    users = UsersRepository(db)
    user = await users.upsert_telegram_user(telegram_user_id=123, username=None, first_name=None, last_name=None)
    current_until = datetime(2026, 1, 20, tzinfo=UTC)
    granted_at = datetime(2026, 1, 10, tzinfo=UTC)
    await users.set_access_until(user.id, current_until)
    await db.commit()

    grant = await grant_manual_access(
        db,
        telegram_user_id=123,
        duration_days=5,
        granted_by_telegram_user_id=999,
        granted_at=granted_at,
    )
    await db.commit()

    cursor = await db.execute("SELECT * FROM access_events WHERE telegram_user_id = ?", (123,))
    row = await cursor.fetchone()
    assert row is not None
    details = loads(row["details"])
    assert grant.user.access_until == current_until + timedelta(days=5)
    assert row["event_type"] == "manual_grant"
    assert details["granted_by_telegram_user_id"] == 999
    assert details["duration_days"] == 5
