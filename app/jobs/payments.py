from aiogram import Bot

from app.config import Settings
from app.db.connection import open_database
from app.db.repositories import PaymentsRepository
from app.messages import message
from app.services.invites import InviteService
from app.services.lava import LavaClient
from app.services.payments import PaymentService


async def check_pending_payments(settings: Settings, bot: Bot, lava_client: LavaClient) -> None:
    async with open_database(settings.database_path) as db:
        payments = PaymentsRepository(db)
        service = PaymentService(db, settings, lava_client)
        invite_service = InviteService(db, settings, bot)
        for payment in await payments.list_pending_for_check(limit=50):
            result = await service.check_pending_payment(payment)
            if result is not None and not result.already_applied:
                link = await invite_service.ensure_personal_invite(result.telegram_user_id, payment_id=result.payment_id)
                if link:
                    await bot.send_message(result.telegram_user_id, message("payment.received_with_link", link=link))
