"""End-to-end acceptance coverage for the public chat API."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from crows import app as app_module
from crows.security import LocalAPIProtection
from crows.store import TaskStore


WORKSPACE_MARKER = "CROWS_E2E_WORKSPACE_MARKER_731"
API_TOKEN = "crows-e2e-acceptance-token"


class DeterministicWorkspaceTools:
    def describe(self):
        return [{
            "name": "workspace.read_file",
            "description": "Read a named file from the approved workspace.",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        }]

    def execute(self, name, arguments):
        assert name == "workspace.read_file"
        assert arguments == {"path": "notes.txt"}
        return {"path": "notes.txt", "content": WORKSPACE_MARKER}


class DeterministicChatRouter:
    def __init__(self):
        self.memories = []

    async def chat_with_tools(self, message, task_type, **kwargs):
        self.memories = list(kwargs["memories"])
        assert task_type == "coding"
        assert kwargs["execution_counter"]() is False
        result = await kwargs["execute_tool"](
            "workspace__read_file", {"path": "notes.txt"}
        )
        assert kwargs["execution_counter"]() is True
        return {
            "answer": f"Read notes.txt successfully: {result['content']}",
            "model": "mock/acceptance",
            "tool_calls": 1,
        }


def test_chat_api_runs_tool_and_persists_memory_and_session(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CROWS_API_TOKEN", API_TOKEN)
    store = TaskStore(tmp_path / "acceptance.sqlite3")
    router = DeterministicChatRouter()
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "router", router)
    monkeypatch.setattr(app_module, "tools", DeterministicWorkspaceTools())

    # Use an isolated ASGI app so middleware captures this test's token,
    # independent of the shared application's import/test initialization order.
    isolated_app = FastAPI()
    isolated_app.router.routes = list(app_module.app.router.routes)
    isolated_app.add_middleware(LocalAPIProtection)
    headers = {"X-Crows-Token": API_TOKEN}

    with TestClient(
        isolated_app, client=("127.0.0.1", 50000)
    ) as client:
        memory_response = client.post(
            "/memory",
            headers=headers,
            json={
                "category": "preference",
                "content": "Use concise answers during Crows acceptance testing",
                "tags": ["acceptance"],
            },
        )
        assert memory_response.status_code == 200
        saved_memory = memory_response.json()

        chat_response = client.post(
            "/chat",
            headers=headers,
            json={
                "message": "For Crows acceptance testing, read notes.txt and summarize it.",
                "task_type": "coding",
                "use_saved_memories": True,
            },
        )
        assert chat_response.status_code == 200
        chat = chat_response.json()

        assert WORKSPACE_MARKER in chat["answer"]
        assert chat["tool_calls"] == 1
        assert chat["memory_context_used"] is True
        assert any(item["id"] == saved_memory["id"] for item in router.memories)

        session_response = client.get(
            f"/sessions/{chat['session_id']}", headers=headers
        )
        assert session_response.status_code == 200
        messages = session_response.json()["messages"]
        assert [item["role"] for item in messages] == ["user", "assistant"]
        assert messages[0]["content"] == (
            "For Crows acceptance testing, read notes.txt and summarize it."
        )
        assert WORKSPACE_MARKER in messages[1]["content"]

    task = store.get_task(chat["task_id"])
    assert task["status"] == "succeeded"
    executions = store.list_tool_executions(chat["task_id"])
    assert len(executions) == 1
    assert executions[0]["tool_name"] == "workspace.read_file"
    assert executions[0]["status"] == "succeeded"
    assert WORKSPACE_MARKER in executions[0]["result_json"]

    reopened = TaskStore(tmp_path / "acceptance.sqlite3")
    assert reopened.get_memory(saved_memory["id"])["content"] == (
        "Use concise answers during Crows acceptance testing"
    )
    assert reopened.get_session(chat["session_id"]) is not None

class DeterministicWorkflowPlanner:
    def __init__(self):
        self.calls = 0

    async def chat(self, message, task_type):
        import json

        self.calls += 1
        assert task_type == "reasoning"
        return {
            "answer": json.dumps({
                "subtasks": [
                    {
                        "title": "Gather source evidence",
                        "input": "Collect evidence from the sample workspace",
                        "role": "research",
                    },
                    {
                        "title": "Prepare evidence summary",
                        "input": "Summarize the verified source evidence",
                        "role": "coding",
                        "depends_on": [0],
                    },
                ]
            })
        }


def test_review_first_workflow_api_executes_approved_plan_and_returns_qa_timeline(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CROWS_API_TOKEN", API_TOKEN)
    store = TaskStore(tmp_path / "workflow-acceptance.sqlite3")
    planner = DeterministicWorkflowPlanner()
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "router", planner)
    monkeypatch.setattr(app_module, "reasoning_provider", type("Route", (), {"configured": True})())

    async def deterministic_task_runner(task_id):
        task = store.get_task(task_id)
        assert task is not None
        assert store.claim_task(task_id) is True
        answer = (
            '{"verdict":"PASS","summary":"All evidence checks passed.",'
            '"findings":[]}'
            if task["title"].startswith("[qa_security]")
            else f"Verified result for {task['title']}"
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

    isolated_app = FastAPI()
    isolated_app.router.routes = list(app_module.app.router.routes)
    isolated_app.add_middleware(LocalAPIProtection)
    headers = {"X-Crows-Token": API_TOKEN}
    goal = "Review the sample workspace and produce an evidence-based summary"

    with TestClient(isolated_app, client=("127.0.0.1", 50001)) as client:
        preview_response = client.post(
            "/agents/plan", headers=headers, json={"goal": goal}
        )
        assert preview_response.status_code == 200
        preview = preview_response.json()
        assert preview["executed"] is False
        assert len(preview["subtasks"]) == 2
        assert store.list_workflows() == []
        assert store.list_tasks() == []
        assert planner.calls == 1

        execution_response = client.post(
            "/agents/orchestrate",
            headers=headers,
            json={"goal": goal, "subtasks": preview["subtasks"]},
        )
        assert execution_response.status_code == 200
        execution = execution_response.json()
        assert execution["status"] == "succeeded"
        assert planner.calls == 1  # the exact reviewed plan was executed

        workflow_id = execution["workflow_id"]
        workflow_response = client.get(
            f"/agents/workflows/{workflow_id}", headers=headers
        )
        assert workflow_response.status_code == 200
        workflow = workflow_response.json()
        assert workflow["status"] == "succeeded"
        assert workflow["results"]["qa_review"]["verdict"] == "PASS"

        timeline_response = client.get(
            f"/agents/workflows/{workflow_id}/timeline", headers=headers
        )
        assert timeline_response.status_code == 200
        timeline = timeline_response.json()["timeline"]
        assert any(
            event["event"] == "task.status"
            and event["phase"] == "specialist"
            and event["status"] == "succeeded"
            for event in timeline
        )
        assert any(
            event["event"] == "task.status"
            and event["phase"] == "qa"
            and event["status"] == "succeeded"
            for event in timeline
        )

        evaluation_response = client.get(
            f"/agents/workflows/{workflow_id}/evaluation", headers=headers
        )
        assert evaluation_response.status_code == 200
        evaluation = evaluation_response.json()["evaluation"]
        assert evaluation["outcome"] == "passed"
        assert evaluation["checks"]["workflow_succeeded"] is True
        assert evaluation["checks"]["qa_passed"] is True

    reopened = TaskStore(tmp_path / "workflow-acceptance.sqlite3")
    persisted = reopened.get_workflow(workflow_id)
    assert persisted is not None
    assert persisted["status"] == "succeeded"
    assert len(persisted["results"]["tasks"]) == 2
