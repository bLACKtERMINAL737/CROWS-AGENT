import importlib.util
import json
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "agent_eval.py"
SPEC = importlib.util.spec_from_file_location("agent_eval", MODULE_PATH)
agent_eval = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(agent_eval)


def test_load_cases_accepts_bounded_valid_case(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([{
        "name": "basic",
        "message": "Say READY",
        "expected_contains": ["READY"],
        "expected_tools": [],
        "min_tool_calls": 0,
    }]), encoding="utf-8")

    cases = agent_eval.load_cases(path)

    assert len(cases) == 1
    assert cases[0]["name"] == "basic"


@pytest.mark.parametrize("payload", [
    [],
    [{}],
    [{"name": "same", "message": "one"}, {"name": "same", "message": "two"}],
    [{"name": "bad", "message": "go", "min_tool_calls": True}],
    [{"name": "bad", "message": "go", "mode": "manual"}],
    [{"name": "bad", "message": "go", "expected_task_status": "maybe"}],
])
def test_load_cases_rejects_invalid_cases(tmp_path, payload):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError):
        agent_eval.load_cases(path)


def test_expected_tool_failure_can_pass_when_journal_confirms_it(monkeypatch):
    responses = iter([
        {
            "answer": "The file could not be read.",
            "task_id": "task-123",
            "tool_calls": 0,
        },
        {
            "status": "failed",
            "tool_executions": [
                {"tool_name": "workspace.read_file", "status": "failed"}
            ],
        },
    ])

    def fake_request(*_args, **_kwargs):
        return next(responses)

    monkeypatch.setattr(agent_eval, "_request_json", fake_request)
    case = {
        "name": "missing file",
        "message": "read missing file",
        "expected_contains": ["could not be read"],
        "expected_tools": ["workspace.read_file"],
        "min_tool_calls": 1,
        "expected_task_status": "failed",
    }

    result = agent_eval.run_case(
        case,
        base_url="http://127.0.0.1:8000",
        token="test-token",
        timeout=1,
    )

    assert result["passed"] is True
    assert result["task_status"] == "failed"
    assert result["tool_calls"] == 0
    assert result["tools_observed"] == ["workspace.read_file"]
    assert "message" not in result
    assert "answer" not in result


def test_unexpected_task_status_fails_evaluation(monkeypatch):
    responses = iter([
        {"answer": "I cannot read it.", "task_id": "task-456", "tool_calls": 0},
        {"status": "unknown", "tool_executions": []},
    ])
    monkeypatch.setattr(agent_eval, "_request_json", lambda *_a, **_kw: next(responses))
    case = {
        "name": "status mismatch",
        "message": "read a file",
        "expected_task_status": "succeeded",
    }

    result = agent_eval.run_case(
        case,
        base_url="http://127.0.0.1:8000",
        token="test-token",
        timeout=1,
    )

    assert result["passed"] is False
    assert "unexpected_task_status" in result["checks_failed"]


def test_load_cases_validates_expected_tool_call_arguments(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([{
        "name": "read exact file",
        "message": "Read notes.txt",
        "expected_tool_calls": [{
            "name": "workspace.read_file",
            "arguments": {"path": "notes.txt"},
        }],
    }]), encoding="utf-8")

    cases = agent_eval.load_cases(path)

    assert cases[0]["expected_tool_calls"] == [{
        "name": "workspace.read_file",
        "arguments": {"path": "notes.txt"},
    }]


@pytest.mark.parametrize("expected_calls", [
    "not-a-list",
    [None],
    [{"name": "", "arguments": {}}],
    [{"name": "workspace.read_file", "arguments": []}],
])
def test_load_cases_rejects_malformed_expected_tool_calls(tmp_path, expected_calls):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([{
        "name": "invalid tool expectation",
        "message": "Read notes.txt",
        "expected_tool_calls": expected_calls,
    }]), encoding="utf-8")

    with pytest.raises(ValueError, match="expected_tool_calls"):
        agent_eval.load_cases(path)


def test_expected_tool_call_arguments_match_journal_exactly(monkeypatch):
    responses = iter([
        {
            "answer": "Read the requested file.",
            "task_id": "task-789",
            "tool_calls": 1,
        },
        {
            "status": "succeeded",
            "tool_executions": [{
                "tool_name": "workspace.read_file",
                "input_json": '{"path": "notes.txt"}',
                "status": "succeeded",
            }],
        },
    ])
    monkeypatch.setattr(agent_eval, "_request_json", lambda *_a, **_kw: next(responses))
    case = {
        "name": "exact tool arguments",
        "message": "Read notes.txt",
        "expected_tools": ["workspace.read_file"],
        "min_tool_calls": 1,
        "expected_tool_calls": [{
            "name": "workspace.read_file",
            "arguments": {"path": "notes.txt"},
        }],
    }

    result = agent_eval.run_case(
        case,
        base_url="http://127.0.0.1:8000",
        token="test-token",
        timeout=1,
    )

    assert result["passed"] is True


@pytest.mark.parametrize("actual_arguments", [
    '{"path": "secrets.txt"}',
    '{"path": "notes.txt", "recursive": true}',
    "not-json",
])
def test_expected_tool_call_argument_mismatch_fails_evaluation(monkeypatch, actual_arguments):
    responses = iter([
        {
            "answer": "Read the requested file.",
            "task_id": "task-790",
            "tool_calls": 1,
        },
        {
            "status": "succeeded",
            "tool_executions": [{
                "tool_name": "workspace.read_file",
                "input_json": actual_arguments,
                "status": "succeeded",
            }],
        },
    ])
    monkeypatch.setattr(agent_eval, "_request_json", lambda *_a, **_kw: next(responses))
    case = {
        "name": "tool arguments must match",
        "message": "Read notes.txt",
        "expected_tool_calls": [{
            "name": "workspace.read_file",
            "arguments": {"path": "notes.txt"},
        }],
    }

    result = agent_eval.run_case(
        case,
        base_url="http://127.0.0.1:8000",
        token="test-token",
        timeout=1,
    )

    assert result["passed"] is False
    assert "tool_call_arguments_mismatch" in result["checks_failed"]
    assert "arguments" not in result


def test_load_cases_accepts_and_rejects_forbidden_answer_assertions(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([{
        "name": "honest failure",
        "message": "Read the unavailable fixture",
        "expected_not_contains": ["invented marker", "fabricated value"],
    }]), encoding="utf-8")

    assert agent_eval.load_cases(path)[0]["expected_not_contains"] == [
        "invented marker", "fabricated value"
    ]

    path.write_text(json.dumps([{
        "name": "invalid forbidden assertion",
        "message": "Read the unavailable fixture",
        "expected_not_contains": ["valid", ""],
    }]), encoding="utf-8")
    with pytest.raises(ValueError, match="expected_not_contains"):
        agent_eval.load_cases(path)


def test_forbidden_answer_content_fails_without_leaking_answer(monkeypatch):
    monkeypatch.setattr(
        agent_eval,
        "_request_json",
        lambda *_a, **_kw: {
            "answer": "The file is missing, but its marker is invented marker.",
            "task_id": None,
            "tool_calls": 0,
        },
    )
    result = agent_eval.run_case(
        {
            "name": "no invented file content",
            "message": "Read the unavailable fixture",
            "expected_contains": ["file is missing"],
            "expected_not_contains": ["invented marker"],
        },
        base_url="http://127.0.0.1:8000",
        token="test-token",
        timeout=1,
    )

    assert result["passed"] is False
    assert result["checks_failed"] == ["forbidden_text_present"]
    assert "invented marker" not in json.dumps(result)
    assert "answer" not in result


def test_forbidden_answer_assertions_are_case_insensitive(monkeypatch):
    monkeypatch.setattr(
        agent_eval,
        "_request_json",
        lambda *_a, **_kw: {
            "answer": "I found UNKNOWN-CONTENT.",
            "task_id": None,
            "tool_calls": 0,
        },
    )
    result = agent_eval.run_case(
        {
            "name": "case insensitive negative check",
            "message": "Check fixture",
            "expected_not_contains": ["unknown-content"],
        },
        base_url="http://127.0.0.1:8000",
        token="test-token",
        timeout=1,
    )

    assert result["passed"] is False
    assert "forbidden_text_present" in result["checks_failed"]


def test_forbidden_answer_assertion_passes_when_content_absent(monkeypatch):
    monkeypatch.setattr(
        agent_eval,
        "_request_json",
        lambda *_a, **_kw: {
            "answer": "The file could not be read, so I cannot verify its contents.",
            "task_id": None,
            "tool_calls": 0,
        },
    )
    result = agent_eval.run_case(
        {
            "name": "honest unavailable file",
            "message": "Read unavailable fixture",
            "expected_contains": ["could not be read"],
            "expected_not_contains": ["CROWS_EVAL_MARKER_0000"],
        },
        base_url="http://127.0.0.1:8000",
        token="test-token",
        timeout=1,
    )

    assert result["passed"] is True
