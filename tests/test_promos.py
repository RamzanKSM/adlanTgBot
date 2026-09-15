import asyncio
from datetime import UTC, datetime, timedelta
from io import BytesIO

from PIL import Image

from app.bot.keyboards import promo_duration_keyboard, promo_redeem_keyboard
from app.bot.handlers_chat_member import _join_source
from app.config import Settings
from app.db.connection import connect_database
from app.db.migrations import run_migrations
from app.db.repositories import InviteLinksRepository, PaymentsRepository, PromoCodesRepository, TariffsRepository, TrialAccessesRepository, TrialSettingsRepository, UsersRepository
from app.services.promos import PromoService
from app.services.trials import TrialService
from app.utils.qr import qr_png


async def test_migration_six_leaves_existing_invite_provenance_empty(tmp_path) -> None:
    path = tmp_path / "migration.sqlite3"
    await run_migrations(str(path))
    db = await connect_database(path)
    user = await UsersRepository(db).upsert_telegram_user(1, None, None, None)
    invite = await InviteLinksRepository(db).create(user.id, None, "https://t.me/+old", None, datetime.now(UTC) + timedelta(days=1))
    await db.execute("UPDATE invite_links SET access_kind = NULL, access_source_id = NULL WHERE id = ?", (invite.id,))
    await db.execute("DELETE FROM schema_migrations WHERE version = 6")
    await db.commit()
    await db.close()
    await run_migrations(str(path))
    db = await connect_database(path)
    row = await (await db.execute("SELECT access_kind, access_source_id FROM invite_links WHERE id = ?", (invite.id,))).fetchone()
    versions = await db.execute_fetchall("SELECT version FROM schema_migrations WHERE version = 6")
    await db.close()
    assert row["access_kind"] is None and row["access_source_id"] is None
    assert len(versions) == 1


async def test_promo_redeem_is_atomic_stacks_and_blocks_trial(tmp_path) -> None:
    path = tmp_path / "promo.sqlite3"
    await run_migrations(str(path))
    setup = await connect_database(path)
    service = PromoService(setup)
    first = await service.create(7, 900)
    second = await service.create(30, 900)
    await setup.close()

    async def redeem_first():
        db = await connect_database(path)
        try:
            return await PromoService(db).redeem(first.id, 42, "buyer", "Buyer", None)
        finally:
            await db.close()

    results = await asyncio.gather(redeem_first(), redeem_first())
    assert [item.status for item in results].count("redeemed") == 1
    db = await connect_database(path)
    stacked = await PromoService(db).redeem(second.id, 42, "buyer", "Buyer", None)
    user = await UsersRepository(db).get_by_telegram_id(42)
    assert stacked.status == "redeemed"
    assert user.access_kind == "promo" and user.access_source_id == second.id
    assert user.access_until is not None and stacked.access_until is not None
    assert user.access_until == stacked.access_until
    await TrialSettingsRepository(db).set(True, 3, 900)
    await UsersRepository(db).set_access_until(user.id, datetime.now(UTC) - timedelta(seconds=1))
    user = await UsersRepository(db).get_by_telegram_id(42)
    assert not await TrialService(db).is_eligible(user)
    await db.close()


async def test_cancelled_and_redeemed_promos_cannot_be_activated(tmp_path) -> None:
    path = tmp_path / "cancelled.sqlite3"
    await run_migrations(str(path))
    db = await connect_database(path)
    service = PromoService(db)
    promo = await service.create(7, 900)
    assert await service.codes.cancel(promo.id)
    await db.commit()
    result = await service.redeem(promo.id, 1, None, None, None)
    assert result.status == "cancelled"
    await db.close()


def test_promo_inline_controls_and_qr_payload_are_bounded() -> None:
    menu = promo_duration_keyboard()
    assert [button.callback_data for button in menu.inline_keyboard[0]] == ["ap:pick:7", "ap:pick:30", "ap:pick:60"]
    selector = promo_duration_keyboard(1000)
    assert all(len(button.callback_data or "") <= 64 for row in selector.inline_keyboard for button in row)
    redeem = promo_redeem_keyboard(123)
    assert redeem.inline_keyboard[0][0].callback_data == "pr:redeem:123"
    image = Image.open(BytesIO(qr_png("https://t.me/example_bot?start=promo_ABCDEFGH")))
    assert image.format == "PNG" and image.width > 20


async def test_new_invite_snapshots_current_promo_provenance(tmp_path) -> None:
    path = tmp_path / "provenance.sqlite3"
    await run_migrations(str(path))
    db = await connect_database(path)
    user = await UsersRepository(db).upsert_telegram_user(10, None, None, None)
    await UsersRepository(db).set_access(user.id, datetime.now(UTC) + timedelta(days=7), "promo", 44)
    invite = await InviteLinksRepository(db).create(user.id, None, "https://t.me/+promo", None, datetime.now(UTC) + timedelta(days=1), "promo", 44)
    await db.commit()
    assert invite.access_kind == "promo" and invite.access_source_id == 44
    await db.close()


async def test_join_source_uses_snapshot_or_reports_unknown(tmp_path) -> None:
    path = tmp_path / "join-source.sqlite3"
    await run_migrations(str(path))
    db = await connect_database(path)
    users = UsersRepository(db)
    user = await users.upsert_telegram_user(1, "buyer", "Buyer", None)
    tariff = await TariffsRepository(db).upsert("month", "Месяц", "", 1500, "RUB", 30)
    payment = await PaymentsRepository(db).create(user.id, tariff.id, "source-payment", 1500, "RUB", None, None, None)
    trial = await TrialAccessesRepository(db).create(user.id, 1, datetime.now(UTC), datetime.now(UTC) + timedelta(days=3))
    promo = await PromoService(db).create(7, 900)
    paid_invite = await InviteLinksRepository(db).create(user.id, payment.id, "https://t.me/+paid", None, datetime.now(UTC) + timedelta(days=1), "paid", payment.id)
    trial_invite = await InviteLinksRepository(db).create(user.id, None, "https://t.me/+trial", None, datetime.now(UTC) + timedelta(days=1), "trial", trial.id)
    promo_invite = await InviteLinksRepository(db).create(user.id, None, "https://t.me/+promo2", None, datetime.now(UTC) + timedelta(days=1), "promo", promo.id)
    unknown_invite = await InviteLinksRepository(db).create(user.id, None, "https://t.me/+unknown", None, datetime.now(UTC) + timedelta(days=1))
    assert "оплата" in await _join_source(db, paid_invite)
    assert "пробный" in await _join_source(db, trial_invite)
    assert promo.code in await _join_source(db, promo_invite)
    assert "не зафиксирован" in await _join_source(db, unknown_invite)
    await db.close()
