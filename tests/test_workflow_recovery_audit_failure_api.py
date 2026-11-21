import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from crows import app as app_module
from crows.security import LocalAPIProtection
from crows.store import TaskStore


API_TOKEN = "crows-recovery-audit-failure-api-token"


def _isolated_app():
    isolated = FastAPI()
    isolated.router.routes = list(app_module.app.router.routes)
    isolated.add_middleware(LocalAPIProtection)
    return isolated


@pytest.mark.asyncio
async def test_recovery_audit_write_failure_releases_claim_without_execution(
    tmp_path, monkeypatch
):
    store = TaskStore(tmp_path / "workflow-recovery-audit-failure.sqlite3")
    task = store.create_task("[research] Must not start", "Keep recovery auditable")
    workflow = store.create_workflow(
        "Recovery audit outage",
        [{
            "title": "Must not start",
            "input": "Keep recovery auditable",
            "role": "research",
        }],
    )
    checkpoint = {
        "tasks": [{
            "plan_index": 0,
            "task_id": task["id"],
            "title": "Must not start",
            "role": "research",
            "status": "queued",
            "depends_on": [],
        }],
        "qa_review": None,
    }
    store.update_workflow(
        workflow["id"],
        "unknown",
        results=checkpoint,
        error="simulated process interruption",
    )
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setenv("CROWS_API_TOKEN", API_TOKEN)
    executions = []

    async def should_not_execute(task_id):
        executions.append(task_id)
        raise AssertionError("recovery must not execute without a durable start audit")

    def fail_audit(*args, **kwargs):
        raise OSError("simulated audit database outage")

    monkeypatch.setattr(app_module, "run_agent_task", should_not_execute)
    monkeypatch.setattr(store, "record_workflow_recovery_event", fail_audit)

    async with AsyncClient(
        transport=ASGITransport(app=_isolated_app()),
        base_url="http://127.0.0.1:8000",
        headers={"X-Crows-Token": API_TOKEN},
    ) as client:
        response = await client.post(
            f"/agents/workflows/{workflow['id']}/recover"
        )

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "Recovery audit storage is unavailable; no recovery tasks were started."
    )
    assert executions == []
    saved = store.get_workflow(workflow["id"])
    assert saved["status"] == "unknown"
    assert saved["error_text"] == (
        "Recovery was not started because its audit event could not be persisted."
    )
    assert saved["results"] == checkpoint
    assert store.get_task(task["id"])["status"] == "queued"
