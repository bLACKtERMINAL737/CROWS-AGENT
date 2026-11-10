import pytest

from crows import app as app_module
from crows.store import TaskStore
from crows.tools import ToolError


class ReadOnlyTools:
    def __init__(self):
        self.executions = []

    def describe(self):
        return [{
            "name": "workspace.read_file",
            "description": "Read a named workspace file.",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        }]

    def execute(self, name, arguments):
        self.executions.append((name, arguments))
        return {"path": arguments["path"], "content": "verified file content"}


class ChatRouter:
    def __init__(self):
        self.tool_error = None

    async def chat_with_tools(self, _message, _task_type, **kwargs):
        try:
            result = await kwargs["execute_tool"](
                "workspace__read_file", {"path": "notes.txt"}
            )
        except ToolError as exc:
            self.tool_error = str(exc)
            return {"answer": "I could not safely verify the file.", "tool_calls": 1}
        return {"answer": f"Read: {result['content']}", "tool_calls": 1}


def configure(monkeypatch, tmp_path):
    store = TaskStore(tmp_path / "chat-tool-journaling.sqlite3")
    tools = ReadOnlyTools()
    router = ChatRouter()
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "tools", tools)
    monkeypatch.setattr(app_module, "router", router)
    return store, tools, router


@pytest.mark.asyncio
async def test_chat_does_not_run_tool_if_journal_creation_fails(tmp_path, monkeypatch):
    store, tools, router = configure(monkeypatch, tmp_path)

    def fail_begin(*_args, **_kwargs):
        raise RuntimeError("simulated journal outage")

    monkeypatch.setattr(store, "begin_tool", fail_begin)

    result = await app_module.chat(
        app_module.ChatRequest(message="Read notes.txt")
    )

    assert "outcome may be uncertain" in router.tool_error
    assert tools.executions == []
    task = store.get_task(result["task_id"])
    assert task["status"] == "unknown"
    assert store.list_tool_executions(task["id"]) == []


@pytest.mark.asyncio
async def test_chat_marks_tool_unknown_if_success_result_cannot_be_journaled(
    tmp_path, monkeypatch
):
    store, tools, router = configure(monkeypatch, tmp_path)
    original_finish_tool = store.finish_tool

    def fail_success_write(execution_id, status, result=None, error=None):
        if status == "succeeded":
            raise RuntimeError("simulated result journal outage")
        return original_finish_tool(execution_id, status, result=result, error=error)

    monkeypatch.setattr(store, "finish_tool", fail_success_write)

    result = await app_module.chat(
        app_module.ChatRequest(message="Read notes.txt")
    )

    assert "could not be safely journaled" in router.tool_error
    assert tools.executions == [("workspace.read_file", {"path": "notes.txt"})]
    task = store.get_task(result["task_id"])
    assert task["status"] == "unknown"
    executions = store.list_tool_executions(task["id"])
    assert len(executions) == 1
    assert executions[0]["status"] == "unknown"

class FailingReadOnlyTools(ReadOnlyTools):
    def execute(self, name, arguments):
        self.executions.append((name, arguments))
        raise ToolError("simulated file read failure")


@pytest.mark.asyncio
async def test_chat_marks_failed_tool_unknown_if_failure_cannot_be_journaled(
    tmp_path, monkeypatch
):
    store, _tools, router = configure(monkeypatch, tmp_path)
    tools = FailingReadOnlyTools()
    monkeypatch.setattr(app_module, "tools", tools)
    original_finish_tool = store.finish_tool

    def fail_failure_write(execution_id, status, result=None, error=None):
        if status == "failed":
            raise RuntimeError("simulated failure journal outage")
        return original_finish_tool(execution_id, status, result=result, error=error)

    monkeypatch.setattr(store, "finish_tool", fail_failure_write)

    result = await app_module.chat(
        app_module.ChatRequest(message="Read notes.txt")
    )

    assert "could not be safely journaled" in router.tool_error
    assert tools.executions == [("workspace.read_file", {"path": "notes.txt"})]
    task = store.get_task(result["task_id"])
    assert task["status"] == "unknown"
    executions = store.list_tool_executions(task["id"])
    assert len(executions) == 1
    assert executions[0]["status"] == "unknown"
