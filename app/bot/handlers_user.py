from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from app.bot.filters import PRIVATE_CHAT_FILTER
from app.bot.keyboards import tariffs_keyboard
from app.config import Settings
from app.db.connection import open_database
from app.db.repositories import TariffsRepository, UsersRepository
from app.services.invites import InviteService
from app.services.lava import LavaClient
from app.services.payments import PaymentService
from app.utils.datetime import format_datetime_moscow, utc_now


router = Router(name="user")
router.message.filter(PRIVATE_CHAT_FILTER)


@router.message(CommandStart())
async def start(message: Message, settings: Settings) -> None:
    if message.from_user is None:
        return
    async with open_database(settings.database_path) as db:
        users = UsersRepository(db)
        await users.upsert_telegram_user(
            telegram_user_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name,
        )
        await db.commit()
    await message.answer("Вы зарегистрированы. Используйте /tariffs для выбора тарифа или /access для проверки доступа.")


@router.message(Command("tariffs"))
async def tariffs(message: Message, settings: Settings) -> None:
    async with open_database(settings.database_path) as db:
        items = await TariffsRepository(db).list_active()
    if not items:
        await message.answer("Активных тарифов пока нет.")
        return
    await message.answer("Выберите тариф:", reply_markup=tariffs_keyboard(items))


@router.callback_query(F.data.startswith("buy:"))
async def buy_tariff(callback: CallbackQuery, settings: Settings, lava_client: LavaClient) -> None:
    if callback.from_user is None or callback.data is None:
        return
    tariff_code = callback.data.removeprefix("buy:")
    async with open_database(settings.database_path) as db:
        await UsersRepository(db).upsert_telegram_user(
            telegram_user_id=callback.from_user.id,
            username=callback.from_user.username,
            first_name=callback.from_user.first_name,
            last_name=callback.from_user.last_name,
        )
        service = PaymentService(db, settings, lava_client)
        try:
            created = await service.create_payment_for_tariff(callback.from_user.id, tariff_code)
        except ValueError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
    if created.payment.provider == "mock":
        await callback.message.answer(
            "Создан mock-платеж для локальной разработки. Это не реальная оплата.\n"
            "Для подтверждения откройте локальную ссылку:\n"
            f"{created.payment_url}"
        )
    else:
        await callback.message.answer(
            "Ссылка на оплату создана. После оплаты бот дождется webhook/status от Lava и сам выдаст доступ.\n"
            f"{created.payment_url}"
        )
    await callback.answer()


@router.message(Command("access"))
async def access(message: Message, settings: Settings) -> None:
    if message.from_user is None:
        return
    async with open_database(settings.database_path) as db:
        users = UsersRepository(db)
        user = await users.get_by_telegram_id(message.from_user.id)
        if user is None:
            user = await users.upsert_telegram_user(
                telegram_user_id=message.from_user.id,
                username=message.from_user.username,
                first_name=message.from_user.first_name,
                last_name=message.from_user.last_name,
            )
        if user.access_until is None or user.access_until <= utc_now():
            await db.commit()
            await message.answer("Активного доступа нет. Используйте /tariffs для оплаты.")
            return
        invite_service = InviteService(db, settings, message.bot)
        try:
            link = await invite_service.ensure_personal_invite(message.from_user.id)
        except TelegramBadRequest:
            await message.answer(
                "Доступ активен, но не удалось создать ссылку в группу. "
                "Проверьте TELEGRAM_GROUP_ID и права бота в группе."
            )
            return
        if link:
            await message.answer(f"Доступ активен до {format_datetime_moscow(user.access_until)}.\nВаша ссылка: {link}")
        else:
            await message.answer(f"Доступ активен до {format_datetime_moscow(user.access_until)}. Вы уже в группе.")
