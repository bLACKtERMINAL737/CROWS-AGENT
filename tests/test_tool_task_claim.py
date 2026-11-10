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
