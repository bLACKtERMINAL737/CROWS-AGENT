from pathlib import Path

import pytest

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
