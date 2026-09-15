from datetime import UTC, datetime

from app.config import Settings
from app.db.connection import connect_database
from app.db.migrations import run_migrations
from app.db.repositories import PaymentsRepository, TariffsRepository, UsersRepository
from app.services.lava import LavaPaymentNotification
from app.services.payments import PaymentService


class RecordingBot:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        if self.fail:
            raise RuntimeError("blocked")
        self.messages.append((chat_id, text))


async def _applied_service(tmp_path, provider: str = "lava"):
    path = tmp_path / f"{provider}.sqlite3"
    await run_migrations(str(path))
    db = await connect_database(path)
    user = await UsersRepository(db).upsert_telegram_user(1, "buyer", "Buyer", None)
    tariff = await TariffsRepository(db).upsert("m", "Month", "", 1500, "RUB", 30)
    payment = await PaymentsRepository(db).create(user.id, tariff.id, f"order-{provider}", 1500, "RUB", "url", "invoice", None, provider)
    await db.commit()
    settings = Settings(bot_token="test", telegram_group_id=-100, database_path=path, admin_ids_raw="10,20")
    service = PaymentService(db, settings, lava_client=None)
    result = await service.handle_paid(LavaPaymentNotification(payment.order_id, payment.invoice_id, "paid", None, None, datetime.now(UTC), {}), expected_provider=provider)
    return db, service, result


async def test_payment_admin_notification_is_once_and_marks_mock(tmp_path) -> None:
    db, service, result = await _applied_service(tmp_path, "mock")
    bot = RecordingBot()
    await service.notify_admins_if_newly_applied(bot, result)
    await service.notify_admins_if_newly_applied(bot, result)
    payment = await service.payments.get_by_id(result.payment_id)
    await db.close()
    assert len(bot.messages) == 2
    assert all("🧪" in body for _, body in bot.messages)
    assert payment.admin_notification_attempted_at is not None


async def test_payment_notification_failure_keeps_paid_access_and_records_event(tmp_path) -> None:
    db, service, result = await _applied_service(tmp_path, "lava")
    await service.notify_admins_if_newly_applied(RecordingBot(fail=True), result)
    payment = await service.payments.get_by_id(result.payment_id)
    user = await UsersRepository(db).get_by_telegram_id(1)
    rows = await db.execute_fetchall("SELECT event_type FROM access_events WHERE event_type = 'payment_admin_notification_failed'")
    await db.close()
    assert payment.applied_at is not None
    assert user.access_until is not None
    assert len(rows) == 1
