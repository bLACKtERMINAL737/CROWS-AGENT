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
    response = FakeResponse()

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
    FakeAsyncClient.response = FakeResponse(200, {"id": "discord-msg-1"})
    monkeypatch.setattr(app_module.httpx, "AsyncClient", FakeAsyncClient)
    return store


def test_discord_webhook_url_accepts_only_expected_https_hosts(monkeypatch):
    monkeypatch.setenv(
        "AETHER_DISCORD_WEBHOOK_URL",
        "https://discord.com/api/webhooks/123456/secret-token",
    )
    assert app_module._discord_webhook_url() == "https://discord.com/api/webhooks/123456/secret-token"

    for invalid in (
        "http://discord.com/api/webhooks/123456/token",
        "https://example.com/api/webhooks/123456/token",
        "https://discord.com.evil.example/api/webhooks/123456/token",
        "https://user:pass@discord.com/api/webhooks/123456/token",
        "https://discord.com/not-a-webhook",
    ):
        monkeypatch.setenv("AETHER_DISCORD_WEBHOOK_URL", invalid)
        assert app_module._discord_webhook_url() == ""


@pytest.mark.asyncio
async def test_discord_send_persists_provider_message_id(monkeypatch, isolated_store):
    monkeypatch.setenv(
        "AETHER_DISCORD_WEBHOOK_URL",
        "https://discord.com/api/webhooks/123456/secret-token",
    )
    result = await app_module.discord_send(
        app_module.DiscordSendRequest(text="hello", idempotency_key="send-1")
    )
    assert result["message"]["status"] == "sent"
    assert result["message"]["provider_message_id"] == "discord-msg-1"
    assert FakeAsyncClient.calls[0][1]["params"] == {"wait": "true"}
    assert isolated_store.get_outbound("discord", "send-1")["status"] == "sent"


@pytest.mark.asyncio
async def test_discord_send_rejects_unconfigured_destination(monkeypatch, isolated_store):
    monkeypatch.delenv("AETHER_DISCORD_WEBHOOK_URL", raising=False)
    with pytest.raises(HTTPException) as error:
        await app_module.discord_send(
            app_module.DiscordSendRequest(text="hello", idempotency_key="send-2")
        )
    assert error.value.status_code == 503
    assert FakeAsyncClient.calls == []


@pytest.mark.asyncio
async def test_discord_send_does_not_retry_unknown_idempotency_key(monkeypatch, isolated_store):
    monkeypatch.setenv(
        "AETHER_DISCORD_WEBHOOK_URL",
        "https://discord.com/api/webhooks/123456/secret-token",
    )

    class BrokenClient(FakeAsyncClient):
        async def post(self, url, **kwargs):
            raise httpx.ConnectError("connection dropped")

    monkeypatch.setattr(app_module.httpx, "AsyncClient", BrokenClient)
    request = app_module.DiscordSendRequest(text="hello", idempotency_key="same-key")
    with pytest.raises(HTTPException) as first_error:
        await app_module.discord_send(request)
    assert first_error.value.status_code == 502
    assert isolated_store.get_outbound("discord", "same-key")["status"] == "unknown"
    call_count = len(FakeAsyncClient.calls)

    with pytest.raises(HTTPException) as retry_error:
        await app_module.discord_send(request)
    assert retry_error.value.status_code == 409
    assert len(FakeAsyncClient.calls) == call_count


@pytest.mark.asyncio
async def test_discord_send_marks_transport_failure_unknown(monkeypatch, isolated_store):
    monkeypatch.setenv(
        "AETHER_DISCORD_WEBHOOK_URL",
        "https://discord.com/api/webhooks/123456/secret-token",
    )

    class BrokenClient(FakeAsyncClient):
        async def post(self, url, **kwargs):
            raise httpx.ConnectError("connection dropped")

    monkeypatch.setattr(app_module.httpx, "AsyncClient", BrokenClient)
    with pytest.raises(HTTPException) as error:
        await app_module.discord_send(
            app_module.DiscordSendRequest(text="hello", idempotency_key="send-3")
        )
    assert error.value.status_code == 502
    assert isolated_store.get_outbound("discord", "send-3")["status"] == "unknown"



@pytest.mark.asyncio
async def test_outbox_worker_sends_telegram_and_finishes_lease(monkeypatch, isolated_store):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("AETHER_MESSAGING_TARGET_KEY", Fernet.generate_key().decode())
    isolated_store.__init__(isolated_store.db_path)
    queued = isolated_store.queue_outbound(
        channel="telegram", provider_user_id="chat-1", text="worker hello",
        idempotency_key="worker-telegram", delivery_target="98765",
    )
    FakeAsyncClient.response = FakeResponse(200, {"ok": True, "result": {"message_id": 123}})
    monkeypatch.setenv("AETHER_TELEGRAM_BOT_TOKEN", "test-token")
    result = await app_module.deliver_outbound_batch(worker_id="worker-test")
    assert result == {"claimed": 1, "sent": 1, "retried": 0, "failed": 0, "unknown": 0, "skipped": 0}
    assert isolated_store.get_outbound("telegram", "worker-telegram")["status"] == "sent"
    assert "98765" in FakeAsyncClient.calls[-1][1]["json"].values()


@pytest.mark.asyncio
async def test_outbox_worker_marks_transport_error_unknown(monkeypatch, isolated_store):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("AETHER_MESSAGING_TARGET_KEY", Fernet.generate_key().decode())
    isolated_store.__init__(isolated_store.db_path)
    isolated_store.queue_outbound(
        channel="telegram", provider_user_id="chat-2", text="worker uncertain",
        idempotency_key="worker-unknown", delivery_target="12345",
    )
    monkeypatch.setenv("AETHER_TELEGRAM_BOT_TOKEN", "test-token")

    class BrokenClient(FakeAsyncClient):
        async def post(self, url, **kwargs):
            raise httpx.ConnectError("connection dropped")

    monkeypatch.setattr(app_module.httpx, "AsyncClient", BrokenClient)
    result = await app_module.deliver_outbound_batch(worker_id="worker-test")
    assert result["unknown"] == 1
    assert isolated_store.get_outbound("telegram", "worker-unknown")["status"] == "unknown"



@pytest.mark.asyncio
async def test_outbox_worker_sends_discord_to_fixed_configured_webhook(monkeypatch, isolated_store):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("AETHER_MESSAGING_TARGET_KEY", Fernet.generate_key().decode())
    isolated_store.__init__(isolated_store.db_path)
    monkeypatch.setenv(
        "AETHER_DISCORD_WEBHOOK_URL",
        "https://discord.com/api/webhooks/123456/secret-token",
    )
    isolated_store.queue_outbound(
        channel="discord", provider_user_id="configured-webhook", text="discord worker",
        idempotency_key="worker-discord", delivery_target="configured-webhook",
    )
    FakeAsyncClient.response = FakeResponse(200, {"id": "discord-worker-message"})
    result = await app_module.deliver_outbound_batch(worker_id="worker-test")
    assert result["sent"] == 1
    assert isolated_store.get_outbound("discord", "worker-discord")["status"] == "sent"
    assert FakeAsyncClient.calls[-1][0] == "https://discord.com/api/webhooks/123456/secret-token"
    assert FakeAsyncClient.calls[-1][1]["params"] == {"wait": "true"}



@pytest.mark.asyncio
async def test_worker_schedules_retry_only_for_explicit_rate_limit(monkeypatch, isolated_store):
    from cryptography.fernet import Fernet
    from datetime import datetime, timedelta, timezone

    monkeypatch.setenv("AETHER_MESSAGING_TARGET_KEY", Fernet.generate_key().decode())
    isolated_store.__init__(isolated_store.db_path)
    isolated_store.queue_outbound(
        channel="telegram", provider_user_id="chat-rate", text="rate limited",
        idempotency_key="worker-rate", delivery_target="555",
    )
    monkeypatch.setenv("AETHER_TELEGRAM_BOT_TOKEN", "test-token")

    class RateLimitedResponse(FakeResponse):
        status_code = 429
        headers = {"Retry-After": "12"}
        is_success = False

    FakeAsyncClient.response = RateLimitedResponse(429, {"ok": False})
    result = await app_module.deliver_outbound_batch(worker_id="worker-rate")
    assert result["retried"] == 1
    assert isolated_store.get_outbound("telegram", "worker-rate")["status"] == "accepted"
    with isolated_store.connect() as con:
        retry = con.execute(
            "SELECT retry_count,next_attempt_at FROM messaging_delivery_retries"
        ).fetchone()
    assert retry["retry_count"] == 1
    scheduled = datetime.fromisoformat(retry["next_attempt_at"])
    assert scheduled > datetime.now(timezone.utc)
