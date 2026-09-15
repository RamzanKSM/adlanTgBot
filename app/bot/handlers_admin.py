import shlex
from dataclasses import dataclass

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message, User

from app.bot.filters import PRIVATE_CHAT_FILTER
from app.bot.keyboards import (
    ADMIN_DISABLE_TARIFF_BUTTON,
    ADMIN_DISABLE_TARIFF_CANCEL,
    ADMIN_DISABLE_TARIFF_CONFIRM_PREFIX,
    ADMIN_DISABLE_TARIFF_SELECT_PREFIX,
    ADMIN_TARIFFS_BUTTON,
    ADMIN_PROMO_BUTTON,
    admin_disable_tariff_confirm_keyboard,
    admin_disable_tariffs_keyboard,
    promo_admin_controls_keyboard,
    promo_card_keyboard,
    promo_confirm_keyboard,
    promo_duration_keyboard,
    is_reply_button_text,
)
from app.config import Settings
from app.db.connection import open_database
from app.db.repositories import AccessEventsRepository, TariffsRepository, TrialSettingsRepository
from app.messages import message as text
from app.services.promos import PromoService
from app.utils.datetime import format_datetime_moscow
from app.utils.qr import qr_png
from aiogram.types import BufferedInputFile


router = Router(name="admin")
router.message.filter(PRIVATE_CHAT_FILTER)


TARIFF_SET_USAGE = text("admin.tariff_set_usage")
TRIAL_SET_USAGE = text("admin.trial_set_usage")


@dataclass(frozen=True)
class TariffSetArgs:
    code: str
    title: str
    price_amount: int
    duration_days: int
    currency: str
    sort_order: int
    description: str


class TariffSetUsageError(ValueError):
    pass


class TariffSetValidationError(ValueError):
    pass


class TrialSetUsageError(ValueError):
    pass


class TrialSetValidationError(ValueError):
    pass


def parse_trial_set_args(command_text: str | None) -> int:
    try:
        args = shlex.split((command_text or "").partition(" ")[2])
    except ValueError as exc:
        raise TrialSetUsageError from exc
    if len(args) != 1:
        raise TrialSetUsageError
    try:
        duration_days = int(args[0])
    except ValueError as exc:
        raise TrialSetUsageError from exc
    if not 1 <= duration_days <= 1000:
        raise TrialSetValidationError(text("admin.trial_duration_invalid"))
    return duration_days


def parse_tariff_set_args(command_text: str | None) -> TariffSetArgs:
    try:
        args = shlex.split((command_text or "").partition(" ")[2])
    except ValueError as exc:
        raise TariffSetUsageError from exc

    if len(args) < 4:
        raise TariffSetUsageError

    try:
        price_amount = int(args[2])
    except ValueError as exc:
        raise TariffSetUsageError from exc
    if price_amount <= 0:
        raise TariffSetValidationError(text("admin.tariff_price_invalid"))

    try:
        duration_days = int(args[3])
    except ValueError as exc:
        raise TariffSetUsageError from exc
    if duration_days <= 0:
        raise TariffSetValidationError(text("admin.tariff_duration_invalid"))

    try:
        sort_order = int(args[5]) if len(args) >= 6 else 100
    except ValueError as exc:
        raise TariffSetUsageError from exc

    return TariffSetArgs(
        code=args[0],
        title=args[1],
        price_amount=price_amount,
        duration_days=duration_days,
        currency=args[4].upper() if len(args) >= 5 else "RUB",
        sort_order=sort_order,
        description=args[6] if len(args) >= 7 else "",
    )


def _is_admin_user(user: User | None, settings: Settings) -> bool:
    if user is None:
        return False
    return settings.is_admin(user.id, user.username)


def _is_admin(message: Message, settings: Settings) -> bool:
    return _is_admin_user(message.from_user, settings)


def _is_private_callback(callback: CallbackQuery) -> bool:
    return isinstance(callback.message, Message) and callback.message.chat.type == ChatType.PRIVATE


async def _answer_admin_tariffs(message: Message, settings: Settings) -> None:
    async with open_database(settings.database_path) as db:
        tariffs = await TariffsRepository(db).list_all()
    if not tariffs:
        await message.answer(text("admin.tariffs_empty"))
        return
    await message.answer(
        "\n\n".join(
            text(
                "admin.tariffs_list_item",
                code=item.code,
                title=item.title,
                price_amount=item.price_amount,
                currency=item.currency,
                duration_days=item.duration_days,
                is_active=item.is_active,
                sort_order=item.sort_order,
            )
            for item in tariffs
        )
    )


async def _answer_disable_tariff_list(message: Message, settings: Settings) -> None:
    async with open_database(settings.database_path) as db:
        tariffs = await TariffsRepository(db).list_active()
    if not tariffs:
        await message.answer(text("admin.active_tariffs_empty"))
        return
    await message.answer(text("admin.disable_tariff_prompt"), reply_markup=admin_disable_tariffs_keyboard(tariffs))


@router.message(F.text.func(lambda text: is_reply_button_text(text, ADMIN_TARIFFS_BUTTON)))
async def admin_tariffs_button(message: Message, settings: Settings) -> None:
    if not _is_admin(message, settings):
        return
    await _answer_admin_tariffs(message, settings)


@router.message(Command("tariff_set"))
async def tariff_set(message: Message, settings: Settings) -> None:
    if not _is_admin(message, settings):
        return
    try:
        args = parse_tariff_set_args(message.text)
    except TariffSetUsageError:
        await message.answer(TARIFF_SET_USAGE)
        return
    except TariffSetValidationError as exc:
        await message.answer(str(exc))
        return

    async with open_database(settings.database_path) as db:
        tariff = await TariffsRepository(db).upsert(
            code=args.code,
            title=args.title,
            description=args.description,
            price_amount=args.price_amount,
            currency=args.currency,
            duration_days=args.duration_days,
            is_active=True,
            sort_order=args.sort_order,
        )
        await db.commit()
    await message.answer(text("admin.tariff_saved", code=tariff.code))


@router.message(F.text.func(lambda text: is_reply_button_text(text, ADMIN_DISABLE_TARIFF_BUTTON)))
async def tariff_disable_button(message: Message, settings: Settings) -> None:
    if not _is_admin(message, settings):
        return
    await _answer_disable_tariff_list(message, settings)


@router.callback_query(F.data.startswith(ADMIN_DISABLE_TARIFF_SELECT_PREFIX))
async def tariff_disable_select(callback: CallbackQuery, settings: Settings) -> None:
    if not _is_admin_user(callback.from_user, settings) or callback.data is None or not _is_private_callback(callback):
        return
    code = callback.data.removeprefix(ADMIN_DISABLE_TARIFF_SELECT_PREFIX)
    await callback.message.edit_text(
        text("admin.tariff_disable_confirm", code=code),
        reply_markup=admin_disable_tariff_confirm_keyboard(code),
    )
    await callback.answer()


@router.callback_query(F.data.startswith(ADMIN_DISABLE_TARIFF_CONFIRM_PREFIX))
async def tariff_disable_confirm(callback: CallbackQuery, settings: Settings) -> None:
    if not _is_admin_user(callback.from_user, settings) or callback.data is None or not _is_private_callback(callback):
        return
    code = callback.data.removeprefix(ADMIN_DISABLE_TARIFF_CONFIRM_PREFIX)
    async with open_database(settings.database_path) as db:
        changed = await TariffsRepository(db).set_active(code, False)
        await db.commit()
    result_text = text("admin.tariff_disabled") if changed else text("admin.tariff_not_found")
    await callback.message.edit_text(result_text)
    await callback.answer()


@router.callback_query(F.data == ADMIN_DISABLE_TARIFF_CANCEL)
async def tariff_disable_cancel(callback: CallbackQuery, settings: Settings) -> None:
    if not _is_admin_user(callback.from_user, settings) or not _is_private_callback(callback):
        return
    await callback.message.edit_text(text("admin.tariff_disable_cancelled"))
    await callback.answer()


def _promo_days(value: str) -> int | None:
    try:
        days = int(value)
    except ValueError:
        return None
    return days if 1 <= days <= 1000 else None


def _promo_status(value: str) -> str:
    return text(f"admin.promo_status_{value}")


async def _show_promo_menu(message: Message) -> None:
    await message.answer(text("admin.promo_menu"), reply_markup=promo_duration_keyboard())


@router.message(F.text.func(lambda value: is_reply_button_text(value, ADMIN_PROMO_BUTTON)))
async def promo_button(message: Message, settings: Settings) -> None:
    if _is_admin(message, settings):
        await _show_promo_menu(message)


@router.callback_query(F.data.startswith("ap:"))
async def promo_callbacks(callback: CallbackQuery, settings: Settings) -> None:
    if not _is_admin_user(callback.from_user, settings) or not _is_private_callback(callback) or callback.data is None:
        return
    if not isinstance(callback.message, Message):
        return
    parts = callback.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action == "menu":
        await callback.message.edit_text(text("admin.promo_menu"), reply_markup=promo_duration_keyboard())
    elif action in {"pick", "custom"} and len(parts) == 3:
        days = _promo_days(parts[2])
        if days is not None:
            await callback.message.edit_text(text("admin.promo_custom", duration_days=days), reply_markup=promo_duration_keyboard(days))
    elif action == "step" and len(parts) == 4:
        current = _promo_days(parts[2])
        try:
            delta = int(parts[3])
        except ValueError:
            delta = 0
        days = max(1, min(1000, (current or 1) + delta))
        await callback.message.edit_text(text("admin.promo_custom", duration_days=days), reply_markup=promo_duration_keyboard(days))
    elif action == "confirm" and len(parts) == 3:
        days = _promo_days(parts[2])
        if days is not None:
            await callback.message.edit_text(text("admin.promo_confirm", duration_days=days), reply_markup=promo_confirm_keyboard(days))
    elif action == "create" and len(parts) == 3 and callback.from_user is not None:
        days = _promo_days(parts[2])
        if days is not None:
            async with open_database(settings.database_path) as db:
                promo = await PromoService(db).create(days, callback.from_user.id)
            bot_info = await callback.bot.get_me()
            link = f"https://t.me/{bot_info.username}?start=promo_{promo.code}"
            await callback.message.edit_text(text("admin.promo_menu"), reply_markup=promo_duration_keyboard())
            await callback.message.answer_photo(
                BufferedInputFile(qr_png(link), filename=f"promo-{promo.code}.png"),
                caption=text("admin.promo_card", duration_days=promo.duration_days, link=link),
                reply_markup=promo_card_keyboard(link),
            )
            await callback.message.answer(
                text("admin.promo_controls", code=promo.code, duration_days=promo.duration_days, status=_promo_status(promo.status)),
                reply_markup=promo_admin_controls_keyboard(promo.id),
            )
    elif action == "recent" and callback.from_user is not None:
        async with open_database(settings.database_path) as db:
            promos = await PromoService(db).codes.list_recent(callback.from_user.id)
        body = text("admin.promo_empty") if not promos else text(
            "admin.promo_recent", items="\n".join(text("admin.promo_recent_item", code=item.code, duration_days=item.duration_days, status=_promo_status(item.status)) for item in promos)
        )
        await callback.message.edit_text(body, reply_markup=promo_duration_keyboard())
    elif action in {"status", "cancel"} and len(parts) == 3:
        try:
            promo_id = int(parts[2])
        except ValueError:
            promo_id = 0
        async with open_database(settings.database_path) as db:
            service = PromoService(db)
            if action == "cancel":
                changed = await service.codes.cancel(promo_id)
                await db.commit()
                if changed:
                    await callback.message.edit_text(text("admin.promo_cancelled"))
                    await callback.answer()
                    return
            promo = await service.codes.get_by_id(promo_id)
        if promo is None:
            await callback.answer(text("admin.promo_not_cancellable"), show_alert=True)
            return
        await callback.message.edit_text(
            text("admin.promo_controls", code=promo.code, duration_days=promo.duration_days, status=_promo_status(promo.status)),
            reply_markup=promo_admin_controls_keyboard(promo.id) if promo.status == "created" else None,
        )
    await callback.answer()


@router.message(Command("trial_set"))
async def trial_set(message: Message, settings: Settings) -> None:
    if not _is_admin(message, settings) or message.from_user is None:
        return
    try:
        duration_days = parse_trial_set_args(message.text)
    except TrialSetUsageError:
        await message.answer(TRIAL_SET_USAGE)
        return
    except TrialSetValidationError as exc:
        await message.answer(str(exc))
        return

    async with open_database(settings.database_path) as db:
        trial_settings = await TrialSettingsRepository(db).set(True, duration_days, message.from_user.id)
        await AccessEventsRepository(db).add(
            telegram_user_id=message.from_user.id,
            event_type="trial_settings_updated",
            details={"enabled": True, "duration_days": trial_settings.duration_days},
        )
        await db.commit()
    await message.answer(text("admin.trial_enabled", duration_days=duration_days))


@router.message(Command("trial_disable"))
async def trial_disable(message: Message, settings: Settings) -> None:
    if not _is_admin(message, settings) or message.from_user is None:
        return
    async with open_database(settings.database_path) as db:
        repository = TrialSettingsRepository(db)
        current = await repository.get()
        await repository.set(False, current.duration_days, message.from_user.id)
        await AccessEventsRepository(db).add(
            telegram_user_id=message.from_user.id,
            event_type="trial_settings_updated",
            details={"enabled": False, "duration_days": current.duration_days},
        )
        await db.commit()
    await message.answer(text("admin.trial_disabled"))


@router.message(Command("trial_status"))
async def trial_status(message: Message, settings: Settings) -> None:
    if not _is_admin(message, settings):
        return
    async with open_database(settings.database_path) as db:
        current = await TrialSettingsRepository(db).get()
    await message.answer(
        text(
            "admin.trial_status",
            enabled=text("admin.trial_enabled_status") if current.enabled else text("admin.trial_disabled_status"),
            duration_days=current.duration_days,
        )
    )
