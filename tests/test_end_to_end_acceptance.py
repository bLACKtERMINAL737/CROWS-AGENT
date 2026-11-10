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
