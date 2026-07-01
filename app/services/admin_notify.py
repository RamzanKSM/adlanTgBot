from aiogram import Bot

from app.config import Settings


async def notify_admins(settings: Settings, bot: Bot, text: str) -> None:
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            continue
