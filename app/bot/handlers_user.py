from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from app.bot.filters import PRIVATE_CHAT_FILTER
from app.bot.keyboards import (
    USER_ACCESS_BUTTON,
    USER_BENEFITS_BUTTON,
    USER_DOCUMENTS_BUTTON,
    USER_SUPPORT_BUTTON,
    USER_TARIFFS_BUTTON,
    USER_TRIAL_BUTTON,
    document_page_keyboard,
    documents_keyboard,
    is_reply_button_text,
    main_menu_keyboard,
    payment_agreement_keyboard,
    payment_url_keyboard,
    promo_redeem_keyboard,
    tariffs_keyboard,
)
from app.config import Settings
from app.db.connection import open_database
from app.db.repositories import PromoCodesRepository, TariffRecord, TariffsRepository, UsersRepository
from app.legal.documents import load_legal_document_page, render_legal_document_page
from app.messages import message as text
from app.services.invites import InviteService
from app.services.lava import LavaClient
from app.services.payments import PaymentService
from app.services.trials import TrialService
from app.services.promos import PromoService
from app.services.admin_notify import notify_admins
from app.utils.datetime import format_datetime_moscow, utc_now


router = Router(name="user")
router.message.filter(PRIVATE_CHAT_FILTER)

SUPPORT_TEXT = text("user.support")
COMMUNITY_BENEFITS_TEXT = text("user.community_benefits")
DOCUMENTS_PROMPT = text("user.documents_prompt")
NO_ACTIVE_ACCESS_TEXT = text("user.no_active_access")


def _is_admin(message: Message, settings: Settings) -> bool:
    user = message.from_user
    if user is None:
        return False
    return settings.is_admin(user.id, user.username)


async def _answer_tariffs(message: Message, settings: Settings) -> None:
    async with open_database(settings.database_path) as db:
        items = await TariffsRepository(db).list_active()
    if not items:
        await message.answer(text("user.tariffs_empty"))
        return
    await message.answer(text("user.tariffs_prompt"), reply_markup=tariffs_keyboard(items))


def _payment_agreement_text(tariff: TariffRecord) -> str:
    return text(
        "user.payment_agreement",
        title=tariff.title,
        price_amount=tariff.price_amount,
        currency=tariff.currency,
        duration_days=tariff.duration_days,
    )


async def _show_payment_agreement(callback: CallbackQuery, settings: Settings, tariff_code: str) -> None:
    async with open_database(settings.database_path) as db:
        tariff = await TariffsRepository(db).get_by_code(tariff_code, active_only=True)
    if tariff is None:
        await callback.answer(text("user.tariff_not_found"), show_alert=True)
        return
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            _payment_agreement_text(tariff),
            reply_markup=payment_agreement_keyboard(tariff.code),
        )
    await callback.answer()


async def _answer_documents(message: Message) -> None:
    await message.answer(DOCUMENTS_PROMPT, reply_markup=documents_keyboard())


async def _answer_menu(message: Message, settings: Settings) -> None:
    trial_available = False
    if message.from_user is not None:
        async with open_database(settings.database_path) as db:
            user = await UsersRepository(db).get_by_telegram_id(message.from_user.id)
            if user is not None:
                trial_available = await TrialService(db).is_eligible(user)
    await message.answer(
        text("user.choose_action"),
        reply_markup=main_menu_keyboard(is_admin=_is_admin(message, settings), trial_available=trial_available),
    )


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
            await message.answer(NO_ACTIVE_ACCESS_TEXT)
            return
        invite_service = InviteService(db, settings, message.bot)
        try:
            link = await invite_service.ensure_personal_invite(message.from_user.id)
        except TelegramBadRequest:
            await message.answer(text("user.access_invite_error"))
            return
        if link:
            await message.answer(
                text("user.access_with_link", access_until=format_datetime_moscow(user.access_until), link=link)
            )
        else:
            await message.answer(text("user.access_already_member", access_until=format_datetime_moscow(user.access_until)))


@router.message(CommandStart())
async def start(message: Message, settings: Settings) -> None:
    if message.from_user is None:
        return
    async with open_database(settings.database_path) as db:
        users = UsersRepository(db)
        user = await users.upsert_telegram_user(
            telegram_user_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name,
        )
        trial_available = await TrialService(db).is_eligible(user)
        payload = (message.text or "").partition(" ")[2].strip()
        promo = None
        if payload.startswith("promo_"):
            code = payload.removeprefix("promo_").upper()
            if 1 <= len(code) <= 32 and code.isalnum():
                promo = await PromoCodesRepository(db).get_by_code(code)
        await db.commit()
    if promo is not None and promo.status == "created":
        await message.answer(
            text("user.promo_offer", duration_days=promo.duration_days),
            reply_markup=promo_redeem_keyboard(promo.id),
        )
        return
    if payload.startswith("promo_"):
        await message.answer(text("user.promo_unavailable"))
        return
    await message.answer(
        text("user.welcome"),
        reply_markup=main_menu_keyboard(is_admin=_is_admin(message, settings), trial_available=trial_available),
    )


@router.callback_query(F.data.startswith("pr:redeem:"))
async def redeem_promo(callback: CallbackQuery, settings: Settings) -> None:
    if callback.from_user is None or callback.data is None:
        return
    try:
        promo_id = int(callback.data.removeprefix("pr:redeem:"))
    except ValueError:
        await callback.answer(text("user.promo_unavailable"), show_alert=True)
        return
    async with open_database(settings.database_path) as db:
        service = PromoService(db)
        result = await service.redeem(
            promo_id, callback.from_user.id, callback.from_user.username,
            callback.from_user.first_name, callback.from_user.last_name,
        )
        if result.status != "redeemed" or result.user is None or result.promo is None or result.access_until is None:
            await callback.answer(text("user.promo_unavailable"), show_alert=True)
            return
        invite_service = InviteService(db, settings, callback.bot)
        try:
            link = await invite_service.ensure_personal_invite(result.user.telegram_user_id)
        except TelegramBadRequest:
            link = ""
            invite_error = True
        else:
            invite_error = False
    if invite_error:
        await callback.message.answer(text("user.promo_activated_invite_error", access_until=format_datetime_moscow(result.access_until)))
    elif link:
        await callback.message.answer(text("user.promo_activated_with_link", access_until=format_datetime_moscow(result.access_until), link=link))
    else:
        await callback.message.answer(text("user.promo_activated_already_member", access_until=format_datetime_moscow(result.access_until)))
    participant = f"@{result.user.username}" if result.user.username else (result.user.first_name or str(result.user.telegram_user_id))
    await notify_admins(settings, callback.bot, text(
        "admin.promo_redeemed", participant=participant, code=result.promo.code,
        duration_days=result.promo.duration_days, access_until=format_datetime_moscow(result.access_until),
    ))
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer()


@router.message(F.text.func(lambda text: is_reply_button_text(text, USER_TARIFFS_BUTTON)))
async def tariffs_button(message: Message, settings: Settings) -> None:
    await _answer_tariffs(message, settings)


@router.message(F.text.func(lambda text: is_reply_button_text(text, USER_TRIAL_BUTTON)))
async def trial_button(message: Message, settings: Settings) -> None:
    if message.from_user is None:
        return
    async with open_database(settings.database_path) as db:
        trial_service = TrialService(db)
        result = await trial_service.grant(
            telegram_user_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name,
        )
        if result.status == "disabled":
            await message.answer(text("user.trial_disabled"))
            return
        if result.status == "already_used":
            await message.answer(text("user.trial_already_used"))
            return
        if result.status == "active_access":
            await message.answer(
                text("user.trial_active_access", access_until=format_datetime_moscow(result.expires_at))
            )
            return

        invite_service = InviteService(db, settings, message.bot)
        try:
            link = await invite_service.ensure_personal_invite(message.from_user.id)
        except TelegramBadRequest:
            await message.answer(
                text("user.trial_activated_invite_error", access_until=format_datetime_moscow(result.expires_at))
            )
            return
        if link:
            await message.answer(
                text("user.trial_activated_with_link", access_until=format_datetime_moscow(result.expires_at), link=link)
            )
        else:
            await message.answer(text("user.trial_activated_already_member", access_until=format_datetime_moscow(result.expires_at)))


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
            text("user.mock_payment_created"),
            reply_markup=payment_url_keyboard(created.payment_url),
        )
    else:
        await callback.message.answer(
            text("user.payment_link_created"),
            reply_markup=payment_url_keyboard(created.payment_url),
        )
    await callback.answer()


@router.message(F.text.func(lambda text: is_reply_button_text(text, USER_DOCUMENTS_BUTTON)))
async def documents_button(message: Message) -> None:
    await _answer_documents(message)


@router.callback_query(F.data == "docs:list")
async def documents_list(callback: CallbackQuery) -> None:
    if isinstance(callback.message, Message):
        await callback.message.edit_text(DOCUMENTS_PROMPT, reply_markup=documents_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("doc:"))
async def legal_document_page(callback: CallbackQuery) -> None:
    if callback.data is None:
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer(text("user.document_not_found"), show_alert=True)
        return
    _, document_key, raw_page = parts
    try:
        page_number = int(raw_page)
    except ValueError:
        await callback.answer(text("user.document_not_found"), show_alert=True)
        return
    page = load_legal_document_page(document_key, page_number)
    if page is None:
        await callback.answer(text("user.document_not_found"), show_alert=True)
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
        await callback.answer(text("user.document_not_found"), show_alert=True)
        return
    try:
        page_number = int(raw_page)
    except ValueError:
        await callback.answer(text("user.document_not_found"), show_alert=True)
        return
    page = load_legal_document_page(document_key, page_number)
    if page is None:
        await callback.answer(text("user.document_not_found"), show_alert=True)
        return
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            render_legal_document_page(page),
            reply_markup=document_page_keyboard(page.meta.key, page.page_number, page.total_pages, tariff_code),
        )
    await callback.answer()


@router.message(F.text.func(lambda text: is_reply_button_text(text, USER_SUPPORT_BUTTON)))
async def support_button(message: Message) -> None:
    await message.answer(SUPPORT_TEXT)


@router.message(F.text.func(lambda text: is_reply_button_text(text, USER_BENEFITS_BUTTON)))
async def community_benefits_button(message: Message) -> None:
    await message.answer(COMMUNITY_BENEFITS_TEXT, parse_mode=ParseMode.MARKDOWN)


@router.message(F.text.func(lambda text: is_reply_button_text(text, USER_ACCESS_BUTTON)))
async def access_button(message: Message, settings: Settings) -> None:
    await _answer_access(message, settings)


@router.message(F.text)
async def unknown_text(message: Message, settings: Settings) -> None:
    await _answer_menu(message, settings)
