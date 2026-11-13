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


def test_load_cases_validates_expected_tool_facts(tmp_path):
    path = tmp_path / "cases.json"
    valid = [{
        "name": "grounded file fact",
        "message": "Read notes.txt",
        "expected_tool_facts": [{
            "tool_name": "workspace.read_file",
            "value": "ORBIT-731",
        }],
    }]
    path.write_text(json.dumps(valid), encoding="utf-8")
    assert agent_eval.load_cases(path)[0]["expected_tool_facts"] == valid[0]["expected_tool_facts"]

    invalid_cases = [
        "not-a-list",
        [None],
        [{"tool_name": "", "value": "ORBIT-731"}],
        [{"tool_name": "workspace.read_file", "value": ""}],
    ]
    for invalid in invalid_cases:
        path.write_text(json.dumps([{
            "name": "invalid tool fact",
            "message": "Read notes.txt",
            "expected_tool_facts": invalid,
        }]), encoding="utf-8")
        with pytest.raises(ValueError, match="expected_tool_facts"):
            agent_eval.load_cases(path)


def test_answer_fact_must_exist_in_successful_tool_result(monkeypatch):
    responses = iter([
        {
            "answer": "The verified marker is ORBIT-731.",
            "task_id": "task-grounded-1",
            "tool_calls": 1,
        },
        {
            "status": "succeeded",
            "tool_executions": [{
                "tool_name": "workspace.read_file",
                "status": "succeeded",
                "result_json": '{"content":"ORBIT-731"}',
            }],
        },
    ])
    monkeypatch.setattr(agent_eval, "_request_json", lambda *_a, **_kw: next(responses))
    result = agent_eval.run_case(
        {
            "name": "grounded marker",
            "message": "Read notes.txt",
            "expected_tool_facts": [{
                "tool_name": "workspace.read_file",
                "value": "ORBIT-731",
            }],
        },
        base_url="http://127.0.0.1:8000",
        token="test-token",
        timeout=1,
    )

    assert result["passed"] is True


def test_hallucinated_answer_fact_fails_when_absent_from_tool_result(monkeypatch):
    responses = iter([
        {
            "answer": "The verified marker is FABRICATED-999.",
            "task_id": "task-grounded-2",
            "tool_calls": 1,
        },
        {
            "status": "succeeded",
            "tool_executions": [{
                "tool_name": "workspace.read_file",
                "status": "succeeded",
                "result_json": '{"content":"ORBIT-731"}',
            }],
        },
    ])
    monkeypatch.setattr(agent_eval, "_request_json", lambda *_a, **_kw: next(responses))
    result = agent_eval.run_case(
        {
            "name": "unverified marker",
            "message": "Read notes.txt",
            "expected_tool_facts": [{
                "tool_name": "workspace.read_file",
                "value": "FABRICATED-999",
            }],
        },
        base_url="http://127.0.0.1:8000",
        token="test-token",
        timeout=1,
    )

    assert result["passed"] is False
    assert "tool_result_missing_expected_fact" in result["checks_failed"]
    assert "FABRICATED-999" not in json.dumps(result)
    assert "answer" not in result


def test_answer_fact_fails_if_not_repeated_from_source(monkeypatch):
    responses = iter([
        {
            "answer": "I read the file, but I will not state the marker.",
            "task_id": "task-grounded-3",
            "tool_calls": 1,
        },
        {
            "status": "succeeded",
            "tool_executions": [{
                "tool_name": "workspace.read_file",
                "status": "succeeded",
                "result_json": '{"content":"ORBIT-731"}',
            }],
        },
    ])
    monkeypatch.setattr(agent_eval, "_request_json", lambda *_a, **_kw: next(responses))
    result = agent_eval.run_case(
        {
            "name": "answer includes verified fact",
            "message": "Read notes.txt",
            "expected_tool_facts": [{
                "tool_name": "workspace.read_file",
                "value": "ORBIT-731",
            }],
        },
        base_url="http://127.0.0.1:8000",
        token="test-token",
        timeout=1,
    )

    assert result["passed"] is False
    assert "answer_not_grounded_in_tool_result" in result["checks_failed"]


def test_tool_fact_requires_successful_matching_tool_execution(monkeypatch):
    responses = iter([
        {
            "answer": "The marker is ORBIT-731.",
            "task_id": "task-grounded-4",
            "tool_calls": 1,
        },
        {
            "status": "failed",
            "tool_executions": [{
                "tool_name": "workspace.read_file",
                "status": "failed",
                "result_json": '{"content":"ORBIT-731"}',
            }],
        },
    ])
    monkeypatch.setattr(agent_eval, "_request_json", lambda *_a, **_kw: next(responses))
    result = agent_eval.run_case(
        {
            "name": "failed tool is not a source",
            "message": "Read notes.txt",
            "expected_tool_facts": [{
                "tool_name": "workspace.read_file",
                "value": "ORBIT-731",
            }],
        },
        base_url="http://127.0.0.1:8000",
        token="test-token",
        timeout=1,
    )

    assert result["passed"] is False
    assert "expected_tool_result_missing" in result["checks_failed"]


def test_run_case_records_bounded_model_routing_metadata(monkeypatch):
    responses = iter([{
        "answer": "Task complete.", "task_id": None, "tool_calls": 0,
        "route": "coding", "model": "openai/gpt-4o-mini",
        "fallback_used": True, "attempts": 2,
    }])
    monkeypatch.setattr(agent_eval, "_request_json", lambda *_a, **_kw: next(responses))
    result = agent_eval.run_case(
        {"name": "routing metadata", "message": "Complete a task", "mode": "manual",
         "task_type": "coding", "model_route": "coding"},
        base_url="http://127.0.0.1:8000", token="test-token", timeout=1,
    )
    assert result["mode"] == "manual"
    assert result["route"] == "coding"
    assert result["model"] == "openai/gpt-4o-mini"
    assert result["fallback_used"] is True
    assert result["attempts"] == 2


def test_response_metadata_omits_unavailable_or_unsafe_values():
    metadata = agent_eval.response_metadata(
        {"route": "https://provider.example/api?key=secret",
         "model": "model-name\\nsecret", "fallback_used": "false", "attempts": True},
        {"mode": "auto"},
    )
    assert metadata == {"mode": "auto"}


def test_response_metadata_keeps_valid_route_without_inventing_cost():
    metadata = agent_eval.response_metadata(
        {"route": "primary", "model": "provider/model-v2",
         "fallback_used": False, "attempts": 1},
        {"mode": "auto"},
    )
    assert metadata == {
        "mode": "auto", "route": "primary", "model": "provider/model-v2",
        "fallback_used": False, "attempts": 1,
    }
    assert "cost" not in metadata



@pytest.mark.parametrize("tool_calls", ["many", -1, True, 1.5, {}])
def test_run_case_rejects_invalid_tool_call_count_without_crashing(monkeypatch, tool_calls):
    monkeypatch.setattr(
        agent_eval,
        "_request_json",
        lambda *_a, **_kw: {"answer": "Done.", "task_id": None, "tool_calls": tool_calls},
    )

    result = agent_eval.run_case(
        {"name": "invalid tool count", "message": "Do a task"},
        base_url="http://127.0.0.1:8000",
        token="test-token",
        timeout=1,
    )

    assert result["passed"] is False
    assert result["tool_calls"] == 0
    assert "invalid_tool_call_count" in result["checks_failed"]


@pytest.mark.parametrize("task_id", [123, {}, "", " " , "x" * 257])
def test_run_case_rejects_invalid_task_id_without_requesting_journal(monkeypatch, task_id):
    calls = []

    def fake_request(url, **_kwargs):
        calls.append(url)
        return {"answer": "Done.", "task_id": task_id, "tool_calls": 0}

    monkeypatch.setattr(agent_eval, "_request_json", fake_request)
    result = agent_eval.run_case(
        {"name": "invalid task id", "message": "Do a task"},
        base_url="http://127.0.0.1:8000",
        token="test-token",
        timeout=1,
    )

    assert result["passed"] is False
    assert "invalid_task_id" in result["checks_failed"]
    assert len(calls) == 1
    serialized_result = json.dumps(result)
    assert "task_id" not in result
    if isinstance(task_id, str) and task_id.strip():
        assert task_id not in serialized_result
    elif isinstance(task_id, int):
        assert str(task_id) not in serialized_result


def test_run_case_url_encodes_task_id_as_one_path_segment(monkeypatch):
    requested_urls = []
    responses = iter([
        {"answer": "Done.", "task_id": "task/with?reserved", "tool_calls": 0},
        {"status": "succeeded", "tool_executions": []},
    ])

    def fake_request(url, **_kwargs):
        requested_urls.append(url)
        return next(responses)

    monkeypatch.setattr(agent_eval, "_request_json", fake_request)
    result = agent_eval.run_case(
        {"name": "encoded task id", "message": "Do a task"},
        base_url="http://127.0.0.1:8000",
        token="test-token",
        timeout=1,
    )

    assert result["passed"] is True
    assert requested_urls[1] == "http://127.0.0.1:8000/tasks/task%2Fwith%3Freserved"


def test_main_reports_output_write_errors_without_traceback(tmp_path, monkeypatch, capsys):
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(json.dumps([{
        "name": "basic",
        "message": "Say READY",
        "expected_contains": ["READY"],
    }]), encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "agent_eval.py",
            "--cases", str(cases_path),
            "--output", str(tmp_path / "missing" / "report.json"),
        ],
    )
    monkeypatch.setattr(
        agent_eval,
        "_request_json",
        lambda *_args, **_kwargs: {
            "answer": "READY",
            "task_id": None,
            "tool_calls": 0,
        },
    )

    exit_code = agent_eval.main()

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "Could not write evaluation report (FileNotFoundError)" in captured.err
    assert "Traceback" not in captured.err
    assert "READY" not in captured.out


@pytest.mark.parametrize("answer", [None, 123, True, [], {}, ["READY"]])
def test_run_case_rejects_non_string_answers_without_serializing_them(monkeypatch, answer):
    monkeypatch.setattr(
        agent_eval,
        "_request_json",
        lambda *_args, **_kwargs: {
            "answer": answer,
            "task_id": None,
            "tool_calls": 0,
        },
    )

    result = agent_eval.run_case(
        {
            "name": "invalid answer shape",
            "message": "Say READY",
            "expected_contains": ["READY"],
        },
        base_url="http://127.0.0.1:8000",
        token="test-token",
        timeout=1,
    )

    assert result["passed"] is False
    assert "invalid_answer_type" in result["checks_failed"]
    assert "answer" not in result
    assert "READY" not in json.dumps(result)


@pytest.mark.parametrize("status", [None, 1, {}, [], "completed"])
def test_run_case_rejects_invalid_task_journal_status(monkeypatch, status):
    responses = iter([
        {"answer": "Done.", "task_id": "task-status-shape", "tool_calls": 0},
        {"status": status, "tool_executions": []},
    ])
    monkeypatch.setattr(agent_eval, "_request_json", lambda *_a, **_kw: next(responses))

    result = agent_eval.run_case(
        {"name": "invalid journal status", "message": "Do a task"},
        base_url="http://127.0.0.1:8000",
        token="test-token",
        timeout=1,
    )

    assert result["passed"] is False
    assert "invalid_task_status" in result["checks_failed"]
    assert result["task_status"] == "unknown"


@pytest.mark.parametrize("executions", [None, {}, "not-a-list", 3])
def test_run_case_rejects_invalid_tool_execution_collection(monkeypatch, executions):
    responses = iter([
        {"answer": "Done.", "task_id": "task-execution-shape", "tool_calls": 0},
        {"status": "succeeded", "tool_executions": executions},
    ])
    monkeypatch.setattr(agent_eval, "_request_json", lambda *_a, **_kw: next(responses))

    result = agent_eval.run_case(
        {"name": "invalid execution collection", "message": "Do a task"},
        base_url="http://127.0.0.1:8000",
        token="test-token",
        timeout=1,
    )

    assert result["passed"] is False
    assert "invalid_tool_execution_list" in result["checks_failed"]


def test_run_case_rejects_malformed_tool_execution_entries(monkeypatch):
    responses = iter([
        {"answer": "Done.", "task_id": "task-execution-entry", "tool_calls": 0},
        {
            "status": "succeeded",
            "tool_executions": [
                None,
                {"tool_name": {"unexpected": "object"}, "status": "succeeded"},
            ],
        },
    ])
    monkeypatch.setattr(agent_eval, "_request_json", lambda *_a, **_kw: next(responses))

    result = agent_eval.run_case(
        {"name": "invalid execution entries", "message": "Do a task"},
        base_url="http://127.0.0.1:8000",
        token="test-token",
        timeout=1,
    )

    assert result["passed"] is False
    assert result["checks_failed"].count("invalid_tool_execution") == 2
    assert result["tools_observed"] == []


@pytest.mark.parametrize("execution_status", [None, 1, {}, [], "completed", "SUCCESS"])
def test_run_case_rejects_invalid_tool_execution_status(monkeypatch, execution_status):
    responses = iter([
        {"answer": "Done.", "task_id": "task-tool-status", "tool_calls": 1},
        {
            "status": "succeeded",
            "tool_executions": [{
                "tool_name": "workspace.read_file",
                "status": execution_status,
                "result_json": '{"content":"fixture"}',
            }],
        },
    ])
    monkeypatch.setattr(agent_eval, "_request_json", lambda *_a, **_kw: next(responses))

    result = agent_eval.run_case(
        {
            "name": "invalid tool execution status",
            "message": "Read a fixture",
            "expected_tools": ["workspace.read_file"],
            "min_tool_calls": 1,
        },
        base_url="http://127.0.0.1:8000",
        token="test-token",
        timeout=1,
    )

    assert result["passed"] is False
    assert "invalid_tool_execution_status" in result["checks_failed"]
    assert result["tools_observed"] == []
