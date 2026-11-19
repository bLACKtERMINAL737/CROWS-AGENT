import asyncio
import importlib

import pytest

from crows.messaging_adapters import OutboundTransportMessage


@pytest.mark.asyncio
async def test_outbound_send_renews_lease_until_provider_finishes(monkeypatch):
    app_module = importlib.import_module("crows.app")
    renewals = []

    class Store:
        def renew_outbound_lease(self, message_id, *, worker_id, lease_seconds):
            renewals.append((message_id, worker_id, lease_seconds))
            return True

    class Adapter:
        async def send(self, message):
            await asyncio.sleep(1.2)
            return "receipt"

    monkeypatch.setattr(app_module, "messaging_store", Store())
    message = OutboundTransportMessage(
        channel="telegram", delivery_target="target", text="hello",
        idempotency_key="heartbeat-test",
    )
    result = await app_module._send_outbound_with_lease_renewal(
        Adapter(), message, message_id="message-1", worker_id="worker-1",
        lease_seconds=5,
    )

    assert result == "receipt"
    assert renewals
    assert all(row == ("message-1", "worker-1", 5) for row in renewals)


@pytest.mark.asyncio
async def test_outbound_send_is_cancelled_when_lease_ownership_is_lost(monkeypatch):
    app_module = importlib.import_module("crows.app")
    cancelled = asyncio.Event()

    class Store:
        def renew_outbound_lease(self, message_id, *, worker_id, lease_seconds):
            return False

    class Adapter:
        async def send(self, message):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    monkeypatch.setattr(app_module, "messaging_store", Store())
    message = OutboundTransportMessage(
        channel="telegram", delivery_target="target", text="hello",
        idempotency_key="heartbeat-loss-test",
    )

    with pytest.raises(RuntimeError, match="lease ownership was lost"):
        await app_module._send_outbound_with_lease_renewal(
            Adapter(), message, message_id="message-2", worker_id="worker-1",
            lease_seconds=5,
        )

    assert cancelled.is_set()
