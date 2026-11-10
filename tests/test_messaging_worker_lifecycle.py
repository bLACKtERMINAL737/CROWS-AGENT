import asyncio
from contextlib import nullcontext

import pytest

from crows import app as app_module


@pytest.mark.asyncio
async def test_lifespan_starts_and_stops_opt_in_messaging_worker(monkeypatch):
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def worker():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setenv("AETHER_MESSAGING_WORKER_ENABLED", "true")
    monkeypatch.setattr(app_module, "messaging_worker_loop", worker)
    monkeypatch.setattr(app_module.store, "runtime_process_lock", lambda blocking=False: nullcontext(True))
    monkeypatch.setattr(app_module.store, "mark_interrupted_as_unknown", lambda: 0)
    monkeypatch.setattr(app_module.store, "coding_apply_lock", lambda blocking=False: nullcontext(True))

    async with app_module.lifespan(app_module.app):
        await asyncio.wait_for(started.wait(), timeout=1)

    assert stopped.is_set()


@pytest.mark.asyncio
async def test_lifespan_does_not_start_messaging_worker_by_default(monkeypatch):
    started = False

    async def worker():
        nonlocal started
        started = True

    monkeypatch.delenv("AETHER_MESSAGING_WORKER_ENABLED", raising=False)
    monkeypatch.setattr(app_module, "messaging_worker_loop", worker)
    monkeypatch.setattr(app_module.store, "runtime_process_lock", lambda blocking=False: nullcontext(True))
    monkeypatch.setattr(app_module.store, "mark_interrupted_as_unknown", lambda: 0)
    monkeypatch.setattr(app_module.store, "coding_apply_lock", lambda blocking=False: nullcontext(True))

    async with app_module.lifespan(app_module.app):
        await asyncio.sleep(0)

    assert started is False
