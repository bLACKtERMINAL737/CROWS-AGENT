import pytest

import crows.provider as provider_module
from crows.provider import ModelProvider


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}


class FakeAsyncClient:
    last_request = None

    def __init__(self, timeout=None):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, url, *, headers, json):
        type(self).last_request = {"url": url, "headers": headers, "payload": json}
        return FakeResponse()


def tool_spec(name):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "test tool",
            "parameters": {"type": "object", "properties": {}},
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_names",
    [
        ["workspace.read_file", "workspace.list_files", "workspace.search_text"],
        ["workspace__read_file", "workspace__list_files", "workspace__search_text"],
    ],
)
async def test_workspace_tool_guidance_is_sent_when_workspace_tools_are_available(
    monkeypatch, tool_names
):
    monkeypatch.setattr(provider_module.httpx, "AsyncClient", FakeAsyncClient)
    provider = ModelProvider("https://example.test/v1", "test-key", "test-model")

    async def execute(_name, _args):
        return {}

    await provider.chat_with_tools(
        "Read notes.txt",
        tool_specs=[tool_spec(name) for name in tool_names],
        execute_tool=execute,
    )

    payload = FakeAsyncClient.last_request["payload"]
    system_prompt = payload["messages"][0]["content"]
    assert "Workspace tool selection rules" in system_prompt
    assert "call workspace.read_file" in system_prompt
    assert "Do not use workspace.list_files or workspace.search_text as substitutes" in system_prompt
    assert "report the actual tool error and do not guess file contents" in system_prompt


@pytest.mark.asyncio
async def test_workspace_guidance_is_not_added_without_workspace_tools(monkeypatch):
    monkeypatch.setattr(provider_module.httpx, "AsyncClient", FakeAsyncClient)
    provider = ModelProvider("https://example.test/v1", "test-key", "test-model")

    async def execute(_name, _args):
        return {}

    await provider.chat_with_tools(
        "Search the web",
        tool_specs=[tool_spec("web.search")],
        execute_tool=execute,
    )

    system_prompt = FakeAsyncClient.last_request["payload"]["messages"][0]["content"]
    assert "Workspace tool selection rules" not in system_prompt


@pytest.mark.asyncio
async def test_workspace_guidance_is_not_added_when_no_tools_are_supplied(monkeypatch):
    monkeypatch.setattr(provider_module.httpx, "AsyncClient", FakeAsyncClient)
    provider = ModelProvider("https://example.test/v1", "test-key", "test-model")

    await provider.chat_with_tools("Hello")

    system_prompt = FakeAsyncClient.last_request["payload"]["messages"][0]["content"]
    assert "Workspace tool selection rules" not in system_prompt


from crows.tools import ToolError, ToolRegistry


def test_workspace_tool_descriptions_distinguish_read_list_and_search(tmp_path):
    registry = ToolRegistry(tmp_path)
    descriptions = {tool["name"]: tool["description"] for tool in registry.describe()}

    assert "do not use this tool to read a file's contents" in descriptions["workspace.list_files"]
    assert "not for reading a known file" in descriptions["workspace.search_text"]
    assert "READ A SPECIFIC FILE" in descriptions["workspace.read_file"]
    assert "Do not substitute" in descriptions["workspace.read_file"]


def test_workspace_read_file_returns_actual_content_for_known_path(tmp_path):
    target = tmp_path / "known.txt"
    target.write_text("ALPHA-MARKER\nBETA-MARKER\n", encoding="utf-8")
    registry = ToolRegistry(tmp_path)

    result = registry.execute("workspace.read_file", {"path": "known.txt"})

    assert result["path"] == "known.txt"
    assert result["content"] == "ALPHA-MARKER\nBETA-MARKER"
    assert result["total_lines"] == 2
    assert result["truncated"] is False


def test_workspace_read_file_reports_missing_path_without_fallback(tmp_path):
    registry = ToolRegistry(tmp_path)

    with pytest.raises(ToolError, match="File does not exist"):
        registry.execute("workspace.read_file", {"path": "missing-unique-file.txt"})


@pytest.mark.parametrize(
    ("tool_name", "payload", "error"),
    [
        ("workspace.read_file", {"path": "folder"}, "Path must be a file"),
        ("workspace.list_files", {"path": "known.txt"}, "Path must be a directory"),
        ("workspace.search_text", {"path": "known.txt", "query": "marker"},
         "Search path must be an existing directory"),
    ],
)
def test_workspace_tools_reject_wrong_path_kind(tmp_path, tool_name, payload, error):
    (tmp_path / "folder").mkdir()
    (tmp_path / "known.txt").write_text("marker", encoding="utf-8")
    registry = ToolRegistry(tmp_path)

    with pytest.raises(ToolError, match=error):
        registry.execute(tool_name, payload)
