import json

import httpx
import pytest

from crows.provider import ModelProvider


def _completion(*, content=None, tool_calls=None):
    return {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls or [],
            }
        }]
    }


def _call(call_id, name="workspace__read_file", arguments='{"path":"known_facts.txt"}'):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _tool_specs():
    return [{
        "type": "function",
        "function": {
            "name": "workspace__read_file",
            "description": "Read a workspace file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    }]


def _install_transport(monkeypatch, responses):
    requests = []

    def handler(request):
        requests.append(request)
        payload = responses.pop(0)
        return httpx.Response(200, json=payload, request=request)

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        return real_client(*args, transport=transport, **kwargs)

    monkeypatch.setattr("crows.provider.httpx.AsyncClient", client_factory)
    return requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("calls", "error"),
    [
        (
            [_call("duplicate-id"), _call("duplicate-id")],
            "duplicate tool-call IDs",
        ),
        (
            [_call("unknown-tool-id", name="workspace__delete_file")],
            "unregistered tool name",
        ),
    ],
)
async def test_provider_rejects_ambiguous_or_unregistered_tool_batches_before_execution(
    monkeypatch, calls, error
):
    requests = _install_transport(monkeypatch, [_completion(tool_calls=calls)])
    provider = ModelProvider(
        "https://provider.example/v1", "test-key", "test-model"
    )
    executed = []

    async def execute_tool(name, arguments):
        executed.append((name, arguments))
        return {"ok": True}

    with pytest.raises(RuntimeError, match=error):
        await provider.chat_with_tools(
            "Inspect known_facts.txt",
            tool_specs=_tool_specs(),
            execute_tool=execute_tool,
        )

    assert executed == []
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_provider_returns_invalid_argument_feedback_without_running_tool(monkeypatch):
    requests = _install_transport(monkeypatch, [
        _completion(tool_calls=[_call("invalid-json", arguments='["not", "an", "object"]')]),
        _completion(content="I could not run the tool because its arguments were invalid."),
    ])
    provider = ModelProvider(
        "https://provider.example/v1", "test-key", "test-model"
    )
    executed = []

    async def execute_tool(name, arguments):
        executed.append((name, arguments))
        return {"unexpected": True}

    result = await provider.chat_with_tools(
        "Read known_facts.txt",
        tool_specs=_tool_specs(),
        execute_tool=execute_tool,
    )

    assert result == {
        "answer": "I could not run the tool because its arguments were invalid.",
        "tool_calls": 0,
    }
    assert executed == []
    assert len(requests) == 2
    follow_up = json.loads(requests[1].content)
    tool_result = next(item for item in follow_up["messages"] if item.get("role") == "tool")
    assert tool_result["tool_call_id"] == "invalid-json"
    assert "Invalid tool arguments" in tool_result["content"]


@pytest.mark.asyncio
async def test_provider_caps_execution_when_model_returns_more_calls_than_allowed(monkeypatch):
    calls = [_call(f"call-{index}") for index in range(7)]
    requests = _install_transport(monkeypatch, [
        _completion(tool_calls=calls),
        _completion(content="Execution limit reached."),
    ])
    provider = ModelProvider(
        "https://provider.example/v1", "test-key", "test-model"
    )
    executed = []

    async def execute_tool(name, arguments):
        executed.append((name, arguments))
        return {"ok": True}

    result = await provider.chat_with_tools(
        "Read known_facts.txt",
        tool_specs=_tool_specs(),
        execute_tool=execute_tool,
        max_tool_calls=2,
    )

    assert result["tool_calls"] == 2
    assert len(executed) == 2
    assert len(requests) == 2
    second_payload = json.loads(requests[1].content)
    assert "tools" not in second_payload
    assert sum(item.get("role") == "tool" for item in second_payload["messages"]) == 5
