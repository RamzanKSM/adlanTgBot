import asyncio
import logging

from app.bot.commands import setup_bot_commands
from app.bot.dispatcher import create_bot, create_dispatcher
from app.config import get_settings
from app.db.migrations import run_migrations
from app.jobs.access import warn_and_expire_access
from app.jobs.payments import check_pending_payments
from app.jobs.scheduler import AsyncScheduler
from app.services.lava import LavaClient


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    await run_migrations(str(settings.database_path))

    bot = create_bot(settings)
    await setup_bot_commands(bot, settings)
    lava_client = LavaClient(settings)
    dispatcher = create_dispatcher(settings, lava_client)
    scheduler = AsyncScheduler()
    scheduler.add_job(
        "pending_payments",
        settings.pending_payment_check_seconds,
        lambda: check_pending_payments(settings, bot, lava_client),
    )
    scheduler.add_job(
        "access_expiration",
        settings.scheduler_interval_seconds,
        lambda: warn_and_expire_access(settings, bot),
    )
    scheduler.start()
    try:
        await dispatcher.start_polling(bot)
    finally:
        await scheduler.stop()
        await lava_client.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
