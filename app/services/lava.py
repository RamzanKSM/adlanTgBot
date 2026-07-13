import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from datetime import UTC
from decimal import Decimal
from decimal import InvalidOperation
from typing import Any

import httpx

from app.config import Settings
from app.utils.datetime import iso_to_datetime


@dataclass(frozen=True, slots=True)
class LavaInvoice:
    invoice_id: str | None
    payment_url: str
    raw_payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LavaPaymentNotification:
    order_id: str | None
    invoice_id: str | None
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
        order_id: str,
        amount: int,
        currency: str,
        description: str,
        success_url: str,
        webhook_url: str,
        expires_minutes: int = 60,
        fail_url: str | None = None,
    ) -> LavaInvoice:
        if not self.settings.lava_shop_id or not self._secret_key:
            return LavaInvoice(
                invoice_id=None,
                payment_url=f"{self.settings.app_base_url.rstrip('/')}/lava/success?invoice={order_id}",
                raw_payload={"stub": True, "reason": "Lava credentials are not configured"},
            )

        payload = {
            "sum": amount,
            "orderId": order_id,
            "shopId": self.settings.lava_shop_id,
            "hookUrl": webhook_url,
            "successUrl": success_url,
        }
        if fail_url:
            payload["failUrl"] = fail_url
        payload["expire"] = expires_minutes
        body = self._json_body(payload)
        response = await self.http_client.post(
            f"{self._base_url}/business/invoice/create",
            content=body,
            headers=self._signed_headers(body),
        )
        response.raise_for_status()
        data = response.json()
        response_data = data.get("data") if isinstance(data.get("data"), dict) else {}
        invoice_id = (
            data.get("invoice_id")
            or data.get("invoiceId")
            or data.get("id")
            or response_data.get("invoice_id")
            or response_data.get("invoiceId")
            or response_data.get("id")
        )
        payment_url = (
            data.get("payment_url")
            or data.get("paymentUrl")
            or data.get("url")
            or response_data.get("payment_url")
            or response_data.get("paymentUrl")
            or response_data.get("url")
        )
        if not payment_url:
            raise ValueError(f"Lava invoice response does not contain payment URL: {data}")
        return LavaInvoice(str(invoice_id) if invoice_id else None, str(payment_url), data)

    async def get_invoice_status(self, invoice_id: str | None, order_id: str) -> LavaPaymentNotification:
        if not self.settings.lava_shop_id or not self._secret_key:
            return LavaPaymentNotification(
                order_id=order_id,
                invoice_id=invoice_id,
                status="pending",
                amount=None,
                currency=None,
                paid_at=None,
                raw_payload={"stub": True, "reason": "Lava credentials are not configured"},
            )

        payload = {"shopId": self.settings.lava_shop_id}
        if invoice_id:
            payload["invoiceId"] = invoice_id
        else:
            payload["orderId"] = order_id
        body = self._json_body(payload)
        response = await self.http_client.post(
            f"{self._base_url}/business/invoice/status",
            content=body,
            headers=self._signed_headers(body),
        )
        if response.status_code == 404:
            return LavaPaymentNotification(
                order_id=order_id,
                invoice_id=invoice_id,
                status="unknown",
                amount=None,
                currency=None,
                paid_at=None,
                raw_payload=_response_payload(response),
            )
        response.raise_for_status()
        data = response.json()
        return self.normalize_payload(data)

    def verify_webhook(self, body: bytes, signature: str | None) -> bool:
        if not self._additional_key or not signature:
            return False
        signed_body = _webhook_signed_body(body)
        if signed_body is None:
            return False
        expected = hmac.new(self._additional_key.encode(), signed_body.encode(), hashlib.sha256).hexdigest()
        return any(hmac.compare_digest(expected, supplied) for supplied in _signature_candidates(signature))

    def normalize_payload(self, payload: dict[str, Any]) -> LavaPaymentNotification:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        raw_status = str(data.get("status") or "").lower()
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
        paid_at = (
            data.get("pay_time")
            or data.get("payTime")
            or data.get("paid_at")
            or data.get("paidAt")
            or data.get("updated_at")
        )
        return LavaPaymentNotification(
            order_id=_optional_str(data.get("order_id") or data.get("orderId")),
            invoice_id=_optional_str(data.get("invoice_id") or data.get("invoiceId") or data.get("id")),
            status=status,
            amount=_optional_int(amount),
            currency=_optional_str(data.get("currency")),
            paid_at=_optional_datetime(paid_at),
            raw_payload=payload,
        )

    @property
    def _base_url(self) -> str:
        return (self.settings.lava_base_url or "https://api.lava.ru").rstrip("/")

    @property
    def _secret_key(self) -> str:
        return self.settings.lava_secret_key

    @property
    def _additional_key(self) -> str:
        return self.settings.lava_additional_key

    def _signed_headers(self, body: str) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Signature": hmac.new(self._secret_key.encode(), body.encode(), hashlib.sha256).hexdigest(),
        }

    @staticmethod
    def _json_body(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return None


def _optional_datetime(value: object) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC)
    try:
        return iso_to_datetime(str(value))
    except ValueError:
        return None


def _signature_candidates(signature: str) -> tuple[str, ...]:
    supplied = signature.strip()
    candidates = {supplied}
    lower = supplied.lower()
    for prefix in ("bearer ", "sha256=", "signature "):
        if lower.startswith(prefix):
            candidates.add(supplied[len(prefix) :].strip())
    if " " in supplied:
        candidates.add(supplied.rsplit(" ", 1)[-1].strip())
    return tuple(candidate for candidate in candidates if candidate)


def _webhook_signed_body(body: bytes) -> str | None:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return json.dumps(dict(sorted(payload.items())), ensure_ascii=False, separators=(",", ":"))


def _response_payload(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {"status_code": response.status_code, "text": response.text}
    if isinstance(payload, dict):
        return payload
    return {"status_code": response.status_code, "data": payload}
