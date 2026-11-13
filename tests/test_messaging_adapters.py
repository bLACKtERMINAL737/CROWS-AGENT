from __future__ import annotations

import pytest

from crows.messaging import MessagingContractError
from crows.messaging_adapters import (
    DiscordOutboundAdapter,
    OutboundTransportMessage,
    OutboundTransportReceipt,
    OutboundTransportRegistry,
    TelegramOutboundAdapter,
)


class FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None):
        self.status_code = status_code
        self._body = body or {}
        self.content = b"present" if body is not None else b""
        self.headers = headers or {}
        self.is_success = 200 <= status_code < 300

    def json(self):
        return self._body


class FakeClient:
    calls = []
    response = FakeResponse(200, {"ok": True, "result": {"message_id": 42}})

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.response

    async def patch(self, url, **kwargs):
        self.calls.append(("PATCH", url, kwargs))
        return self.response


@pytest.fixture(autouse=True)
def fake_httpx(monkeypatch):
    import crows.messaging_adapters as adapters

    FakeClient.calls = []
    FakeClient.response = FakeResponse(200, {"ok": True, "result": {"message_id": 42}})
    monkeypatch.setattr(adapters.httpx, "AsyncClient", FakeClient)


def test_outbound_registry_is_explicit_and_rejects_duplicates():
    registry = OutboundTransportRegistry()
    adapter = TelegramOutboundAdapter(lambda: ("bot-token", ""))
    registry.register(adapter)
    assert registry.list_channels() == ["telegram"]
    assert registry.get("telegram") is adapter
    with pytest.raises(MessagingContractError):
        registry.register(adapter)
    with pytest.raises(MessagingContractError):
        registry.get("matrix")


@pytest.mark.asyncio
async def test_telegram_adapter_sends_and_extracts_provider_message_id():
    adapter = TelegramOutboundAdapter(lambda: ("bot-token", ""))
    message = OutboundTransportMessage("telegram", "777", "hello", "key-1")
    receipt = await adapter.send(message)
    assert receipt == OutboundTransportReceipt(True, 200, "42", None)
    method, url, kwargs = FakeClient.calls[0]
    assert method == "POST"
    assert url == "https://api.telegram.org/botbot-token/sendMessage"
    assert kwargs["json"] == {"chat_id": "777", "text": "hello"}


@pytest.mark.asyncio
async def test_telegram_adapter_reports_missing_configuration_without_network():
    adapter = TelegramOutboundAdapter(lambda: ("", ""))
    receipt = await adapter.send(
        OutboundTransportMessage("telegram", "777", "hello", "key-2")
    )
    assert receipt.success is False
    assert receipt.status_code == 503
    assert FakeClient.calls == []


@pytest.mark.asyncio
async def test_discord_webhook_adapter_uses_wait_and_parses_receipt():
    FakeClient.response = FakeResponse(200, {"id": "discord-message"})
    adapter = DiscordOutboundAdapter(
        lambda: "https://discord.com/api/webhooks/123/secret"
    )
    receipt = await adapter.send(
        OutboundTransportMessage("discord", "configured-webhook", "hello", "key-3")
    )
    assert receipt.success is True
    assert receipt.provider_message_id == "discord-message"
    method, url, kwargs = FakeClient.calls[0]
    assert method == "POST"
    assert url.endswith("/secret")
    assert kwargs["json"] == {"content": "hello"}
    assert kwargs["params"] == {"wait": "true"}


@pytest.mark.asyncio
async def test_discord_interaction_adapter_patches_original_response():
    adapter = DiscordOutboundAdapter(lambda: "")
    target = '{"kind":"discord_interaction","application_id":"123456","token":"interaction-token-0123456789"}'
    FakeClient.response = FakeResponse(200, {"id": "discord-message"})
    receipt = await adapter.send(
        OutboundTransportMessage("discord", target, "reply", "key-4")
    )
    assert receipt.success is True
    assert FakeClient.calls[0][0] == "PATCH"
    assert FakeClient.calls[0][1].endswith("/messages/@original")
    assert FakeClient.calls[0][2]["json"] == {"content": "reply"}


@pytest.mark.asyncio
async def test_discord_adapter_rejects_invalid_interaction_target():
    adapter = DiscordOutboundAdapter(lambda: "")
    receipt = await adapter.send(
        OutboundTransportMessage("discord", '{"kind":"discord_interaction","token":"bad"}', "reply", "key-5")
    )
    assert receipt.success is False
    assert receipt.status_code == 400
    assert FakeClient.calls == []


@pytest.mark.asyncio
async def test_adapters_preserve_rate_limit_retry_after():
    adapter = TelegramOutboundAdapter(lambda: ("bot-token", ""))
    FakeClient.response = FakeResponse(429, {"ok": False}, {"Retry-After": "17"})
    receipt = await adapter.send(
        OutboundTransportMessage("telegram", "777", "hello", "key-6")
    )
    assert receipt.status_code == 429
    assert receipt.retry_after_seconds == 17


@pytest.mark.asyncio
async def test_telegram_rate_limit_reads_retry_after_from_bot_api_parameters():
    adapter = TelegramOutboundAdapter(lambda: ("bot-token", ""))
    FakeClient.response = FakeResponse(429, {"ok": False, "parameters": {"retry_after": 23}})
    receipt = await adapter.send(OutboundTransportMessage("telegram", "777", "hello", "key-7"))
    assert receipt.retry_after_seconds == 23


@pytest.mark.asyncio
async def test_discord_rate_limit_accepts_fractional_json_retry_after_and_caps_delay():
    adapter = DiscordOutboundAdapter(lambda: "https://discord.com/api/webhooks/123/secret")
    FakeClient.response = FakeResponse(429, {"message": "rate limited", "retry_after": 1.25})
    receipt = await adapter.send(OutboundTransportMessage("discord", "configured-webhook", "hello", "key-8"))
    assert receipt.retry_after_seconds == 2
    FakeClient.response = FakeResponse(429, {"retry_after": 900})
    receipt = await adapter.send(OutboundTransportMessage("discord", "configured-webhook", "hello", "key-9"))
    assert receipt.retry_after_seconds == 300


@pytest.mark.asyncio
async def test_rate_limit_uses_default_for_invalid_header_and_survives_invalid_json():
    class MalformedResponse(FakeResponse):
        def __init__(self):
            super().__init__(429, None, {"Retry-After": "not-a-delay"})
            self.content = b"{"

        def json(self):
            raise ValueError("invalid JSON")

    adapter = TelegramOutboundAdapter(lambda: ("bot-token", ""))
    FakeClient.response = MalformedResponse()
    receipt = await adapter.send(OutboundTransportMessage("telegram", "777", "hello", "key-10"))
    assert receipt.retry_after_seconds == 5
