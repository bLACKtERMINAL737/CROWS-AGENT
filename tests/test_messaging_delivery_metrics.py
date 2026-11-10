from pathlib import Path

from crows.messaging_store import MessagingStore


def test_delivery_metrics_aggregate_status_and_channel_without_message_content(tmp_path: Path):
    store = MessagingStore(tmp_path / "messaging.sqlite3")
    store.queue_outbound(
        channel="telegram", provider_user_id="chat-1",
        text="private telegram text", idempotency_key="tg-1",
    )
    store.update_outbound_status("telegram", "tg-1", "sent", provider_message_id="tg-msg-1")
    store.queue_outbound(
        channel="discord", provider_user_id="fixed-webhook",
        text="private discord text", idempotency_key="dc-1",
    )
    store.update_outbound_status("discord", "dc-1", "unknown")
    store.queue_outbound(
        channel="discord", provider_user_id="fixed-webhook",
        text="another private message", idempotency_key="dc-2",
    )
    store.update_outbound_status("discord", "dc-2", "failed")

    metrics = store.delivery_metrics()
    assert metrics["total_outbound"] == 3
    assert metrics["by_status"] == {
        "accepted": 0, "sent": 1, "delivered": 0, "failed": 1, "unknown": 1
    }
    assert metrics["by_channel"]["telegram"]["sent"] == 1
    assert metrics["by_channel"]["discord"]["unknown"] == 1
    assert metrics["by_channel"]["discord"]["failed"] == 1
    assert len(metrics["recent_failures"]) == 2
    assert "private telegram text" not in str(metrics)
    assert "private discord text" not in str(metrics)


def test_delivery_metrics_returns_zero_counts_for_empty_store(tmp_path: Path):
    store = MessagingStore(tmp_path / "messaging.sqlite3")
    metrics = store.delivery_metrics()
    assert metrics["total_outbound"] == 0
    assert all(value == 0 for value in metrics["by_status"].values())
    assert metrics["by_channel"] == {}
    assert metrics["recent_failures"] == []
