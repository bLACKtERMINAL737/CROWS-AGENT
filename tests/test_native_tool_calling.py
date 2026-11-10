import json

import httpx
import pytest

from crows.provider import ModelProvider, ModelRouter
from crows.tools import ToolError


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeAsyncClient:
    def __init__(self, responses, calls):
        self.responses = responses
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url, *, headers, json):
        self.calls.append({"url": url, "headers": headers, "payload": json})
        if not self.responses:
            raise AssertionError("Unexpected extra provider request")
        return FakeResponse(self.responses.pop(0))


def completion(content=None, tool_calls=None):
    return {"choices": [{"message": {"role": "assistant", "content": content, "tool_calls": tool_calls or []}}]}


def tool_call(call_id, name, arguments):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


@pytest.mark.asyncio
async def test_native_tool_call_sends_result_back_to_model(monkeypatch):
    calls = []
    responses = [
        completion(tool_calls=[tool_call("call-1", "workspace.list_files", {"path": "."})]),
        completion(content="I found two files."),
    ]

    def client_factory(*_args, **_kwargs):
        return FakeAsyncClient(responses, calls)

    monkeypatch.setattr("crows.provider.httpx.AsyncClient", client_factory)
    provider = ModelProvider("https://example.test/v1", "secret", "test-model")
    executed = []

    async def execute(name, args):
        executed.append((name, args))
        return {"path": ".", "entries": [{"name": "a.py", "type": "file"}]}

    specs = [{
        "type": "function",
        "function": {
            "name": "workspace.list_files",
            "description": "List workspace files",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    }]
    result = await provider.chat_with_tools(
        "List the files", tool_specs=specs, execute_tool=execute, max_tool_calls=5
    )

    assert result == {"answer": "I found two files.", "tool_calls": 1}
    assert executed == [("workspace.list_files", {"path": "."})]
    second_messages = calls[1]["payload"]["messages"]
    assert any(message.get("role") == "tool" and message.get("tool_call_id") == "call-1"
               for message in second_messages)
    assert "tools" in calls[1]["payload"]  # Still below the per-request cap.


@pytest.mark.asyncio
async def test_native_tool_call_cap_is_hard_limited_to_five(monkeypatch):
    calls = []
    many_calls = [tool_call(f"call-{i}", "workspace.list_files", {"path": "."}) for i in range(8)]
    responses = [
        completion(tool_calls=many_calls),
        completion(content="Stopped after the allowed tools."),
    ]

    def client_factory(*_args, **_kwargs):
        return FakeAsyncClient(responses, calls)

    monkeypatch.setattr("crows.provider.httpx.AsyncClient", client_factory)
    provider = ModelProvider("https://example.test/v1", "secret", "test-model")
    executed = []

    async def execute(name, args):
        executed.append((name, args))
        return {"entries": []}

    result = await provider.chat_with_tools(
        "List files", tool_specs=[{"type": "function", "function": {"name": "workspace.list_files"}}],
        execute_tool=execute, max_tool_calls=500,
    )

    assert len(executed) == 5
    assert result["tool_calls"] == 5
    assert len(calls) == 2
    assert "tools" not in calls[1]["payload"]


@pytest.mark.asyncio
async def test_router_does_not_fall_back_after_tool_execution_attempt():
    class ToolProvider(ModelProvider):
        async def chat_with_tools(self, *args, **kwargs):
            raise httpx.ConnectError("connection dropped after tool execution")

    primary = ToolProvider("https://example.test/v1", "secret", "primary")
    fallback = ModelProvider("https://fallback.test/v1", "secret", "fallback")
    router = ModelRouter(primary, fallback=fallback, max_attempts=2)

    with pytest.raises(httpx.ConnectError):
        await router.chat_with_tools(
            "run tool",
            tool_specs=[],
            execute_tool=None,
            execution_counter=lambda: True,
        )


@pytest.mark.asyncio
async def test_explicitly_named_tool_is_forced_for_first_request(monkeypatch):
    calls = []
    file_contents = "Secret test marker: CLAW-7319\nSecond line: FILE-READ-SUCCESS"
    responses = [
        completion(tool_calls=[tool_call(
            "call-read", "workspace__read_file", {"path": ".claw-live-test.txt"}
        )]),
        completion(content=file_contents),
    ]

    def client_factory(*_args, **_kwargs):
        return FakeAsyncClient(responses, calls)

    monkeypatch.setattr("crows.provider.httpx.AsyncClient", client_factory)
    provider = ModelProvider("https://example.test/v1", "secret", "test-model")
    executed = []

    async def execute(name, args):
        executed.append((name, args))
        return {"path": args["path"], "content": file_contents}

    specs = [
        {"type": "function", "function": {"name": "workspace__list_files", "parameters": {"type": "object"}}},
        {"type": "function", "function": {"name": "workspace__search_text", "parameters": {"type": "object"}}},
        {"type": "function", "function": {"name": "workspace__read_file", "parameters": {"type": "object"}}},
    ]
    result = await provider.chat_with_tools(
        'Use workspace.read_file to read ".claw-live-test.txt".',
        tool_specs=specs, execute_tool=execute,
    )

    assert calls[0]["payload"]["tool_choice"] == {
        "type": "function", "function": {"name": "workspace__read_file"}
    }
    assert executed == [("workspace__read_file", {"path": ".claw-live-test.txt"})]
    assert result["tool_calls"] == 1
    assert calls[1]["payload"]["tool_choice"] == "auto"


@pytest.mark.parametrize("message", [
    "Don't use workspace.search_text; use workspace.read_file to read notes.txt.",
    "Avoid calling `workspace.search_text`; use workspace.read_file to read notes.txt.",
    "Never invoke workspace.search_text. Use workspace.read_file to read notes.txt.",
])
@pytest.mark.asyncio
async def test_negated_tool_mention_does_not_override_requested_tool(monkeypatch, message):
    calls = []
    responses = [
        completion(tool_calls=[tool_call(
            "call-read", "workspace__read_file", {"path": "notes.txt"}
        )]),
        completion(content="The file contains the requested notes."),
    ]

    def client_factory(*_args, **_kwargs):
        return FakeAsyncClient(responses, calls)

    monkeypatch.setattr("crows.provider.httpx.AsyncClient", client_factory)
    provider = ModelProvider("https://example.test/v1", "secret", "test-model")
    executed = []

    async def execute(name, args):
        executed.append((name, args))
        return {"path": args["path"], "content": "actual notes"}

    # Deliberately put the negated tool first to catch order-dependent selection.
    specs = [
        {"type": "function", "function": {"name": "workspace__search_text", "parameters": {"type": "object"}}},
        {"type": "function", "function": {"name": "workspace__read_file", "parameters": {"type": "object"}}},
        {"type": "function", "function": {"name": "workspace__list_files", "parameters": {"type": "object"}}},
    ]
    result = await provider.chat_with_tools(
        message,
        tool_specs=specs, execute_tool=execute,
    )

    assert calls[0]["payload"]["tool_choice"] == {
        "type": "function", "function": {"name": "workspace__read_file"}
    }
    assert executed == [("workspace__read_file", {"path": "notes.txt"})]
    assert result["tool_calls"] == 1

@pytest.mark.asyncio
async def test_duplicate_tool_call_ids_are_rejected_before_execution(monkeypatch):
    calls = []
    responses = [
        completion(tool_calls=[
            tool_call("duplicate-id", "workspace.list_files", {"path": "."}),
            tool_call("duplicate-id", "workspace.list_files", {"path": "."}),
        ]),
    ]

    def client_factory(*_args, **_kwargs):
        return FakeAsyncClient(responses, calls)

    monkeypatch.setattr("crows.provider.httpx.AsyncClient", client_factory)
    provider = ModelProvider("https://example.test/v1", "secret", "test-model")
    executed = []

    async def execute(name, args):
        executed.append((name, args))
        return {"entries": []}

    specs = [{
        "type": "function",
        "function": {
            "name": "workspace.list_files",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    }]

    with pytest.raises(RuntimeError, match="duplicate tool-call IDs"):
        await provider.chat_with_tools(
            "List files", tool_specs=specs, execute_tool=execute,
        )

    assert executed == []



@pytest.mark.asyncio
@pytest.mark.parametrize("web_tool_name", ["web.search", "web__search"])
async def test_live_search_guidance_recognizes_provider_tool_name_variants(
    monkeypatch, web_tool_name
):
    calls = []
    responses = [completion(content="Search guidance was included.")]

    def client_factory(*_args, **_kwargs):
        return FakeAsyncClient(responses, calls)

    monkeypatch.setattr("crows.provider.httpx.AsyncClient", client_factory)
    provider = ModelProvider("https://example.test/v1", "secret", "test-model")
    result = await provider.chat_with_tools(
        "What is the latest update?",
        tool_specs=[{
            "type": "function",
            "function": {"name": web_tool_name, "parameters": {"type": "object"}},
        }],
        execute_tool=lambda *_args: None,
    )

    system_message = calls[0]["payload"]["messages"][0]["content"]
    assert "Live-search response standard" in system_message
    assert result["answer"] == "Search guidance was included."


@pytest.mark.asyncio
async def test_unregistered_tool_call_is_rejected_before_any_execution(monkeypatch):
    calls = []
    responses = [completion(tool_calls=[
        tool_call("call-valid", "workspace.list_files", {"path": "."}),
        tool_call("call-unknown", "workspace.delete_everything", {"path": "."}),
    ])]

    def client_factory(*_args, **_kwargs):
        return FakeAsyncClient(responses, calls)

    monkeypatch.setattr("crows.provider.httpx.AsyncClient", client_factory)
    provider = ModelProvider("https://example.test/v1", "secret", "test-model")
    executed = []

    async def execute(name, args):
        executed.append((name, args))
        return {"entries": []}

    specs = [{
        "type": "function",
        "function": {
            "name": "workspace.list_files",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    }]

    with pytest.raises(RuntimeError, match="unregistered tool name"):
        await provider.chat_with_tools(
            "List files", tool_specs=specs, execute_tool=execute,
        )

    assert executed == []


@pytest.mark.asyncio
async def test_tool_exception_details_are_not_sent_back_to_model(monkeypatch):
    calls = []
    responses = [
        completion(tool_calls=[tool_call("call-1", "workspace.list_files", {"path": "."})]),
        completion(content="The tool failed."),
    ]

    def client_factory(*_args, **_kwargs):
        return FakeAsyncClient(responses, calls)

    monkeypatch.setattr("crows.provider.httpx.AsyncClient", client_factory)
    provider = ModelProvider("https://example.test/v1", "secret", "test-model")
    secret_detail = "private-token-at-/Users/example/private"
    
    async def execute(_name, _args):
        raise RuntimeError(secret_detail)

    result = await provider.chat_with_tools(
        "List files",
        tool_specs=[{
            "type": "function",
            "function": {
                "name": "workspace.list_files",
                "parameters": {"type": "object", "properties": {}},
            },
        }],
        execute_tool=execute,
    )

    tool_result = next(
        message["content"]
        for message in calls[1]["payload"]["messages"]
        if message.get("role") == "tool"
    )
    assert secret_detail not in tool_result
    assert "Tool execution failed (RuntimeError)." in tool_result
    assert result["answer"] == "The tool failed."


@pytest.mark.asyncio
async def test_expected_tool_error_is_actionable_but_redacted_before_model(monkeypatch):
    calls = []
    responses = [
        completion(tool_calls=[tool_call("call-expected", "workspace.read_file", {"path": "notes.txt"})]),
        completion(content="The file could not be read.") ,
    ]

    def client_factory(*_args, **_kwargs):
        return FakeAsyncClient(responses, calls)

    monkeypatch.setattr("crows.provider.httpx.AsyncClient", client_factory)
    provider = ModelProvider("https://example.test/v1", "secret", "test-model")

    async def execute(_name, _args):
        raise ToolError(
            "File not found: /Users/alice/private/notes.txt; api_key=sk-or-v1-123456789abcdef"
        )

    result = await provider.chat_with_tools(
        "Read notes.txt",
        tool_specs=[{
            "type": "function",
            "function": {"name": "workspace.read_file", "parameters": {"type": "object"}},
        }],
        execute_tool=execute,
    )

    tool_result = next(
        json.loads(message["content"])
        for message in calls[1]["payload"]["messages"]
        if message.get("role") == "tool"
    )
    assert "File not found" in tool_result["error"]
    assert "[REDACTED_PATH]" in tool_result["error"]
    assert "[REDACTED_CREDENTIAL]" in tool_result["error"]
    assert "/Users/alice" not in tool_result["error"]
    assert "sk-or-v1-123456789abcdef" not in tool_result["error"]
    assert result["answer"] == "The file could not be read."


@pytest.mark.parametrize("raw_arguments", ["{not-json", '["not", "an", "object"]', "null"])
@pytest.mark.asyncio
async def test_invalid_tool_arguments_are_returned_without_execution(monkeypatch, raw_arguments):
    calls = []
    responses = [
        completion(tool_calls=[{
            "id": "call-invalid",
            "type": "function",
            "function": {
                "name": "workspace.list_files",
                "arguments": raw_arguments,
            },
        }]),
        completion(content="The arguments were invalid."),
    ]

    def client_factory(*_args, **_kwargs):
        return FakeAsyncClient(responses, calls)

    monkeypatch.setattr("crows.provider.httpx.AsyncClient", client_factory)
    provider = ModelProvider("https://example.test/v1", "secret", "test-model")
    executed = []

    async def execute(name, args):
        executed.append((name, args))
        return {"entries": []}

    result = await provider.chat_with_tools(
        "List files",
        tool_specs=[{
            "type": "function",
            "function": {
                "name": "workspace.list_files",
                "parameters": {"type": "object", "properties": {}},
            },
        }],
        execute_tool=execute,
    )

    assert executed == []
    tool_message = next(
        message
        for message in calls[1]["payload"]["messages"]
        if message.get("role") == "tool"
    )
    assert json.loads(tool_message["content"]) == {
        "error": "Invalid tool arguments; expected a JSON object."
    }
    assert result["answer"] == "The arguments were invalid."


@pytest.mark.asyncio
async def test_oversized_tool_result_is_replaced_with_valid_json(monkeypatch):
    calls = []
    responses = [
        completion(tool_calls=[
            tool_call("call-large", "workspace.read_file", {"path": "large.txt"})
        ]),
        completion(content="The result was too large to include safely."),
    ]

    def client_factory(*_args, **_kwargs):
        return FakeAsyncClient(responses, calls)

    monkeypatch.setattr("crows.provider.httpx.AsyncClient", client_factory)
    provider = ModelProvider("https://example.test/v1", "secret", "test-model")

    async def execute(_name, _args):
        return {"content": "x" * 20_000}

    result = await provider.chat_with_tools(
        "Read large.txt",
        tool_specs=[{
            "type": "function",
            "function": {
                "name": "workspace.read_file",
                "parameters": {"type": "object", "properties": {}},
            },
        }],
        execute_tool=execute,
    )

    tool_message = next(
        message
        for message in calls[1]["payload"]["messages"]
        if message.get("role") == "tool"
    )
    parsed = json.loads(tool_message["content"])
    assert parsed == {
        "error": "Tool result exceeded the response size limit and was omitted.",
        "truncated": True,
    }
    assert len(tool_message["content"]) < 12_000
    assert result["answer"] == "The result was too large to include safely."


def test_tool_result_serialization_failure_returns_safe_json():
    from crows.provider import _tool_result_message

    cyclic = {}
    cyclic["self"] = cyclic

    assert json.loads(_tool_result_message(cyclic)) == {
        "error": "Tool result could not be serialized safely."
    }


@pytest.mark.asyncio
async def test_tool_failure_context_is_sent_back_to_model(monkeypatch):
    calls = []
    responses = [
        completion(tool_calls=[tool_call(
            "call-fail", "workspace.read_file", {"path": "missing.txt"}
        )]),
        completion(content="The file could not be read because the tool reported an error."),
    ]

    def client_factory(*_args, **_kwargs):
        return FakeAsyncClient(responses, calls)

    monkeypatch.setattr("crows.provider.httpx.AsyncClient", client_factory)
    provider = ModelProvider("https://example.test/v1", "secret", "test-model")

    async def execute(_name, _args):
        raise FileNotFoundError("do not expose /private/local/path")

    specs = [{
        "type": "function",
        "function": {
            "name": "workspace.read_file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    }]
    result = await provider.chat_with_tools(
        "Read missing.txt", tool_specs=specs, execute_tool=execute
    )

    tool_message = next(
        message for message in calls[1]["payload"]["messages"]
        if message.get("role") == "tool"
    )
    payload = json.loads(tool_message["content"])
    assert payload == {
        "error": "Tool execution failed (FileNotFoundError).",
        "tool": "workspace.read_file",
        "retryable": False,
    }
    assert "/private/local/path" not in tool_message["content"]
    assert result["answer"] == "The file could not be read because the tool reported an error."


@pytest.mark.asyncio
async def test_native_chat_sends_image_as_openai_compatible_multimodal_content(monkeypatch):
    calls = []
    responses = [completion(content="The image shows a red bicycle.")]

    def client_factory(*_args, **_kwargs):
        return FakeAsyncClient(responses, calls)

    monkeypatch.setattr("crows.provider.httpx.AsyncClient", client_factory)
    provider = ModelProvider("https://example.test/v1", "secret", "vision-model")
    image_data_url = "data:image/png;base64,aGVsbG8="

    result = await provider.chat_with_tools(
        "Describe this image",
        image_data_url=image_data_url,
    )

    assert result["answer"] == "The image shows a red bicycle."
    user_message = calls[0]["payload"]["messages"][-1]
    assert user_message["role"] == "user"
    assert user_message["content"] == [
        {"type": "text", "text": "Describe this image"},
        {"type": "image_url", "image_url": {"url": image_data_url}},
    ]


@pytest.mark.asyncio
async def test_oversized_tool_arguments_are_rejected_before_execution(monkeypatch):
    calls = []
    responses = [
        completion(tool_calls=[{
            "id": "call-huge",
            "type": "function",
            "function": {
                "name": "workspace.read_file",
                "arguments": json.dumps({"path": "x" * (64 * 1024)}),
            },
        }])
    ]

    def client_factory(*_args, **_kwargs):
        return FakeAsyncClient(responses, calls)

    monkeypatch.setattr("crows.provider.httpx.AsyncClient", client_factory)
    provider = ModelProvider("https://example.test/v1", "secret", "test-model")
    executed = []

    async def execute(name, args):
        executed.append((name, args))
        return {"ok": True}

    specs = [{
        "type": "function",
        "function": {
            "name": "workspace.read_file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    }]
    with pytest.raises(RuntimeError, match="tool arguments exceeded the size limit"):
        await provider.chat_with_tools("Read a file", tool_specs=specs, execute_tool=execute)
    assert executed == []


class InvalidJsonResponse:
    def raise_for_status(self):
        return None

    def json(self):
        raise ValueError("provider sent invalid JSON")


class InvalidJsonClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, *_args, **_kwargs):
        return InvalidJsonResponse()


@pytest.mark.asyncio
async def test_native_tool_chat_normalizes_invalid_provider_json(monkeypatch):
    monkeypatch.setattr("crows.provider.httpx.AsyncClient", lambda *_a, **_kw: InvalidJsonClient())
    provider = ModelProvider("https://example.test/v1", "secret", "test-model")

    with pytest.raises(RuntimeError, match="Provider returned invalid JSON"):
        await provider.chat_with_tools("hello")


@pytest.mark.asyncio
async def test_regular_chat_normalizes_invalid_provider_json(monkeypatch):
    monkeypatch.setattr("crows.provider.httpx.AsyncClient", lambda *_a, **_kw: InvalidJsonClient())
    provider = ModelProvider("https://example.test/v1", "secret", "test-model")

    with pytest.raises(RuntimeError, match="Provider returned invalid JSON"):
        await provider.chat("hello")


@pytest.mark.asyncio
async def test_invalid_provider_json_error_helper_contract():
    from crows.provider import _decode_provider_json

    class BadJsonResponse:
        def json(self):
            raise ValueError("not json")

    with pytest.raises(RuntimeError, match="Provider returned invalid JSON"):
        _decode_provider_json(BadJsonResponse())


@pytest.mark.asyncio
async def test_regular_chat_rejects_unexpected_provider_response_shape(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "crows.provider.httpx.AsyncClient",
        lambda *_a, **_kw: FakeAsyncClient([{}], calls),
    )
    provider = ModelProvider("https://example.test/v1", "secret", "test-model")

    with pytest.raises(RuntimeError, match="unexpected chat completion response"):
        await provider.chat("hello")


@pytest.mark.asyncio
async def test_tool_chat_rejects_unexpected_provider_response_shape(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "crows.provider.httpx.AsyncClient",
        lambda *_a, **_kw: FakeAsyncClient([{}], calls),
    )
    provider = ModelProvider("https://example.test/v1", "secret", "test-model")

    with pytest.raises(RuntimeError, match="unexpected chat completion response"):
        await provider.chat_with_tools("hello")


@pytest.mark.asyncio
async def test_tool_execution_exception_is_reported_without_reexecuting(monkeypatch):
    calls = []
    responses = [
        completion(tool_calls=[{
            "id": "call-fails",
            "type": "function",
            "function": {"name": "workspace.read_file", "arguments": "{}"},
        }]),
        completion(content="The file tool failed."),
    ]
    monkeypatch.setattr(
        "crows.provider.httpx.AsyncClient",
        lambda *_a, **_kw: FakeAsyncClient(responses, calls),
    )
    provider = ModelProvider("https://example.test/v1", "secret", "test-model")
    executions = 0

    async def execute(name, args):
        nonlocal executions
        executions += 1
        raise OSError("sensitive local path /private/secret.txt")

    specs = [{
        "type": "function",
        "function": {
            "name": "workspace.read_file",
            "parameters": {"type": "object", "properties": {}},
        },
    }]
    result = await provider.chat_with_tools(
        "Read the file", tool_specs=specs, execute_tool=execute,
    )

    assert executions == 1
    assert result["answer"] == "The file tool failed."
    tool_messages = [
        message for message in calls[1]["payload"]["messages"]
        if message.get("role") == "tool"
    ]
    assert len(tool_messages) == 1
    assert "Tool execution failed (OSError)." in tool_messages[0]["content"]
    assert "/private/secret.txt" not in tool_messages[0]["content"]


@pytest.mark.asyncio
async def test_unserializable_tool_result_is_replaced_with_safe_json():
    from crows.provider import _tool_result_message

    recursive = {}
    recursive["self"] = recursive
    result = _tool_result_message({"result": recursive})
    assert "could not be serialized safely" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    None,
    [],
    {},
    {"choices": []},
    {"choices": {}},
    {"choices": [None]},
    {"choices": [{"message": None}]},
    {"choices": [{"message": {"content": None}}]},
])
async def test_regular_chat_rejects_malformed_completion_shapes(monkeypatch, payload):
    class ShapeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class ShapeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return ShapeResponse()

    monkeypatch.setattr(
        "crows.provider.httpx.AsyncClient",
        lambda *_args, **_kwargs: ShapeClient(),
    )
    provider = ModelProvider("https://example.test/v1", "secret", "test-model")

    with pytest.raises(RuntimeError, match="unexpected chat completion response"):
        await provider.chat("hello")


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    None,
    [],
    {},
    {"choices": []},
    {"choices": {}},
    {"choices": [None]},
    {"choices": [{"message": None}]},
])
async def test_tool_chat_rejects_malformed_completion_envelopes(monkeypatch, payload):
    class ShapeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class ShapeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return ShapeResponse()

    monkeypatch.setattr(
        "crows.provider.httpx.AsyncClient",
        lambda *_args, **_kwargs: ShapeClient(),
    )
    provider = ModelProvider("https://example.test/v1", "secret", "test-model")

    with pytest.raises(RuntimeError, match="unexpected chat completion response"):
        await provider.chat_with_tools("hello")


@pytest.mark.asyncio
async def test_provider_tool_call_batch_over_five_is_rejected_before_execution(monkeypatch):
    calls = []
    tool_calls = [
        tool_call(f"call-{i}", "workspace.list_files", {"path": "."})
        for i in range(6)
    ]
    responses = [completion(tool_calls=tool_calls), completion(content="Stopped safely.")]
    monkeypatch.setattr(
        "crows.provider.httpx.AsyncClient",
        lambda *_a, **_kw: FakeAsyncClient(responses, calls),
    )
    provider = ModelProvider("https://example.test/v1", "secret", "test-model")
    executed = []

    async def execute(name, args):
        executed.append((name, args))
        return {"ok": True}

    specs = [{
        "type": "function",
        "function": {
            "name": "workspace.list_files",
            "parameters": {"type": "object", "properties": {}},
        },
    }]
    result = await provider.chat_with_tools("List files", tool_specs=specs, execute_tool=execute)
    assert result["tool_calls"] == 5
    assert len(executed) == 5
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_deeply_nested_tool_arguments_fail_safely(monkeypatch):
    calls = []
    nested = '{"value":' + ("[" * 2500) + "0" + ("]" * 2500) + "}"
    response = completion(tool_calls=[{
        "id": "call-deep",
        "type": "function",
        "function": {"name": "workspace.list_files", "arguments": nested},
    }])
    responses = [response, completion(content="Invalid arguments.")]
    monkeypatch.setattr(
        "crows.provider.httpx.AsyncClient",
        lambda *_a, **_kw: FakeAsyncClient(responses, calls),
    )
    provider = ModelProvider("https://example.test/v1", "secret", "test-model")
    executed = []

    async def execute(name, args):
        executed.append((name, args))
        return {"ok": True}

    specs = [{
        "type": "function",
        "function": {
            "name": "workspace.list_files",
            "parameters": {"type": "object", "properties": {}},
        },
    }]
    result = await provider.chat_with_tools(
        "List files", tool_specs=specs, execute_tool=execute,
    )
    assert result["tool_calls"] == 0
    assert executed == []



@pytest.mark.asyncio
async def test_oversized_tool_call_id_is_rejected_before_execution(monkeypatch):
    calls = []
    response = completion(tool_calls=[
        tool_call("x" * 257, "workspace.list_files", {"path": "."}),
    ])
    monkeypatch.setattr(
        "crows.provider.httpx.AsyncClient",
        lambda *_a, **_kw: FakeAsyncClient([response], calls),
    )
    provider = ModelProvider("https://example.test/v1", "secret", "test-model")
    executed = []

    async def execute(name, args):
        executed.append((name, args))
        return {"ok": True}

    specs = [{
        "type": "function",
        "function": {
            "name": "workspace.list_files",
            "parameters": {"type": "object", "properties": {}},
        },
    }]
    with pytest.raises(RuntimeError, match="malformed tool call"):
        await provider.chat_with_tools("List files", tool_specs=specs, execute_tool=execute)
    assert executed == []


@pytest.mark.asyncio
async def test_lone_surrogate_tool_arguments_are_normalized_before_execution(monkeypatch):
    calls = []
    response = completion(tool_calls=[{
        "id": "call-surrogate",
        "type": "function",
        "function": {"name": "workspace.list_files", "arguments": '{"path":"\\ud800"}'},
    }])
    # Simulate a provider-decoded Python string containing a lone surrogate.
    response["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = '{"path":"' + chr(0xD800) + '"}'
    monkeypatch.setattr(
        "crows.provider.httpx.AsyncClient",
        lambda *_a, **_kw: FakeAsyncClient([response], calls),
    )
    provider = ModelProvider("https://example.test/v1", "secret", "test-model")
    executed = []

    async def execute(name, args):
        executed.append((name, args))
        return {"ok": True}

    specs = [{
        "type": "function",
        "function": {
            "name": "workspace.list_files",
            "parameters": {"type": "object", "properties": {}},
        },
    }]
    with pytest.raises(RuntimeError, match="malformed tool arguments"):
        await provider.chat_with_tools("List files", tool_specs=specs, execute_tool=execute)
    assert executed == []


@pytest.mark.asyncio
async def test_reused_tool_call_id_across_rounds_is_rejected_before_second_execution(
    monkeypatch,
):
    calls = []
    responses = [
        completion(tool_calls=[tool_call("stable-id", "workspace.list_files", {"path": "."})]),
        completion(tool_calls=[tool_call("stable-id", "workspace.list_files", {"path": "."})]),
    ]

    def client_factory(*_args, **_kwargs):
        return FakeAsyncClient(responses, calls)

    monkeypatch.setattr("crows.provider.httpx.AsyncClient", client_factory)
    provider = ModelProvider("https://example.test/v1", "secret", "test-model")
    executed = []

    async def execute(name, args):
        executed.append((name, args))
        return {"entries": []}

    specs = [{
        "type": "function",
        "function": {
            "name": "workspace.list_files",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    }]

    with pytest.raises(RuntimeError, match="reused a tool-call ID"):
        await provider.chat_with_tools(
            "List files", tool_specs=specs, execute_tool=execute, max_tool_calls=5
        )

    assert executed == [("workspace.list_files", {"path": "."})]
    assert len(calls) == 2
