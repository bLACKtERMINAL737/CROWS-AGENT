from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from crows import app as app_module
from crows.messaging_adapters import (
    OutboundTransportReceipt,
    OutboundTransportRegistry,
)
from crows.messaging_store import MessagingStore


@pytest.mark.asyncio
async def test_unexpected_adapter_failure_does_not_abort_remaining_batch(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("CROWS_MESSAGING_TARGET_KEY", Fernet.generate_key().decode())
    store = MessagingStore(tmp_path / "outbox.sqlite3")
    store.queue_outbound(
        channel="telegram", provider_user_id="recipient-a", text="message-a",
        idempotency_key="batch-a", delivery_target="target-a",
    )
    store.queue_outbound(
        channel="discord", provider_user_id="recipient-b", text="message-b",
        idempotency_key="batch-b", delivery_target="target-b",
    )

    class Adapter:
        def __init__(self, channel, should_fail=False):
            self.channel_name = channel
            self.should_fail = should_fail
            self.calls = 0

        async def send(self, message):
            self.calls += 1
            if self.should_fail:
                raise RuntimeError("private provider diagnostic must not be stored")
            return OutboundTransportReceipt(True, 200, f"{self.channel_name}-message")

    telegram = Adapter("telegram", should_fail=True)
    discord = Adapter("discord")
    registry = OutboundTransportRegistry()
    registry.register(telegram)
    registry.register(discord)
    monkeypatch.setattr(app_module, "messaging_store", store)
    monkeypatch.setattr(app_module, "outbound_adapter_registry", registry)

    result = await app_module.deliver_outbound_batch(worker_id="batch-worker", limit=10)

    assert result == {
        "claimed": 2, "sent": 1, "retried": 0, "failed": 0, "unknown": 1, "skipped": 0,
    }
    assert telegram.calls == 1
    assert discord.calls == 1
    failed = store.get_outbound("telegram", "batch-a")
    sent = store.get_outbound("discord", "batch-b")
    assert failed["status"] == "unknown"
    assert failed["failure_code"] == "transport_unexpected_error"
    assert sent["status"] == "sent"
    assert "private provider diagnostic" not in str(store.list_outbound_attention())
    assert store.claim_outbound(worker_id="second-worker") == []
