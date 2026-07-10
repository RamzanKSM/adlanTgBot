from app.bot.keyboards import (
    ADMIN_DISABLE_TARIFF_CANCEL,
    ADMIN_DISABLE_TARIFF_CONFIRM_PREFIX,
    ADMIN_DISABLE_TARIFF_SELECT_PREFIX,
    ADMIN_DISABLE_TARIFF_BUTTON,
    ADMIN_TARIFFS_BUTTON,
    USER_ACCESS_BUTTON,
    USER_DOCUMENTS_BUTTON,
    USER_SUPPORT_BUTTON,
    USER_TARIFFS_BUTTON,
    admin_disable_tariff_confirm_keyboard,
    admin_disable_tariffs_keyboard,
    document_page_keyboard,
    documents_keyboard,
    main_menu_keyboard,
    payment_agreement_keyboard,
    reply_text_key,
    tariffs_keyboard,
)
from app.db.repositories import TariffRecord


def _tariff(code: str, title: str, is_active: bool = True) -> TariffRecord:
    return TariffRecord(
        id=1,
        code=code,
        title=title,
        description="",
        price_amount=500,
        currency="RUB",
        duration_days=7,
        is_active=is_active,
        sort_order=10,
    )


def test_tariffs_keyboard_button_contains_duration_days() -> None:
    keyboard = tariffs_keyboard([_tariff("week", "7 дней")])

    button = keyboard.inline_keyboard[0][0]

    assert button.text == "7 дней · 500 RUB · 7 дн."
    assert button.callback_data == "buy:week"


def test_main_menu_keyboard_adds_admin_rows_only_for_admins() -> None:
    user_keyboard = main_menu_keyboard()
    admin_keyboard = main_menu_keyboard(is_admin=True)
    user_rows = [[button.text for button in row] for row in user_keyboard.keyboard]
    admin_rows = [[button.text for button in row] for row in admin_keyboard.keyboard]

    assert user_rows == [
        [USER_TARIFFS_BUTTON, USER_ACCESS_BUTTON],
        [USER_DOCUMENTS_BUTTON, USER_SUPPORT_BUTTON],
    ]
    assert [ADMIN_TARIFFS_BUTTON, ADMIN_DISABLE_TARIFF_BUTTON] not in user_rows
    assert admin_rows == user_rows + [
        [ADMIN_TARIFFS_BUTTON, ADMIN_DISABLE_TARIFF_BUTTON],
    ]
    assert user_keyboard.is_persistent is True


def test_reply_text_key_ignores_emoji_and_variation_selectors() -> None:
    assert reply_text_key("📄 Документы") == reply_text_key("Документы")
    assert reply_text_key("Админ: список тарифов") == reply_text_key("Админ список тарифов")


def test_documents_keyboard_contains_known_document_callbacks() -> None:
    keyboard = documents_keyboard()

    callbacks = [row[0].callback_data for row in keyboard.inline_keyboard]

    assert callbacks == [
        "doc:offer:1",
        "doc:privacy:1",
        "doc:refunds:1",
        "doc:community_rules:1",
    ]


def test_payment_agreement_keyboard_carries_tariff_code() -> None:
    keyboard = payment_agreement_keyboard("week")

    callbacks = [row[0].callback_data for row in keyboard.inline_keyboard]

    assert callbacks[:-1] == [
        "pdoc:week:offer:1",
        "pdoc:week:privacy:1",
        "pdoc:week:refunds:1",
        "pdoc:week:community_rules:1",
    ]
    assert callbacks[-1] == "pay:week"


def test_document_page_keyboard_supports_pagination_and_payment_return() -> None:
    keyboard = document_page_keyboard("offer", page_number=2, total_pages=3, tariff_code="week")

    assert [button.callback_data for button in keyboard.inline_keyboard[0]] == [
        "pdoc:week:offer:1",
        "pdoc:week:offer:3",
    ]
    assert keyboard.inline_keyboard[1][0].callback_data == "agree:week"
    assert keyboard.inline_keyboard[2][0].callback_data == "pay:week"


def test_admin_disable_tariffs_keyboard_uses_admin_callback_prefix() -> None:
    keyboard = admin_disable_tariffs_keyboard([_tariff("week", "7 дней")])

    button = keyboard.inline_keyboard[0][0]

    assert button.text == "7 дней (week)"
    assert button.callback_data == f"{ADMIN_DISABLE_TARIFF_SELECT_PREFIX}week"


def test_admin_disable_tariff_confirm_keyboard_has_confirm_and_cancel() -> None:
    keyboard = admin_disable_tariff_confirm_keyboard("week")

    assert keyboard.inline_keyboard[0][0].text == "Да, отключить"
    assert keyboard.inline_keyboard[0][0].callback_data == f"{ADMIN_DISABLE_TARIFF_CONFIRM_PREFIX}week"
    assert keyboard.inline_keyboard[1][0].text == "Отмена"
    assert keyboard.inline_keyboard[1][0].callback_data == ADMIN_DISABLE_TARIFF_CANCEL
