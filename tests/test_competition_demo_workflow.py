import json

import pytest

from crows import app as app_module
from crows.store import TaskStore


class DemoPlanningRouter:
    def __init__(self):
        self.plan = {
            "subtasks": [
                {
                    "title": "Gather source evidence",
                    "input": "Collect evidence for the requested review",
                    "role": "research",
                },
                {
                    "title": "Write dependent summary",
                    "input": "Summarize the gathered evidence",
                    "role": "coding",
                    "depends_on": [0],
                },
            ]
        }
        self.planning_calls = 0

    async def chat(self, message, task_type):
        self.planning_calls += 1
        return {"answer": json.dumps(self.plan)}


class NoTools:
    def describe(self):
        return []


class ConfiguredRoute:
    configured = True


@pytest.mark.asyncio
async def test_review_first_demo_flow_keeps_failure_visible_through_timeline_and_evaluation(
    tmp_path, monkeypatch
):
    store = TaskStore(tmp_path / "demo-flow.sqlite3")
    router = DemoPlanningRouter()
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "router", router)
    monkeypatch.setattr(app_module, "tools", NoTools())
    monkeypatch.setattr(app_module, "reasoning_provider", ConfiguredRoute())

    goal = "Review a sample project and summarize verified risks"

    # Planning is preview-only: it must not create a workflow or task records.
    preview = await app_module.preview_agent_plan(
        app_module.AgentPlanRequest(goal=goal)
    )
    assert preview["executed"] is False
    assert preview["approval_required"] is True
    assert len(preview["subtasks"]) == 2
    assert store.list_workflows() == []
    assert router.planning_calls == 1

    async def deterministic_task_runner(task_id):
        task = store.get_task(task_id)
        assert task is not None
        store.claim_task(task_id)

        if task["title"].startswith("[research]"):
            store.set_task_status(
                task_id, "failed", error="deterministic demo fixture failure"
            )
            return {
                "task_id": task_id,
                "status": "failed",
                "result": "",
                "tool_calls": 0,
                "tool_attempts": 0,
            }

        answer = (
            '{"verdict":"PASS","summary":"QA reviewed the recorded outcomes.",'
            '"findings":[]}'
            if task["title"].startswith("[qa_security]")
            else "Unexpected task reached execution"
        )
        store.set_task_status(task_id, "succeeded", result={"answer": answer})
        return {
            "task_id": task_id,
            "status": "succeeded",
            "result": answer,
            "tool_calls": 0,
            "tool_attempts": 0,
        }

    monkeypatch.setattr(app_module, "run_agent_task", deterministic_task_runner)

    # Execute the exact plan returned by preview; do not ask the planner again.
    result = await app_module.orchestrate_agent_work(
        app_module.AgentPlanRequest(goal=goal, subtasks=preview["subtasks"])
    )
    assert router.planning_calls == 1
    assert result["status"] == "partial"

    saved = store.get_workflow(result["workflow_id"])
    assert saved is not None
    task_outcomes = {item["title"]: item for item in saved["results"]["tasks"]}
    assert task_outcomes["Gather source evidence"]["status"] == "failed"
    assert task_outcomes["Write dependent summary"]["status"] == "blocked"

    timeline = app_module.get_agent_workflow_timeline(result["workflow_id"])
    timeline_statuses = {
        item["status"] for item in timeline["timeline"] if item["event"] == "task.status"
    }
    assert "failed" in timeline_statuses
    assert "blocked" in timeline_statuses

    evaluation = app_module.evaluate_agent_workflow(result["workflow_id"])
    report = evaluation["evaluation"]
    assert report["outcome"] == "needs_attention"
    assert report["checks"]["workflow_succeeded"] is False
    assert report["checks"]["all_tasks_succeeded"] is False
    assert report["task_counts"]["failed"] == 1
    assert report["task_counts"]["blocked"] == 1

    # The deterministic failure detail is persisted for local debugging, but
    # the content-minimizing evaluation response must not expose it.
    assert "deterministic demo fixture failure" not in str(report)

@pytest.mark.asyncio
async def test_review_first_demo_happy_path_completes_qa_timeline_and_evaluation(
    tmp_path, monkeypatch
):
    store = TaskStore(tmp_path / "demo-happy-path.sqlite3")
    router = DemoPlanningRouter()
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "router", router)
    monkeypatch.setattr(app_module, "tools", NoTools())
    monkeypatch.setattr(app_module, "reasoning_provider", ConfiguredRoute())

    goal = "Review a sample project and produce an evidence-based summary"
    preview = await app_module.preview_agent_plan(app_module.AgentPlanRequest(goal=goal))

    assert preview["executed"] is False
    assert store.list_workflows() == []
    assert router.planning_calls == 1

    async def deterministic_success_runner(task_id):
        task = store.get_task(task_id)
        assert task is not None
        assert store.claim_task(task_id) is True
        answer = (
            '{"verdict":"PASS","summary":"All required evidence checks passed.",'
            '"findings":[]}'
            if task["title"].startswith("[qa_security]")
            else f"Verified evidence for {task['title']}"
        )
        store.set_task_status(task_id, "succeeded", result={"answer": answer})
        return {
            "task_id": task_id,
            "status": "succeeded",
            "result": answer,
            "tool_calls": 0,
            "tool_attempts": 0,
        }

    monkeypatch.setattr(app_module, "run_agent_task", deterministic_success_runner)
    result = await app_module.orchestrate_agent_work(
        app_module.AgentPlanRequest(goal=goal, subtasks=preview["subtasks"])
    )

    assert result["status"] == "succeeded"
    assert router.planning_calls == 1  # reviewed plan was executed, not regenerated
    saved = store.get_workflow(result["workflow_id"])
    assert saved is not None
    assert all(item["status"] == "succeeded" for item in saved["results"]["tasks"])
    assert saved["results"]["qa_review"]["status"] == "succeeded"
    assert saved["results"]["qa_review"]["verdict"] == "PASS"

    timeline = app_module.get_agent_workflow_timeline(result["workflow_id"])
    specialist_successes = [
        item for item in timeline["timeline"]
        if item["event"] == "task.status"
        and item["phase"] == "specialist"
        and item["status"] == "succeeded"
    ]
    qa_successes = [
        item for item in timeline["timeline"]
        if item["event"] == "task.status"
        and item["phase"] == "qa"
        and item["status"] == "succeeded"
    ]
    assert len(specialist_successes) == 2
    assert len(qa_successes) == 1

    evaluation = app_module.evaluate_agent_workflow(result["workflow_id"])
    report = evaluation["evaluation"]
    assert report["outcome"] == "passed"
    assert report["checks"]["workflow_succeeded"] is True
    assert report["checks"]["all_tasks_succeeded"] is True
    assert report["checks"]["qa_passed"] is True
