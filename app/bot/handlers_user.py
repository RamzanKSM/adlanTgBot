from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from app.bot.filters import PRIVATE_CHAT_FILTER
from app.bot.keyboards import (
    USER_ACCESS_BUTTON,
    USER_DOCUMENTS_BUTTON,
    USER_SUPPORT_BUTTON,
    USER_TARIFFS_BUTTON,
    document_page_keyboard,
    documents_keyboard,
    main_menu_keyboard,
    payment_agreement_keyboard,
    tariffs_keyboard,
)
from app.config import Settings
from app.db.connection import open_database
from app.db.repositories import TariffRecord, TariffsRepository, UsersRepository
from app.legal.documents import load_legal_document_page, render_legal_document_page
from app.services.invites import InviteService
from app.services.lava import LavaClient
from app.services.payments import PaymentService
from app.utils.datetime import format_datetime_moscow, utc_now


router = Router(name="user")
router.message.filter(PRIVATE_CHAT_FILTER)


def _is_admin(message: Message, settings: Settings) -> bool:
    user = message.from_user
    if user is None:
        return False
    return settings.is_admin(user.id, user.username)


async def _answer_tariffs(message: Message, settings: Settings) -> None:
    async with open_database(settings.database_path) as db:
        items = await TariffsRepository(db).list_active()
    if not items:
        await message.answer("Активных тарифов пока нет.")
        return
    await message.answer("Выберите тариф:", reply_markup=tariffs_keyboard(items))


def _payment_agreement_text(tariff: TariffRecord) -> str:
    return (
        "Перед оплатой ознакомьтесь с документами.\n\n"
        f"Тариф: {tariff.title}\n"
        f"Стоимость: {tariff.price_amount} {tariff.currency}\n"
        f"Срок доступа: {tariff.duration_days} дн.\n\n"
        "Нажимая «✅ Принимаю и перейти к оплате», вы подтверждаете, что принимаете "
        "публичную оферту, политику конфиденциальности, условия возврата и правила сообщества."
    )


async def _show_payment_agreement(callback: CallbackQuery, settings: Settings, tariff_code: str) -> None:
    async with open_database(settings.database_path) as db:
        tariff = await TariffsRepository(db).get_by_code(tariff_code, active_only=True)
    if tariff is None:
        await callback.answer("Тариф не найден или отключен.", show_alert=True)
        return
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            _payment_agreement_text(tariff),
            reply_markup=payment_agreement_keyboard(tariff.code),
        )
    await callback.answer()


async def _answer_documents(message: Message) -> None:
    await message.answer("Выберите документ:", reply_markup=documents_keyboard())


async def _answer_access(message: Message, settings: Settings) -> None:
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
            await message.answer(f"Активного доступа нет. Нажмите «{USER_TARIFFS_BUTTON}», чтобы выбрать тариф.")
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
    await message.answer(
        "Вы зарегистрированы. Выберите действие на клавиатуре.",
        reply_markup=main_menu_keyboard(is_admin=_is_admin(message, settings)),
    )


@router.message(Command("tariffs"))
async def tariffs(message: Message, settings: Settings) -> None:
    await _answer_tariffs(message, settings)


@router.message(F.text == USER_TARIFFS_BUTTON)
async def tariffs_button(message: Message, settings: Settings) -> None:
    await _answer_tariffs(message, settings)


@router.callback_query(F.data.startswith("buy:"))
async def buy_tariff(callback: CallbackQuery, settings: Settings) -> None:
    if callback.data is None:
        return
    await _show_payment_agreement(callback, settings, callback.data.removeprefix("buy:"))


@router.callback_query(F.data.startswith("agree:"))
async def back_to_payment_agreement(callback: CallbackQuery, settings: Settings) -> None:
    if callback.data is None:
        return
    await _show_payment_agreement(callback, settings, callback.data.removeprefix("agree:"))


@router.callback_query(F.data.startswith("pay:"))
async def pay_tariff(callback: CallbackQuery, settings: Settings, lava_client: LavaClient) -> None:
    if callback.from_user is None or callback.data is None:
        return
    tariff_code = callback.data.removeprefix("pay:")
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


@router.message(F.text == USER_DOCUMENTS_BUTTON)
async def documents_button(message: Message) -> None:
    await _answer_documents(message)


@router.callback_query(F.data == "docs:list")
async def documents_list(callback: CallbackQuery) -> None:
    if isinstance(callback.message, Message):
        await callback.message.edit_text("Выберите документ:", reply_markup=documents_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("doc:"))
async def legal_document_page(callback: CallbackQuery) -> None:
    if callback.data is None:
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Документ не найден.", show_alert=True)
        return
    _, document_key, raw_page = parts
    try:
        page_number = int(raw_page)
    except ValueError:
        await callback.answer("Документ не найден.", show_alert=True)
        return
    page = load_legal_document_page(document_key, page_number)
    if page is None:
        await callback.answer("Документ не найден.", show_alert=True)
        return
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            render_legal_document_page(page),
            reply_markup=document_page_keyboard(page.meta.key, page.page_number, page.total_pages),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("pdoc:"))
async def payment_legal_document_page(callback: CallbackQuery) -> None:
    if callback.data is None:
        return
    try:
        tariff_code, document_key, raw_page = callback.data.removeprefix("pdoc:").rsplit(":", 2)
    except ValueError:
        await callback.answer("Документ не найден.", show_alert=True)
        return
    try:
        page_number = int(raw_page)
    except ValueError:
        await callback.answer("Документ не найден.", show_alert=True)
        return
    page = load_legal_document_page(document_key, page_number)
    if page is None:
        await callback.answer("Документ не найден.", show_alert=True)
        return
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            render_legal_document_page(page),
            reply_markup=document_page_keyboard(page.meta.key, page.page_number, page.total_pages, tariff_code),
        )
    await callback.answer()


@router.message(F.text == USER_SUPPORT_BUTTON)
async def support_button(message: Message) -> None:
    await message.answer("Поддержка: @gymvash\nОбычно отвечаем в течение 12 часов.")


@router.message(Command("access"))
async def access(message: Message, settings: Settings) -> None:
    await _answer_access(message, settings)


@router.message(F.text == USER_ACCESS_BUTTON)
async def access_button(message: Message, settings: Settings) -> None:
    await _answer_access(message, settings)
