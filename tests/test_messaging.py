from __future__ import annotations

import pytest

from crows.messaging import (
    ChannelRegistry,
    DeliveryReceipt,
    DeliveryStatus,
    InboundMessage,
    MessagingContractError,
    OutboundMessage,
    conversation_scope,
)


def inbound(**overrides):
    values = {
        "channel": "telegram",
        "provider_message_id": "provider-msg-1",
        "provider_user_id": "user-42",
        "text": "  Hello Maximu Agent  ",
        "received_at": "2026-10-29T12:00:00+00:00",
    }
    values.update(overrides)
    return InboundMessage(**values)


def test_inbound_normalizes_channel_and_text():
    message = inbound(channel=" Telegram ")
    assert message.channel == "telegram"
    assert message.text == "Hello Maximu Agent"


@pytest.mark.parametrize("values", [
    {"channel": "../telegram"},
    {"provider_message_id": ""},
    {"provider_user_id": " "},
    {"text": "  "},
    {"received_at": "not-a-date"},
    {"received_at": "2026-10-29T12:00:00"},
])
def test_inbound_rejects_invalid_fields(values):
    with pytest.raises(MessagingContractError):
        inbound(**values)


def test_inbound_rejects_oversized_text():
    with pytest.raises(MessagingContractError, match="exceeds"):
        inbound(text="x" * 20_001)


def test_outbound_rejects_empty_or_oversized_fields():
    with pytest.raises(MessagingContractError):
        OutboundMessage("telegram", "user", " ", "key")
    with pytest.raises(MessagingContractError):
        OutboundMessage("telegram", "user", "hello", "x" * 257)


def test_registry_is_explicit_and_duplicate_safe():
    class Adapter:
        channel_name = "telegram"

        async def send(self, message):
            return DeliveryReceipt("telegram", DeliveryStatus.SENT)

    registry = ChannelRegistry()
    assert registry.list_channels() == []
    registry.register(Adapter())
    assert registry.list_channels() == ["telegram"]
    assert registry.get("telegram").channel_name == "telegram"
    with pytest.raises(MessagingContractError, match="already registered"):
        registry.register(Adapter())
    with pytest.raises(MessagingContractError, match="not configured"):
        registry.get("whatsapp")


def test_delivery_receipt_has_timestamp_and_bounded_errors():
    receipt = DeliveryReceipt("discord", DeliveryStatus.UNKNOWN, error_code="provider_timeout")
    assert receipt.updated_at
    with pytest.raises(MessagingContractError):
        DeliveryReceipt("discord", DeliveryStatus.FAILED, error_code="x" * 101)


def test_conversation_scope_is_stable_and_channel_isolated():
    first = conversation_scope("telegram", "user-42", namespace="workspace-a")
    assert first == conversation_scope("telegram", "user-42", namespace="workspace-a")
    assert first != conversation_scope("discord", "user-42", namespace="workspace-a")
    assert first != conversation_scope("telegram", "user-42", namespace="workspace-b")
    assert first.startswith("msg_")
    assert "user-42" not in first


@pytest.mark.asyncio
async def test_telegram_json_reader_accepts_valid_payload():
    from starlette.requests import Request

    from crows.app import _read_telegram_json

    body = b'{"update_id": 12}'
    messages = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        return messages.pop(0)

    request = Request({
        "type": "http", "method": "POST", "path": "/",
        "headers": [(b"content-length", str(len(body)).encode())],
        "query_string": b"", "server": ("test", 80), "client": ("test", 1),
        "scheme": "http", "http_version": "1.1",
    }, receive)
    assert await _read_telegram_json(request) == {"update_id": 12}


@pytest.mark.asyncio
async def test_telegram_json_reader_rejects_declared_oversize_body():
    from starlette.requests import Request
    from fastapi import HTTPException

    from crows.app import _read_telegram_json

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request({
        "type": "http", "method": "POST", "path": "/",
        "headers": [(b"content-length", b"11")],
        "query_string": b"", "server": ("test", 80), "client": ("test", 1),
        "scheme": "http", "http_version": "1.1",
    }, receive)
    with pytest.raises(HTTPException) as error:
        await _read_telegram_json(request, max_bytes=10)
    assert error.value.status_code == 413


@pytest.mark.asyncio
async def test_telegram_json_reader_rejects_chunked_oversize_body():
    from starlette.requests import Request
    from fastapi import HTTPException

    from crows.app import _read_telegram_json

    chunks = [
        {"type": "http.request", "body": b"123456", "more_body": True},
        {"type": "http.request", "body": b"78901", "more_body": False},
    ]

    async def receive():
        return chunks.pop(0)

    request = Request({
        "type": "http", "method": "POST", "path": "/",
        "headers": [], "query_string": b"", "server": ("test", 80),
        "client": ("test", 1), "scheme": "http", "http_version": "1.1",
    }, receive)
    with pytest.raises(HTTPException) as error:
        await _read_telegram_json(request, max_bytes=10)
    assert error.value.status_code == 413


@pytest.mark.asyncio
async def test_telegram_json_reader_rejects_invalid_json():
    from starlette.requests import Request
    from fastapi import HTTPException

    from crows.app import _read_telegram_json

    async def receive():
        return {"type": "http.request", "body": b"{bad json", "more_body": False}

    request = Request({
        "type": "http", "method": "POST", "path": "/",
        "headers": [], "query_string": b"", "server": ("test", 80),
        "client": ("test", 1), "scheme": "http", "http_version": "1.1",
    }, receive)
    with pytest.raises(HTTPException) as error:
        await _read_telegram_json(request)
    assert error.value.status_code == 400


def test_telegram_provider_message_id_prefers_update_id_for_deduplication():
    from crows.app import _telegram_provider_message_id

    assert _telegram_provider_message_id(42, 7, 9001) == "update:9001"
    assert _telegram_provider_message_id(99, 7, 9001) == "update:9001"


def test_telegram_provider_message_id_falls_back_for_legacy_updates():
    from crows.app import _telegram_provider_message_id

    assert _telegram_provider_message_id(42, 7, None) == "42:7"


def test_telegram_timestamp_accepts_valid_unix_seconds():
    from crows.app import _telegram_received_at

    assert _telegram_received_at(1793318400) == "2026-10-30T00:00:00+00:00"


@pytest.mark.parametrize("raw_date", ["1793318400", 1.5, True, False, [], {}, -10**30, 10**30])
def test_telegram_timestamp_rejects_malformed_or_out_of_range_values(raw_date):
    from crows.app import _telegram_received_at

    with pytest.raises(ValueError):
        _telegram_received_at(raw_date)


def test_telegram_timestamp_uses_utc_now_when_missing():
    from datetime import datetime

    from crows.app import _telegram_received_at

    parsed = datetime.fromisoformat(_telegram_received_at(None))
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0
