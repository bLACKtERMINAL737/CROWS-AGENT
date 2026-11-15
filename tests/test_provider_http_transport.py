import json

import httpx
import pytest

from crows.provider import ModelProvider


def _completion(*, content=None, tool_calls=None):
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls or [],
            },
            "finish_reason": "tool_calls" if tool_calls else "stop",
        }],
    }


@pytest.mark.asyncio
async def test_openai_compatible_http_transport_executes_tool_round_trip(monkeypatch):
    requests = []
    responses = [
        _completion(tool_calls=[{
            "id": "call-read-1",
            "type": "function",
            "function": {
                "name": "workspace__read_file",
                "arguments": json.dumps({"path": "known_facts.txt"}),
            },
        }]),
        _completion(content="The verified marker is ORBIT-731."),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer integration-test-key"
        assert request.headers["content-type"].startswith("application/json")
        return httpx.Response(200, json=responses.pop(0))

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        return real_async_client(*args, transport=transport, **kwargs)

    monkeypatch.setattr("crows.provider.httpx.AsyncClient", client_factory)
    provider = ModelProvider(
        "https://provider.example/v1/",
        "integration-test-key",
        "test-model",
    )
    executed = []

    async def execute_tool(name, arguments):
        executed.append((name, arguments))
        return {"path": arguments["path"], "content": "Crows demo marker: ORBIT-731."}

    result = await provider.chat_with_tools(
        "Read known_facts.txt using workspace.read_file.",
        tool_specs=[{
            "type": "function",
            "function": {
                "name": "workspace__read_file",
                "description": "Read a workspace file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        }],
        execute_tool=execute_tool,
    )

    assert result == {"answer": "The verified marker is ORBIT-731.", "tool_calls": 1}
    assert executed == [("workspace__read_file", {"path": "known_facts.txt"})]
    assert len(requests) == 2

    first_payload = json.loads(requests[0].content)
    assert first_payload["model"] == "test-model"
    assert first_payload["tool_choice"] == {
        "type": "function",
        "function": {"name": "workspace__read_file"},
    }

    second_payload = json.loads(requests[1].content)
    tool_result = next(
        item for item in second_payload["messages"] if item.get("role") == "tool"
    )
    assert tool_result["tool_call_id"] == "call-read-1"
    assert json.loads(tool_result["content"]) == {
        "path": "known_facts.txt",
        "content": "Crows demo marker: ORBIT-731.",
    }


@pytest.mark.asyncio
async def test_openai_compatible_http_transport_surfaces_provider_status_errors(monkeypatch):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            429,
            json={"error": {"message": "rate limit", "type": "rate_limit_error"}},
            request=request,
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        return real_async_client(*args, transport=transport, **kwargs)

    monkeypatch.setattr("crows.provider.httpx.AsyncClient", client_factory)
    provider = ModelProvider(
        "https://provider.example/v1",
        "integration-test-key",
        "test-model",
    )

    with pytest.raises(httpx.HTTPStatusError) as error:
        await provider.chat("Hello")

    assert error.value.response.status_code == 429
    assert len(requests) == 1
    assert requests[0].url.path == "/v1/chat/completions"
