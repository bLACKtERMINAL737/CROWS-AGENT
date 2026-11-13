from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from crows import app as app_module
from crows.messaging_store import MessagingStore


def test_attention_list_contains_only_failed_and_unknown_metadata(tmp_path: Path):
    store = MessagingStore(tmp_path / "messaging.sqlite3")
    failed = store.queue_outbound(
        channel="telegram", provider_user_id="private-recipient",
        text="private message body", idempotency_key="private-idempotency",
    )
    unknown = store.queue_outbound(
        channel="discord", provider_user_id="another-recipient",
        text="another private body", idempotency_key="another-private-key",
    )
    pending = store.queue_outbound(
        channel="telegram", provider_user_id="third-recipient",
        text="still pending", idempotency_key="pending-key",
    )
    store.update_outbound_status(
        "telegram", failed["message"]["idempotency_key"], "failed"
    )
    store.update_outbound_status(
        "discord", unknown["message"]["idempotency_key"], "unknown"
    )

    records = store.list_outbound_attention()

    assert {record["message_id"] for record in records} == {
        failed["message"]["id"], unknown["message"]["id"]
    }
    assert {record["status"] for record in records} == {"failed", "unknown"}
    assert all(record["retry_count"] == 0 for record in records)
    assert all(record["max_retries"] == 0 for record in records)
    serialized = str(records)
    for private_value in (
        "private-recipient", "another-recipient", "private message body",
        "another private body", "private-idempotency", "another-private-key",
        "still pending", pending["message"]["id"],
    ):
        assert private_value not in serialized


@pytest.mark.parametrize("limit", [0, 101, True, 1.5])
def test_attention_list_rejects_invalid_limits(tmp_path: Path, limit):
    store = MessagingStore(tmp_path / "messaging.sqlite3")
    with pytest.raises(ValueError, match="limit must be"):
        store.list_outbound_attention(limit=limit)


def test_attention_endpoint_is_bounded_and_returns_sanitized_records(
    monkeypatch, tmp_path: Path
):
    store = MessagingStore(tmp_path / "messaging.sqlite3")
    queued = store.queue_outbound(
        channel="telegram", provider_user_id="private-recipient",
        text="private message", idempotency_key="private-key",
    )
    store.update_outbound_status(
        "telegram", queued["message"]["idempotency_key"], "unknown"
    )
    monkeypatch.setattr(app_module, "messaging_store", store)

    result = app_module.messaging_outbox_attention(limit=10)

    assert len(result["messages"]) == 1
    assert result["messages"][0]["message_id"] == queued["message"]["id"]
    assert result["messages"][0]["status"] == "unknown"
    assert "private message" not in str(result)
    assert "private-recipient" not in str(result)
    with pytest.raises(Exception):
        app_module.messaging_outbox_attention(limit=101)


def test_attention_includes_safe_failure_code_without_private_payload(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CROWS_MESSAGING_TARGET_KEY", Fernet.generate_key().decode())
    store = MessagingStore(tmp_path / "failure-code.sqlite3")
    queued = store.queue_outbound(
        channel="telegram", provider_user_id="secret-recipient",
        text="secret message body", idempotency_key="secret-key",
        delivery_target="secret-recipient",
    )
    message_id = queued["message"]["id"]
    assert store.claim_outbound(worker_id="worker-1", message_id=message_id)
    assert store.finish_outbound_lease(
        message_id, worker_id="worker-1", status="failed", failure_code="provider_http_403"
    )
    record = store.list_outbound_attention()[0]
    assert record["failure_code"] == "provider_http_403"
    assert "secret-recipient" not in str(record)
    assert "secret message body" not in str(record)
    assert "secret-key" not in str(record)


def test_finish_outbound_lease_rejects_unsafe_failure_code(tmp_path: Path):
    from crows.messaging import MessagingContractError
    store = MessagingStore(tmp_path / "invalid-failure-code.sqlite3")
    queued = store.queue_outbound(channel="telegram", provider_user_id="u", text="hi", idempotency_key="k")
    message_id = queued["message"]["id"]
    store.claim_outbound(worker_id="worker-1", message_id=message_id)
    with pytest.raises(MessagingContractError, match="safe lowercase"):
        store.finish_outbound_lease(message_id, worker_id="worker-1", status="failed", failure_code="Bearer SECRET")
