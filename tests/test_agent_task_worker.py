import asyncio

import pytest

from crows import app as app_module
from crows.store import TaskStore


@pytest.mark.asyncio
async def test_durable_agent_worker_claims_and_completes_queued_task(monkeypatch, tmp_path):
    store = TaskStore(tmp_path / "agent-worker.sqlite3")
    task = store.create_task("[research] Durable queue item", "Run once")
    completed = asyncio.Event()
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "agent_global_max_concurrency", lambda: 1)
    monkeypatch.setattr(
        app_module,
        "get_env",
        lambda name, default=None: {
            "CROWS_AGENT_WORKER_INTERVAL_SECONDS": "0.25",
            "CROWS_AGENT_WORKER_CONCURRENCY": "1",
        }.get(name, default),
    )

    async def fake_runner(task_id, *, worker_id):
        assert store.owns_task_lease(task_id, worker_id)
        assert store.finish_task_lease(
            task_id, worker_id, "succeeded", result={"answer": "processed"}
        )
        completed.set()

    monkeypatch.setattr(app_module, "_run_agent_task_unlimited", fake_runner)
    worker = asyncio.create_task(app_module.agent_task_worker_loop())
    try:
        await asyncio.wait_for(completed.wait(), timeout=2)
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

    saved = store.get_task(task["id"])
    assert saved["status"] == "succeeded"
    assert saved["lease_owner"] is None
    assert saved["attempt_count"] == 1


@pytest.mark.asyncio
async def test_durable_agent_worker_marks_expired_lease_unknown_without_replay(
    monkeypatch, tmp_path
):
    store = TaskStore(tmp_path / "agent-worker-expired.sqlite3")
    task = store.create_task("[research] Uncertain queue item", "May have executed")
    claimed = store.claim_next_task("crashed-worker", lease_seconds=60)
    assert claimed["id"] == task["id"]
    with store.connect() as con:
        con.execute(
            "UPDATE tasks SET lease_expires_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", task["id"]),
        )
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "agent_global_max_concurrency", lambda: 1)
    monkeypatch.setattr(
        app_module,
        "get_env",
        lambda name, default=None: {
            "CROWS_AGENT_WORKER_INTERVAL_SECONDS": "0.25",
            "CROWS_AGENT_WORKER_CONCURRENCY": "1",
        }.get(name, default),
    )
    executed = False

    async def unexpected_runner(task_id, *, worker_id):
        nonlocal executed
        executed = True

    monkeypatch.setattr(app_module, "_run_agent_task_unlimited", unexpected_runner)
    worker = asyncio.create_task(app_module.agent_task_worker_loop())
    try:
        for _ in range(20):
            if store.get_task(task["id"])["status"] == "unknown":
                break
            await asyncio.sleep(0.01)
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

    assert store.get_task(task["id"])["status"] == "unknown"
    assert executed is False


@pytest.mark.asyncio
async def test_worker_cancellation_marks_live_leased_task_unknown(monkeypatch, tmp_path):
    store = TaskStore(tmp_path / "agent-worker-cancel.sqlite3")
    task = store.create_task("[research] Cancelled queue item", "Do not replay")
    claimed = store.claim_next_task("worker-cancel", lease_seconds=60)
    assert claimed["id"] == task["id"]
    monkeypatch.setattr(app_module, "store", store)
    started = asyncio.Event()

    async def blocking_runner(task_id, *, worker_id):
        assert store.owns_task_lease(task_id, worker_id)
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(app_module, "_run_agent_task_unlimited", blocking_runner)
    execution = asyncio.create_task(
        app_module.run_agent_task_with_worker_lease(task["id"], "worker-cancel")
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution

    saved = store.get_task(task["id"])
    assert saved["status"] == "unknown"
    assert saved["lease_owner"] is None
