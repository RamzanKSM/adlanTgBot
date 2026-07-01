import shlex
from dataclasses import dataclass

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.bot.filters import PRIVATE_CHAT_FILTER
from app.config import Settings
from app.db.connection import open_database
from app.db.repositories import TariffsRepository
from app.services.access import grant_manual_access
from app.utils.datetime import format_datetime_moscow


router = Router(name="admin")
router.message.filter(PRIVATE_CHAT_FILTER)


TARIFF_SET_USAGE = """Как создать тариф:
Формат: /tariff_set CODE "Название" PRICE DURATION_DAYS [CURRENCY] [sort_order] [description]

Аргументы:
- CODE - короткий код тарифа без пробелов, например week или month.
- "Название" - название тарифа. Если в названии есть пробелы, берите его в кавычки.
- PRICE - цена целым числом больше 0.
- DURATION_DAYS - длительность доступа в днях, целое число больше 0.
- CURRENCY - валюта, по умолчанию RUB.
- sort_order - порядок в списке, по умолчанию 100.
- description - описание, необязательно.

Примеры:
/tariff_set week "7 дней" 500 7
/tariff_set month "30 дней" 1500 30 RUB 20
"""


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


def parse_tariff_set_args(text: str | None) -> TariffSetArgs:
    try:
        args = shlex.split((text or "").partition(" ")[2])
    except ValueError as exc:
        raise TariffSetUsageError from exc

    if len(args) < 4:
        raise TariffSetUsageError

    try:
        price_amount = int(args[2])
    except ValueError as exc:
        raise TariffSetUsageError from exc
    if price_amount <= 0:
        raise TariffSetValidationError("Цена тарифа должна быть больше 0.")

    try:
        duration_days = int(args[3])
    except ValueError as exc:
        raise TariffSetUsageError from exc
    if duration_days <= 0:
        raise TariffSetValidationError("Длительность тарифа должна быть больше 0 дней.")

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


def _is_admin(message: Message, settings: Settings) -> bool:
    user = message.from_user
    if user is None:
        return False
    return settings.is_admin(user.id, user.username)


@router.message(Command("admin_tariffs"))
async def admin_tariffs(message: Message, settings: Settings) -> None:
    if not _is_admin(message, settings):
        return
    async with open_database(settings.database_path) as db:
        tariffs = await TariffsRepository(db).list_all()
    if not tariffs:
        await message.answer("Тарифов нет.")
        return
    await message.answer(
        "\n".join(
            f"{item.code}: {item.title}, {item.price_amount} {item.currency}, "
            f"{item.duration_days} дн., active={item.is_active}, sort={item.sort_order}"
            for item in tariffs
        )
    )


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
    await message.answer(f"Тариф сохранен: {tariff.code}")


@router.message(Command("tariff_disable"))
async def tariff_disable(message: Message, settings: Settings) -> None:
    if not _is_admin(message, settings):
        return
    code = (message.text or "").partition(" ")[2].strip()
    if not code:
        await message.answer("Формат: /tariff_disable CODE")
        return
    async with open_database(settings.database_path) as db:
        changed = await TariffsRepository(db).set_active(code, False)
        await db.commit()
    await message.answer("Тариф отключен." if changed else "Тариф не найден.")


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
        await message.answer("Формат: /grant_access <telegram_id> <days> или /grant_access <days>")
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
        f"Доступ выдан пользователю {grant.user.telegram_user_id} до "
        f"{format_datetime_moscow(grant.user.access_until)}."
    )
