from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from app.db.repositories import TariffRecord
from app.legal.documents import LEGAL_DOCUMENTS, LegalDocumentMeta


USER_TARIFFS_BUTTON = "💳 Тарифы"
USER_ACCESS_BUTTON = "🔐 Мой доступ"
USER_DOCUMENTS_BUTTON = "📄 Документы"
USER_SUPPORT_BUTTON = "🛟 Поддержка"

ADMIN_TARIFFS_BUTTON = "Админ: список тарифов"
ADMIN_DISABLE_TARIFF_BUTTON = "Админ: отключить тариф"
ADMIN_GRANT_ACCESS_7_BUTTON = "Админ: доступ 7 дней"
ADMIN_GRANT_ACCESS_30_BUTTON = "Админ: доступ 30 дней"
ADMIN_GRANT_ACCESS_90_BUTTON = "Админ: доступ 90 дней"

ADMIN_GRANT_ACCESS_DAYS_BY_BUTTON = {
    ADMIN_GRANT_ACCESS_7_BUTTON: 7,
    ADMIN_GRANT_ACCESS_30_BUTTON: 30,
    ADMIN_GRANT_ACCESS_90_BUTTON: 90,
}

ADMIN_DISABLE_TARIFF_SELECT_PREFIX = "adtd:s:"
ADMIN_DISABLE_TARIFF_CONFIRM_PREFIX = "adtd:c:"
ADMIN_DISABLE_TARIFF_CANCEL = "adtd:x"


def main_menu_keyboard(*, is_admin: bool = False) -> ReplyKeyboardMarkup:
    keyboard = [
        [
            KeyboardButton(text=USER_TARIFFS_BUTTON),
            KeyboardButton(text=USER_ACCESS_BUTTON),
        ],
        [
            KeyboardButton(text=USER_DOCUMENTS_BUTTON),
            KeyboardButton(text=USER_SUPPORT_BUTTON),
        ]
    ]
    if is_admin:
        keyboard.extend(
            [
                [
                    KeyboardButton(text=ADMIN_TARIFFS_BUTTON),
                    KeyboardButton(text=ADMIN_DISABLE_TARIFF_BUTTON),
                ],
                [
                    KeyboardButton(text=ADMIN_GRANT_ACCESS_7_BUTTON),
                    KeyboardButton(text=ADMIN_GRANT_ACCESS_30_BUTTON),
                    KeyboardButton(text=ADMIN_GRANT_ACCESS_90_BUTTON),
                ],
            ]
        )
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True, input_field_placeholder="Выберите действие")


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


def documents_keyboard(documents: tuple[LegalDocumentMeta, ...] = LEGAL_DOCUMENTS) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=document.title, callback_data=f"doc:{document.key}:1")]
            for document in documents
        ]
    )


def payment_agreement_keyboard(
    tariff_code: str,
    documents: tuple[LegalDocumentMeta, ...] = LEGAL_DOCUMENTS,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=document.title, callback_data=f"pdoc:{tariff_code}:{document.key}:1")]
            for document in documents
        ]
        + [[InlineKeyboardButton(text="✅ Принимаю и перейти к оплате", callback_data=f"pay:{tariff_code}")]]
    )


def document_page_keyboard(
    document_key: str,
    page_number: int,
    total_pages: int,
    tariff_code: str | None = None,
) -> InlineKeyboardMarkup:
    prefix = f"pdoc:{tariff_code}:{document_key}" if tariff_code is not None else f"doc:{document_key}"
    rows: list[list[InlineKeyboardButton]] = []
    navigation: list[InlineKeyboardButton] = []
    if page_number > 1:
        navigation.append(InlineKeyboardButton(text="◀️", callback_data=f"{prefix}:{page_number - 1}"))
    if page_number < total_pages:
        navigation.append(InlineKeyboardButton(text="▶️", callback_data=f"{prefix}:{page_number + 1}"))
    if navigation:
        rows.append(navigation)
    if tariff_code is None:
        rows.append([InlineKeyboardButton(text="📄 Все документы", callback_data="docs:list")])
    else:
        rows.append([InlineKeyboardButton(text="↩️ К согласию", callback_data=f"agree:{tariff_code}")])
        rows.append([InlineKeyboardButton(text="✅ Принимаю и перейти к оплате", callback_data=f"pay:{tariff_code}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_disable_tariffs_keyboard(tariffs: list[TariffRecord]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{tariff.title} ({tariff.code})",
                    callback_data=f"{ADMIN_DISABLE_TARIFF_SELECT_PREFIX}{tariff.code}",
                )
            ]
            for tariff in tariffs
        ]
    )


def admin_disable_tariff_confirm_keyboard(code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, отключить",
                    callback_data=f"{ADMIN_DISABLE_TARIFF_CONFIRM_PREFIX}{code}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Отмена",
                    callback_data=ADMIN_DISABLE_TARIFF_CANCEL,
                )
            ],
        ]
    )
