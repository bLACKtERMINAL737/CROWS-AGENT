from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from crows import app as app_module
from crows.messaging_dispatch import InboundDispatchStore
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
    db_path = tmp_path / "messaging.sqlite3"
    store = MessagingStore(db_path)
    dispatch_store = InboundDispatchStore(db_path)
    monkeypatch.setattr(app_module, "messaging_store", store)
    monkeypatch.setattr(app_module, "inbound_dispatch_store", dispatch_store)
    FakeAsyncClient.calls = []
    FakeAsyncClient.response = FakeResponse(200, {"ok": True, "result": {"message_id": 71}})
    monkeypatch.setattr(app_module.httpx, "AsyncClient", FakeAsyncClient)
    return store


def make_request(payload, secret="test-secret"):
    body = __import__("json").dumps(payload).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/messaging/webhooks/telegram",
            "headers": [(b"x-telegram-bot-api-secret-token", secret.encode())],
            "query_string": b"",
            "server": ("testserver", 443),
            "client": ("127.0.0.1", 12345),
            "scheme": "https",
        },
        receive,
    )


@pytest.mark.asyncio
async def test_telegram_webhook_rejects_missing_or_wrong_secret(monkeypatch, isolated_store):
    monkeypatch.setenv("AETHER_TELEGRAM_WEBHOOK_SECRET", "expected-secret")
    with pytest.raises(HTTPException) as error:
        await app_module.telegram_webhook(make_request({}, secret="wrong-secret"))
    assert error.value.status_code == 401
    assert isolated_store.list_conversations() == []


@pytest.mark.asyncio
async def test_telegram_webhook_persists_and_deduplicates_message(monkeypatch, isolated_store):
    monkeypatch.setenv("AETHER_TELEGRAM_WEBHOOK_SECRET", "test-secret")
    payload = {
        "update_id": 101,
        "message": {
            "message_id": 5,
            "date": 1793275200,
            "chat": {"id": 777, "type": "private"},
            "from": {"id": 777, "first_name": "Test"},
            "text": "hello from telegram",
        },
    }
    first = await app_module.telegram_webhook(make_request(payload))
    second = await app_module.telegram_webhook(make_request(payload))
    assert first == {"ok": True, "duplicate": False}
    assert second == {"ok": True, "duplicate": True}
    conversations = isolated_store.list_conversations()
    assert len(conversations) == 1
    assert conversations[0]["last_message"] == "hello from telegram"
    dispatches = app_module.inbound_dispatch_store.list_recent()
    assert len(dispatches) == 1
    assert dispatches[0]["channel"] == "telegram"
    assert dispatches[0]["state"] == "pending"


@pytest.mark.asyncio
async def test_telegram_send_persists_success_and_uses_bot_api(monkeypatch, isolated_store):
    monkeypatch.setenv("AETHER_TELEGRAM_BOT_TOKEN", "test-token")
    FakeAsyncClient.response = FakeResponse(200, {"ok": True, "result": {"message_id": 71}})
    request = app_module.TelegramSendRequest(
        chat_id="777", text="hello", idempotency_key="telegram-send-1"
    )
    result = await app_module.telegram_send(request)
    assert result["message"]["status"] == "sent"
    assert result["message"]["provider_message_id"] == "71"
    url, kwargs = FakeAsyncClient.calls[0]
    assert url == "https://api.telegram.org/bottest-token/sendMessage"
    assert kwargs["json"] == {"chat_id": "777", "text": "hello"}
    assert isolated_store.get_outbound("telegram", "telegram-send-1")["status"] == "sent"


@pytest.mark.asyncio
async def test_telegram_direct_send_does_not_race_background_outbox_worker(monkeypatch, isolated_store):
    monkeypatch.setenv("AETHER_TELEGRAM_BOT_TOKEN", "test-token")
    original_queue = isolated_store.queue_outbound

    def queue_then_claim_for_background_worker(**kwargs):
        queued = original_queue(**kwargs)
        if not queued["duplicate"]:
            claimed = isolated_store.claim_outbound(
                worker_id="background-worker", limit=1, lease_seconds=60
            )
            assert [item["id"] for item in claimed] == [queued["message"]["id"]]
        return queued

    monkeypatch.setattr(isolated_store, "queue_outbound", queue_then_claim_for_background_worker)
    request = app_module.TelegramSendRequest(
        chat_id="777", text="hello", idempotency_key="telegram-worker-race"
    )
    with pytest.raises(HTTPException) as error:
        await app_module.telegram_send(request)
    assert error.value.status_code == 409
    assert FakeAsyncClient.calls == []
    assert isolated_store.get_outbound("telegram", "telegram-worker-race")["status"] == "accepted"

    # The background worker already owns the active lease. A second claim
    # must not send the same message, even when it uses the same worker ID;
    # the original worker continues from its already-claimed batch.
    assert isolated_store.claim_outbound(
        worker_id="background-worker", limit=1, lease_seconds=60
    ) == []
    assert FakeAsyncClient.calls == []
    assert isolated_store.get_outbound("telegram", "telegram-worker-race")["status"] == "accepted"


@pytest.mark.asyncio
async def test_telegram_send_marks_unknown_and_blocks_same_key_retry(monkeypatch, isolated_store):
    monkeypatch.setenv("AETHER_TELEGRAM_BOT_TOKEN", "test-token")

    class BrokenClient(FakeAsyncClient):
        async def post(self, url, **kwargs):
            raise httpx.ConnectError("connection dropped")

    monkeypatch.setattr(app_module.httpx, "AsyncClient", BrokenClient)
    request = app_module.TelegramSendRequest(
        chat_id="777", text="hello", idempotency_key="telegram-unknown-1"
    )
    with pytest.raises(HTTPException) as first:
        await app_module.telegram_send(request)
    assert first.value.status_code == 502
    assert isolated_store.get_outbound("telegram", "telegram-unknown-1")["status"] == "unknown"
    call_count = len(FakeAsyncClient.calls)
    with pytest.raises(HTTPException) as retry:
        await app_module.telegram_send(request)
    assert retry.value.status_code == 409
    assert len(FakeAsyncClient.calls) == call_count


@pytest.mark.asyncio
async def test_telegram_send_does_not_call_provider_without_token(monkeypatch, isolated_store):
    monkeypatch.delenv("AETHER_TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(HTTPException) as error:
        await app_module.telegram_send(
            app_module.TelegramSendRequest(
                chat_id="777", text="hello", idempotency_key="telegram-send-no-token"
            )
        )
    assert error.value.status_code == 503
    assert FakeAsyncClient.calls == []
