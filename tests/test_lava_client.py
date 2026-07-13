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


def _webhook_signature(secret: str, payload: dict[str, object]) -> str:
    body = json.dumps(dict(sorted(payload.items())), ensure_ascii=False, separators=(",", ":"))
    return _signature(secret, body)


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
    payload = {"invoice_id": "lava-invoice-1", "order_id": "order-1", "status": "success"}
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    client = LavaClient(_settings())

    assert client.verify_webhook(body, _webhook_signature("additional-key", payload))
    assert client.verify_webhook(body, f"Bearer {_webhook_signature('additional-key', payload)}")
    assert not client.verify_webhook(body, _webhook_signature("secret-key", payload))
    assert not LavaClient(_settings(lava_additional_key="")).verify_webhook(
        body,
        _webhook_signature("additional-key", payload),
    )


def test_verify_webhook_matches_official_lava_sdk_example() -> None:
    payload = {
        "invoice_id": "18cf0c0b-6539-4d7c-b3e9-479e4922b87c",
        "status": "success",
        "pay_time": "2022-11-08 11:26:46",
        "amount": "1.00",
        "order_id": "636a3c2f3e82b",
        "pay_service": "card",
        "payer_details": "553691******8079",
        "custom_fields": "test",
        "type": 1,
        "credited": "1.00",
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    client = LavaClient(_settings(lava_additional_key="f4b91efb9b8da35737fcd97ab123c74566f9a654"))

    assert client.verify_webhook(body, "b0b011552beb994cc04401e088db7b296796a07fc76976b632518fe146ffa330")


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
