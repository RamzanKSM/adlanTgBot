from aiogram import Bot

from app.config import Settings


async def notify_admins(settings: Settings, bot: Bot, text: str) -> None:
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            continue


async def notify_admins_with_errors(settings: Settings, bot: Bot, text: str) -> list[str]:
    """Send all notifications and return failures without affecting business state."""
    errors: list[str] = []
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except Exception as exc:
            errors.append(f"{admin_id}:{type(exc).__name__}")
    return errors
