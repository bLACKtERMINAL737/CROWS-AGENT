import json

import httpx
import pytest

from crows import app as app_module
from crows.provider import ModelProvider, ModelRouter
from crows.store import TaskStore


class ChatWorkspaceTools:
    def __init__(self):
        self.executions = []

    def describe(self):
        return [{
            "name": "workspace.read_file",
            "description": "Read a file from the approved workspace.",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        }]

    def execute(self, name, arguments):
        self.executions.append((name, arguments))
        return {
            "path": arguments["path"],
            "content": "Verified Crows workspace evidence: CHAT-E2E-731",
        }


@pytest.mark.asyncio
async def test_chat_provider_tool_execution_preserves_session_and_saved_memory(
    tmp_path, monkeypatch
):
    store = TaskStore(tmp_path / "chat-provider-session-memory.sqlite3")
    session = store.create_session("Crows integration conversation")
    store.add_session_message(session["id"], "user", "Earlier project question")
    store.add_session_message(session["id"], "assistant", "Earlier project answer")
    store.add_memory(
        "preference",
        "For project reviews, cite source evidence before conclusions.",
    )
    tools = ChatWorkspaceTools()
    responses = [
        {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "chat-read-1",
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
                    "content": "The file confirms CHAT-E2E-731; source evidence is included.",
                    "tool_calls": [],
                }
            }]
        },
    ]
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=responses.pop(0), request=request)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        return real_async_client(*args, transport=transport, **kwargs)

    monkeypatch.setattr("crows.provider.httpx.AsyncClient", client_factory)
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "tools", tools)
    monkeypatch.setattr(
        app_module,
        "router",
        ModelRouter(
            ModelProvider(
                "https://provider.example/v1",
                "integration-test-key",
                "crows-chat-test",
            )
        ),
    )

    result = await app_module.chat(
        app_module.ChatRequest(
            message="Use workspace.read_file to read notes.txt and summarize the evidence.",
            session_id=session["id"],
            use_saved_memories=True,
        )
    )

    assert result["answer"] == (
        "The file confirms CHAT-E2E-731; source evidence is included."
    )
    assert result["session_id"] == session["id"]
    assert result["memory_context_used"] is True
    assert result["tool_calls"] == 1
    assert tools.executions == [("workspace.read_file", {"path": "notes.txt"})]

    messages = store.list_session_messages(session["id"])
    assert [item["role"] for item in messages] == [
        "user", "assistant", "user", "assistant"
    ]
    assert messages[-2]["content"].startswith("Use workspace.read_file")
    assert messages[-1]["content"] == result["answer"]

    task = store.get_task(result["task_id"])
    assert task["status"] == "succeeded"
    journal = store.list_tool_executions(result["task_id"])
    assert len(journal) == 1
    assert journal[0]["tool_name"] == "workspace.read_file"
    assert journal[0]["status"] == "succeeded"
    assert "CHAT-E2E-731" in journal[0]["result_json"]

    assert len(requests) == 2
    first_payload = json.loads(requests[0].content)
    payload_messages = first_payload["messages"]
    assert any(
        item["role"] == "system"
        and "cite source evidence before conclusions" in item["content"]
        for item in payload_messages
    )
    assert any(
        item["role"] == "user" and item["content"] == "Earlier project question"
        for item in payload_messages
    )
    assert any(
        item["role"] == "assistant" and item["content"] == "Earlier project answer"
        for item in payload_messages
    )
    follow_up = json.loads(requests[1].content)
    tool_message = next(
        item for item in follow_up["messages"] if item.get("role") == "tool"
    )
    assert tool_message["tool_call_id"] == "chat-read-1"
    assert json.loads(tool_message["content"])["content"] == (
        "Verified Crows workspace evidence: CHAT-E2E-731"
    )
