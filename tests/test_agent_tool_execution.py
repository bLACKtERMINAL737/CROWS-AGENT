import json

import httpx
import pytest
from fastapi import HTTPException

from crows import app as app_module
from crows.store import TaskStore
from crows.tools import ToolError
from crows.provider import ModelProvider, ModelRouter


class ReadOnlyTools:
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
        if name != "workspace.read_file":
            raise ToolError("Unknown test tool")
        if arguments["path"] == "missing.txt":
            raise ToolError("File does not exist")
        return {
            "path": arguments["path"],
            "content": "Verified workspace evidence: MAXIMU-AGENT-TOOL-OK",
        }


class ToolCallingRouter:
    def __init__(self, mode="success"):
        self.mode = mode
        self.calls = []
        self.tool_error = None

    async def chat_with_tools(self, message, task_type, **kwargs):
        self.calls.append({"message": message, "task_type": task_type, **kwargs})
        if self.mode == "provider_failure":
            await kwargs["execute_tool"](
                "workspace__read_file", {"path": "notes.txt"}
            )
            raise httpx.ConnectError("simulated provider disconnect after tool result")

        path = "missing.txt" if self.mode == "tool_failure" else "notes.txt"
        try:
            result = await kwargs["execute_tool"](
                "workspace__read_file", {"path": path}
            )
        except ToolError as exc:
            # ModelProvider converts tool exceptions into an error tool result
            # and lets the model produce a final response.
            self.tool_error = str(exc)
            return {"answer": "The requested file could not be read.", "tool_calls": 1}
        return {
            "answer": f"Read evidence: {result['content']}",
            "tool_calls": 1,
        }


def configure(monkeypatch, tmp_path, router):
    store = TaskStore(tmp_path / "agent-tools.sqlite3")
    task = store.create_task(
        "[research] Inspect workspace evidence",
        "Read notes.txt and report its actual content.",
    )
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "router", router)
    monkeypatch.setattr(app_module, "tools", ReadOnlyTools())
    return store, task


@pytest.mark.asyncio
async def test_specialist_task_uses_tools_and_journals_success(
    tmp_path, monkeypatch
):
    router = ToolCallingRouter()
    store, task = configure(monkeypatch, tmp_path, router)

    result = await app_module.run_agent_task(task["id"])

    assert result["status"] == "succeeded"
    assert result["tool_calls"] == 1
    assert result["tool_attempts"] == 1
    assert "MAXIMU-AGENT-TOOL-OK" in result["result"]
    assert result["workspace_modified"] is False

    call = router.calls[0]
    assert call["task_type"] == "reasoning"
    assert call["max_tool_calls"] == 5
    assert call["execution_counter"]() is True
    assert any(
        spec["function"]["name"] == "workspace__read_file"
        for spec in call["tool_specs"]
    )

    saved_task = store.get_task(task["id"])
    assert saved_task["status"] == "succeeded"
    saved_result = json.loads(saved_task["result_json"])
    assert saved_result["tool_calls"] == 1
    assert saved_result["tool_attempts"] == 1

    executions = store.list_tool_executions(task["id"])
    assert len(executions) == 1
    assert executions[0]["tool_name"] == "workspace.read_file"
    assert executions[0]["status"] == "succeeded"
    assert "MAXIMU-AGENT-TOOL-OK" in executions[0]["result_json"]


@pytest.mark.asyncio
async def test_specialist_task_records_tool_errors_as_failed(
    tmp_path, monkeypatch
):
    router = ToolCallingRouter(mode="tool_failure")
    store, task = configure(monkeypatch, tmp_path, router)

    result = await app_module.run_agent_task(task["id"])

    assert "File does not exist" in router.tool_error
    assert result["status"] == "failed"
    assert result["tool_calls"] == 0
    assert result["tool_attempts"] == 1
    assert store.get_task(task["id"])["status"] == "failed"

    executions = store.list_tool_executions(task["id"])
    assert len(executions) == 1
    assert executions[0]["status"] == "failed"
    assert "File does not exist" in executions[0]["error_text"]


@pytest.mark.asyncio
async def test_provider_failure_after_tool_keeps_tool_record_and_fails_task(
    tmp_path, monkeypatch
):
    router = ToolCallingRouter(mode="provider_failure")
    store, task = configure(monkeypatch, tmp_path, router)

    with pytest.raises(HTTPException) as exc_info:
        await app_module.run_agent_task(task["id"])

    assert exc_info.value.status_code == 502
    assert store.get_task(task["id"])["status"] == "failed"

    executions = store.list_tool_executions(task["id"])
    assert len(executions) == 1
    # The child tool completed successfully; a later provider failure must not
    # rewrite the actual tool result to failed or unknown.
    assert executions[0]["status"] == "succeeded"
    assert "MAXIMU-AGENT-TOOL-OK" in executions[0]["result_json"]


@pytest.mark.asyncio
async def test_real_provider_transport_runs_specialist_tool_and_persists_journal(
    tmp_path, monkeypatch
):
    store = TaskStore(tmp_path / "provider-agent-journal.sqlite3")
    task = store.create_task(
        "[research] Verify workspace evidence",
        "Read notes.txt and report the verified content.",
    )
    tools = ReadOnlyTools()
    executed_tools = []
    original_execute = tools.execute

    def tracked_execute(name, arguments):
        executed_tools.append((name, arguments))
        return original_execute(name, arguments)

    tools.execute = tracked_execute
    responses = [
        {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call-workspace-read",
                        "type": "function",
                        "function": {
                            "name": "workspace__read_file",
                            "arguments": json.dumps({"path": "notes.txt"}),
                        },
                    }],
                }
            }]
        },
        {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "Verified workspace evidence: MAXIMU-AGENT-TOOL-OK",
                    "tool_calls": [],
                }
            }]
        },
    ]
    requests = []
    transport = httpx.MockTransport(
        lambda request: (
            requests.append(request)
            or httpx.Response(200, json=responses.pop(0), request=request)
        )
    )
    real_async_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        return real_async_client(*args, transport=transport, **kwargs)

    monkeypatch.setattr("crows.provider.httpx.AsyncClient", client_factory)
    provider = ModelProvider(
        "https://provider.example/v1", "integration-test-key", "crows-test-model"
    )
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "tools", tools)
    monkeypatch.setattr(app_module, "router", ModelRouter(provider))

    result = await app_module.run_agent_task(task["id"])

    assert result["status"] == "succeeded"
    assert result["tool_calls"] == 1
    assert result["tool_attempts"] == 1
    assert result["workspace_modified"] is False
    assert "MAXIMU-AGENT-TOOL-OK" in result["result"]
    assert executed_tools == [("workspace.read_file", {"path": "notes.txt"})]
    assert store.get_task(task["id"])["status"] == "succeeded"

    journal = store.list_tool_executions(task["id"])
    assert len(journal) == 1
    assert journal[0]["tool_name"] == "workspace.read_file"
    assert journal[0]["status"] == "succeeded"
    assert "Verified workspace evidence" in journal[0]["result_json"]

    assert len(requests) == 2
    first_payload = json.loads(requests[0].content)
    assert first_payload["model"] == "crows-test-model"
    assert first_payload["tool_choice"] == {
        "type": "function",
        "function": {"name": "workspace__read_file"},
    }
    second_payload = json.loads(requests[1].content)
    tool_result = next(
        item for item in second_payload["messages"] if item.get("role") == "tool"
    )
    assert tool_result["tool_call_id"] == "call-workspace-read"
    assert json.loads(tool_result["content"])["content"] == (
        "Verified workspace evidence: MAXIMU-AGENT-TOOL-OK"
    )
