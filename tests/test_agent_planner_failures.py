import httpx
import pytest
from fastapi import HTTPException

from crows import app as app_module
from crows.store import TaskStore


class PlannerRouter:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error

    async def chat(self, message, task_type):
        if self.error:
            raise self.error
        return self.response


@pytest.mark.parametrize(
    ("response", "expected_detail"),
    [
        (None, "Planner returned an invalid plan"),
        ({}, "Planner returned an invalid plan"),
        ({"answer": None}, "Planner returned an invalid plan"),
        ({"answer": "not json"}, "Planner returned an invalid plan"),
        ({"answer": '{"subtasks": []}'}, "Planner returned an invalid plan"),
    ],
)
@pytest.mark.asyncio
async def test_invalid_planner_responses_fail_before_creating_workflow(
    tmp_path, monkeypatch, response, expected_detail
):
    store = TaskStore(tmp_path / "planner.sqlite3")
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "router", PlannerRouter(response=response))

    with pytest.raises(HTTPException) as error:
        await app_module.orchestrate_agent_work(
            app_module.AgentPlanRequest(goal="Inspect the repository")
        )

    assert error.value.status_code == 502
    assert expected_detail in error.value.detail
    assert store.list_workflows(limit=10) == []


@pytest.mark.parametrize(
    ("failure", "status_code", "detail"),
    [
        (httpx.ReadTimeout("upstream timed out"), 504, "Planner timed out"),
        (httpx.ConnectError("connection refused"), 502, "Planner provider request failed"),
        (RuntimeError("unexpected provider adapter failure"), 502, "Planner failed unexpectedly"),
    ],
)
@pytest.mark.asyncio
async def test_planner_upstream_failures_are_mapped_and_create_no_work(
    tmp_path, monkeypatch, failure, status_code, detail
):
    store = TaskStore(tmp_path / "planner-failure.sqlite3")
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "router", PlannerRouter(error=failure))

    with pytest.raises(HTTPException) as error:
        await app_module.orchestrate_agent_work(
            app_module.AgentPlanRequest(goal="Inspect the repository")
        )

    assert error.value.status_code == status_code
    assert detail in error.value.detail
    assert store.list_workflows(limit=10) == []


@pytest.mark.asyncio
async def test_planner_provider_not_configured_keeps_service_unavailable(
    tmp_path, monkeypatch
):
    store = TaskStore(tmp_path / "planner-unconfigured.sqlite3")
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(
        app_module, "router", PlannerRouter(error=app_module.ProviderNotConfigured("missing"))
    )

    with pytest.raises(HTTPException) as error:
        await app_module.orchestrate_agent_work(
            app_module.AgentPlanRequest(goal="Inspect the repository")
        )

    assert error.value.status_code == 503
    assert store.list_workflows(limit=10) == []
