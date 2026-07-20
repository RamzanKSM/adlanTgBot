import asyncio
from datetime import UTC, datetime, timedelta
from json import loads

import pytest

from app.config import Settings
from app.db.connection import connect_database
from app.db.migrations import run_migrations
from app.db.repositories import PaymentsRepository, TariffsRepository, UsersRepository
from app.services.access import grant_manual_access
from app.services.lava import LavaInvoice, LavaPaymentNotification
from app.services.payments import PaymentService
from app.utils.datetime import iso_to_datetime


@pytest.fixture
async def db(tmp_path):
    database_path = tmp_path / "test.sqlite3"
    await run_migrations(str(database_path))
    connection = await connect_database(database_path)
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
        order_id="order-1",
        amount=1000,
        currency="RUB",
        payment_url="https://pay.example/1",
        invoice_id="lava-1",
        expires_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    await db.commit()

    assert payment.order_id == "order-1"
    assert payment.invoice_id == "lava-1"
    assert (await PaymentsRepository(db).get_by_order_id("order-1")).id == payment.id
    assert (await PaymentsRepository(db).get_by_invoice_id("lava-1")).id == payment.id
    assert (await UsersRepository(db).get_by_telegram_id(123)).id == user.id
    assert (await TariffsRepository(db).get_by_code("m1")).id == tariff.id


async def test_migrations_rename_legacy_payment_columns(tmp_path) -> None:
    database_path = tmp_path / "legacy.sqlite3"
    connection = await connect_database(database_path)
    await connection.executescript(
        """
        CREATE TABLE payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            tariff_id INTEGER NOT NULL,
            provider TEXT NOT NULL DEFAULT 'lava',
            provider_invoice_id TEXT,
            internal_invoice_id TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            amount INTEGER NOT NULL,
            currency TEXT NOT NULL,
            payment_url TEXT,
            created_at TEXT NOT NULL,
            paid_at TEXT,
            expires_at TEXT,
            raw_payload TEXT NOT NULL DEFAULT '{}',
            applied_at TEXT
        );
        CREATE INDEX idx_payments_provider_invoice_id ON payments(provider_invoice_id);
        INSERT INTO payments (
            user_id, tariff_id, provider, provider_invoice_id, internal_invoice_id, status,
            amount, currency, created_at
        )
        VALUES (1, 1, 'lava', 'lava-1', 'order-1', 'pending', 1000, 'RUB', '2026-01-01T00:00:00+00:00');
        """
    )
    await connection.commit()
    await connection.close()

    await run_migrations(str(database_path))

    connection = await connect_database(database_path)
    try:
        columns = {row["name"] for row in await connection.execute_fetchall("PRAGMA table_info(payments)")}
        indexes = {row["name"] for row in await connection.execute_fetchall("PRAGMA index_list(payments)")}
        cursor = await connection.execute("SELECT order_id, invoice_id FROM payments WHERE id = 1")
        row = await cursor.fetchone()
    finally:
        await connection.close()

    assert "order_id" in columns
    assert "invoice_id" in columns
    assert "internal_invoice_id" not in columns
    assert "provider_invoice_id" not in columns
    assert "idx_payments_invoice_id" in indexes
    assert "idx_payments_provider_invoice_id" not in indexes
    assert row["order_id"] == "order-1"
    assert row["invoice_id"] == "lava-1"


async def test_migrations_preserve_populated_legacy_database_and_are_repeatable(tmp_path) -> None:
    database_path = tmp_path / "legacy-populated.sqlite3"
    connection = await connect_database(database_path)
    await connection.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_user_id INTEGER NOT NULL UNIQUE,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE tariffs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            price_amount INTEGER NOT NULL,
            duration_days INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            tariff_id INTEGER NOT NULL,
            provider_invoice_id TEXT,
            internal_invoice_id TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            amount INTEGER NOT NULL,
            currency TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        INSERT INTO users (telegram_user_id, username, created_at, updated_at)
        VALUES (1001, 'legacy_user', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00');
        INSERT INTO tariffs (code, title, price_amount, duration_days, created_at, updated_at)
        VALUES ('legacy', 'Legacy tariff', 1500, 30, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00');
        INSERT INTO payments (
            user_id, tariff_id, provider_invoice_id, internal_invoice_id, status,
            amount, currency, created_at
        )
        VALUES (1, 1, 'provider-legacy-1', 'order-legacy-1', 'paid', 1500, 'RUB', '2026-01-01T00:00:00+00:00');
        """
    )
    await connection.commit()
    await connection.close()

    await run_migrations(str(database_path))
    await run_migrations(str(database_path))

    connection = await connect_database(database_path)
    try:
        user = await (await connection.execute("SELECT * FROM users WHERE id = 1")).fetchone()
        tariff = await (await connection.execute("SELECT * FROM tariffs WHERE id = 1")).fetchone()
        payment = await (await connection.execute("SELECT * FROM payments WHERE id = 1")).fetchone()
        versions = await connection.execute_fetchall("SELECT version FROM schema_migrations ORDER BY version")
    finally:
        await connection.close()

    assert user["telegram_user_id"] == 1001
    assert user["is_in_group"] == 0
    assert tariff["code"] == "legacy"
    assert tariff["currency"] == "RUB"
    assert payment["order_id"] == "order-legacy-1"
    assert payment["invoice_id"] == "provider-legacy-1"
    assert user["created_at"] == "2026-01-01T03:00:00+03:00"
    assert tariff["updated_at"] == "2026-01-01T03:00:00+03:00"
    assert payment["created_at"] == "2026-01-01T03:00:00+03:00"
    assert [row["version"] for row in versions] == [1, 2, 3, 4]


async def test_timestamp_migration_converts_populated_v3_database_once(tmp_path) -> None:
    database_path = tmp_path / "timestamps-v3.sqlite3"
    await run_migrations(str(database_path))

    connection = await connect_database(database_path)
    await connection.executescript(
        """
        DELETE FROM schema_migrations WHERE version = 4;
        INSERT INTO users (
            id, telegram_user_id, access_until, warned_access_until, created_at, updated_at
        ) VALUES (1, 1001, '2026-07-01T23:39:13.768248+00:00',
                  '2026-07-01T23:39:13.768248+00:00',
                  '2026-07-01T23:39:13.768248+00:00',
                  '2026-07-01T23:39:13.768248+00:00');
        INSERT INTO tariffs (
            id, code, title, price_amount, duration_days, created_at, updated_at
        ) VALUES (1, 'm1', 'Month', 1000, 30,
                  '2026-07-01T23:39:13.768248+00:00',
                  '2026-07-01T23:39:13.768248+00:00');
        INSERT INTO payments (
            id, user_id, tariff_id, order_id, status, amount, currency,
            created_at, paid_at, expires_at, applied_at
        ) VALUES (1, 1, 1, 'order-1', 'applied', 1000, 'RUB',
                  '2026-07-01T23:39:13.768248+00:00',
                  '2026-07-01T23:39:13.768248+00:00',
                  '2026-07-01T23:39:13.768248+00:00',
                  '2026-07-01T23:39:13.768248+00:00');
        INSERT INTO invite_links (
            id, user_id, payment_id, invite_link, status, expires_at, created_at, used_at, revoked_at
        ) VALUES (1, 1, 1, 'https://t.me/+invite', 'used',
                  '2026-07-01T23:39:13.768248+00:00',
                  '2026-07-01T23:39:13.768248+00:00',
                  '2026-07-01T23:39:13.768248+00:00',
                  '2026-07-01T23:39:13.768248+00:00');
        INSERT INTO access_events (id, user_id, telegram_user_id, event_type, created_at)
        VALUES (1, 1, 1001, 'legacy', '2026-07-01T23:39:13.768248+00:00');
        UPDATE schema_migrations
        SET applied_at = '2026-07-01T23:39:13.768248+00:00';
        """
    )
    await connection.commit()
    await connection.close()

    await run_migrations(str(database_path))
    await run_migrations(str(database_path))

    connection = await connect_database(database_path)
    try:
        timestamp_columns = {
            "users": ("access_until", "warned_access_until", "created_at", "updated_at"),
            "tariffs": ("created_at", "updated_at"),
            "payments": ("created_at", "paid_at", "expires_at", "applied_at"),
            "invite_links": ("expires_at", "created_at", "used_at", "revoked_at"),
            "access_events": ("created_at",),
        }
        for table_name, columns in timestamp_columns.items():
            row = await (await connection.execute(f"SELECT * FROM {table_name} WHERE id = 1")).fetchone()
            assert row is not None
            for column_name in columns:
                assert row[column_name] == "2026-07-02T02:39:13.768248+03:00"
                assert iso_to_datetime(row[column_name]) == datetime(2026, 7, 1, 23, 39, 13, 768248, tzinfo=UTC)
        versions = await connection.execute_fetchall("SELECT version FROM schema_migrations ORDER BY version")
    finally:
        await connection.close()

    assert [row["version"] for row in versions] == [1, 2, 3, 4]


async def test_migrations_serialize_concurrent_startup(tmp_path) -> None:
    database_path = tmp_path / "concurrent.sqlite3"

    await asyncio.gather(*(run_migrations(str(database_path)) for _ in range(4)))

    connection = await connect_database(database_path)
    try:
        versions = await connection.execute_fetchall("SELECT version FROM schema_migrations ORDER BY version")
        tables = await connection.execute_fetchall("SELECT name FROM sqlite_master WHERE type = 'table'")
    finally:
        await connection.close()

    assert [row["version"] for row in versions] == [1, 2, 3, 4]
    assert {row["name"] for row in tables} >= {
        "users",
        "tariffs",
        "payments",
        "invite_links",
        "access_events",
        "schema_migrations",
    }


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
        order_id="order-1",
        amount=1000,
        currency="RUB",
        payment_url="https://pay.example/1",
        invoice_id=None,
        expires_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    await db.commit()

    settings = Settings(bot_token="test", telegram_group_id=-100, database_path=tmp_path / "test.sqlite3")
    service = PaymentService(db, settings, lava_client=None)
    paid_at = datetime(2026, 1, 10, tzinfo=UTC)
    notification = LavaPaymentNotification(
        order_id="order-1",
        invoice_id="lava-1",
        status="paid",
        amount=1000,
        currency="RUB",
        paid_at=paid_at,
        raw_payload={"status": "paid"},
    )

    first = await service.handle_paid(notification)
    second = await service.handle_paid(notification)
    updated_user = await users.get_by_telegram_id(123)
    updated_payment = await payments.get_by_order_id("order-1")

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
            invoice_id="lava-1",
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
    assert created.payment.payment_url == f"http://localhost:8000/mock/payments/{created.payment.order_id}/pay"
    assert created.payment.invoice_id == f"mock-{created.payment.order_id}"


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

    first = await service.confirm_mock_payment(created.payment.order_id)
    second = await service.confirm_mock_payment(created.payment.order_id)
    updated_user = await UsersRepository(db).get_by_telegram_id(user.telegram_user_id)
    updated_payment = await PaymentsRepository(db).get_by_order_id(created.payment.order_id)

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
