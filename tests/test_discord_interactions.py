from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from starlette.requests import Request

from crows import app as app_module
from crows.messaging_dispatch import InboundDispatchStore
from crows.messaging_runtime import process_inbound_dispatch
from crows.messaging_store import MessagingStore
from crows.store import TaskStore


class FakeRouter:
    async def chat(self, message, **kwargs):
        assert "Discord" in message
        assert kwargs["history"] == []
        return {"answer": "Hello from Crows"}


class FakeResponse:
    status_code = 200
    is_success = True
    content = b'{"id":"discord-original-response"}'
    headers = {}

    @staticmethod
    def json():
        return {"id": "discord-original-response"}


class FakeClient:
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def patch(self, url, **kwargs):
        self.calls.append(("PATCH", url, kwargs))
        return FakeResponse()

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return FakeResponse()


@pytest.fixture
def discord_setup(monkeypatch, tmp_path: Path):
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ).hex()
    monkeypatch.setenv("CROWS_DISCORD_APPLICATION_ID", "123456789012345678")
    monkeypatch.setenv("CROWS_DISCORD_PUBLIC_KEY", public_key)
    monkeypatch.setenv("CROWS_MESSAGING_TARGET_KEY", Fernet.generate_key().decode())
    db_path = tmp_path / "discord.sqlite3"
    messages = MessagingStore(db_path)
    dispatches = InboundDispatchStore(db_path)
    sessions = TaskStore(db_path)
    monkeypatch.setattr(app_module, "messaging_store", messages)
    monkeypatch.setattr(app_module, "inbound_dispatch_store", dispatches)
    monkeypatch.setattr(app_module, "store", sessions)
    FakeClient.calls = []
    monkeypatch.setattr(app_module.httpx, "AsyncClient", FakeClient)
    return private_key, messages, dispatches, sessions


def make_request(payload, private_key, *, timestamp=None, signature_override=None):
    body = json.dumps(payload, separators=(",", ":")).encode()
    timestamp = str(int(time.time())) if timestamp is None else timestamp
    signature = (
        private_key.sign(timestamp.encode("ascii") + body).hex()
        if signature_override is None else signature_override
    )

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/messaging/webhooks/discord",
            "headers": [
                (b"x-signature-ed25519", signature.encode()),
                (b"x-signature-timestamp", timestamp.encode()),
                (b"content-length", str(len(body)).encode()),
            ],
            "query_string": b"",
            "server": ("testserver", 443),
            "client": ("127.0.0.1", 12345),
            "scheme": "https",
        },
        receive,
    )


def slash_payload():
    return {
        "type": 2,
        "application_id": "123456789012345678",
        "id": "223456789012345678",
        "token": "discord-interaction-token-0123456789",
        "channel_id": "323456789012345678",
        "member": {
            "user": {
                "id": "423456789012345678",
                "username": "crows-user",
                "global_name": "Crows User",
            }
        },
        "data": {
            "name": "crows",
            "options": [{"name": "message", "type": 3, "value": "Hi Crows"}],
        },
    }


@pytest.mark.asyncio
async def test_discord_interaction_signature_rejects_tampered_request(discord_setup):
    private_key, messages, _, _ = discord_setup
    with pytest.raises(HTTPException) as error:
        await app_module.discord_interactions(
            make_request(slash_payload(), private_key, signature_override="00" * 64)
        )
    assert error.value.status_code == 401
    assert messages.list_conversations() == []


@pytest.mark.asyncio
async def test_discord_ping_is_acknowledged_after_signature_verification(discord_setup):
    private_key, messages, _, _ = discord_setup
    response = await app_module.discord_interactions(
        make_request({"type": 1}, private_key)
    )
    assert response == {"type": 1}
    assert messages.list_conversations() == []


@pytest.mark.asyncio
async def test_discord_slash_command_queues_private_agent_reply(discord_setup):
    private_key, messages, dispatches, sessions = discord_setup
    response = await app_module.discord_interactions(
        make_request(slash_payload(), private_key)
    )
    assert response == {"type": 5, "data": {"flags": 64}}
    conversations = messages.list_conversations()
    assert len(conversations) == 1
    assert conversations[0]["channel"] == "discord"
    pending = dispatches.list_recent()
    assert len(pending) == 1 and pending[0]["state"] == "pending"

    claimed = dispatches.claim(worker_id="discord-test", limit=1)
    assert len(claimed) == 1
    outcome = await process_inbound_dispatch(
        claimed[0], worker_id="discord-test", dispatches=dispatches,
        messages=messages, sessions=sessions, router=FakeRouter(),
    )
    assert outcome == "completed"
    inbound = messages.get_message(claimed[0]["message_id"])
    outbound = messages.get_outbound("discord", f"inbound-reply:{claimed[0]['id']}")
    assert outbound["text"] == "Hello from Crows"
    assert outbound["conversation_id"] == inbound["conversation_id"]
    assert messages.get_delivery_target(outbound["id"]).startswith('{"kind":"discord_interaction"')


@pytest.mark.asyncio
async def test_discord_agent_reply_edits_deferred_interaction_response(discord_setup):
    private_key, messages, dispatches, sessions = discord_setup
    await app_module.discord_interactions(make_request(slash_payload(), private_key))
    claimed = dispatches.claim(worker_id="discord-test", limit=1)
    await process_inbound_dispatch(
        claimed[0], worker_id="discord-test", dispatches=dispatches,
        messages=messages, sessions=sessions, router=FakeRouter(),
    )

    result = await app_module.deliver_outbound_batch(worker_id="discord-outbox", limit=1)
    assert result["sent"] == 1
    assert FakeClient.calls[0][0] == "PATCH"
    assert FakeClient.calls[0][1].endswith("/messages/@original")
    assert FakeClient.calls[0][2]["json"] == {"content": "Hello from Crows"}


@pytest.mark.asyncio
async def test_discord_interaction_rejects_stale_timestamp(discord_setup):
    private_key, messages, _, _ = discord_setup
    with pytest.raises(HTTPException) as error:
        await app_module.discord_interactions(
            make_request(slash_payload(), private_key, timestamp="1")
        )
    assert error.value.status_code == 401
    assert messages.list_conversations() == []
