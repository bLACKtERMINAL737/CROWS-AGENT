from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from crows import app as app_module
from crows.messaging_store import MessagingStore


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}
        self.content = b"present" if body is not None else b""

    @property
    def is_success(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._body


class FakeAsyncClient:
    calls = []
    response = FakeResponse(200, {"id": "discord-message-1"})

    def __init__(self, *args, **kwargs):
        self.options = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


@pytest.fixture
def isolated_store(monkeypatch, tmp_path: Path):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("AETHER_MESSAGING_TARGET_KEY", Fernet.generate_key().decode())
    store = MessagingStore(tmp_path / "messaging.sqlite3")
    monkeypatch.setattr(app_module, "messaging_store", store)
    FakeAsyncClient.calls = []
    FakeAsyncClient.response = FakeResponse(200, {"id": "discord-message-1"})
    monkeypatch.setattr(app_module.httpx, "AsyncClient", FakeAsyncClient)
    return store


def test_discord_url_validator_rejects_untrusted_urls(monkeypatch):
    for value in (
        "",
        "http://discord.com/api/webhooks/123/token",
        "https://example.com/api/webhooks/123/token",
        "https://discord.com.evil.example/api/webhooks/123/token",
        "https://user:pass@discord.com/api/webhooks/123/token",
        "https://discord.com/not-webhook/123/token",
    ):
        monkeypatch.setenv("AETHER_DISCORD_WEBHOOK_URL", value)
        assert app_module._discord_webhook_url() == ""


@pytest.mark.asyncio
async def test_discord_send_persists_success_without_leaking_webhook(monkeypatch, isolated_store):
    webhook = "https://discord.com/api/webhooks/123/secret-value"
    monkeypatch.setenv("AETHER_DISCORD_WEBHOOK_URL", webhook)
    result = await app_module.discord_send(
        app_module.DiscordSendRequest(text="hello", idempotency_key="discord-send-1")
    )
    assert result["message"]["status"] == "sent"
    assert result["message"]["provider_message_id"] == "discord-message-1"
    assert FakeAsyncClient.calls[0][0] == webhook
    assert FakeAsyncClient.calls[0][1]["json"] == {"content": "hello"}
    assert FakeAsyncClient.calls[0][1]["params"] == {"wait": "true"}
    status = app_module.discord_status()
    assert status["configured"] is True
    assert webhook not in str(status)
    assert isolated_store.get_outbound("discord", "discord-send-1")["status"] == "sent"


@pytest.mark.asyncio
async def test_discord_rejects_invalid_provider_response(monkeypatch, isolated_store):
    monkeypatch.setenv(
        "AETHER_DISCORD_WEBHOOK_URL",
        "https://discord.com/api/webhooks/123/secret-value",
    )
    FakeAsyncClient.response = FakeResponse(200, {"ok": True})
    with pytest.raises(HTTPException) as error:
        await app_module.discord_send(
            app_module.DiscordSendRequest(text="hello", idempotency_key="discord-send-2")
        )
    assert error.value.status_code == 502
    assert isolated_store.get_outbound("discord", "discord-send-2")["status"] == "failed"


@pytest.mark.asyncio
async def test_discord_transport_failure_marks_unknown_and_prevents_retry(monkeypatch, isolated_store):
    monkeypatch.setenv(
        "AETHER_DISCORD_WEBHOOK_URL",
        "https://discord.com/api/webhooks/123/secret-value",
    )

    class BrokenClient(FakeAsyncClient):
        async def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            raise httpx.ConnectError("connection dropped")

    monkeypatch.setattr(app_module.httpx, "AsyncClient", BrokenClient)
    request = app_module.DiscordSendRequest(text="hello", idempotency_key="discord-unknown-1")
    with pytest.raises(HTTPException) as first:
        await app_module.discord_send(request)
    assert first.value.status_code == 502
    assert isolated_store.get_outbound("discord", "discord-unknown-1")["status"] == "unknown"
    call_count = len(FakeAsyncClient.calls)
    with pytest.raises(HTTPException) as retry:
        await app_module.discord_send(request)
    assert retry.value.status_code == 409
    assert len(FakeAsyncClient.calls) == call_count


@pytest.mark.asyncio
async def test_discord_send_requires_configured_webhook(monkeypatch, isolated_store):
    monkeypatch.delenv("AETHER_DISCORD_WEBHOOK_URL", raising=False)
    with pytest.raises(HTTPException) as error:
        await app_module.discord_send(
            app_module.DiscordSendRequest(text="hello", idempotency_key="discord-no-config")
        )
    assert error.value.status_code == 503
    assert FakeAsyncClient.calls == []
