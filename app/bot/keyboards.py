from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.db.repositories import TariffRecord


def tariffs_keyboard(tariffs: list[TariffRecord]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{tariff.title} · {tariff.price_amount} {tariff.currency} · {tariff.duration_days} дн.",
                    callback_data=f"buy:{tariff.code}",
                )
            ]
            for tariff in tariffs
        ]
    )
