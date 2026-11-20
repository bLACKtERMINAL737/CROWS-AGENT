import asyncio

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from crows import app as app_module
from crows.security import LocalAPIProtection
from crows.store import TaskStore


API_TOKEN = "crows-recovery-concurrency-api-token"


def _isolated_app():
    isolated = FastAPI()
    isolated.router.routes = list(app_module.app.router.routes)
    isolated.add_middleware(LocalAPIProtection)
    return isolated


@pytest.mark.asyncio
async def test_concurrent_http_recovery_allows_only_one_executor(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "workflow-recovery-concurrency.sqlite3")
    task = store.create_task("[research] Recover once", "A checkpointed queued task")
    workflow = store.create_workflow(
        "Concurrent recovery claim",
        [{
            "title": "Recover once",
            "input": "A checkpointed queued task",
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
                "title": "Recover once",
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

    execution_started = asyncio.Event()
    allow_execution_to_finish = asyncio.Event()
    executed = []

    async def controlled_run(task_id):
        assert task_id == task["id"]
        assert store.claim_task(task_id)
        executed.append(task_id)
        execution_started.set()
        await allow_execution_to_finish.wait()
        store.set_task_status(task_id, "succeeded", result={"answer": "completed once"})
        return {
            "task_id": task_id,
            "status": "succeeded",
            "result": "completed once",
            "tool_calls": 0,
            "tool_attempts": 0,
        }

    monkeypatch.setattr(app_module, "run_agent_task", controlled_run)

    async with AsyncClient(
        transport=ASGITransport(app=_isolated_app()),
        base_url="http://127.0.0.1:8000",
        headers={"X-Crows-Token": API_TOKEN},
    ) as client:
        first_request = asyncio.create_task(
            client.post(f"/agents/workflows/{workflow['id']}/recover")
        )
        await asyncio.wait_for(execution_started.wait(), timeout=5)

        second_response = await client.post(
            f"/agents/workflows/{workflow['id']}/recover"
        )
        assert second_response.status_code == 409
        assert executed == [task["id"]]

        allow_execution_to_finish.set()
        first_response = await asyncio.wait_for(first_request, timeout=5)

    assert first_response.status_code == 200
    assert executed == [task["id"]]
    assert store.get_task(task["id"])["status"] == "succeeded"
    audit = store.list_workflow_recovery_audit(workflow["id"])
    assert len(audit["events"]) == 2
    assert {event["event_type"] for event in audit["events"]} == {
        "started",
        "completed",
    }
    assert len({event["attempt_id"] for event in audit["events"]}) == 1
