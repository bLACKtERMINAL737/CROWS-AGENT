import asyncio
import json

import pytest
from fastapi import HTTPException

from crows import app as app_module
from crows.store import TaskStore


class PlanningAndSpecialistRouter:
    def __init__(self, fail_role=None):
        self.plan = {
            "subtasks": [
                {"title": "Inspect repository", "input": "Read the relevant implementation and report evidence", "role": "research"},
                {"title": "Review risks", "input": "Check implementation risks and missing tests", "role": "monitor"},
            ]
        }
        self.fail_role = fail_role
        self.planning_calls = []

    async def chat(self, message, task_type):
        self.planning_calls.append((message, task_type))
        return {"answer": json.dumps(self.plan)}

    async def chat_with_tools(self, message, task_type, **kwargs):
        if self.fail_role and f"You are the {self.fail_role} specialist" in message:
            raise RuntimeError("simulated specialist failure")
        return {"answer": ("{\"verdict\":\"PASS\",\"summary\":\"Evidence is sufficient.\",\"findings\":[]}" if "You are the qa_security specialist" in message else f"Verified result for {task_type}"), "tool_calls": 0}


class NoTools:
    def describe(self):
        return []


class ConfiguredRoute:
    configured = True


def configure(monkeypatch, tmp_path, *, fail_role=None):
    store = TaskStore(tmp_path / "workflow-app.sqlite3")
    router = PlanningAndSpecialistRouter(fail_role=fail_role)
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "router", router)
    monkeypatch.setattr(app_module, "tools", NoTools())
    monkeypatch.setattr(app_module, "reasoning_provider", ConfiguredRoute())
    return store, router


@pytest.mark.asyncio
async def test_orchestrator_persists_plan_tasks_and_qa_checkpoint(tmp_path, monkeypatch):
    store, router = configure(monkeypatch, tmp_path)

    result = await app_module.orchestrate_agent_work(
        app_module.AgentPlanRequest(goal="Inspect the repository and review its risks")
    )

    assert result["status"] == "succeeded"
    assert result["workflow_id"]
    assert result["workspace_modified"] is False
    assert result["approval_required_before_changes"] is True
    assert len(router.planning_calls) == 1

    saved = store.get_workflow(result["workflow_id"])
    assert saved is not None
    assert saved["status"] == "succeeded"
    assert saved["plan"] == [
        {**item, "depends_on": item.get("depends_on", [])}
        for item in router.plan["subtasks"]
    ]
    assert [item["status"] for item in saved["results"]["tasks"]] == [
        "succeeded", "succeeded"
    ]
    assert saved["results"]["qa_review"]["status"] == "succeeded"
    assert saved["error_text"] is None

    # Checkpoints refer to durable task-journal entries, not only in-memory output.
    for item in saved["results"]["tasks"]:
        task = store.get_task(item["task_id"])
        assert task is not None
        assert task["status"] == "succeeded"
    qa = store.get_task(saved["results"]["qa_review"]["task_id"])
    assert qa is not None
    assert qa["status"] == "succeeded"


@pytest.mark.asyncio
async def test_orchestrator_persists_partial_outcome_when_specialist_fails(
    tmp_path, monkeypatch
):
    store, _router = configure(monkeypatch, tmp_path, fail_role="research")

    result = await app_module.orchestrate_agent_work(
        app_module.AgentPlanRequest(goal="Inspect a task and report issues")
    )

    assert result["status"] == "partial"
    saved = store.get_workflow(result["workflow_id"])
    assert saved is not None
    assert saved["status"] == "partial"
    assert saved["results"]["tasks"][0]["status"] == "failed"
    assert saved["results"]["tasks"][1]["status"] == "succeeded"
    assert saved["results"]["qa_review"]["status"] == "succeeded"
    assert saved["error_text"] is not None


def test_workflow_read_endpoints_return_saved_state_and_validate_ids(
    tmp_path, monkeypatch
):
    store, _router = configure(monkeypatch, tmp_path)
    workflow = store.create_workflow(
        "Saved workflow",
        [{"title": "Inspect", "input": "Read safely", "role": "research"}],
    )

    item = app_module.get_agent_workflow(workflow["id"])
    listing = app_module.list_agent_workflows(limit=10)

    assert item["id"] == workflow["id"]
    assert listing["workflows"][0]["id"] == workflow["id"]

    with pytest.raises(HTTPException) as missing:
        app_module.get_agent_workflow("missing-workflow")
    assert missing.value.status_code == 404

    with pytest.raises(HTTPException) as bad_limit:
        app_module.list_agent_workflows(limit=0)
    assert bad_limit.value.status_code == 400


@pytest.mark.asyncio
async def test_orchestrator_executes_dependencies_first_and_blocks_dependents_on_failure(
    tmp_path, monkeypatch
):
    store, router = configure(monkeypatch, tmp_path)
    router.plan = {
        "subtasks": [
            {"title": "Publish summary", "input": "Summarize findings", "role": "coding", "depends_on": [2]},
            {"title": "Independent check", "input": "Check independently", "role": "monitor"},
            {"title": "Gather evidence", "input": "Gather evidence", "role": "research"},
        ]
    }
    execution_order = []

    async def fake_run(task_id):
        task = store.get_task(task_id)
        execution_order.append(task["title"])
        store.claim_task(task_id)
        if task["title"].startswith("[research]"):
            store.set_task_status(task_id, "failed", error="simulated failure")
            return {"task_id": task_id, "status": "failed", "result": "", "tool_calls": 0, "tool_attempts": 0}
        answer = "{\"verdict\":\"PASS\",\"summary\":\"Evidence is sufficient.\",\"findings\":[]}" if task["title"].startswith("[qa_security]") else "ok"
        store.set_task_status(task_id, "succeeded", result={"answer": answer})
        return {"task_id": task_id, "status": "succeeded", "result": answer, "tool_calls": 0, "tool_attempts": 0}

    monkeypatch.setattr(app_module, "run_agent_task", fake_run)
    result = await app_module.orchestrate_agent_work(
        app_module.AgentPlanRequest(goal="Gather evidence then summarize")
    )

    assert any(title.startswith("[research] Gather evidence") for title in execution_order)
    assert any(title.startswith("[monitor] Independent check") for title in execution_order)
    assert not any(title.startswith("[coding] Publish summary") for title in execution_order)
    saved = store.get_workflow(result["workflow_id"])
    by_title = {item["title"]: item for item in saved["results"]["tasks"]}
    assert by_title["Gather evidence"]["status"] == "failed"
    assert by_title["Publish summary"]["status"] == "blocked"
    assert by_title["Publish summary"]["blocked_by"] == [2]
    assert result["status"] == "partial"
    diagnostics = result["diagnostics"]
    assert diagnostics["task_counts"]["failed"] == 1
    assert diagnostics["task_counts"]["blocked"] == 1
    assert diagnostics["failure_count"] == 2
    assert {item["category"] for item in diagnostics["failures"]} == {
        "task_failed", "dependency_blocked"
    }
    saved_diagnostics = store.get_workflow(result["workflow_id"])["results"]["diagnostics"]
    assert saved_diagnostics == diagnostics



@pytest.mark.asyncio
async def test_orchestrator_runs_independent_tasks_concurrently_with_a_hard_cap(
    tmp_path, monkeypatch
):
    store, router = configure(monkeypatch, tmp_path)
    router.plan = {
        "subtasks": [
            {"title": f"Independent {index}", "input": "Inspect independently", "role": "research"}
            for index in range(4)
        ]
    }
    active = 0
    peak_active = 0
    started = []
    lock = asyncio.Lock()
    release_batch = asyncio.Event()

    async def fake_run(task_id):
        nonlocal active, peak_active
        task = store.get_task(task_id)
        if task["title"].startswith("[qa_security]"):
            store.claim_task(task_id)
            store.set_task_status(task_id, "succeeded", result={"answer": "{\"verdict\":\"PASS\",\"summary\":\"Evidence is sufficient.\",\"findings\":[]}"})
            return {
                "task_id": task_id, "status": "succeeded", "result": "{\"verdict\":\"PASS\",\"summary\":\"Evidence is sufficient.\",\"findings\":[]}",
                "tool_calls": 0, "tool_attempts": 0,
            }
        async with lock:
            active += 1
            peak_active = max(peak_active, active)
            started.append(task["title"])
            if len(started) >= 3:
                release_batch.set()
        await asyncio.wait_for(release_batch.wait(), timeout=2)
        await asyncio.sleep(0.01)
        store.claim_task(task_id)
        store.set_task_status(task_id, "succeeded", result={"answer": task["title"]})
        async with lock:
            active -= 1
        return {
            "task_id": task_id, "status": "succeeded", "result": task["title"],
            "tool_calls": 0, "tool_attempts": 0,
        }

    monkeypatch.setattr(app_module, "run_agent_task", fake_run)
    result = await app_module.orchestrate_agent_work(
        app_module.AgentPlanRequest(goal="Run independent checks")
    )

    assert result["status"] == "succeeded"
    assert peak_active == 3
    assert len(started) == 4
    assert [item["plan_index"] for item in result["tasks"]] == [0, 1, 2, 3]
    assert result["max_concurrency"] == 3


@pytest.mark.asyncio
async def test_orchestrator_waits_for_all_prerequisites_before_dependent_task(
    tmp_path, monkeypatch
):
    store, router = configure(monkeypatch, tmp_path)
    router.plan = {
        "subtasks": [
            {"title": "Prerequisite A", "input": "A", "role": "research"},
            {"title": "Prerequisite B", "input": "B", "role": "monitor"},
            {"title": "Dependent C", "input": "Use A and B", "role": "coding", "depends_on": [0, 1]},
        ]
    }
    completed = set()
    completion_order = []
    lock = asyncio.Lock()

    async def fake_run(task_id):
        task = store.get_task(task_id)
        title = task["title"]
        if title.startswith("[coding]"):
            assert {"[research] Prerequisite A", "[monitor] Prerequisite B"} <= completed
        store.claim_task(task_id)
        await asyncio.sleep(0.01)
        answer = "{\"verdict\":\"PASS\",\"summary\":\"Evidence is sufficient.\",\"findings\":[]}" if title.startswith("[qa_security]") else title
        store.set_task_status(task_id, "succeeded", result={"answer": answer})
        async with lock:
            completed.add(title)
            completion_order.append(title)
        return {
            "task_id": task_id, "status": "succeeded", "result": answer,
            "tool_calls": 0, "tool_attempts": 0,
        }

    monkeypatch.setattr(app_module, "run_agent_task", fake_run)
    result = await app_module.orchestrate_agent_work(
        app_module.AgentPlanRequest(goal="Run dependent checks safely")
    )

    assert result["status"] == "succeeded"
    assert completion_order.index("[coding] Dependent C") > completion_order.index("[research] Prerequisite A")
    assert completion_order.index("[coding] Dependent C") > completion_order.index("[monitor] Prerequisite B")



@pytest.mark.asyncio
async def test_orchestrator_marks_timed_out_task_unknown_and_blocks_dependents(
    tmp_path, monkeypatch
):
    store, router = configure(monkeypatch, tmp_path)
    monkeypatch.setenv("AETHER_AGENT_TASK_TIMEOUT_SECONDS", "1")
    router.plan = {
        "subtasks": [
            {"title": "Slow prerequisite", "input": "May take too long", "role": "research"},
            {"title": "Must wait", "input": "Use prerequisite", "role": "coding", "depends_on": [0]},
        ]
    }
    started = []

    async def fake_run(task_id):
        task = store.get_task(task_id)
        if task["title"].startswith("[qa_security]"):
            store.claim_task(task_id)
            store.set_task_status(task_id, "succeeded", result={"answer": "{\"verdict\":\"PASS\",\"summary\":\"Evidence is sufficient.\",\"findings\":[]}"})
            return {
                "task_id": task_id, "status": "succeeded", "result": "{\"verdict\":\"PASS\",\"summary\":\"Evidence is sufficient.\",\"findings\":[]}",
                "tool_calls": 0, "tool_attempts": 0,
            }
        started.append(task["title"])
        assert store.claim_task(task_id)
        if task["title"].startswith("[research]"):
            await asyncio.sleep(1.2)
        store.set_task_status(task_id, "succeeded", result={"answer": "ok"})
        return {
            "task_id": task_id, "status": "succeeded", "result": "ok",
            "tool_calls": 0, "tool_attempts": 0,
        }

    monkeypatch.setattr(app_module, "run_agent_task", fake_run)
    result = await app_module.orchestrate_agent_work(
        app_module.AgentPlanRequest(goal="Handle slow tasks safely")
    )

    assert result["status"] == "partial"
    assert result["task_timeout_seconds"] == 1
    assert len(started) == 1
    saved = store.get_workflow(result["workflow_id"])
    by_title = {item["title"]: item for item in saved["results"]["tasks"]}
    assert by_title["Slow prerequisite"]["status"] == "unknown"
    assert "timed out" in by_title["Slow prerequisite"]["error"].lower()
    assert by_title["Must wait"]["status"] == "blocked"
    assert saved["results"]["qa_review"]["status"] == "succeeded"
    diagnostic_categories = {
        item["category"] for item in saved["results"]["diagnostics"]["failures"]
    }
    assert diagnostic_categories == {"outcome_unknown", "dependency_blocked"}


@pytest.mark.asyncio
async def test_orchestrator_respects_configured_concurrency_limit(
    tmp_path, monkeypatch
):
    store, router = configure(monkeypatch, tmp_path)
    monkeypatch.setenv("AETHER_AGENT_MAX_CONCURRENCY", "2")
    router.plan = {
        "subtasks": [
            {"title": f"Bounded task {index}", "input": "Run safely", "role": "research"}
            for index in range(4)
        ]
    }
    active = 0
    peak_active = 0
    started = []
    lock = asyncio.Lock()
    release_batch = asyncio.Event()

    async def fake_run(task_id):
        nonlocal active, peak_active
        task = store.get_task(task_id)
        if task["title"].startswith("[qa_security]"):
            store.claim_task(task_id)
            store.set_task_status(task_id, "succeeded", result={"answer": "{\"verdict\":\"PASS\",\"summary\":\"Evidence is sufficient.\",\"findings\":[]}"})
            return {
                "task_id": task_id, "status": "succeeded", "result": "{\"verdict\":\"PASS\",\"summary\":\"Evidence is sufficient.\",\"findings\":[]}",
                "tool_calls": 0, "tool_attempts": 0,
            }
        async with lock:
            active += 1
            peak_active = max(peak_active, active)
            started.append(task["title"])
            if len(started) >= 2:
                release_batch.set()
        await asyncio.wait_for(release_batch.wait(), timeout=2)
        await asyncio.sleep(0.01)
        store.claim_task(task_id)
        store.set_task_status(task_id, "succeeded", result={"answer": task["title"]})
        async with lock:
            active -= 1
        return {
            "task_id": task_id, "status": "succeeded", "result": task["title"],
            "tool_calls": 0, "tool_attempts": 0,
        }

    monkeypatch.setattr(app_module, "run_agent_task", fake_run)
    result = await app_module.orchestrate_agent_work(
        app_module.AgentPlanRequest(goal="Respect configured concurrency")
    )

    assert result["status"] == "succeeded"
    assert peak_active == 2
    assert len(started) == 4
    assert result["max_concurrency"] == 2


@pytest.mark.parametrize(
    ("configured", "expected"),
    [("0", 1), ("1", 1), ("3", 3), ("99", 3), ("invalid", 3)],
)
def test_workflow_concurrency_configuration_is_clamped(
    tmp_path, monkeypatch, configured, expected
):
    monkeypatch.setenv("AETHER_AGENT_MAX_CONCURRENCY", configured)
    # The orchestration response exposes the effective safe limit. Reuse the
    # async orchestration test path through a synchronous pytest wrapper.
    import asyncio

    store, router = configure(monkeypatch, tmp_path)
    router.plan = {
        "subtasks": [
            {"title": "Single task", "input": "Run once", "role": "research"}
        ]
    }

    async def fake_run(task_id):
        task = store.get_task(task_id)
        store.claim_task(task_id)
        store.set_task_status(task_id, "succeeded", result={"answer": task["title"]})
        return {
            "task_id": task_id, "status": "succeeded", "result": task["title"],
            "tool_calls": 0, "tool_attempts": 0,
        }

    monkeypatch.setattr(app_module, "run_agent_task", fake_run)
    result = asyncio.run(
        app_module.orchestrate_agent_work(
            app_module.AgentPlanRequest(goal="Check safe concurrency setting")
        )
    )
    assert result["max_concurrency"] == expected



@pytest.mark.asyncio
async def test_cancelled_workflow_marks_running_task_unknown_and_checkpoints(
    tmp_path, monkeypatch
):
    store, router = configure(monkeypatch, tmp_path)
    router.plan = {
        "subtasks": [
            {"title": "Long-running task", "input": "Work until cancelled", "role": "research"}
        ]
    }
    started = asyncio.Event()
    never_release = asyncio.Event()

    async def fake_run(task_id):
        task = store.get_task(task_id)
        assert store.claim_task(task_id)
        started.set()
        await never_release.wait()
        store.set_task_status(task_id, "succeeded", result={"answer": "finished"})
        return {
            "task_id": task_id, "status": "succeeded", "result": "finished",
            "tool_calls": 0, "tool_attempts": 0,
        }

    monkeypatch.setattr(app_module, "run_agent_task", fake_run)
    operation = asyncio.create_task(
        app_module.orchestrate_agent_work(
            app_module.AgentPlanRequest(goal="Cancel safely")
        )
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    workflow = store.list_workflows()[0]
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation

    saved_workflow = store.get_workflow(workflow["id"])
    assert saved_workflow["status"] == "unknown"
    outcome = saved_workflow["results"]["tasks"][0]
    assert outcome["status"] == "unknown"
    assert "cancelled" in outcome["error"].lower()
    assert store.get_task(outcome["task_id"])["status"] == "unknown"
    assert saved_workflow["results"]["diagnostics"]["failure_count"] == 1


@pytest.mark.asyncio
async def test_orchestrator_cancellation_during_qa_persists_unknown_state(
    tmp_path, monkeypatch
):
    store, _router = configure(monkeypatch, tmp_path)
    executed = []

    async def fake_run(task_id):
        task = store.get_task(task_id)
        assert task is not None
        assert store.claim_task(task_id)
        executed.append(task["title"])
        if task["title"].startswith("[qa_security]"):
            raise asyncio.CancelledError()
        store.set_task_status(task_id, "succeeded", result={"answer": "checked"})
        return {
            "task_id": task_id,
            "status": "succeeded",
            "result": "checked",
            "tool_calls": 0,
            "tool_attempts": 0,
        }

    monkeypatch.setattr(app_module, "run_agent_task", fake_run)

    with pytest.raises(asyncio.CancelledError):
        await app_module.orchestrate_agent_work(
            app_module.AgentPlanRequest(goal="Persist cancellation safely")
        )

    workflows = store.list_workflows(limit=10)
    assert len(workflows) == 1
    saved = store.get_workflow(workflows[0]["id"])
    assert saved["status"] == "unknown"
    assert "cancelled" in saved["error_text"].lower()
    assert saved["results"]["qa_review"]["status"] == "unknown"
    assert all(item["status"] == "succeeded" for item in saved["results"]["tasks"])
    qa_task = store.get_task(saved["results"]["qa_review"]["task_id"])
    assert qa_task is not None
    assert qa_task["status"] == "unknown"


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("invalid", 120.0),
        ("nan", 120.0),
        ("inf", 120.0),
        ("-inf", 120.0),
        ("0", 1.0),
        ("0.25", 1.0),
        ("999999", 600.0),
        ("30", 30.0),
    ],
)
def test_agent_task_timeout_config_is_finite_and_bounded(
    monkeypatch, configured, expected
):
    monkeypatch.setenv("AETHER_AGENT_TASK_TIMEOUT_SECONDS", configured)
    assert app_module.agent_task_timeout_seconds() == expected


@pytest.mark.asyncio
async def test_orchestrator_reports_normalized_timeout_for_nonfinite_config(
    tmp_path, monkeypatch
):
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv("AETHER_AGENT_TASK_TIMEOUT_SECONDS", "nan")

    result = await app_module.orchestrate_agent_work(
        app_module.AgentPlanRequest(goal="Inspect the repository and review its risks")
    )

    assert result["status"] == "succeeded"
    assert result["task_timeout_seconds"] == 120.0


@pytest.mark.asyncio
async def test_qa_timeout_preserves_durable_error_diagnostic(tmp_path, monkeypatch):
    store, _router = configure(monkeypatch, tmp_path)
    monkeypatch.setenv("AETHER_AGENT_TASK_TIMEOUT_SECONDS", "1")
    qa_started = asyncio.Event()

    async def fake_run(task_id):
        task = store.get_task(task_id)
        assert task is not None
        assert store.claim_task(task_id)
        if task["title"].startswith("[qa_security]"):
            qa_started.set()
            await asyncio.Event().wait()
        store.set_task_status(task_id, "succeeded", result={"answer": "{\"verdict\":\"PASS\",\"summary\":\"Evidence is sufficient.\",\"findings\":[]}"})
        return {
            "task_id": task_id,
            "status": "succeeded",
            "result": "reviewed",
            "tool_calls": 0,
            "tool_attempts": 0,
        }

    monkeypatch.setattr(app_module, "run_agent_task", fake_run)
    result = await app_module.orchestrate_agent_work(
        app_module.AgentPlanRequest(goal="Inspect the repository and review its risks")
    )

    assert qa_started.is_set()
    assert result["status"] == "partial"
    qa = result["qa_review"]
    assert qa["status"] == "unknown"
    assert qa["error"]
    saved_task = store.get_task(qa["task_id"])
    assert saved_task is not None
    assert saved_task["status"] == "unknown"
    assert saved_task["error_text"]


@pytest.mark.parametrize(
    ("exception", "phase", "expected"),
    [
        (RuntimeError("secret token should not leak"), "Agent task", "RuntimeError"),
        (ValueError("private path /Users/alice/private"), "QA review", "ValueError"),
        (TimeoutError(), "QA review", "timed out"),
    ],
)
def test_workflow_exception_diagnostics_are_safe_and_actionable(
    exception, phase, expected
):
    message = app_module.workflow_exception_diagnostic(exception, phase=phase)
    assert expected.lower() in message.lower()
    assert "secret token" not in message
    assert "/Users/alice/private" not in message
    assert len(message) <= 1000


def test_http_exception_detail_is_preserved_in_workflow_diagnostics():
    error = HTTPException(status_code=504, detail="Agent task timed out safely")
    assert app_module.workflow_exception_diagnostic(
        error, phase="Agent task"
    ) == "Agent task timed out safely"


@pytest.mark.parametrize(
    ("detail", "sensitive"),
    [
        ("Provider failed with Bearer abc.def_123", "abc.def_123"),
        ("Request rejected: api_key=supersecretvalue", "supersecretvalue"),
        ("Provider path /Users/alice/.config/secret.json unavailable", "/Users/alice/.config/secret.json"),
        ("Invalid token: token=private-token-value", "private-token-value"),
    ],
)
def test_http_exception_diagnostic_redacts_credentials_and_local_paths(
    detail, sensitive
):
    message = app_module.workflow_exception_diagnostic(
        HTTPException(status_code=502, detail=detail), phase="Agent task"
    )
    assert sensitive not in message
    assert "[REDACTED" in message
    assert len(message) <= 1000


def test_http_exception_diagnostic_keeps_non_sensitive_detail():
    message = app_module.workflow_exception_diagnostic(
        HTTPException(status_code=502, detail="Provider is unavailable"),
        phase="Agent task",
    )
    assert message == "Provider is unavailable"

@pytest.mark.parametrize(
    ("qa_answer", "expected_verdict", "expected_status"),
    [
        ('{"verdict":"PASS","summary":"Evidence is sufficient.","findings":[{"severity":"low","title":"Minor note","detail":"Non-blocking."}]}', "PASS", "succeeded"),
        ('{"verdict":"PASS","summary":"A critical issue remains.","findings":[{"severity":"critical","title":"Unsafe behavior","detail":"Must be fixed."}]}', "NEEDS_ATTENTION", "partial"),
        ("The review looks good to me.", "UNVERIFIED", "partial"),
        ('{"verdict":"NEEDS_ATTENTION","summary":"Important evidence is missing.","findings":[{"severity":"high","title":"Missing verification","detail":"No test evidence."}]}', "NEEDS_ATTENTION", "partial"),
    ],
)
@pytest.mark.asyncio
async def test_qa_quality_gate_requires_explicit_structured_pass(
    tmp_path, monkeypatch, qa_answer, expected_verdict, expected_status
):
    store, _router = configure(monkeypatch, tmp_path)

    async def fake_run(task_id):
        task = store.get_task(task_id)
        assert task is not None
        assert store.claim_task(task_id)
        is_qa = task["title"].startswith("[qa_security]")
        answer = qa_answer if is_qa else "Specialist evidence collected."
        store.set_task_status(
            task_id, "succeeded",
            result={"role": "qa_security" if is_qa else "research", "answer": answer},
        )
        return {
            "task_id": task_id, "status": "succeeded", "result": answer,
            "tool_calls": 0, "tool_attempts": 0,
        }

    monkeypatch.setattr(app_module, "run_agent_task", fake_run)
    result = await app_module.orchestrate_agent_work(
        app_module.AgentPlanRequest(goal="Inspect implementation and verify its quality")
    )

    assert result["status"] == expected_status
    assert result["qa_review"]["verdict"] == expected_verdict
    assert result["diagnostics"]["qa_verdict"] == expected_verdict
    if expected_status == "partial":
        assert any(item["category"] == "qa_quality_gate" for item in result["diagnostics"]["failures"])



@pytest.mark.asyncio
async def test_plan_preview_returns_validated_plan_without_creating_tasks_or_workflow(
    tmp_path, monkeypatch
):
    store, router = configure(monkeypatch, tmp_path)
    result = await app_module.preview_agent_plan(
        app_module.AgentPlanRequest(goal="Inspect the repository safely")
    )

    assert result["executed"] is False
    assert result["approval_required"] is True
    assert result["goal"] == "Inspect the repository safely"
    assert result["subtasks"] == [
        {**item, "depends_on": item.get("depends_on", [])}
        for item in router.plan["subtasks"]
    ]
    assert store.list_workflows(limit=10) == []
    assert store.list_tasks(limit=10) == []


@pytest.mark.asyncio
async def test_orchestrator_executes_the_exact_reviewed_plan_without_replanning(
    tmp_path, monkeypatch
):
    store, router = configure(monkeypatch, tmp_path)
    reviewed = [
        {
            "title": "Only reviewed task",
            "input": "Perform this bounded read-only check",
            "role": "research",
            "depends_on": [],
        }
    ]
    executed = []

    async def fake_run(task_id):
        task = store.get_task(task_id)
        assert store.claim_task(task_id)
        if task["title"] == "[qa_security] Review specialist results":
            # The orchestrator always runs a separate, strict QA gate after
            # specialist work. Give that gate a valid explicit PASS fixture.
            result = {
                "answer": json.dumps({
                    "verdict": "PASS",
                    "summary": "The reviewed specialist result is verified.",
                    "findings": [],
                })
            }
        else:
            executed.append(task["title"])
            result = {"answer": "verified"}
        store.set_task_status(task_id, "succeeded", result=result)
        return {
            "task_id": task_id,
            "status": "succeeded",
            "result": result,
            "tool_calls": 0,
            "tool_attempts": 0,
        }

    monkeypatch.setattr(app_module, "run_agent_task", fake_run)
    result = await app_module.orchestrate_agent_work(
        app_module.AgentPlanRequest(goal="Reviewed goal", subtasks=reviewed)
    )

    assert result["status"] == "succeeded"
    assert executed == ["[research] Only reviewed task"]
    assert router.planning_calls == []
    assert store.get_workflow(result["workflow_id"])["plan"] == reviewed


@pytest.mark.asyncio
async def test_orchestrator_rejects_invalid_reviewed_plan_before_creating_work(
    tmp_path, monkeypatch
):
    store, router = configure(monkeypatch, tmp_path)
    invalid = [
        {
            "title": "Unsafe role",
            "input": "Try to use a reserved role",
            "role": "planner",
            "depends_on": [],
        }
    ]

    with pytest.raises(HTTPException) as error:
        await app_module.orchestrate_agent_work(
            app_module.AgentPlanRequest(goal="Invalid reviewed plan", subtasks=invalid)
        )

    assert error.value.status_code == 400
    assert store.list_workflows(limit=10) == []
    assert router.planning_calls == []



@pytest.mark.asyncio
async def test_workflow_pause_resume_and_cancel_endpoints_are_durable(tmp_path, monkeypatch):
    store, _router = configure(monkeypatch, tmp_path)
    workflow = store.create_workflow("Control endpoint lifecycle", [])
    workflow_id = workflow["id"]

    paused = app_module.pause_agent_workflow(workflow_id)
    assert paused["control_state"] == "paused"
    assert store.get_workflow(workflow_id)["control_state"] == "paused"

    resumed = app_module.resume_agent_workflow(workflow_id)
    assert resumed["control_state"] == "running"

    cancelled = app_module.cancel_agent_workflow(workflow_id)
    assert cancelled["control_state"] == "cancel_requested"
    with pytest.raises(HTTPException) as error:
        app_module.resume_agent_workflow(workflow_id)
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_cancelled_workflow_does_not_schedule_remaining_tasks_or_qa(
    tmp_path, monkeypatch
):
    store, _router = configure(monkeypatch, tmp_path)
    monkeypatch.setattr(
        app_module,
        "get_env",
        lambda key, default="": "1" if key == "CROWS_AGENT_MAX_CONCURRENCY" else default,
    )
    executed = []

    async def fake_run(task_id):
        task = store.get_task(task_id)
        assert task is not None and store.claim_task(task_id)
        executed.append(task["title"])
        if len(executed) == 1:
            app_module.cancel_agent_workflow(active_workflow_id[0])
        store.set_task_status(task_id, "succeeded", result={"answer": "verified"})
        return {
            "task_id": task_id,
            "status": "succeeded",
            "result": {"answer": "verified"},
            "tool_calls": 0,
            "tool_attempts": 0,
        }

    active_workflow_id = [None]
    original_create_workflow = store.create_workflow

    def capture_workflow(goal, plan):
        workflow = original_create_workflow(goal, plan)
        active_workflow_id[0] = workflow["id"]
        return workflow

    monkeypatch.setattr(store, "create_workflow", capture_workflow)
    monkeypatch.setattr(app_module, "run_agent_task", fake_run)
    reviewed = [
        {"title": "First bounded task", "input": "Run first", "role": "research", "depends_on": []},
        {"title": "Second bounded task", "input": "Must not run", "role": "monitor", "depends_on": []},
    ]
    result = await app_module.orchestrate_agent_work(
        app_module.AgentPlanRequest(goal="Cancel after first task", subtasks=reviewed)
    )

    assert result["status"] == "partial"
    assert result["control_state"] == "completed"
    assert len(executed) == 1
    assert not any(title.startswith("[qa_security]") for title in executed)
    saved = store.get_workflow(result["workflow_id"])
    assert saved["control_state"] == "completed"
    assert saved["results"]["qa_review"]["status"] == "not_run"
    assert saved["results"]["tasks"][1]["status"] == "blocked"


@pytest.mark.asyncio
async def test_orchestrate_retries_with_same_idempotency_key_return_saved_workflow(
    tmp_path, monkeypatch
):
    store, router = configure(monkeypatch, tmp_path)
    executions = []

    async def fake_run(task_id):
        task = store.get_task(task_id)
        assert task is not None and store.claim_task(task_id)
        executions.append(task_id)
        answer = (
            '{"verdict":"PASS","summary":"Evidence is sufficient.","findings":[]}'
            if task["title"].startswith("[qa_security]")
            else "Verified specialist result"
        )
        store.set_task_status(task_id, "succeeded", result=answer)
        return {
            "task_id": task_id,
            "status": "succeeded",
            "result": answer,
            "tool_calls": 0,
            "tool_attempts": 0,
        }

    monkeypatch.setattr(app_module, "run_agent_task", fake_run)
    request = app_module.AgentPlanRequest(
        goal="Inspect the repository",
        idempotency_key="workflow-request-unique",
    )

    first = await app_module.orchestrate_agent_work(request)
    executions_after_first = list(executions)
    replay = await app_module.orchestrate_agent_work(request)

    assert first["workflow_id"] == replay["workflow_id"]
    assert first["status"] == replay["status"] == "succeeded"
    assert replay["idempotent_replay"] is True
    assert executions == executions_after_first
    assert len(router.planning_calls) == 1
    assert len(store.list_workflows()) == 1


@pytest.mark.asyncio
async def test_orchestrate_rejects_idempotency_key_reused_for_different_goal(
    tmp_path, monkeypatch
):
    store, router = configure(monkeypatch, tmp_path)
    async def fake_run(task_id):
        task = store.get_task(task_id)
        assert task is not None and store.claim_task(task_id)
        answer = (
            '{"verdict":"PASS","summary":"Evidence is sufficient.","findings":[]}'
            if task["title"].startswith("[qa_security]")
            else "Verified specialist result"
        )
        store.set_task_status(task_id, "succeeded", result=answer)
        return {
            "task_id": task_id,
            "status": "succeeded",
            "result": answer,
            "tool_calls": 0,
            "tool_attempts": 0,
        }

    monkeypatch.setattr(app_module, "run_agent_task", fake_run)

    first = await app_module.orchestrate_agent_work(
        app_module.AgentPlanRequest(
            goal="Inspect repository A",
            idempotency_key="workflow-request-conflict",
        )
    )
    assert first["workflow_id"]

    with pytest.raises(HTTPException) as error:
        await app_module.orchestrate_agent_work(
            app_module.AgentPlanRequest(
                goal="Inspect repository B",
                idempotency_key="workflow-request-conflict",
            )
        )

    assert error.value.status_code == 409
    assert len(router.planning_calls) == 1
    assert len(store.list_workflows()) == 1


@pytest.mark.asyncio
async def test_global_agent_concurrency_is_shared_across_workflows(tmp_path, monkeypatch):
    store, router = configure(monkeypatch, tmp_path)
    router.plan = {
        "subtasks": [
            {"title": f"Global slot check {index}", "input": "Run concurrently", "role": "research"}
            for index in range(2)
        ]
    }
    monkeypatch.setattr(
        app_module,
        "get_env",
        lambda name, default=None: "2" if name == "CROWS_AGENT_GLOBAL_MAX_CONCURRENCY" else default,
    )
    active = 0
    peak_active = 0
    lock = asyncio.Lock()

    async def fake_run(task_id):
        nonlocal active, peak_active
        task = store.get_task(task_id)
        assert task is not None
        assert store.claim_task(task_id)
        async with lock:
            active += 1
            peak_active = max(peak_active, active)
        await asyncio.sleep(0.02)
        answer = (
            '{"verdict":"PASS","summary":"Evidence is sufficient.","findings":[]}'
            if task["title"].startswith("[qa_security]")
            else task["title"]
        )
        store.set_task_status(task_id, "succeeded", result={"answer": answer})
        async with lock:
            active -= 1
        return {
            "task_id": task_id,
            "status": "succeeded",
            "result": answer,
            "tool_calls": 0,
            "tool_attempts": 0,
        }

    monkeypatch.setattr(app_module, "_run_agent_task_unlimited", fake_run)
    direct_task = store.create_task(
        "[research] Direct task outside orchestration",
        "Must share the same server-wide execution budget",
    )
    first, second, direct = await asyncio.gather(
        app_module.orchestrate_agent_work(
            app_module.AgentPlanRequest(goal="First concurrent workflow")
        ),
        app_module.orchestrate_agent_work(
            app_module.AgentPlanRequest(goal="Second concurrent workflow")
        ),
        app_module.run_agent_task(direct_task["id"]),
    )

    assert first["status"] == second["status"] == "succeeded"
    assert direct["status"] == "succeeded"
    assert first["global_max_concurrency"] == second["global_max_concurrency"] == 2
    assert 1 < peak_active <= 2


def test_workflow_listing_uses_stable_bounded_pagination(tmp_path, monkeypatch):
    store, _router = configure(monkeypatch, tmp_path)
    created = [
        store.create_workflow(f"Workflow {index}", [{"title": "Task", "input": "Run"}])
        for index in range(5)
    ]
    ordered = store.list_workflows(limit=5)
    assert len(ordered) == 5
    assert [item["id"] for item in store.list_workflows(limit=2, offset=0)] == [
        item["id"] for item in ordered[:2]
    ]
    assert [item["id"] for item in store.list_workflows(limit=2, offset=2)] == [
        item["id"] for item in ordered[2:4]
    ]
    assert [item["id"] for item in store.list_workflows(limit=2, offset=4)] == [
        item["id"] for item in ordered[4:]
    ]
    with pytest.raises(ValueError, match="limit or offset"):
        store.list_workflows(limit=201)
    with pytest.raises(ValueError, match="limit or offset"):
        store.list_workflows(offset=-1)


def test_workflow_listing_api_validates_offset(tmp_path, monkeypatch):
    _store, _router = configure(monkeypatch, tmp_path)
    assert app_module.list_agent_workflows(limit=10, offset=0)["workflows"] == []
    with pytest.raises(HTTPException) as error:
        app_module.list_agent_workflows(limit=10, offset=-1)
    assert error.value.status_code == 400


def test_workflow_listing_filters_status_and_control_state(tmp_path, monkeypatch):
    store, _router = configure(monkeypatch, tmp_path)
    running = store.create_workflow("Running", [{"title": "Task", "input": "Run"}])
    paused = store.create_workflow("Paused", [{"title": "Task", "input": "Run"}])
    store.set_workflow_control(paused["id"], "paused")
    failed = store.create_workflow("Failed", [{"title": "Task", "input": "Run"}])
    store.update_workflow(failed["id"], "failed", results={"tasks": []})

    assert [item["id"] for item in store.list_workflows(status="running")] == [
        paused["id"], running["id"]
    ]
    assert [item["id"] for item in store.list_workflows(control_state="paused")] == [
        paused["id"]
    ]
    assert [item["id"] for item in store.list_workflows(status="failed")] == [
        failed["id"]
    ]
    assert [item["id"] for item in store.list_workflows(
        status="running", control_state="paused"
    )] == [paused["id"]]
    with pytest.raises(ValueError, match="status filter"):
        store.list_workflows(status="not-a-status")
    with pytest.raises(ValueError, match="control-state filter"):
        store.list_workflows(control_state="not-a-state")


def test_workflow_listing_api_filters_and_rejects_invalid_filters(tmp_path, monkeypatch):
    store, _router = configure(monkeypatch, tmp_path)
    paused = store.create_workflow("Paused API", [{"title": "Task", "input": "Run"}])
    store.set_workflow_control(paused["id"], "paused")
    monkeypatch.setattr(app_module, "store", store)

    response = app_module.list_agent_workflows(control_state="paused")
    assert [item["id"] for item in response["workflows"]] == [paused["id"]]
    with pytest.raises(HTTPException) as error:
        app_module.list_agent_workflows(status="invalid")
    assert error.value.status_code == 400
    with pytest.raises(HTTPException) as error:
        app_module.list_agent_workflows(control_state="invalid")
    assert error.value.status_code == 400


def test_workflow_listing_searches_goal_case_insensitively_and_escapes_wildcards(tmp_path, monkeypatch):
    store, _router = configure(monkeypatch, tmp_path)
    matching = store.create_workflow("Deploy Crows Agent", [])
    store.create_workflow("Review unrelated task", [])
    wildcard = store.create_workflow("Literal 100% coverage", [])

    assert [item["id"] for item in store.list_workflows(query="CROWS")] == [matching["id"]]
    assert [item["id"] for item in store.list_workflows(query="100%")] == [wildcard["id"]]
    assert store.list_workflows(query="   ") == store.list_workflows()
    with pytest.raises(ValueError, match="workflow query"):
        store.list_workflows(query="x" * 201)


def test_workflow_listing_api_search_combines_with_filters(tmp_path, monkeypatch):
    store, _router = configure(monkeypatch, tmp_path)
    matching = store.create_workflow("Crows deployment", [])
    store.create_workflow("Crows investigation", [])
    other_status = store.create_workflow("Other deployment", [])
    store.update_workflow(other_status["id"], "failed", results={"tasks": []})
    monkeypatch.setattr(app_module, "store", store)

    response = app_module.list_agent_workflows(query="deployment", status="running")
    assert [item["id"] for item in response["workflows"]] == [matching["id"]]
    with pytest.raises(HTTPException) as error:
        app_module.list_agent_workflows(query="x" * 201)
    assert error.value.status_code == 400


def test_workflow_listing_filters_creation_time_range(tmp_path, monkeypatch):
    store, _router = configure(monkeypatch, tmp_path)
    first = store.create_workflow("First", [])
    with store.connect() as con:
        first_created = con.execute(
            "SELECT created_at FROM agent_workflows WHERE id=?", (first["id"],)
        ).fetchone()["created_at"]
    second = store.create_workflow("Second", [])
    with store.connect() as con:
        second_created = con.execute(
            "SELECT created_at FROM agent_workflows WHERE id=?", (second["id"],)
        ).fetchone()["created_at"]
    assert [item["id"] for item in store.list_workflows(
        created_after=first_created, created_before=first_created
    )] == [first["id"]]
    assert [item["id"] for item in store.list_workflows(
        created_after=first_created, created_before=second_created
    )] == [second["id"], first["id"]]
    with pytest.raises(ValueError, match="must include a timezone"):
        store.list_workflows(created_after="2026-01-01T00:00:00")
    with pytest.raises(ValueError, match="must not be later"):
        store.list_workflows(created_after=second_created, created_before=first_created)


def test_workflow_listing_api_passes_creation_time_range(tmp_path, monkeypatch):
    store, _router = configure(monkeypatch, tmp_path)
    matching = store.create_workflow("Time filtered", [])
    monkeypatch.setattr(app_module, "store", store)
    with store.connect() as con:
        created = con.execute(
            "SELECT created_at FROM agent_workflows WHERE id=?", (matching["id"],)
        ).fetchone()["created_at"]
    response = app_module.list_agent_workflows(created_after=created, created_before=created)
    assert [item["id"] for item in response["workflows"]] == [matching["id"]]
    with pytest.raises(HTTPException) as error:
        app_module.list_agent_workflows(created_after="not-a-date")
    assert error.value.status_code == 400


def test_workflow_health_reports_stale_but_never_mutates_workflows(tmp_path, monkeypatch):
    store, _router = configure(monkeypatch, tmp_path)
    stale = store.create_workflow("Stale running", [])
    paused = store.create_workflow("Paused intentionally", [])
    store.set_workflow_control(paused["id"], "paused")
    unknown = store.create_workflow("Uncertain outcome", [])
    store.update_workflow(unknown["id"], "unknown", results={"tasks": []})
    old = "2020-01-01T00:00:00+00:00"
    with store.connect() as con:
        con.execute(
            "UPDATE agent_workflows SET updated_at=? WHERE id IN (?,?)",
            (old, stale["id"], unknown["id"]),
        )
        con.execute(
            "UPDATE workflow_controls SET updated_at=? WHERE workflow_id IN (?,?)",
            (old, stale["id"], unknown["id"]),
        )
    before = {item["id"]: item["status"] for item in store.list_workflows()}
    report = store.workflow_health(stale_after_seconds=60)
    assert report["stale_count"] == 2
    assert report["returned_count"] == 2
    assert {item["id"] for item in report["stale_workflows"]} == {stale["id"], unknown["id"]}
    bounded = store.workflow_health(stale_after_seconds=60, limit=1)
    assert bounded["stale_count"] == 2
    assert bounded["returned_count"] == 1
    assert paused["id"] not in {item["id"] for item in report["stale_workflows"]}
    assert all(item["age_seconds"] >= 60 for item in report["stale_workflows"])
    after = {item["id"]: item["status"] for item in store.list_workflows()}
    assert after == before


def test_workflow_health_api_validates_limits(tmp_path, monkeypatch):
    store, _router = configure(monkeypatch, tmp_path)
    monkeypatch.setattr(app_module, "store", store)
    assert app_module.get_agent_workflow_health(stale_after_seconds=60)["stale_count"] == 0
    with pytest.raises(HTTPException) as error:
        app_module.get_agent_workflow_health(stale_after_seconds=0)
    assert error.value.status_code == 400
