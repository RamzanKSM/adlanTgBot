from aiogram.exceptions import TelegramBadRequest
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.db.connection import open_database
from app.services.invites import InviteService
from app.services.payments import PaymentService


router = APIRouter()


INVITE_ERROR_TEXT = (
    "Mock payment paid. Access was applied, but invite link was not created. "
    "Check TELEGRAM_GROUP_ID and bot permissions in the target group."
)


@router.api_route(
    "/mock/payments/{order_id}/pay",
    methods=["GET", "POST"],
    response_class=PlainTextResponse,
    response_model=None,
)
@router.api_route(
    "/mock/pay/{order_id}",
    methods=["GET", "POST"],
    response_class=PlainTextResponse,
    response_model=None,
)
async def mock_pay(order_id: str, request: Request) -> PlainTextResponse:
    settings = request.app.state.settings
    if not settings.is_mock_payments_enabled:
        raise HTTPException(status_code=404, detail="mock payments are disabled")

    async with open_database(settings.database_path) as db:
        payment_service = PaymentService(db, settings, request.app.state.lava_client)
        try:
            result = await payment_service.confirm_mock_payment(order_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        invite_service = InviteService(db, settings, request.app.state.bot)
        try:
            link = await invite_service.ensure_personal_invite(result.telegram_user_id, result.payment_id)
            if link and not result.already_applied:
                await request.app.state.bot.send_message(
                    result.telegram_user_id,
                    f"Mock-оплата подтверждена. Ваша ссылка в группу: {link}",
                )
        except TelegramBadRequest:
            return PlainTextResponse(INVITE_ERROR_TEXT, status_code=400)

    status = "already applied" if result.already_applied else "paid"
    if link:
        return PlainTextResponse(f"Mock payment {status}. Invite link: {link}")
    return PlainTextResponse(f"Mock payment {status}. Access is active; invite link was not created.")
