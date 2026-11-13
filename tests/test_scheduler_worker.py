import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import crows.app as app_module
from crows.scheduler_store import SchedulerStore
from crows.store import TaskStore


@pytest.mark.asyncio
async def test_scheduler_worker_dispatches_due_registered_specialist_once(monkeypatch, tmp_path):
    scheduler = SchedulerStore(tmp_path / "scheduler.sqlite3")
    tasks = TaskStore(tmp_path / "tasks.sqlite3")
    due = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    schedule = scheduler.create_schedule(
        "[research] Summarize release notes", "Review the release notes", due, "dispatch-1"
    )
    monkeypatch.setattr(app_module, "scheduler_store", scheduler)
    monkeypatch.setattr(app_module, "store", tasks)
    monkeypatch.setattr(app_module, "list_agent_roles", lambda: [{"id": "research"}])

    async def fake_run_with_lease_renewal(task_id, schedule_id, worker_id):
        assert tasks.claim_task(task_id)
        tasks.set_task_status(task_id, "succeeded", result={"summary": "Reviewed"})

    async def stop_after_one_iteration(delay):
        if delay == 60:
            await asyncio.Event().wait()
        raise asyncio.CancelledError()

    monkeypatch.setattr(
        app_module, "run_agent_task_with_lease_renewal", fake_run_with_lease_renewal
    )
    monkeypatch.setattr(app_module.asyncio, "sleep", stop_after_one_iteration)

    with pytest.raises(asyncio.CancelledError):
        await app_module.scheduler_worker_loop()

    saved = scheduler.get_schedule(schedule["id"])
    assert saved["status"] == "completed"
    assert saved["task_id"]
    assert tasks.get_task(saved["task_id"])["status"] == "succeeded"


@pytest.mark.asyncio
async def test_scheduler_worker_does_not_preclaim_work_it_cannot_start(monkeypatch, tmp_path):
    scheduler = SchedulerStore(tmp_path / "scheduler-batch.sqlite3")
    tasks = TaskStore(tmp_path / "tasks-batch.sqlite3")
    due = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    first = scheduler.create_schedule(
        "[research] First due task", "Run first", due, "batch-first"
    )
    second = scheduler.create_schedule(
        "[research] Second due task", "Run second", due, "batch-second"
    )
    monkeypatch.setattr(app_module, "scheduler_store", scheduler)
    monkeypatch.setattr(app_module, "store", tasks)
    monkeypatch.setattr(app_module, "list_agent_roles", lambda: [{"id": "research"}])

    async def fake_run_with_lease_renewal(task_id, schedule_id, worker_id):
        assert tasks.claim_task(task_id)
        tasks.set_task_status(task_id, "succeeded", result={"ok": True})

    async def stop_after_one_iteration(_delay):
        raise asyncio.CancelledError()

    monkeypatch.setattr(
        app_module, "run_agent_task_with_lease_renewal", fake_run_with_lease_renewal
    )
    monkeypatch.setattr(app_module.asyncio, "sleep", stop_after_one_iteration)

    with pytest.raises(asyncio.CancelledError):
        await app_module.scheduler_worker_loop()

    assert scheduler.get_schedule(first["id"])["status"] == "completed"
    assert scheduler.get_schedule(second["id"])["status"] == "scheduled"


@pytest.mark.asyncio
async def test_scheduler_worker_rejects_unregistered_specialist_without_execution(monkeypatch, tmp_path):
    scheduler = SchedulerStore(tmp_path / "scheduler.sqlite3")
    tasks = TaskStore(tmp_path / "tasks.sqlite3")
    due = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    schedule = scheduler.create_schedule(
        "[unknown] Do something", "Must not run", due, "dispatch-invalid"
    )
    monkeypatch.setattr(app_module, "scheduler_store", scheduler)
    monkeypatch.setattr(app_module, "store", tasks)
    monkeypatch.setattr(app_module, "list_agent_roles", lambda: [{"id": "research"}])

    async def unexpected_run(_task_id):
        raise AssertionError("unregistered role must never execute")

    async def stop_after_one_iteration(_delay):
        raise asyncio.CancelledError()

    monkeypatch.setattr(app_module, "run_agent_task", unexpected_run)
    monkeypatch.setattr(app_module.asyncio, "sleep", stop_after_one_iteration)

    with pytest.raises(asyncio.CancelledError):
        await app_module.scheduler_worker_loop()

    saved = scheduler.get_schedule(schedule["id"])
    assert saved["status"] == "failed"
    assert saved["task_id"] is None
    assert tasks.list_tasks() == []


@pytest.mark.asyncio
async def test_lifespan_starts_and_stops_opt_in_scheduler_worker(monkeypatch):
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def worker():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setenv("AETHER_SCHEDULER_WORKER_ENABLED", "true")
    monkeypatch.delenv("AETHER_MESSAGING_WORKER_ENABLED", raising=False)
    monkeypatch.setattr(app_module, "scheduler_worker_loop", worker)
    monkeypatch.setattr(app_module.store, "runtime_process_lock", lambda blocking=False: nullcontext(True))
    monkeypatch.setattr(app_module.store, "mark_interrupted_as_unknown", lambda: 0)

    from contextlib import nullcontext
    monkeypatch.setattr(app_module.store, "coding_apply_lock", lambda blocking=False: nullcontext(True))

    async with app_module.lifespan(app_module.app):
        await asyncio.wait_for(started.wait(), timeout=1)

    assert stopped.is_set()


@pytest.mark.asyncio
async def test_lifespan_does_not_start_scheduler_worker_by_default(monkeypatch):
    started = False

    async def worker():
        nonlocal started
        started = True

    monkeypatch.delenv("AETHER_SCHEDULER_WORKER_ENABLED", raising=False)
    monkeypatch.delenv("AETHER_MESSAGING_WORKER_ENABLED", raising=False)
    monkeypatch.setattr(app_module, "scheduler_worker_loop", worker)
    monkeypatch.setattr(app_module.store, "runtime_process_lock", lambda blocking=False: nullcontext(True))
    monkeypatch.setattr(app_module.store, "mark_interrupted_as_unknown", lambda: 0)

    from contextlib import nullcontext
    monkeypatch.setattr(app_module.store, "coding_apply_lock", lambda blocking=False: nullcontext(True))

    async with app_module.lifespan(app_module.app):
        pass

    assert started is False


@pytest.mark.asyncio
async def test_lease_renewal_failure_cancels_in_flight_task(monkeypatch):
    class LostLeaseStore:
        def renew_claim(self, *_args, **_kwargs):
            return False

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def fake_sleep(_seconds):
        # Do not report lease loss until the execution coroutine is active.
        await started.wait()

    async def long_running_task(_task_id):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(app_module, "scheduler_store", LostLeaseStore())
    monkeypatch.setattr(app_module, "run_agent_task", long_running_task)
    monkeypatch.setattr(app_module.asyncio, "sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="lease ownership was lost"):
        await app_module.run_agent_task_with_lease_renewal(
            "task-1", "schedule-1", "worker-1"
        )

    assert started.is_set()
    assert cancelled.is_set()
