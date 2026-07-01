from app.bot.keyboards import tariffs_keyboard
from app.db.repositories import TariffRecord


def test_tariffs_keyboard_button_contains_duration_days() -> None:
    keyboard = tariffs_keyboard(
        [
            TariffRecord(
                id=1,
                code="week",
                title="7 дней",
                description="",
                price_amount=500,
                currency="RUB",
                duration_days=7,
                is_active=True,
                sort_order=10,
            )
        ]
    )

    button = keyboard.inline_keyboard[0][0]

    assert button.text == "7 дней · 500 RUB · 7 дн."
    assert button.callback_data == "buy:week"
