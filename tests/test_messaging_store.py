from pathlib import Path

import pytest

from crows.messaging_store import MessagingStore


def test_inbound_message_persists_and_deduplicates(tmp_path: Path):
    path = tmp_path / "inbox.sqlite3"
    store = MessagingStore(path)
    payload = dict(
        channel=" Telegram ", provider_message_id="msg-1", provider_user_id="user-secret",
        text="hello", received_at="2026-10-29T10:00:00+00:00", display_name="Alex",
    )
    first = store.ingest(**payload)
    second = store.ingest(**payload)
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert first["conversation_id"] == second["conversation_id"]
    assert first["message"]["status"] == "accepted"
    reopened = MessagingStore(path)
    assert len(reopened.list_messages(first["conversation_id"])) == 1
    assert reopened.list_conversations()[0]["last_message"] == "hello"
    assert "user-secret" not in str(reopened.list_conversations())


def test_channels_are_isolated_and_provider_ids_not_persisted(tmp_path: Path):
    store = MessagingStore(tmp_path / "inbox.sqlite3")
    a = store.ingest(channel="telegram", provider_message_id="1", provider_user_id="same-user", text="one")
    b = store.ingest(channel="discord", provider_message_id="1", provider_user_id="same-user", text="two")
    assert a["conversation_id"] != b["conversation_id"]
    with store.connect() as con:
        rows = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='messaging_conversations'").fetchone()
        assert "provider_user_id" not in rows["sql"]


def test_outbound_idempotency_and_conflict(tmp_path: Path):
    store = MessagingStore(tmp_path / "inbox.sqlite3")
    kwargs = dict(channel="telegram", provider_user_id="u1", text="reply", idempotency_key="send-1")
    first = store.queue_outbound(**kwargs)
    second = store.queue_outbound(**kwargs)
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    with pytest.raises(ValueError, match="different message"):
        store.queue_outbound(**{**kwargs, "text": "changed"})


@pytest.mark.parametrize("kwargs", [
    {"channel": "../telegram", "provider_message_id": "x", "provider_user_id": "u", "text": "hi"},
    {"channel": "telegram", "provider_message_id": "x", "provider_user_id": "u", "text": " "},
    {"channel": "telegram", "provider_message_id": "x", "provider_user_id": "", "text": "hi"},
])
def test_inbound_validation(tmp_path: Path, kwargs):
    with pytest.raises(ValueError):
        MessagingStore(tmp_path / "inbox.sqlite3").ingest(**kwargs)


def test_message_history_limit_is_bounded(tmp_path: Path):
    store = MessagingStore(tmp_path / "inbox.sqlite3")
    result = store.ingest(channel="telegram", provider_message_id="x", provider_user_id="u", text="hi")
    with pytest.raises(ValueError):
        store.list_messages(result["conversation_id"], limit=201)



def test_outbound_delivery_status_is_persisted(tmp_path: Path):
    store = MessagingStore(tmp_path / "inbox.sqlite3")
    result = store.queue_outbound(
        channel="telegram", provider_user_id="chat-1", text="hello", idempotency_key="send-1"
    )
    store.update_outbound_status(
        "telegram", "send-1", "sent", provider_message_id="telegram-message-9"
    )
    saved = store.get_outbound("telegram", "send-1")
    assert saved is not None
    assert saved["id"] == result["message"]["id"]
    assert saved["status"] == "sent"
    assert saved["provider_message_id"] == "telegram-message-9"


def test_outbound_delivery_status_rejects_invalid_value(tmp_path: Path):
    store = MessagingStore(tmp_path / "inbox.sqlite3")
    with pytest.raises(ValueError, match="invalid outbound delivery status"):
        store.update_outbound_status("telegram", "missing", "accepted")



def test_outbound_delivery_status_cannot_rewrite_terminal_state(tmp_path: Path):
    store = MessagingStore(tmp_path / "inbox.sqlite3")
    store.queue_outbound(
        channel="telegram", provider_user_id="chat-1", text="hello", idempotency_key="terminal-1"
    )
    store.update_outbound_status("telegram", "terminal-1", "sent")
    with pytest.raises(ValueError, match="only transition from accepted"):
        store.update_outbound_status("telegram", "terminal-1", "failed")
    saved = store.get_outbound("telegram", "terminal-1")
    assert saved["status"] == "sent"


def test_outbound_delivery_status_rejects_missing_message(tmp_path: Path):
    store = MessagingStore(tmp_path / "inbox.sqlite3")
    with pytest.raises(ValueError, match="outbound message not found"):
        store.update_outbound_status("telegram", "missing", "sent")


@pytest.mark.parametrize("provider_message_id", ["", "   ", 123, "x" * 257])
def test_outbound_delivery_status_rejects_invalid_provider_message_id(
    tmp_path: Path, provider_message_id
):
    store = MessagingStore(tmp_path / "inbox.sqlite3")
    store.queue_outbound(
        channel="telegram", provider_user_id="chat-1", text="hello", idempotency_key="provider-id-1"
    )
    with pytest.raises(ValueError, match="provider_message_id is invalid"):
        store.update_outbound_status(
            "telegram", "provider-id-1", "sent", provider_message_id=provider_message_id
        )


def test_outbound_delivery_status_normalizes_provider_message_id(tmp_path: Path):
    store = MessagingStore(tmp_path / "inbox.sqlite3")
    store.queue_outbound(
        channel="telegram", provider_user_id="chat-1", text="hello", idempotency_key="provider-id-2"
    )
    store.update_outbound_status(
        "telegram", "provider-id-2", "sent", provider_message_id="  telegram-9  "
    )
    assert store.get_outbound("telegram", "provider-id-2")["provider_message_id"] == "telegram-9"



def test_delivery_target_is_encrypted_and_decryptable(tmp_path: Path, monkeypatch):
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("AETHER_MESSAGING_TARGET_KEY", key)
    store = MessagingStore(tmp_path / "inbox.sqlite3")
    queued = store.queue_outbound(
        channel="telegram", provider_user_id="chat-private", text="hello",
        idempotency_key="target-1", delivery_target="123456789",
    )
    assert store.get_delivery_target(queued["message"]["id"]) == "123456789"
    with store.connect() as con:
        raw = con.execute(
            "SELECT ciphertext FROM messaging_delivery_targets WHERE message_id=?",
            (queued["message"]["id"],),
        ).fetchone()["ciphertext"]
    assert b"123456789" not in bytes(raw)
    assert "ciphertext" not in queued["message"]


def test_delivery_target_requires_encryption_key_and_is_not_queued(tmp_path: Path, monkeypatch):
    from crows.messaging import MessagingContractError

    monkeypatch.delenv("AETHER_MESSAGING_TARGET_KEY", raising=False)
    monkeypatch.delenv("CROWS_MESSAGING_TARGET_KEY", raising=False)
    store = MessagingStore(tmp_path / "inbox.sqlite3")
    with pytest.raises(MessagingContractError, match="TARGET_KEY is required"):
        store.queue_outbound(
            channel="telegram", provider_user_id="chat-private", text="hello",
            idempotency_key="target-2", delivery_target="123456789",
        )
    with store.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM messaging_messages").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM messaging_delivery_targets").fetchone()[0] == 0


def test_invalid_delivery_target_key_fails_closed(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AETHER_MESSAGING_TARGET_KEY", "not-a-fernet-key")
    with pytest.raises(ValueError, match="valid Fernet key"):
        MessagingStore(tmp_path / "inbox.sqlite3")



def test_outbox_claim_is_atomic_and_only_claims_target_backed_messages(tmp_path: Path, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("AETHER_MESSAGING_TARGET_KEY", Fernet.generate_key().decode())
    store = MessagingStore(tmp_path / "inbox.sqlite3")
    with_target = store.queue_outbound(
        channel="telegram", provider_user_id="u1", text="ready",
        idempotency_key="claim-1", delivery_target="123",
    )
    store.queue_outbound(
        channel="telegram", provider_user_id="u2", text="no target",
        idempotency_key="claim-2",
    )
    first = store.claim_outbound(worker_id="worker-a", limit=10, lease_seconds=30)
    second = store.claim_outbound(worker_id="worker-b", limit=10, lease_seconds=30)
    assert [item["id"] for item in first] == [with_target["message"]["id"]]
    assert second == []


def test_outbox_lease_requires_current_owner_and_finishes_once(tmp_path: Path, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("AETHER_MESSAGING_TARGET_KEY", Fernet.generate_key().decode())
    store = MessagingStore(tmp_path / "inbox.sqlite3")
    queued = store.queue_outbound(
        channel="telegram", provider_user_id="u1", text="ready",
        idempotency_key="claim-owner", delivery_target="123",
    )
    message_id = queued["message"]["id"]
    assert store.claim_outbound(worker_id="worker-a", lease_seconds=30)
    assert not store.finish_outbound_lease(message_id, worker_id="worker-b", status="sent")
    assert store.finish_outbound_lease(message_id, worker_id="worker-a", status="sent")
    assert not store.finish_outbound_lease(message_id, worker_id="worker-a", status="sent")
    assert store.get_outbound("telegram", "claim-owner")["status"] == "sent"


def test_expired_lease_becomes_unknown_not_automatically_requeued(tmp_path: Path, monkeypatch):
    from cryptography.fernet import Fernet
    from datetime import datetime, timedelta, timezone

    monkeypatch.setenv("AETHER_MESSAGING_TARGET_KEY", Fernet.generate_key().decode())
    store = MessagingStore(tmp_path / "inbox.sqlite3")
    queued = store.queue_outbound(
        channel="telegram", provider_user_id="u1", text="ready",
        idempotency_key="claim-expiry", delivery_target="123",
    )
    message_id = queued["message"]["id"]
    store.claim_outbound(worker_id="worker-a", lease_seconds=5)
    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    with store.connect() as con:
        con.execute("UPDATE messaging_delivery_leases SET lease_until=? WHERE message_id=?", (expired, message_id))
    assert store.expire_outbound_leases() == 1
    assert store.get_outbound("telegram", "claim-expiry")["status"] == "unknown"
    assert store.claim_outbound(worker_id="worker-b") == []



@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"delay_seconds": True}, "delay_seconds"),
        ({"delay_seconds": 1.5}, "delay_seconds"),
        ({"max_retries": True}, "max_retries"),
        ({"max_retries": 2.5}, "max_retries"),
    ],
)
def test_retry_rejects_boolean_and_non_integer_limits(tmp_path: Path, monkeypatch, kwargs, message):
    from cryptography.fernet import Fernet
    from crows.messaging import MessagingContractError

    monkeypatch.setenv("CROWS_MESSAGING_TARGET_KEY", Fernet.generate_key().decode())
    store = MessagingStore(tmp_path / "retry-validation.sqlite3")
    queued = store.queue_outbound(
        channel="telegram", provider_user_id="u", text="hello",
        idempotency_key="retry-validation", delivery_target="123",
    )
    store.claim_outbound(worker_id="worker")
    params = {"delay_seconds": 10, "max_retries": 2}
    params.update(kwargs)
    with pytest.raises(ValueError, match=message):
        store.retry_outbound_lease(queued["message"]["id"], worker_id="worker", **params)


def test_rate_limit_retry_is_delayed_and_bounded(tmp_path: Path, monkeypatch):
    from cryptography.fernet import Fernet
    from datetime import datetime, timedelta, timezone

    monkeypatch.setenv("AETHER_MESSAGING_TARGET_KEY", Fernet.generate_key().decode())
    store = MessagingStore(tmp_path / "retry.sqlite3")
    queued = store.queue_outbound(
        channel="telegram", provider_user_id="u", text="hello",
        idempotency_key="retry-1", delivery_target="123",
    )
    message_id = queued["message"]["id"]
    assert store.claim_outbound(worker_id="w1")
    assert store.retry_outbound_lease(
        message_id, worker_id="w1", delay_seconds=30, max_retries=2
    ) == "scheduled"
    assert store.claim_outbound(worker_id="w2") == []
    with store.connect() as con:
        con.execute(
            "UPDATE messaging_delivery_retries SET next_attempt_at=? WHERE message_id=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), message_id),
        )
    assert store.claim_outbound(worker_id="w2")
    assert store.retry_outbound_lease(
        message_id, worker_id="w2", delay_seconds=1, max_retries=2
    ) == "scheduled"
    with store.connect() as con:
        con.execute(
            "UPDATE messaging_delivery_retries SET next_attempt_at=? WHERE message_id=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), message_id),
        )
    assert store.claim_outbound(worker_id="w3")
    assert store.retry_outbound_lease(
        message_id, worker_id="w3", delay_seconds=1, max_retries=2
    ) == "exhausted"
    assert store.get_outbound("telegram", "retry-1")["status"] == "failed"


def test_unknown_delivery_reconciliation_requires_evidence_and_is_audited(tmp_path: Path):
    store = MessagingStore(tmp_path / "reconcile.sqlite3")
    queued = store.queue_outbound(
        channel="telegram", provider_user_id="u", text="hello",
        idempotency_key="unknown-1",
    )
    store.update_outbound_status("telegram", "unknown-1", "unknown")
    message_id = queued["message"]["id"]
    with pytest.raises(ValueError, match="evidence"):
        store.reconcile_unknown(message_id, status="sent", evidence=" ")
    result = store.reconcile_unknown(
        message_id, status="sent", evidence="Confirmed in provider conversation",
        provider_message_id="provider-123",
    )
    assert result["status"] == "sent"
    saved = store.get_outbound("telegram", "unknown-1")
    assert saved["status"] == "sent"
    assert saved["provider_message_id"] == "provider-123"
    with store.connect() as con:
        audit = con.execute(
            "SELECT previous_status,new_status,evidence FROM messaging_delivery_reconciliations WHERE message_id=?",
            (message_id,),
        ).fetchone()
    assert tuple(audit) == ("unknown", "sent", "Confirmed in provider conversation")
    with pytest.raises(ValueError, match="only unknown"):
        store.reconcile_unknown(message_id, status="failed", evidence="Checked again")



def test_delivery_metrics_include_queue_depth_and_retry_schedule(tmp_path: Path, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("AETHER_MESSAGING_TARGET_KEY", Fernet.generate_key().decode())
    store = MessagingStore(tmp_path / "metrics.sqlite3")
    store.queue_outbound(
        channel="telegram", provider_user_id="u1", text="pending",
        idempotency_key="metrics-pending", delivery_target="100",
    )
    claimed = store.queue_outbound(
        channel="telegram", provider_user_id="u2", text="leased",
        idempotency_key="metrics-leased", delivery_target="200",
    )
    store.claim_outbound(worker_id="metrics-worker", limit=1)
    metrics = store.delivery_metrics()
    assert metrics["queue"]["leased"] == 1
    assert metrics["queue"]["pending"] == 1
    assert metrics["queue"]["unknown"] == 0
    assert metrics["queue"]["scheduled_retries"] == 0
    assert "text" not in metrics["queue"]
    assert "message_id" not in metrics["queue"]


def test_queue_metrics_count_only_retries_ready_for_future_claim(tmp_path: Path, monkeypatch):
    from cryptography.fernet import Fernet
    from datetime import datetime, timedelta, timezone

    monkeypatch.setenv("AETHER_MESSAGING_TARGET_KEY", Fernet.generate_key().decode())
    store = MessagingStore(tmp_path / "retry-metrics.sqlite3")
    queued = store.queue_outbound(
        channel="telegram", provider_user_id="u", text="retry",
        idempotency_key="retry-metrics", delivery_target="123",
    )
    message_id = queued["message"]["id"]
    assert store.claim_outbound(worker_id="worker-1")
    assert store.retry_outbound_lease(
        message_id, worker_id="worker-1", delay_seconds=30, max_retries=2
    ) == "scheduled"
    metrics = store.delivery_metrics()
    assert metrics["queue"]["scheduled_retries"] == 1
    assert metrics["queue"]["pending"] == 1

    with store.connect() as con:
        con.execute(
            "UPDATE messaging_delivery_retries SET next_attempt_at=? WHERE message_id=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), message_id),
        )
    assert store.claim_outbound(worker_id="worker-2")
    metrics = store.delivery_metrics()
    assert metrics["queue"]["scheduled_retries"] == 0
    assert metrics["queue"]["leased"] == 1
    assert store.finish_outbound_lease(message_id, worker_id="worker-2", status="sent")
    assert store.delivery_metrics()["queue"]["scheduled_retries"] == 0



def test_delivery_target_uses_crows_environment_key(tmp_path: Path, monkeypatch):
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("CROWS_MESSAGING_TARGET_KEY", key)
    monkeypatch.delenv("AETHER_MESSAGING_TARGET_KEY", raising=False)
    store = MessagingStore(tmp_path / "crows-key.sqlite3")
    queued = store.queue_outbound(
        channel="telegram", provider_user_id="chat-crows", text="hello",
        idempotency_key="crows-key", delivery_target="987654321",
    )
    assert store.get_delivery_target(queued["message"]["id"]) == "987654321"


def test_retry_budget_cannot_be_increased_after_scheduling(tmp_path: Path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from cryptography.fernet import Fernet
    from crows.messaging import MessagingContractError

    monkeypatch.setenv("AETHER_MESSAGING_TARGET_KEY", Fernet.generate_key().decode())
    store = MessagingStore(tmp_path / "immutable-retry-budget.sqlite3")
    queued = store.queue_outbound(
        channel="telegram", provider_user_id="u", text="hello",
        idempotency_key="immutable-budget", delivery_target="123",
    )
    message_id = queued["message"]["id"]

    assert store.claim_outbound(worker_id="worker-1")
    assert store.retry_outbound_lease(
        message_id, worker_id="worker-1", delay_seconds=1, max_retries=1
    ) == "scheduled"

    with store.connect() as con:
        con.execute(
            "UPDATE messaging_delivery_retries SET next_attempt_at=? WHERE message_id=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), message_id),
        )

    assert store.claim_outbound(worker_id="worker-2")
    with pytest.raises(MessagingContractError, match="max_retries cannot change"):
        store.retry_outbound_lease(
            message_id, worker_id="worker-2", delay_seconds=1, max_retries=5
        )



def test_outbound_idempotency_rejects_different_delivery_target(
    tmp_path: Path, monkeypatch
):
    from cryptography.fernet import Fernet
    from crows.messaging import MessagingContractError

    monkeypatch.setenv("CROWS_MESSAGING_TARGET_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("AETHER_MESSAGING_TARGET_KEY", raising=False)
    store = MessagingStore(tmp_path / "target-idempotency.sqlite3")
    kwargs = dict(
        channel="telegram", provider_user_id="u1", text="reply",
        idempotency_key="target-conflict",
    )

    first = store.queue_outbound(**kwargs, delivery_target="target-A")
    duplicate = store.queue_outbound(**kwargs, delivery_target="target-A")

    assert first["duplicate"] is False
    assert duplicate["duplicate"] is True
    with pytest.raises(MessagingContractError, match="different message"):
        store.queue_outbound(**kwargs, delivery_target="target-B")


@pytest.mark.parametrize(
    ("first_target", "second_target"),
    [(None, "target-A"), ("target-A", None)],
)
def test_outbound_idempotency_rejects_target_presence_change(
    tmp_path: Path, monkeypatch, first_target, second_target
):
    from cryptography.fernet import Fernet
    from crows.messaging import MessagingContractError

    monkeypatch.setenv("CROWS_MESSAGING_TARGET_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("AETHER_MESSAGING_TARGET_KEY", raising=False)
    store = MessagingStore(tmp_path / "target-presence.sqlite3")
    kwargs = dict(
        channel="telegram", provider_user_id="u1", text="reply",
        idempotency_key="target-presence",
    )

    store.queue_outbound(**kwargs, delivery_target=first_target)
    with pytest.raises(MessagingContractError, match="different message"):
        store.queue_outbound(**kwargs, delivery_target=second_target)


def test_outbound_idempotency_fails_closed_for_corrupt_target(
    tmp_path: Path, monkeypatch
):
    from cryptography.fernet import Fernet
    from crows.messaging import MessagingContractError

    monkeypatch.setenv("CROWS_MESSAGING_TARGET_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("AETHER_MESSAGING_TARGET_KEY", raising=False)
    store = MessagingStore(tmp_path / "corrupt-target.sqlite3")
    kwargs = dict(
        channel="telegram", provider_user_id="u1", text="reply",
        idempotency_key="corrupt-target",
    )

    queued = store.queue_outbound(**kwargs, delivery_target="target-A")
    with store.connect() as con:
        con.execute(
            "UPDATE messaging_delivery_targets SET ciphertext=? WHERE message_id=?",
            (b"invalid-ciphertext", queued["message"]["id"]),
        )

    with pytest.raises(MessagingContractError, match="cannot be verified"):
        store.queue_outbound(**kwargs, delivery_target="target-A")

def test_finish_outbound_lease_rejects_duplicate_provider_message_id_atomically(
    tmp_path: Path, monkeypatch
):
    from cryptography.fernet import Fernet
    from crows.messaging import MessagingContractError

    monkeypatch.setenv("CROWS_MESSAGING_TARGET_KEY", Fernet.generate_key().decode())
    store = MessagingStore(tmp_path / "duplicate-finish-provider-id.sqlite3")
    store.queue_outbound(
        channel="telegram", provider_user_id="u1", text="first",
        idempotency_key="first", delivery_target="100",
    )
    second = store.queue_outbound(
        channel="telegram", provider_user_id="u2", text="second",
        idempotency_key="second", delivery_target="200",
    )
    store.update_outbound_status(
        "telegram", "first", "sent", provider_message_id="provider-duplicate"
    )
    message_id = second["message"]["id"]
    assert store.claim_outbound(worker_id="worker", message_id=message_id)

    with pytest.raises(MessagingContractError, match="already recorded"):
        store.finish_outbound_lease(
            message_id, worker_id="worker", status="sent",
            provider_message_id="provider-duplicate",
        )

    assert store.get_outbound("telegram", "second")["status"] == "accepted"
    assert store.finish_outbound_lease(message_id, worker_id="worker", status="unknown")


def test_reconcile_unknown_rejects_duplicate_provider_message_id_atomically(
    tmp_path: Path
):
    from crows.messaging import MessagingContractError

    store = MessagingStore(tmp_path / "duplicate-reconcile-provider-id.sqlite3")
    store.queue_outbound(
        channel="telegram", provider_user_id="u1", text="first",
        idempotency_key="first",
    )
    second = store.queue_outbound(
        channel="telegram", provider_user_id="u2", text="second",
        idempotency_key="second",
    )
    store.update_outbound_status(
        "telegram", "first", "sent", provider_message_id="provider-duplicate"
    )
    store.update_outbound_status("telegram", "second", "unknown")

    with pytest.raises(MessagingContractError, match="already recorded"):
        store.reconcile_unknown(
            second["message"]["id"], status="sent",
            evidence="Provider lookup returned a conflicting message ID",
            provider_message_id="provider-duplicate",
        )

    assert store.get_outbound("telegram", "second")["status"] == "unknown"
    with store.connect() as con:
        assert con.execute(
            "SELECT COUNT(*) FROM messaging_delivery_reconciliations WHERE message_id=?",
            (second["message"]["id"],),
        ).fetchone()[0] == 0
