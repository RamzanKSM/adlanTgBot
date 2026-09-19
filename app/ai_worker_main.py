import asyncio
import logging

from app.bot.dispatcher import create_bot
from app.config import get_settings
from app.db.migrations import run_migrations
from app.jobs.ai import process_due_ai_turns, process_knowledge_candidates, retry_pending_onboarding
from app.jobs.scheduler import AsyncScheduler


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    await run_migrations(str(settings.database_path))
    bot = create_bot(settings)
    scheduler = AsyncScheduler()
    scheduler.add_job("ai_candidates", settings.ai_scheduler_interval_seconds, lambda: process_knowledge_candidates(settings))
    scheduler.add_job("ai_turns", settings.ai_scheduler_interval_seconds, lambda: process_due_ai_turns(settings, bot))
    scheduler.add_job("ai_onboarding", settings.ai_scheduler_interval_seconds, lambda: retry_pending_onboarding(settings, bot))
    scheduler.start()
    try:
        await asyncio.Event().wait()
    finally:
        await scheduler.stop()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
