from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from aiogram.types import Update
from fastapi import FastAPI, HTTPException, Request

from app.api.lava_webhook import router as lava_router
from app.api.mock_payments import router as mock_payments_router
from app.bot.dispatcher import create_bot, create_dispatcher
from app.config import get_settings
from app.db.migrations import run_migrations
from app.jobs.access import warn_and_expire_access
from app.jobs.payments import check_pending_payments
from app.jobs.scheduler import AsyncScheduler
from app.services.lava import LavaClient


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    await run_migrations(str(settings.database_path))

    bot = create_bot(settings)
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

    app.state.settings = settings
    app.state.bot = bot
    app.state.dispatcher = dispatcher
    app.state.lava_client = lava_client
    app.state.scheduler = scheduler
    try:
        yield
    finally:
        await scheduler.stop()
        await lava_client.close()
        await bot.session.close()


def create_app() -> FastAPI:
    app = FastAPI(title="adlanTgBot", lifespan=lifespan)
    app.include_router(lava_router)
    app.include_router(mock_payments_router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/telegram/webhook/{secret}")
    async def telegram_webhook(secret: str, request: Request) -> dict[str, str]:
        if secret != request.app.state.settings.telegram_webhook_secret:
            raise HTTPException(status_code=403, detail="wrong webhook secret")
        payload = await request.json()
        update = Update.model_validate(payload, context={"bot": request.app.state.bot})
        await request.app.state.dispatcher.feed_update(request.app.state.bot, update)
        return {"status": "ok"}

    return app


app = create_app()
