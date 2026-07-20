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
    admin_disable_tariff_confirm_keyboard,
    admin_disable_tariffs_keyboard,
    is_reply_button_text,
)
from app.config import Settings
from app.db.connection import open_database
from app.db.repositories import TariffsRepository
from app.messages import message as text
from app.services.access import grant_manual_access
from app.utils.datetime import format_datetime_moscow


router = Router(name="admin")
router.message.filter(PRIVATE_CHAT_FILTER)


TARIFF_SET_USAGE = text("admin.tariff_set_usage")


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


@router.message(Command("grant_access"))
async def grant_access(message: Message, settings: Settings) -> None:
    if not _is_admin(message, settings):
        return
    if message.from_user is None:
        return

    try:
        args = shlex.split((message.text or "").partition(" ")[2])
        if len(args) == 1:
            telegram_user_id = message.from_user.id
            duration_days = int(args[0])
            username = message.from_user.username
            first_name = message.from_user.first_name
            last_name = message.from_user.last_name
        elif len(args) == 2:
            telegram_user_id = int(args[0])
            duration_days = int(args[1])
            username = None
            first_name = None
            last_name = None
        else:
            raise ValueError
        if telegram_user_id <= 0 or duration_days <= 0:
            raise ValueError
    except ValueError:
        await message.answer(text("admin.grant_access_usage"))
        return

    async with open_database(settings.database_path) as db:
        grant = await grant_manual_access(
            db,
            telegram_user_id=telegram_user_id,
            duration_days=duration_days,
            granted_by_telegram_user_id=message.from_user.id,
            username=username,
            first_name=first_name,
            last_name=last_name,
        )
        await db.commit()

    await message.answer(
        text(
            "admin.access_granted",
            telegram_user_id=grant.user.telegram_user_id,
            access_until=format_datetime_moscow(grant.user.access_until),
        )
    )
