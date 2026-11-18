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


@pytest.mark.asyncio
async def test_checkpointed_reply_recovers_after_outbox_failure_without_second_model_call(runtime, monkeypatch):
    messages, dispatches, sessions, router = runtime
    _, session, queued = enqueue_telegram(runtime)
    claimed = dispatches.claim(worker_id="worker-1", limit=1)[0]
    original_queue, calls = messages.queue_outbound, 0
    def fail_once(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated outbox write interruption")
        return original_queue(**kwargs)
    monkeypatch.setattr(messages, "queue_outbound", fail_once)
    first = await process_inbound_dispatch(claimed, worker_id="worker-1", dispatches=dispatches, messages=messages, sessions=sessions, router=router)
    assert first == "pending"
    assert dispatches.get(queued["id"])["reply_text"] == "Hello from Crows"
    reclaimed = dispatches.claim(worker_id="worker-2", limit=1)[0]
    second = await process_inbound_dispatch(reclaimed, worker_id="worker-2", dispatches=dispatches, messages=messages, sessions=sessions, router=router)
    assert second == "completed" and len(router.calls) == 1
    assert [(r["role"], r["content"]) for r in sessions.list_session_messages(session["id"])] == [("user", "hi"), ("assistant", "Hello from Crows")]
    assert messages.get_outbound("telegram", f"inbound-reply:{queued['id']}")["text"] == "Hello from Crows"


def test_add_session_turn_once_is_idempotent(runtime):
    _, _, sessions, _ = runtime
    session = sessions.create_session(title="New conversation")
    first = sessions.add_session_turn_once("dispatch-123", session["id"], "hello", "hi")
    second = sessions.add_session_turn_once("dispatch-123", session["id"], "hello", "hi")
    assert [r["id"] for r in first] == [r["id"] for r in second]
    assert len(sessions.list_session_messages(session["id"])) == 2



@pytest.mark.asyncio
async def test_expired_reply_checkpoint_recovers_without_second_model_call(runtime):
    messages, dispatches, sessions, router = runtime
    _, session, queued = enqueue_telegram(runtime, text="please help")
    first_claim = dispatches.claim(worker_id="worker-crashed", limit=1)[0]

    result = await process_inbound_dispatch(
        first_claim,
        worker_id="worker-crashed",
        dispatches=dispatches,
        messages=messages,
        sessions=sessions,
        router=router,
    )
    assert result == "completed"
    assert len(router.calls) == 1

    # Simulate a crash after a reply has been checkpointed and outbound delivery
    # has been queued, but before the dispatch's completion state was persisted.
    with dispatches.connect() as con:
        con.execute(
            """UPDATE messaging_inbound_dispatch
               SET state='processing', lease_owner='worker-crashed',
                   lease_until='2000-01-01T00:00:00+00:00'
               WHERE id=?""",
            (queued["id"],),
        )

    assert dispatches.expire_leases() == 1
    recovered = dispatches.get(queued["id"])
    assert recovered["state"] == "pending"
    assert recovered["reply_text"] == "Hello from Crows"

    second_claim = dispatches.claim(worker_id="worker-recovered", limit=1)[0]
    result = await process_inbound_dispatch(
        second_claim,
        worker_id="worker-recovered",
        dispatches=dispatches,
        messages=messages,
        sessions=sessions,
        router=router,
    )

    assert result == "completed"
    assert len(router.calls) == 1
    assert dispatches.get(queued["id"])["state"] == "completed"
    outbound = messages.get_outbound(
        "telegram", f"inbound-reply:{queued['id']}"
    )
    assert outbound["status"] == "accepted"
    assert outbound["text"] == "Hello from Crows"
    assert [
        (row["role"], row["content"])
        for row in sessions.list_session_messages(session["id"])
    ] == [
        ("user", "please help"),
        ("assistant", "Hello from Crows"),
    ]


@pytest.mark.asyncio
async def test_outbox_queue_idempotency_survives_dispatch_recovery(runtime, monkeypatch):
    messages, dispatches, sessions, router = runtime
    _, _, queued = enqueue_telegram(runtime, provider_message_id="recovery-duplicate")
    claim = dispatches.claim(worker_id="worker-1", limit=1)[0]
    original_queue = messages.queue_outbound
    attempts = 0

    def queue_then_raise(**kwargs):
        nonlocal attempts
        attempts += 1
        result = original_queue(**kwargs)
        if attempts == 1:
            raise RuntimeError("simulated interruption after durable outbox insert")
        return result

    monkeypatch.setattr(messages, "queue_outbound", queue_then_raise)
    first = await process_inbound_dispatch(
        claim, worker_id="worker-1", dispatches=dispatches,
        messages=messages, sessions=sessions, router=router,
    )
    assert first == "pending"
    assert dispatches.get(queued["id"])["reply_text"] == "Hello from Crows"

    recovered_claim = dispatches.claim(worker_id="worker-2", limit=1)[0]
    second = await process_inbound_dispatch(
        recovered_claim, worker_id="worker-2", dispatches=dispatches,
        messages=messages, sessions=sessions, router=router,
    )

    assert second == "completed"
    assert len(router.calls) == 1
    with messages.connect() as con:
        rows = con.execute(
            """SELECT idempotency_key,COUNT(*) AS count
               FROM messaging_messages
               WHERE direction='outbound' AND idempotency_key=?
               GROUP BY idempotency_key""",
            (f"inbound-reply:{queued['id']}",),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["count"] == 1


@pytest.mark.asyncio
async def test_lease_loss_after_model_response_does_not_queue_reply(runtime, monkeypatch):
    messages, dispatches, sessions, router = runtime
    _, _, queued = enqueue_telegram(runtime, provider_message_id="lease-lost")
    claim = dispatches.claim(worker_id="worker-1", limit=1)[0]
    original_save = dispatches.save_reply

    def lose_lease_before_checkpoint(dispatch_id, *, worker_id, reply_text):
        with dispatches.connect() as con:
            con.execute(
                """UPDATE messaging_inbound_dispatch
                   SET lease_until='2000-01-01T00:00:00+00:00'
                   WHERE id=?""",
                (dispatch_id,),
            )
        return original_save(
            dispatch_id, worker_id=worker_id, reply_text=reply_text
        )

    monkeypatch.setattr(dispatches, "save_reply", lose_lease_before_checkpoint)
    result = await process_inbound_dispatch(
        claim, worker_id="worker-1", dispatches=dispatches,
        messages=messages, sessions=sessions, router=router,
    )

    assert result == "unknown"
    assert len(router.calls) == 1
    assert dispatches.get(queued["id"])["state"] == "processing"
    assert messages.get_outbound(
        "telegram", f"inbound-reply:{queued['id']}"
    ) is None
