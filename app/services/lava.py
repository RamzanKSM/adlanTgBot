import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from typing import Any

import httpx

from app.config import Settings
from app.utils.datetime import datetime_to_iso, iso_to_datetime, utc_now


@dataclass(frozen=True, slots=True)
class LavaInvoice:
    provider_invoice_id: str | None
    payment_url: str
    raw_payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LavaPaymentNotification:
    internal_invoice_id: str | None
    provider_invoice_id: str | None
    status: str
    amount: int | None
    currency: str | None
    paid_at: datetime | None
    raw_payload: dict[str, Any]


class LavaClient:
    """Small Lava boundary.

    The exact Lava contract can be adjusted here without leaking provider payloads
    into handlers or repository code.
    """

    def __init__(self, settings: Settings, http_client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.http_client = http_client or httpx.AsyncClient(timeout=15)
        self._owns_client = http_client is None

    async def close(self) -> None:
        if self._owns_client:
            await self.http_client.aclose()

    async def create_invoice(
        self,
        internal_invoice_id: str,
        amount: int,
        currency: str,
        description: str,
        success_url: str,
        webhook_url: str,
        expires_minutes: int = 60,
    ) -> LavaInvoice:
        if not self.settings.lava_base_url or not self.settings.lava_api_key:
            return LavaInvoice(
                provider_invoice_id=None,
                payment_url=f"{self.settings.app_base_url.rstrip('/')}/lava/success?invoice={internal_invoice_id}",
                raw_payload={"stub": True, "reason": "Lava credentials are not configured"},
            )

        payload = {
            "shop_id": self.settings.lava_shop_id,
            "order_id": internal_invoice_id,
            "amount": amount,
            "currency": currency,
            "description": description,
            "success_url": success_url,
            "webhook_url": webhook_url,
            "expires_at": datetime_to_iso(utc_now() + timedelta(minutes=expires_minutes)),
        }
        response = await self.http_client.post(
            f"{self.settings.lava_base_url.rstrip('/')}/invoice/create",
            json=payload,
            headers={"Authorization": f"Bearer {self.settings.lava_api_key}"},
        )
        response.raise_for_status()
        data = response.json()
        invoice_id = data.get("id") or data.get("invoice_id") or data.get("data", {}).get("id")
        payment_url = data.get("payment_url") or data.get("url") or data.get("data", {}).get("payment_url")
        if not payment_url:
            raise ValueError("Lava invoice response does not contain payment_url")
        return LavaInvoice(str(invoice_id) if invoice_id else None, str(payment_url), data)

    async def get_invoice_status(self, provider_invoice_id: str | None, internal_invoice_id: str) -> LavaPaymentNotification:
        if not self.settings.lava_base_url or not self.settings.lava_api_key:
            return LavaPaymentNotification(
                internal_invoice_id=internal_invoice_id,
                provider_invoice_id=provider_invoice_id,
                status="pending",
                amount=None,
                currency=None,
                paid_at=None,
                raw_payload={"stub": True, "reason": "Lava credentials are not configured"},
            )

        response = await self.http_client.get(
            f"{self.settings.lava_base_url.rstrip('/')}/invoice/status",
            params={"invoice_id": provider_invoice_id, "order_id": internal_invoice_id},
            headers={"Authorization": f"Bearer {self.settings.lava_api_key}"},
        )
        response.raise_for_status()
        data = response.json()
        return self.normalize_payload(data)

    def verify_webhook(self, body: bytes, signature: str | None) -> bool:
        if not self.settings.lava_webhook_secret:
            return True
        if not signature:
            return False
        expected = hmac.new(
            self.settings.lava_webhook_secret.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()
        supplied = signature.removeprefix("sha256=").strip()
        return hmac.compare_digest(expected, supplied)

    def normalize_payload(self, payload: dict[str, Any]) -> LavaPaymentNotification:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        raw_status = str(data.get("status") or data.get("invoice_status") or "").lower()
        status = {
            "success": "paid",
            "completed": "paid",
            "paid": "paid",
            "failed": "failed",
            "expired": "expired",
            "cancelled": "cancelled",
            "canceled": "cancelled",
        }.get(raw_status, raw_status or "unknown")
        amount = data.get("amount") or data.get("sum")
        paid_at = data.get("paid_at") or data.get("paidAt") or data.get("updated_at")
        return LavaPaymentNotification(
            internal_invoice_id=_optional_str(data.get("order_id") or data.get("internal_invoice_id")),
            provider_invoice_id=_optional_str(data.get("invoice_id") or data.get("id")),
            status=status,
            amount=int(amount) if amount is not None and str(amount).isdigit() else None,
            currency=_optional_str(data.get("currency")),
            paid_at=iso_to_datetime(str(paid_at)) if paid_at else None,
            raw_payload=payload,
        )


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None
