import asyncio

import pytest

from crows import app as app_module


@pytest.mark.asyncio
async def test_messaging_worker_continues_after_iteration_failure(monkeypatch):
    expire_calls = 0
    delivery_calls = 0
    sleep_calls = 0

    def expire_leases():
        nonlocal expire_calls
        expire_calls += 1
        if expire_calls == 1:
            raise RuntimeError("temporary lease-store failure")
        return 0

    async def deliver_batch(*, worker_id, limit):
        nonlocal delivery_calls
        delivery_calls += 1
        return {
            "claimed": 0,
            "sent": 0,
            "retried": 0,
            "failed": 0,
            "unknown": 0,
            "skipped": 0,
        }

    async def stop_after_second_iteration(_delay):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 2:
            raise asyncio.CancelledError

    monkeypatch.setenv("AETHER_MESSAGING_WORKER_INTERVAL_SECONDS", "1")
    monkeypatch.setattr(app_module.messaging_store, "expire_outbound_leases", expire_leases)
    monkeypatch.setattr(app_module, "deliver_outbound_batch", deliver_batch)
    monkeypatch.setattr(app_module.asyncio, "sleep", stop_after_second_iteration)

    with pytest.raises(asyncio.CancelledError):
        await app_module.messaging_worker_loop()

    assert expire_calls == 2
    assert delivery_calls == 1
