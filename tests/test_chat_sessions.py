import pytest
from fastapi import HTTPException

from crows import app as app_module
from crows.store import TaskStore


class CapturingRouter:
    def __init__(self, answer="A concise answer"):
        self.answer = answer
        self.calls = []

    async def chat(self, message, task_type, *, history, memories, model_route=None, allow_fallback=True):
        self.calls.append({
            "message": message,
            "task_type": task_type,
            "history": history,
            "memories": memories,
        })
        return {"answer": self.answer}


@pytest.mark.asyncio
async def test_follow_up_chat_receives_saved_session_history_and_appends_turn(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "chat-session.sqlite3")
    session = store.create_session("Existing conversation")
    store.add_session_message(session["id"], "user", "Earlier question")
    store.add_session_message(session["id"], "assistant", "Earlier answer")
    router = CapturingRouter(answer="Follow-up answer")
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "router", router)

    result = await app_module.chat(
        app_module.ChatRequest(
            message="Please continue",
            mode="manual",
            task_type="reasoning",
            model_route="primary",
            session_id=session["id"],
        )
    )

    assert result["session_id"] == session["id"]
    assert router.calls == [{
        "message": "Please continue",
        "task_type": "reasoning",
        "history": [
            {"role": "user", "content": "Earlier question"},
            {"role": "assistant", "content": "Earlier answer"},
        ],
        "memories": [],
    }]
    messages = store.list_session_messages(session["id"])
    assert [(item["role"], item["content"]) for item in messages] == [
        ("user", "Earlier question"),
        ("assistant", "Earlier answer"),
        ("user", "Please continue"),
        ("assistant", "Follow-up answer"),
    ]


@pytest.mark.asyncio
async def test_chat_does_not_retrieve_saved_memories_by_default(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "chat-memory-opt-out.sqlite3")
    store.add_memory("preference", "Use concise explanations")
    router = CapturingRouter()
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "router", router)

    result = await app_module.chat(app_module.ChatRequest(message="Explain this"))

    assert result["memory_context_used"] is False
    assert router.calls[0]["memories"] == []


@pytest.mark.asyncio
async def test_chat_rejects_unknown_session_before_calling_router(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "missing-session.sqlite3")
    router = CapturingRouter()
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "router", router)

    with pytest.raises(HTTPException) as exc_info:
        await app_module.chat(
            app_module.ChatRequest(
                message="Continue please",
                session_id="missing-session-id",
            )
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Conversation session not found"
    assert router.calls == []



def test_chat_turn_persistence_is_atomic_when_assistant_insert_fails(tmp_path):
    import sqlite3

    store = TaskStore(tmp_path / "atomic-chat-turn.sqlite3")
    session = store.create_session("New conversation")
    with store.connect() as con:
        con.execute("""
            CREATE TRIGGER fail_assistant_message
            BEFORE INSERT ON session_messages
            WHEN NEW.role = 'assistant'
            BEGIN
                SELECT RAISE(ABORT, 'forced assistant insert failure');
            END
        """)

    with pytest.raises(sqlite3.IntegrityError, match="forced assistant insert failure"):
        store.add_session_turn(session["id"], "user turn", "assistant turn")

    assert store.list_session_messages(session["id"]) == []
    assert store.get_session(session["id"])["title"] == "New conversation"


def test_chat_turn_persistence_saves_both_messages_and_sets_title(tmp_path):
    store = TaskStore(tmp_path / "atomic-chat-turn-success.sqlite3")
    session = store.create_session("New conversation")

    saved = store.add_session_turn(session["id"], "What is a workflow?", "A sequence of steps.")

    assert [(item["role"], item["content"]) for item in saved] == [
        ("user", "What is a workflow?"),
        ("assistant", "A sequence of steps."),
    ]
    assert store.get_session(session["id"])["title"] == "What is a workflow?"



@pytest.mark.asyncio
async def test_native_tool_task_is_failed_when_model_request_fails_after_tool(tmp_path, monkeypatch):
    import httpx

    store = TaskStore(tmp_path / "native-tool-model-failure.sqlite3")

    class ToolRouter:
        async def chat_with_tools(self, message, task_type, **kwargs):
            await kwargs["execute_tool"]("workspace.list_files", {"path": "."})
            raise httpx.ConnectError("provider disconnected after tool result")

    class ReadOnlyTools:
        def describe(self):
            return [{
                "name": "workspace.list_files",
                "description": "List workspace files",
                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
            }]

        def execute(self, name, arguments):
            assert name == "workspace.list_files"
            return {"entries": [{"name": "example.py", "type": "file"}]}

    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "router", ToolRouter())
    monkeypatch.setattr(app_module, "tools", ReadOnlyTools())

    with pytest.raises(HTTPException) as exc_info:
        await app_module.chat(app_module.ChatRequest(message="List files"))

    assert exc_info.value.status_code == 502
    tasks = store.list_tasks()
    assert len(tasks) == 1
    task = store.get_task(tasks[0]["id"])
    assert task["status"] == "failed"
    executions = store.list_tool_executions(task["id"])
    assert len(executions) == 1
    assert executions[0]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_auto_mode_preserves_explicit_coding_task_type(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "explicit-coding-task.sqlite3")
    router = CapturingRouter()
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "router", router)

    await app_module.chat(
        app_module.ChatRequest(
            message="Read a file and report its contents",
            mode="auto",
            task_type="coding",
        )
    )

    assert router.calls[0]["task_type"] == "coding"


def test_chat_request_rejects_empty_or_oversized_session_id():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        app_module.ChatRequest(message="Continue", session_id="")
    with pytest.raises(ValidationError):
        app_module.ChatRequest(message="Continue", session_id="s" * 201)


def test_chat_request_accepts_bounded_session_id():
    request = app_module.ChatRequest(message="Continue", session_id="session-123")
    assert request.session_id == "session-123"



def test_chat_rejects_whitespace_only_message_and_normalizes_valid_input():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="message must not be blank"):
        app_module.ChatRequest(message="  \n\t  ")

    request = app_module.ChatRequest(message="  explain this  ", session_id="  session-1  ")
    assert request.message == "explain this"
    assert request.session_id == "session-1"


def test_chat_rejects_whitespace_only_session_id():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="session_id must not be blank"):
        app_module.ChatRequest(message="Hello", session_id="   ")


@pytest.mark.asyncio
async def test_native_tool_task_is_unknown_when_chat_is_cancelled_after_tool(tmp_path, monkeypatch):
    import asyncio

    store = TaskStore(tmp_path / "native-tool-chat-cancelled.sqlite3")

    class ToolRouter:
        async def chat_with_tools(self, message, task_type, **kwargs):
            await kwargs["execute_tool"]("workspace.list_files", {"path": "."})
            raise asyncio.CancelledError()

    class ReadOnlyTools:
        def describe(self):
            return [{
                "name": "workspace.list_files",
                "description": "List workspace files",
                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
            }]

        def execute(self, name, arguments):
            assert name == "workspace.list_files"
            return {"entries": [{"name": "example.py", "type": "file"}]}

    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "router", ToolRouter())
    monkeypatch.setattr(app_module, "tools", ReadOnlyTools())

    with pytest.raises(asyncio.CancelledError):
        await app_module.chat(app_module.ChatRequest(message="List files"))

    tasks = store.list_tasks()
    assert len(tasks) == 1
    task = store.get_task(tasks[0]["id"])
    assert task["status"] == "unknown"
    assert "cancelled" in task["error_text"].lower()
    executions = store.list_tool_executions(task["id"])
    assert len(executions) == 1
    assert executions[0]["status"] == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_error", "expected_status"),
    [
        ("timeout", 504),
        ("connect", 502),
        ("http_status", 502),
    ],
)
async def test_native_tool_provider_failures_return_actionable_http_errors(
    tmp_path, monkeypatch, provider_error, expected_status
):
    import httpx

    store = TaskStore(tmp_path / f"native-tool-provider-{provider_error}.sqlite3")

    class ToolRouter:
        async def chat_with_tools(self, message, task_type, **kwargs):
            await kwargs["execute_tool"]("workspace.list_files", {"path": "."})
            if provider_error == "timeout":
                raise httpx.ReadTimeout("provider timeout")
            if provider_error == "connect":
                raise httpx.ConnectError("connection failed")
            request = httpx.Request("POST", "https://provider.example/v1/chat/completions")
            response = httpx.Response(503, request=request)
            raise httpx.HTTPStatusError("service unavailable", request=request, response=response)

    class ReadOnlyTools:
        def describe(self):
            return [{
                "name": "workspace.list_files",
                "description": "List workspace files",
                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
            }]

        def execute(self, name, arguments):
            return {"entries": [{"name": "example.py", "type": "file"}]}

    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "router", ToolRouter())
    monkeypatch.setattr(app_module, "tools", ReadOnlyTools())

    with pytest.raises(HTTPException) as exc_info:
        await app_module.chat(app_module.ChatRequest(message="List files"))

    assert exc_info.value.status_code == expected_status
    tasks = store.list_tasks()
    assert len(tasks) == 1
    task = store.get_task(tasks[0]["id"])
    assert task["status"] == "failed"
    executions = store.list_tool_executions(task["id"])
    assert len(executions) == 1
    assert executions[0]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_native_tool_timeout_warns_that_tool_actions_may_have_occurred(
    tmp_path, monkeypatch
):
    import httpx

    store = TaskStore(tmp_path / "native-tool-timeout-retry-guidance.sqlite3")

    class ToolRouter:
        async def chat_with_tools(self, message, task_type, **kwargs):
            await kwargs["execute_tool"]("workspace.list_files", {"path": "."})
            raise httpx.ReadTimeout("provider timeout after tool result")

    class ReadOnlyTools:
        def describe(self):
            return [{
                "name": "workspace.list_files",
                "description": "List workspace files",
                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
            }]

        def execute(self, name, arguments):
            return {"entries": [{"name": "example.py", "type": "file"}]}

    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "router", ToolRouter())
    monkeypatch.setattr(app_module, "tools", ReadOnlyTools())

    with pytest.raises(HTTPException) as exc_info:
        await app_module.chat(app_module.ChatRequest(message="List files"))

    assert exc_info.value.status_code == 504
    assert "tool actions may already have occurred" in exc_info.value.detail
    tasks = store.list_tasks()
    task = store.get_task(tasks[0]["id"])
    assert task["status"] == "failed"
    assert store.list_tool_executions(task["id"])[0]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_native_tool_cancellation_marks_execution_unknown(
    tmp_path, monkeypatch
):
    import asyncio

    store = TaskStore(tmp_path / "native-tool-execution-cancelled.sqlite3")

    class ToolRouter:
        async def chat_with_tools(self, message, task_type, **kwargs):
            await kwargs["execute_tool"]("workspace.list_files", {"path": "."})
            return {"answer": "done", "task_type": "general"}

    class CancellingTools:
        def describe(self):
            return [{
                "name": "workspace.list_files",
                "description": "List workspace files",
                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
            }]

        def execute(self, name, arguments):
            raise asyncio.CancelledError()

    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "router", ToolRouter())
    monkeypatch.setattr(app_module, "tools", CancellingTools())

    with pytest.raises(asyncio.CancelledError):
        await app_module.chat(app_module.ChatRequest(message="List files"))

    tasks = store.list_tasks()
    task = store.get_task(tasks[0]["id"])
    assert task["status"] == "unknown"
    executions = store.list_tool_executions(task["id"])
    assert len(executions) == 1
    assert executions[0]["status"] == "unknown"
    assert "cancelled" in executions[0]["error_text"].lower()


@pytest.mark.asyncio
async def test_native_tool_is_not_executed_when_journal_creation_fails(
    tmp_path, monkeypatch
):
    store = TaskStore(tmp_path / "native-tool-journal-create-failure.sqlite3")
    executed = []

    def fail_begin_tool(task_id, name, arguments):
        raise RuntimeError("simulated journal storage failure")

    store.begin_tool = fail_begin_tool

    class ToolRouter:
        async def chat_with_tools(self, message, task_type, **kwargs):
            await kwargs["execute_tool"]("workspace.list_files", {"path": "."})
            return {"answer": "done", "task_type": "general"}

    class ReadOnlyTools:
        def describe(self):
            return [{
                "name": "workspace.list_files",
                "description": "List workspace files",
                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
            }]

        def execute(self, name, arguments):
            executed.append((name, arguments))
            return {"entries": []}

    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "router", ToolRouter())
    monkeypatch.setattr(app_module, "tools", ReadOnlyTools())

    with pytest.raises(HTTPException):
        await app_module.chat(app_module.ChatRequest(message="List files"))

    assert executed == []
    tasks = store.list_tasks()
    assert len(tasks) == 1
    task = store.get_task(tasks[0]["id"])
    assert task["status"] == "unknown"
    assert store.list_tool_executions(task["id"]) == []
