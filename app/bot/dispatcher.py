from aiogram import Bot, Dispatcher

from app.bot.event_logging import TelegramEventLoggingMiddleware
from app.bot.handlers_admin import router as admin_router
from app.bot.handlers_chat_member import router as chat_member_router
from app.bot.handlers_group_service import router as group_service_router
from app.bot.handlers_user import router as user_router
from app.config import Settings
from app.services.lava import LavaClient


def create_dispatcher(settings: Settings, lava_client: LavaClient) -> Dispatcher:
    dp = Dispatcher(settings=settings, lava_client=lava_client)
    event_logging_middleware = TelegramEventLoggingMiddleware()
    dp.message.middleware(event_logging_middleware)
    dp.callback_query.middleware(event_logging_middleware)
    dp.chat_member.middleware(event_logging_middleware)
    dp.include_router(admin_router)
    dp.include_router(user_router)
    dp.include_router(group_service_router)
    dp.include_router(chat_member_router)
    return dp


def create_bot(settings: Settings) -> Bot:
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN is not configured")
    return Bot(settings.bot_token)
