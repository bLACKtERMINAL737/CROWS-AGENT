from pathlib import Path

import pytest
from fastapi import HTTPException

from crows import app as app_module
from crows.store import TaskStore


def test_tool_execution_cannot_reuse_completed_task(tmp_path: Path, monkeypatch):
    store = TaskStore(tmp_path / "tool-claim.sqlite3")
    task = store.create_task("single-use tool task", "run once")
    store.set_task_status(task["id"], "succeeded", result={"previous": True})
    calls = []
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module.tools, "execute", lambda name, payload: calls.append(name) or {"ok": True})

    with pytest.raises(HTTPException) as exc:
        app_module.execute_tool(
            "workspace.list_files",
            app_module.ToolRequest(input={"path": "."}, task_id=task["id"]),
        )

    assert exc.value.status_code == 409
    assert calls == []
    assert store.get_task(task["id"])["status"] == "succeeded"


def test_tool_execution_claims_queued_task_before_running(tmp_path: Path, monkeypatch):
    store = TaskStore(tmp_path / "tool-claim-success.sqlite3")
    task = store.create_task("queued tool task", "run once")
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module.tools, "execute", lambda name, payload: {"ok": True})

    result = app_module.execute_tool(
        "workspace.list_files",
        app_module.ToolRequest(input={"path": "."}, task_id=task["id"]),
    )

    assert result["status"] == "succeeded"
    assert store.get_task(task["id"])["status"] == "succeeded"
    assert len(store.list_tool_executions(task["id"])) == 1


def test_task_api_accepts_priority_and_idempotency_key(tmp_path: Path, monkeypatch):
    store = TaskStore(tmp_path / "task-api-idempotency.sqlite3")
    monkeypatch.setattr(app_module, "store", store)

    request = app_module.TaskRequest(
        title="[research] API task",
        input="Research this",
        priority=7,
        idempotency_key="api-request-77",
    )
    first = app_module.create_task(request)
    repeated = app_module.create_task(request)

    assert first["id"] == repeated["id"]
    assert first["priority"] == 7
    assert first["attempt_count"] == 0


def test_task_api_rejects_idempotency_key_payload_conflict(tmp_path: Path, monkeypatch):
    store = TaskStore(tmp_path / "task-api-conflict.sqlite3")
    monkeypatch.setattr(app_module, "store", store)
    app_module.create_task(
        app_module.TaskRequest(
            title="[research] API task",
            input="Original request",
            idempotency_key="api-request-conflict",
        )
    )

    with pytest.raises(HTTPException) as exc:
        app_module.create_task(
            app_module.TaskRequest(
                title="[research] API task",
                input="Changed request",
                idempotency_key="api-request-conflict",
            )
        )
    assert exc.value.status_code == 409


def test_retry_api_only_requeues_known_failed_task(tmp_path: Path, monkeypatch):
    store = TaskStore(tmp_path / "task-api-retry.sqlite3")
    task = store.create_task("[research] Retry task", "run once")
    assert store.claim_task(task["id"])
    store.set_task_status(task["id"], "failed", error="transient provider failure")
    monkeypatch.setattr(app_module, "store", store)

    retried = app_module.retry_failed_task(
        task["id"], app_module.TaskRetryRequest(max_attempts=2)
    )
    assert retried["status"] == "queued"

    store.set_task_status(task["id"], "unknown", error="outcome uncertain")
    with pytest.raises(HTTPException) as exc:
        app_module.retry_failed_task(
            task["id"], app_module.TaskRetryRequest(max_attempts=3)
        )
    assert exc.value.status_code == 409
