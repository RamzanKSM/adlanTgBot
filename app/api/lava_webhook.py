from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.db.connection import open_database
from app.services.invites import InviteService
from app.services.payments import PaymentService


router = APIRouter()


@router.post("/lava/webhook")
async def lava_webhook(request: Request) -> dict[str, str]:
    settings = request.app.state.settings
    lava_client = request.app.state.lava_client
    bot = request.app.state.bot

    body = await request.body()
    signature = request.headers.get("X-Lava-Signature") or request.headers.get("X-Signature")
    if not lava_client.verify_webhook(body, signature):
        raise HTTPException(status_code=401, detail="invalid signature")

    payload = await request.json()
    notification = lava_client.normalize_payload(payload)

    async with open_database(settings.database_path) as db:
        payment_service = PaymentService(db, settings, lava_client)
        result = await payment_service.apply_status_notification(notification)
        if result is not None and not result.already_applied:
            invite_service = InviteService(db, settings, bot)
            link = await invite_service.ensure_personal_invite(result.telegram_user_id, result.payment_id)
            if link:
                await bot.send_message(result.telegram_user_id, f"Оплата получена. Ваша ссылка в группу: {link}")

    return {"status": "ok"}


@router.get("/lava/success", response_class=PlainTextResponse)
async def lava_success() -> str:
    return "Спасибо. Платеж проверяется автоматически; эта страница не подтверждает оплату."
