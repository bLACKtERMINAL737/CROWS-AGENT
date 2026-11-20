import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from crows import app as app_module
from crows.security import LocalAPIProtection
from crows.store import TaskStore


API_TOKEN = "crows-recovery-unexpected-error-api-token"


def _isolated_app():
    isolated = FastAPI()
    isolated.router.routes = list(app_module.app.router.routes)
    isolated.add_middleware(LocalAPIProtection)
    return isolated


@pytest.mark.asyncio
async def test_unexpected_recovery_error_persists_interruption_and_audit(
    tmp_path, monkeypatch
):
    store = TaskStore(tmp_path / "workflow-recovery-unexpected-error.sqlite3")
    task = store.create_task("[research] May have acted", "Inspect external state")
    workflow = store.create_workflow(
        "Unexpected recovery error",
        [{
            "title": "May have acted",
            "input": "Inspect external state",
            "role": "research",
        }],
    )
    store.update_workflow(
        workflow["id"],
        "unknown",
        results={
            "tasks": [{
                "plan_index": 0,
                "task_id": task["id"],
                "title": "May have acted",
                "role": "research",
                "status": "queued",
                "depends_on": [],
            }],
            "qa_review": None,
        },
        error="simulated process interruption",
    )
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setenv("CROWS_API_TOKEN", API_TOKEN)
    executions = []

    async def crash_after_claim(task_id):
        assert store.claim_task(task_id)
        executions.append(task_id)
        raise RuntimeError("simulated worker failure after task claim")

    monkeypatch.setattr(app_module, "run_agent_task", crash_after_claim)

    async with AsyncClient(
        transport=ASGITransport(app=_isolated_app(), raise_app_exceptions=False),
        base_url="http://127.0.0.1:8000",
        headers={"X-Crows-Token": API_TOKEN},
    ) as client:
        response = await client.post(
            f"/agents/workflows/{workflow['id']}/recover"
        )

    assert response.status_code == 500
    assert executions == [task["id"]]
    # The task was claimed before the worker failed. Its outcome is uncertain,
    # so recovery must preserve that fact rather than reset it to queued.
    assert store.get_task(task["id"])["status"] == "running"
    saved = store.get_workflow(workflow["id"])
    assert saved["status"] == "unknown"
    assert saved["results"]["tasks"][0]["status"] == "unknown"
    assert "unexpectedly" in saved["error"]
    audit = store.list_workflow_recovery_audit(workflow["id"])
    assert {event["event_type"] for event in audit["events"]} == {
        "started",
        "interrupted",
    }
    assert len({event["attempt_id"] for event in audit["events"]}) == 1
