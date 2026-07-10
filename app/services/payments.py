from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import uuid4

import aiosqlite

from app.config import Settings
from app.db.repositories import (
    AccessEventsRepository,
    PaymentsRepository,
    PaymentRecord,
    TariffsRepository,
    UsersRepository,
)
from app.services.access import AccessExtension, calculate_access_extension
from app.services.lava import LavaClient, LavaPaymentNotification
from app.utils.datetime import datetime_to_iso, utc_now


@dataclass(frozen=True, slots=True)
class CreatedPayment:
    payment: PaymentRecord
    payment_url: str


@dataclass(frozen=True, slots=True)
class PaidPaymentResult:
    payment_id: int
    user_id: int
    telegram_user_id: int
    already_applied: bool
    access_extension: AccessExtension | None


class PaymentService:
    def __init__(self, db: aiosqlite.Connection, settings: Settings, lava_client: LavaClient):
        self.db = db
        self.settings = settings
        self.lava_client = lava_client
        self.users = UsersRepository(db)
        self.tariffs = TariffsRepository(db)
        self.payments = PaymentsRepository(db)
        self.events = AccessEventsRepository(db)

    async def create_payment_for_tariff(self, telegram_user_id: int, tariff_code: str) -> CreatedPayment:
        user = await self.users.get_by_telegram_id(telegram_user_id)
        if user is None:
            raise ValueError("user is not registered")
        tariff = await self.tariffs.get_by_code(tariff_code, active_only=True)
        if tariff is None:
            raise ValueError("tariff is not active")

        order_id = uuid4().hex
        expires_at = utc_now() + timedelta(hours=1)
        if self.settings.is_mock_payments_enabled:
            payment_url = f"{self.settings.app_base_url.rstrip('/')}/mock/payments/{order_id}/pay"
            payment = await self.payments.create(
                user_id=user.id,
                tariff_id=tariff.id,
                order_id=order_id,
                amount=tariff.price_amount,
                currency=tariff.currency,
                payment_url=payment_url,
                invoice_id=f"mock-{order_id}",
                expires_at=expires_at,
                provider="mock",
                raw_payload={"mock": True, "payment_url": payment_url},
            )
            await self.events.add(
                telegram_user_id=user.telegram_user_id,
                user_id=user.id,
                event_type="payment_created",
                details={"payment_id": payment.id, "tariff": tariff.code, "provider": "mock"},
            )
            await self.db.commit()
            return CreatedPayment(payment=payment, payment_url=payment_url)

        success_url = f"{self.settings.app_base_url.rstrip('/')}/lava/success?invoice={order_id}"
        webhook_url = f"{self.settings.app_base_url.rstrip('/')}/lava/webhook"
        invoice = await self.lava_client.create_invoice(
            order_id=order_id,
            amount=tariff.price_amount,
            currency=tariff.currency,
            description=f"{tariff.title} ({tariff.duration_days} days)",
            success_url=success_url,
            webhook_url=webhook_url,
        )
        payment = await self.payments.create(
            user_id=user.id,
            tariff_id=tariff.id,
            order_id=order_id,
            amount=tariff.price_amount,
            currency=tariff.currency,
            payment_url=invoice.payment_url,
            invoice_id=invoice.invoice_id,
            expires_at=expires_at,
            provider="lava",
            raw_payload=invoice.raw_payload,
        )
        await self.events.add(
            telegram_user_id=user.telegram_user_id,
            user_id=user.id,
            event_type="payment_created",
            details={"payment_id": payment.id, "tariff": tariff.code, "provider": "lava"},
        )
        await self.db.commit()
        return CreatedPayment(payment=payment, payment_url=invoice.payment_url)

    async def handle_paid(
        self,
        notification: LavaPaymentNotification,
        expected_provider: str | None = None,
    ) -> PaidPaymentResult:
        order_id = _notification_order_id(notification)
        invoice_id = _notification_invoice_id(notification)
        if not order_id and not invoice_id:
            raise ValueError("payment notification has no invoice id")

        paid_at = notification.paid_at or utc_now()
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            payment = None
            if order_id:
                payment = await self.payments.get_by_order_id(order_id)
            if payment is None and invoice_id:
                payment = await self.payments.get_by_invoice_id(invoice_id)
            if payment is None:
                raise ValueError("payment is not found")
            if expected_provider is not None and payment.provider != expected_provider:
                raise ValueError("payment provider does not match")

            user = await self.users.get_by_id(payment.user_id)
            tariff = await self.tariffs.get_by_id(payment.tariff_id)
            if user is None or tariff is None:
                raise ValueError("payment references missing user or tariff")

            if payment.applied_at is not None:
                await self.db.commit()
                return PaidPaymentResult(
                    payment_id=payment.id,
                    user_id=user.id,
                    telegram_user_id=user.telegram_user_id,
                    already_applied=True,
                    access_extension=None,
                )

            await self.payments.mark_paid(
                payment_id=payment.id,
                invoice_id=invoice_id,
                paid_at=paid_at,
                raw_payload=notification.raw_payload,
            )

            extension = calculate_access_extension(user.access_until, tariff.duration_days, paid_at)
            applied_at = utc_now()
            await self.users.set_access_until(user.id, extension.new_access_until)
            await self.payments.mark_applied(payment.id, applied_at)
            await self.events.add(
                telegram_user_id=user.telegram_user_id,
                user_id=user.id,
                event_type="payment_applied",
                details={
                    "payment_id": payment.id,
                    "tariff_id": tariff.id,
                    "previous_access_until": datetime_to_iso(extension.previous_access_until),
                    "new_access_until": datetime_to_iso(extension.new_access_until),
                },
            )
            await self.db.commit()
            return PaidPaymentResult(
                payment_id=payment.id,
                user_id=user.id,
                telegram_user_id=user.telegram_user_id,
                already_applied=False,
                access_extension=extension,
            )
        except Exception:
            await self.db.rollback()
            raise

    async def apply_status_notification(self, notification: LavaPaymentNotification) -> PaidPaymentResult | None:
        if notification.status == "paid":
            return await self.handle_paid(notification)
        order_id = _notification_order_id(notification)
        invoice_id = _notification_invoice_id(notification)
        payment = None
        if order_id:
            payment = await self.payments.get_by_order_id(order_id)
        if payment is None and invoice_id:
            payment = await self.payments.get_by_invoice_id(invoice_id)
        if payment is not None and notification.status in {"failed", "expired", "cancelled"}:
            await self.payments.mark_status(payment.id, notification.status, notification.raw_payload)
            await self.db.commit()
        return None

    async def check_pending_payment(self, payment: PaymentRecord) -> PaidPaymentResult | None:
        notification = await self.lava_client.get_invoice_status(
            invoice_id=payment.invoice_id,
            order_id=payment.order_id,
        )
        return await self.apply_status_notification(notification)

    async def confirm_mock_payment(self, order_id: str) -> PaidPaymentResult:
        notification = notification_from_paid_payment(
            order_id=order_id,
            invoice_id=f"mock-{order_id}",
            raw_payload={"mock": True, "status": "paid"},
        )
        return await self.handle_paid(notification, expected_provider="mock")


def notification_from_paid_payment(
    order_id: str,
    invoice_id: str | None = None,
    raw_payload: dict[str, Any] | None = None,
) -> LavaPaymentNotification:
    return LavaPaymentNotification(
        order_id=order_id,
        invoice_id=invoice_id,
        status="paid",
        amount=None,
        currency=None,
        paid_at=utc_now(),
        raw_payload=raw_payload or {"test": True},
    )


def _notification_order_id(notification: LavaPaymentNotification) -> str | None:
    return notification.order_id


def _notification_invoice_id(notification: LavaPaymentNotification) -> str | None:
    return notification.invoice_id
