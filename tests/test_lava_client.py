import hashlib
import hmac
import json
from datetime import UTC
from datetime import datetime

import httpx
import pytest

from app.config import Settings
from app.services.lava import LavaClient


def _settings(**overrides) -> Settings:
    values = {
        "app_base_url": "https://bot.example",
        "lava_base_url": "https://api.lava.ru",
        "lava_shop_id": "shop-1",
        "lava_secret_key": "secret-key",
        "lava_additional_key": "additional-key",
    }
    values.update(overrides)
    return Settings(**values)


def _signature(secret: str, body: str | bytes) -> str:
    body_bytes = body if isinstance(body, bytes) else body.encode()
    return hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()


@pytest.mark.asyncio
async def test_create_invoice_posts_business_payload_with_signature() -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = body
        seen["headers"] = request.headers
        return httpx.Response(
            200,
            json={"data": {"id": "lava-invoice-1", "url": "https://pay.example/i/1"}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        invoice = await LavaClient(_settings(), http_client).create_invoice(
            order_id="order-1",
            amount=1500,
            currency="RUB",
            description="ignored by Lava Business API",
            success_url="https://bot.example/lava/success?invoice=order-1",
            webhook_url="https://bot.example/lava/webhook",
            expires_minutes=45,
        )

    expected_body = json.dumps(
        {
            "sum": 1500,
            "orderId": "order-1",
            "shopId": "shop-1",
            "hookUrl": "https://bot.example/lava/webhook",
            "successUrl": "https://bot.example/lava/success?invoice=order-1",
            "expire": 45,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    headers = seen["headers"]
    assert seen["method"] == "POST"
    assert seen["url"] == "https://api.lava.ru/business/invoice/create"
    assert seen["body"] == expected_body
    assert headers["Accept"] == "application/json"
    assert headers["Content-Type"] == "application/json"
    assert headers["Signature"] == _signature("secret-key", expected_body)
    assert invoice.invoice_id == "lava-invoice-1"
    assert invoice.payment_url == "https://pay.example/i/1"


@pytest.mark.asyncio
async def test_get_invoice_status_posts_business_payload_with_invoice_id() -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = body
        seen["signature"] = request.headers["Signature"]
        return httpx.Response(
            200,
            json={
                "data": {
                    "invoiceId": "lava-invoice-1",
                    "orderId": "order-1",
                    "status": "success",
                    "sum": "1500",
                    "pay_time": "2026-07-10T10:11:12+00:00",
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        notification = await LavaClient(_settings(), http_client).get_invoice_status(
            invoice_id="lava-invoice-1",
            order_id="order-1",
        )

    expected_body = json.dumps(
        {"shopId": "shop-1", "invoiceId": "lava-invoice-1"},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    assert seen["method"] == "POST"
    assert seen["url"] == "https://api.lava.ru/business/invoice/status"
    assert seen["body"] == expected_body
    assert seen["signature"] == _signature("secret-key", expected_body)
    assert notification.order_id == "order-1"
    assert notification.invoice_id == "lava-invoice-1"
    assert notification.status == "paid"
    assert notification.amount == 1500
    assert notification.paid_at == datetime(2026, 7, 10, 10, 11, 12, tzinfo=UTC)


@pytest.mark.asyncio
async def test_get_invoice_status_treats_not_found_as_unknown_without_raising() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"data": None, "error": "Invoice not found", "status": 404, "status_check": False},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        notification = await LavaClient(_settings(), http_client).get_invoice_status(
            invoice_id="lava-invoice-1",
            order_id="order-1",
        )

    assert notification.order_id == "order-1"
    assert notification.invoice_id == "lava-invoice-1"
    assert notification.status == "unknown"
    assert notification.raw_payload["error"] == "Invoice not found"


def test_verify_webhook_uses_additional_key() -> None:
    body = b'{"invoice_id":"lava-invoice-1","order_id":"order-1","status":"success"}'
    client = LavaClient(_settings())

    assert client.verify_webhook(body, _signature("additional-key", body))
    assert not client.verify_webhook(body, _signature("secret-key", body))
    assert not LavaClient(_settings(lava_additional_key="")).verify_webhook(
        body,
        _signature("additional-key", body),
    )


def test_normalize_payload_understands_business_webhook() -> None:
    payload = {
        "invoice_id": "lava-invoice-1",
        "order_id": "order-1",
        "status": "success",
        "pay_time": "2026-07-10T10:11:12Z",
        "amount": "1500",
    }

    notification = LavaClient(_settings()).normalize_payload(payload)

    assert notification.order_id == "order-1"
    assert notification.invoice_id == "lava-invoice-1"
    assert notification.status == "paid"
    assert notification.amount == 1500
    assert notification.paid_at == datetime(2026, 7, 10, 10, 11, 12, tzinfo=UTC)
