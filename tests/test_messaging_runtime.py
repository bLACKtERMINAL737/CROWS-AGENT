from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from crows.messaging_dispatch import InboundDispatchStore
from crows.messaging_gateway import InboundChannelPolicy, build_default_inbound_channels
from crows.messaging_runtime import process_inbound_dispatch
from crows.messaging_store import MessagingStore
from crows.store import TaskStore


class FakeRouter:
    def __init__(self, answer="Hello from Crows"):
        self.answer = answer
        self.calls = []

    async def chat(self, message, **kwargs):
        self.calls.append((message, kwargs))
        return {"answer": self.answer, "route": "primary", "model": "fake-model"}


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CROWS_MESSAGING_TARGET_KEY", Fernet.generate_key().decode())
    db_path = tmp_path / "runtime.sqlite3"
    messages = MessagingStore(db_path)
    dispatches = InboundDispatchStore(db_path)
    sessions = TaskStore(db_path)
    router = FakeRouter()
    return messages, dispatches, sessions, router


def enqueue_telegram(runtime, *, provider_message_id="telegram-1", text="hi", chat_id="777"):
    messages, dispatches, sessions, _ = runtime
    ingested = messages.ingest(
        channel="telegram",
        provider_message_id=provider_message_id,
        provider_user_id=chat_id,
        text=text,
        delivery_target=chat_id,
    )
    session = sessions.get_or_create_messaging_session(
        ingested["conversation_id"], "telegram"
    )
    queued = dispatches.enqueue(
        message_id=ingested["message"]["id"],
        conversation_id=ingested["conversation_id"],
        channel="telegram",
        session_id=session["id"],
    )
    return ingested, session, queued


@pytest.mark.asyncio
async def test_inbound_telegram_generates_session_reply_and_enqueues_encrypted_outbox(runtime):
    messages, dispatches, sessions, router = runtime
    ingested, session, queued = enqueue_telegram(runtime)
    claimed = dispatches.claim(worker_id="worker-1", limit=1)
    assert len(claimed) == 1

    result = await process_inbound_dispatch(
        claimed[0], worker_id="worker-1", dispatches=dispatches,
        messages=messages, sessions=sessions, router=router,
        channels=build_default_inbound_channels(),
    )

    assert result == "completed"
    saved = dispatches.get(queued["id"])
    assert saved["state"] == "completed"
    history = sessions.list_session_messages(session["id"])
    assert [(item["role"], item["content"]) for item in history] == [
        ("user", "hi"), ("assistant", "Hello from Crows")
    ]
    outbound = messages.get_outbound(
        "telegram", f"inbound-reply:{queued['id']}"
    )
    assert outbound["status"] == "accepted"
    assert outbound["text"] == "Hello from Crows"
    assert messages.get_delivery_target(outbound["id"]) == "777"
    prompt, kwargs = router.calls[0]
    assert "untrusted" in prompt.lower()
    assert "no tools" in prompt.lower()
    assert kwargs["history"] == []


@pytest.mark.asyncio
async def test_inbound_dispatch_does_not_replay_completed_message(runtime):
    messages, dispatches, sessions, router = runtime
    _, _, queued = enqueue_telegram(runtime)
    claimed = dispatches.claim(worker_id="worker-1", limit=1)
    await process_inbound_dispatch(
        claimed[0], worker_id="worker-1", dispatches=dispatches,
        messages=messages, sessions=sessions, router=router,
    )
    assert dispatches.claim(worker_id="worker-2", limit=10) == []
    assert len(router.calls) == 1


def test_dispatch_claims_only_oldest_message_per_conversation(runtime):
    messages, dispatches, sessions, _ = runtime
    first, session, first_dispatch = enqueue_telegram(
        runtime, provider_message_id="telegram-1", text="first"
    )
    second, _, second_dispatch = enqueue_telegram(
        runtime, provider_message_id="telegram-2", text="second"
    )

    claimed = dispatches.claim(worker_id="worker-1", limit=10)
    assert [item["id"] for item in claimed] == [first_dispatch["id"]]
    assert dispatches.finish(
        first_dispatch["id"], worker_id="worker-1", state="completed"
    )
    next_claim = dispatches.claim(worker_id="worker-2", limit=10)
    assert [item["id"] for item in next_claim] == [second_dispatch["id"]]


def test_inbound_destination_requires_encryption_key(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("CROWS_MESSAGING_TARGET_KEY", raising=False)
    monkeypatch.delenv("AETHER_MESSAGING_TARGET_KEY", raising=False)
    messages = MessagingStore(tmp_path / "no-key.sqlite3")
    with pytest.raises(ValueError, match="CROWS_MESSAGING_TARGET_KEY"):
        messages.ingest(
            channel="telegram", provider_message_id="one",
            provider_user_id="777", text="hello", delivery_target="777",
        )


@pytest.mark.asyncio
async def test_unconfigured_channel_fails_closed_without_model_call(runtime):
    messages, dispatches, sessions, router = runtime
    ingested = messages.ingest(
        channel="whatsapp", provider_message_id="whatsapp-1",
        provider_user_id="user-1", text="hello",
    )
    session = sessions.get_or_create_messaging_session(
        ingested["conversation_id"], "whatsapp"
    )
    queued = dispatches.enqueue(
        message_id=ingested["message"]["id"],
        conversation_id=ingested["conversation_id"],
        channel="whatsapp", session_id=session["id"],
    )
    claimed = dispatches.claim(worker_id="worker-1", limit=1)
    result = await process_inbound_dispatch(
        claimed[0], worker_id="worker-1", dispatches=dispatches,
        messages=messages, sessions=sessions, router=router,
    )
    assert result == "failed"
    assert dispatches.get(queued["id"])["state"] == "failed"
    assert router.calls == []



@pytest.mark.asyncio
async def test_shared_runtime_accepts_explicit_channel_policy_and_applies_reply_limit(runtime):
    messages, dispatches, sessions, router = runtime
    ingested = messages.ingest(
        channel="matrix", provider_message_id="matrix-1",
        provider_user_id="room-42", text="hello", delivery_target="room-42",
    )
    session = sessions.get_or_create_messaging_session(
        ingested["conversation_id"], "matrix"
    )
    queued = dispatches.enqueue(
        message_id=ingested["message"]["id"],
        conversation_id=ingested["conversation_id"],
        channel="matrix",
        session_id=session["id"],
    )
    claimed = dispatches.claim(worker_id="worker-matrix", limit=1)
    channels = build_default_inbound_channels()
    channels.register(InboundChannelPolicy("matrix", "Matrix", 5))

    result = await process_inbound_dispatch(
        claimed[0], worker_id="worker-matrix", dispatches=dispatches,
        messages=messages, sessions=sessions, router=router, channels=channels,
    )

    assert result == "completed"
    outbound = messages.get_outbound("matrix", f"inbound-reply:{queued['id']}")
    assert outbound["status"] == "accepted"
    assert outbound["text"] == "Hello"
    assert "Matrix" in router.calls[0][0]
