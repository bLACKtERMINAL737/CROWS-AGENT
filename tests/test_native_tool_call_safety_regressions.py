import pytest

from crows.provider import ModelProvider


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class Client:
    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, *args, **kwargs):
        return Response(self.payload)


@pytest.mark.asyncio
async def test_duplicate_tool_call_ids_rejected_before_execution(monkeypatch):
    payload = {"choices": [{"message": {"content": None, "tool_calls": [
        {"id": "same", "function": {"name": "read_file", "arguments": "{}"}},
        {"id": "same", "function": {"name": "list_files", "arguments": "{}"}},
    ]}}]}
    monkeypatch.setattr("crows.provider.httpx.AsyncClient", lambda *a, **k: Client(payload))
    provider = ModelProvider("https://example.test/v1", "secret", "model")
    executed = []

    async def execute(name, args):
        executed.append(name)
        return {"ok": True}

    specs = [{"type": "function", "function": {"name": name}}
             for name in ("read_file", "list_files")]
    with pytest.raises(RuntimeError, match="duplicate tool-call IDs"):
        await provider.chat_with_tools("do work", tool_specs=specs, execute_tool=execute)
    assert executed == []


@pytest.mark.asyncio
async def test_unregistered_tool_rejected_before_execution(monkeypatch):
    payload = {"choices": [{"message": {"content": None, "tool_calls": [
        {"id": "known", "function": {"name": "read_file", "arguments": "{}"}},
        {"id": "unknown", "function": {"name": "unregistered", "arguments": "{}"}},
    ]}}]}
    monkeypatch.setattr("crows.provider.httpx.AsyncClient", lambda *a, **k: Client(payload))
    provider = ModelProvider("https://example.test/v1", "secret", "model")
    executed = []

    async def execute(name, args):
        executed.append(name)
        return {"ok": True}

    specs = [{"type": "function", "function": {"name": "read_file"}}]
    with pytest.raises(RuntimeError, match="unregistered tool name"):
        await provider.chat_with_tools("do work", tool_specs=specs, execute_tool=execute)
    assert executed == []
