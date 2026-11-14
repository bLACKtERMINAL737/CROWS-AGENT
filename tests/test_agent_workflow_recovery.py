import asyncio

import pytest
from fastapi import HTTPException

from crows import app as app_module
from crows.store import TaskStore


def make_workflow(tmp_path, monkeypatch, *, task_status="queued", qa=True):
    store = TaskStore(tmp_path / "recovery.sqlite3")
    monkeypatch.setattr(app_module, "store", store)
    task = store.create_task("[research] Inspect files", "Read-only inspection")
    if task_status != "queued":
        store.set_task_status(task["id"], task_status, error="saved state")
    results = {
        "tasks": [{
            "task_id": task["id"],
            "title": "Inspect files",
            "role": "research",
            "status": "queued" if task_status == "queued" else task_status,
        }],
        "qa_review": None,
    }
    if qa:
        qa_task = store.create_task("[qa_security] Review specialist results", "Review saved results")
        results["qa_review"] = {"task_id": qa_task["id"], "status": "queued"}
    workflow = store.create_workflow("Inspect safely", [{"title": "Inspect files", "input": "Read-only inspection", "role": "research"}])
    store.update_workflow(workflow["id"], "unknown", results=results, error="simulated interruption")
    return store, workflow["id"], task["id"], results.get("qa_review", {}).get("task_id") if qa else None


@pytest.mark.asyncio
async def test_recovery_runs_only_checkpointed_queued_tasks(tmp_path, monkeypatch):
    store, workflow_id, task_id, qa_id = make_workflow(tmp_path, monkeypatch)
    executed = []

    async def fake_run(task_id_arg):
        task = store.get_task(task_id_arg)
        assert task is not None and task["status"] == "queued"
        assert store.claim_task(task_id_arg)
        store.set_task_status(task_id_arg, "succeeded", result={"answer": ("{\"verdict\":\"PASS\",\"summary\":\"Evidence is sufficient.\",\"findings\":[]}" if task["title"].startswith("[qa_security]") else "verified")})
        executed.append(task_id_arg)
        return {"task_id": task_id_arg, "status": "succeeded", "result": ("{\"verdict\":\"PASS\",\"summary\":\"Evidence is sufficient.\",\"findings\":[]}" if task["title"].startswith("[qa_security]") else "verified"), "tool_calls": 0, "tool_attempts": 0}

    monkeypatch.setattr(app_module, "run_agent_task", fake_run)
    result = await app_module.recover_agent_workflow(workflow_id)

    assert result["status"] == "succeeded"
    assert set(executed) == {task_id, qa_id}
    saved = store.get_workflow(workflow_id)
    assert saved["status"] == "succeeded"
    assert saved["results"]["tasks"][0]["status"] == "succeeded"
    assert saved["results"]["qa_review"]["status"] == "succeeded"
    diagnostics = saved["results"]["diagnostics"]
    assert diagnostics["task_counts"]["succeeded"] == 1
    assert diagnostics["qa_status"] == "succeeded"
    assert diagnostics["failure_count"] == 0
    assert diagnostics["failures"] == []


@pytest.mark.asyncio
async def test_recovery_never_replays_unknown_task_and_marks_partial(tmp_path, monkeypatch):
    store, workflow_id, task_id, qa_id = make_workflow(
        tmp_path, monkeypatch, task_status="unknown"
    )
    executed = []

    async def fake_run(task_id_arg):
        task = store.get_task(task_id_arg)
        assert task is not None and task["status"] == "queued"
        assert store.claim_task(task_id_arg)
        store.set_task_status(task_id_arg, "succeeded", result={"answer": ("{\"verdict\":\"PASS\",\"summary\":\"Evidence is sufficient.\",\"findings\":[]}" if task["title"].startswith("[qa_security]") else "reviewed")})
        executed.append(task_id_arg)
        return {"task_id": task_id_arg, "status": "succeeded", "result": ("{\"verdict\":\"PASS\",\"summary\":\"Evidence is sufficient.\",\"findings\":[]}" if task["title"].startswith("[qa_security]") else "reviewed"), "tool_calls": 0, "tool_attempts": 0}

    monkeypatch.setattr(app_module, "run_agent_task", fake_run)
    result = await app_module.recover_agent_workflow(workflow_id)

    assert task_id not in executed
    assert executed == [qa_id]
    assert result["status"] == "partial"
    assert store.get_task(task_id)["status"] == "unknown"
    assert store.get_workflow(workflow_id)["status"] == "partial"


@pytest.mark.asyncio
async def test_recovery_rejects_checkpoint_task_state_mismatch(tmp_path, monkeypatch):
    store, workflow_id, task_id, _qa_id = make_workflow(tmp_path, monkeypatch)
    store.set_task_status(task_id, "running")
    with pytest.raises(HTTPException) as error:
        await app_module.recover_agent_workflow(workflow_id)
    assert error.value.status_code == 409
    assert store.get_workflow(workflow_id)["status"] == "unknown"


def test_workflow_recovery_claim_is_atomic(tmp_path):
    store = TaskStore(tmp_path / "claim.sqlite3")
    workflow = store.create_workflow("Atomic claim", [])
    store.update_workflow(workflow["id"], "unknown", results={"tasks": [], "qa_review": None})

    assert store.claim_workflow_recovery(workflow["id"]) is True
    assert store.claim_workflow_recovery(workflow["id"]) is False
    assert store.get_workflow(workflow["id"])["status"] == "running"



@pytest.mark.asyncio
async def test_recovery_runs_prerequisite_with_later_plan_index_first(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "reverse-dependency.sqlite3")
    monkeypatch.setattr(app_module, "store", store)
    first = store.create_task("[research] Dependent first", "Wait for prerequisite")
    second = store.create_task("[research] Prerequisite second", "Run before dependent")
    qa = store.create_task("[qa_security] Review specialist results", "Review completed tasks")
    workflow = store.create_workflow(
        "Recover reverse dependency",
        [
            {"title": "Dependent first", "input": "Wait", "role": "research", "depends_on": [1]},
            {"title": "Prerequisite second", "input": "Prerequisite", "role": "research", "depends_on": []},
        ],
    )
    results = {
        "tasks": [
            {"plan_index": 0, "task_id": first["id"], "title": "Dependent first", "status": "queued", "depends_on": [1]},
            {"plan_index": 1, "task_id": second["id"], "title": "Prerequisite second", "status": "queued", "depends_on": []},
        ],
        "qa_review": {"task_id": qa["id"], "status": "queued"},
    }
    store.update_workflow(workflow["id"], "unknown", results=results, error="simulated interruption")
    executed = []

    async def fake_run(task_id_arg):
        task = store.get_task(task_id_arg)
        assert task is not None and task["status"] == "queued"
        assert store.claim_task(task_id_arg)
        if task_id_arg == first["id"]:
            assert second["id"] in executed, "dependent must wait for its prerequisite"
        answer = "{\"verdict\":\"PASS\",\"summary\":\"Evidence is sufficient.\",\"findings\":[]}" if task["title"].startswith("[qa_security]") else task["title"]
        store.set_task_status(task_id_arg, "succeeded", result={"answer": answer})
        executed.append(task_id_arg)
        return {
            "task_id": task_id_arg, "status": "succeeded", "result": answer,
            "tool_calls": 0, "tool_attempts": 0,
        }

    monkeypatch.setattr(app_module, "run_agent_task", fake_run)
    result = await app_module.recover_agent_workflow(workflow["id"])

    assert result["status"] == "succeeded"
    assert executed.index(second["id"]) < executed.index(first["id"])
    assert executed[-1] == qa["id"]


@pytest.mark.asyncio
async def test_recovery_rejects_cyclic_dependency_checkpoint_before_claim(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "cyclic-dependency.sqlite3")
    monkeypatch.setattr(app_module, "store", store)
    task = store.create_task("[research] Cyclic task", "Do not execute")
    workflow = store.create_workflow("Invalid dependency graph", [{"title": "Cyclic task"}])
    results = {
        "tasks": [{
            "plan_index": 0, "task_id": task["id"], "title": "Cyclic task",
            "status": "queued", "depends_on": [0],
        }],
        "qa_review": None,
    }
    store.update_workflow(workflow["id"], "unknown", results=results, error="simulated corrupt checkpoint")

    with pytest.raises(HTTPException) as error:
        await app_module.recover_agent_workflow(workflow["id"])

    assert error.value.status_code == 409
    assert store.get_workflow(workflow["id"])["status"] == "unknown"
    assert store.get_task(task["id"])["status"] == "queued"



@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda task: task.update(plan_index=7), "plan indexes"),
        (lambda task: task.update(status="mystery"), "task status"),
        (lambda task: task.update(task_id=""), "task IDs"),
    ],
)
@pytest.mark.asyncio
async def test_recovery_rejects_inconsistent_checkpoint_identity_before_claim(
    tmp_path, monkeypatch, mutate, message
):
    store = TaskStore(tmp_path / "checkpoint-integrity.sqlite3")
    monkeypatch.setattr(app_module, "store", store)
    task = store.create_task("[research] Must remain queued", "Do not run on invalid checkpoint")
    workflow = store.create_workflow(
        "Invalid checkpoint identity",
        [{"title": "Must remain queued", "input": "Inspect", "role": "research"}],
    )
    saved_task = {
        "plan_index": 0, "task_id": task["id"], "title": "Must remain queued",
        "status": "queued", "depends_on": [],
    }
    mutate(saved_task)
    store.update_workflow(
        workflow["id"], "unknown",
        results={"tasks": [saved_task], "qa_review": None},
        error="simulated inconsistent checkpoint",
    )

    with pytest.raises(HTTPException) as error:
        await app_module.recover_agent_workflow(workflow["id"])

    assert error.value.status_code == 409
    assert store.get_workflow(workflow["id"])["status"] == "unknown"
    assert store.get_task(task["id"])["status"] == "queued"



@pytest.mark.asyncio
async def test_recovery_rejects_succeeded_checkpoint_when_task_is_still_queued(
    tmp_path, monkeypatch
):
    store = TaskStore(tmp_path / "journal-mismatch.sqlite3")
    monkeypatch.setattr(app_module, "store", store)
    prerequisite = store.create_task("[research] Prerequisite", "Must actually have completed")
    dependent = store.create_task("[research] Dependent", "Must not run if prerequisite is uncertain")
    qa = store.create_task("[qa_security] Review", "Review completed work")
    workflow = store.create_workflow(
        "Reject journal mismatch",
        [
            {"title": "Prerequisite", "input": "Complete prerequisite", "role": "research"},
            {"title": "Dependent", "input": "Use prerequisite", "role": "research", "depends_on": [0]},
        ],
    )
    checkpoint = {
        "tasks": [
            {
                "plan_index": 0, "task_id": prerequisite["id"], "title": "Prerequisite",
                "status": "succeeded", "depends_on": [], "result": "fabricated checkpoint result",
            },
            {
                "plan_index": 1, "task_id": dependent["id"], "title": "Dependent",
                "status": "queued", "depends_on": [0],
            },
        ],
        "qa_review": {"task_id": qa["id"], "status": "queued"},
    }
    store.update_workflow(workflow["id"], "unknown", results=checkpoint, error="simulated crash")
    executed = []

    async def should_not_run(task_id):
        executed.append(task_id)
        raise AssertionError("recovery must reject inconsistent checkpoint before task execution")

    monkeypatch.setattr(app_module, "run_agent_task", should_not_run)
    with pytest.raises(HTTPException) as error:
        await app_module.recover_agent_workflow(workflow["id"])

    assert error.value.status_code == 409
    assert executed == []
    assert store.get_task(prerequisite["id"])["status"] == "queued"
    assert store.get_task(dependent["id"])["status"] == "queued"
    assert store.get_workflow(workflow["id"])["status"] == "unknown"


@pytest.mark.asyncio
async def test_recovery_accepts_not_started_qa_checkpoint_without_replaying_qa(tmp_path, monkeypatch):
    store, workflow_id, task_id, _qa_id = make_workflow(tmp_path, monkeypatch, qa=False)
    workflow = store.get_workflow(workflow_id)
    checkpoint = workflow["results"]
    checkpoint["qa_review"] = {
        "status": "not_started",
        "error": "QA task was not checkpointed before interruption",
    }
    store.update_workflow(
        workflow_id, "unknown", results=checkpoint, error="simulated interruption"
    )
    executed = []

    async def fake_run(task_id_arg):
        assert task_id_arg == task_id
        task = store.get_task(task_id_arg)
        assert task is not None and task["status"] == "queued"
        assert store.claim_task(task_id_arg)
        store.set_task_status(task_id_arg, "succeeded", result={"answer": ("{\"verdict\":\"PASS\",\"summary\":\"Evidence is sufficient.\",\"findings\":[]}" if task["title"].startswith("[qa_security]") else "verified")})
        executed.append(task_id_arg)
        return {
            "task_id": task_id_arg,
            "status": "succeeded",
            "result": ("{\"verdict\":\"PASS\",\"summary\":\"Evidence is sufficient.\",\"findings\":[]}" if task["title"].startswith("[qa_security]") else "verified"),
            "tool_calls": 0,
            "tool_attempts": 0,
        }

    monkeypatch.setattr(app_module, "run_agent_task", fake_run)
    result = await app_module.recover_agent_workflow(workflow_id)

    assert result["status"] == "partial"
    assert executed == [task_id]
    assert result["qa_review"]["status"] == "not_started"
    assert store.get_workflow(workflow_id)["status"] == "partial"


@pytest.mark.asyncio
async def test_recovery_rejects_checkpoint_that_disagrees_with_saved_plan_before_claim(
    tmp_path, monkeypatch
):
    store, workflow_id, _task_id, _qa_id = make_workflow(tmp_path, monkeypatch, qa=False)
    workflow = store.get_workflow(workflow_id)
    checkpoint = workflow["results"]
    checkpoint["tasks"][0]["title"] = "Different task than the saved plan"
    # The store intentionally makes unknown checkpoints immutable through its normal
    # update API. Simulate persisted corruption by using the guarded recovery transition
    # and writing the checkpoint while running, then interrupt it back to unknown.
    assert store.claim_workflow_recovery(workflow_id)
    store.update_workflow(workflow_id, "running", results=checkpoint)
    store.update_workflow(workflow_id, "unknown", error="simulated corruption")

    with pytest.raises(HTTPException) as exc:
        await app_module.recover_agent_workflow(workflow_id)

    assert exc.value.status_code == 409
    assert store.get_workflow(workflow_id)["status"] == "unknown"


@pytest.mark.asyncio
async def test_recovery_rejects_qa_checkpoint_reusing_task_id_before_claim(tmp_path, monkeypatch):
    store, workflow_id, task_id, _qa_id = make_workflow(tmp_path, monkeypatch)
    workflow = store.get_workflow(workflow_id)
    checkpoint = workflow["results"]
    checkpoint["qa_review"]["task_id"] = task_id
    assert store.claim_workflow_recovery(workflow_id)
    store.update_workflow(workflow_id, "running", results=checkpoint)
    store.update_workflow(workflow_id, "unknown", error="simulated corrupted QA checkpoint")

    with pytest.raises(HTTPException) as exc:
        await app_module.recover_agent_workflow(workflow_id)

    assert exc.value.status_code == 409
    assert store.get_workflow(workflow_id)["status"] == "unknown"


@pytest.mark.asyncio
async def test_recovery_rejects_qa_checkpoint_that_disagrees_with_task_journal(
    tmp_path, monkeypatch
):
    store, workflow_id, _task_id, qa_id = make_workflow(tmp_path, monkeypatch)
    assert qa_id is not None
    store.set_task_status(qa_id, "failed", error="saved QA failure")

    with pytest.raises(HTTPException) as exc:
        await app_module.recover_agent_workflow(workflow_id)

    assert exc.value.status_code == 409
    assert store.get_workflow(workflow_id)["status"] == "unknown"



@pytest.mark.parametrize("invalid_dependencies", [[True], [0, 0]])
@pytest.mark.asyncio
async def test_recovery_rejects_invalid_dependency_index_types_and_duplicates(
    tmp_path, monkeypatch, invalid_dependencies
):
    store = TaskStore(tmp_path / "recovery.sqlite3")
    monkeypatch.setattr(app_module, "store", store)
    task = store.create_task("[research] Inspect files", "Read-only inspection")
    plan = [{
        "title": "Inspect files",
        "input": "Read-only inspection",
        "role": "research",
        "depends_on": invalid_dependencies,
    }]
    checkpoint = {
        "tasks": [{
            "task_id": task["id"],
            "title": "Inspect files",
            "role": "research",
            "status": "queued",
            "depends_on": invalid_dependencies,
        }],
        "qa_review": None,
    }
    workflow = store.create_workflow("Inspect safely", plan)
    workflow_id = workflow["id"]
    store.update_workflow(
        workflow_id, "unknown", results=checkpoint, error="simulated malformed dependency metadata"
    )

    with pytest.raises(HTTPException) as exc:
        await app_module.recover_agent_workflow(workflow_id)

    assert exc.value.status_code == 409
    assert store.get_workflow(workflow_id)["status"] == "unknown"


@pytest.mark.parametrize("invalid_dependencies", [[-1], [1]])
@pytest.mark.asyncio
async def test_recovery_rejects_out_of_range_dependency_indexes(
    tmp_path, monkeypatch, invalid_dependencies
):
    store = TaskStore(tmp_path / "recovery.sqlite3")
    monkeypatch.setattr(app_module, "store", store)
    task = store.create_task("[research] Inspect files", "Read-only inspection")
    plan = [{
        "title": "Inspect files",
        "input": "Read-only inspection",
        "role": "research",
        "depends_on": invalid_dependencies,
    }]
    checkpoint = {
        "tasks": [{
            "task_id": task["id"],
            "title": "Inspect files",
            "role": "research",
            "status": "queued",
            "depends_on": invalid_dependencies,
        }],
        "qa_review": None,
    }
    workflow = store.create_workflow("Inspect safely", plan)
    workflow_id = workflow["id"]
    store.update_workflow(
        workflow_id, "unknown", results=checkpoint,
        error="simulated out-of-range dependency metadata"
    )

    with pytest.raises(HTTPException) as exc:
        await app_module.recover_agent_workflow(workflow_id)

    assert exc.value.status_code == 409
    assert store.get_workflow(workflow_id)["status"] == "unknown"


@pytest.mark.asyncio
async def test_recovery_rejects_checkpoint_role_that_differs_from_saved_plan(
    tmp_path, monkeypatch
):
    store, workflow_id, _task_id, _qa_id = make_workflow(tmp_path, monkeypatch, qa=False)
    workflow = store.get_workflow(workflow_id)
    checkpoint = workflow["results"]
    checkpoint["tasks"][0]["role"] = "qa_security"
    assert store.claim_workflow_recovery(workflow_id)
    store.update_workflow(workflow_id, "running", results=checkpoint)
    store.update_workflow(workflow_id, "unknown", error="simulated role mismatch")

    with pytest.raises(HTTPException) as exc:
        await app_module.recover_agent_workflow(workflow_id)

    assert exc.value.status_code == 409
    assert store.get_workflow(workflow_id)["status"] == "unknown"


@pytest.mark.asyncio
async def test_recovery_rejects_unknown_qa_checkpoint_status_with_conflict(
    tmp_path, monkeypatch
):
    store, workflow_id, _task_id, _qa_id = make_workflow(tmp_path, monkeypatch)
    workflow = store.get_workflow(workflow_id)
    checkpoint = workflow["results"]
    checkpoint["qa_review"]["status"] = "mystery"
    assert store.claim_workflow_recovery(workflow_id)
    store.update_workflow(workflow_id, "running", results=checkpoint)
    store.update_workflow(workflow_id, "unknown", error="simulated invalid QA status")

    with pytest.raises(HTTPException) as exc:
        await app_module.recover_agent_workflow(workflow_id)

    assert exc.value.status_code == 409
    assert store.get_workflow(workflow_id)["status"] == "unknown"


@pytest.mark.parametrize("invalid_task_id", [None, "", "   ", 17, [], {}])
@pytest.mark.asyncio
async def test_recovery_rejects_invalid_qa_task_id_before_journal_lookup(
    tmp_path, monkeypatch, invalid_task_id
):
    store, workflow_id, _task_id, _qa_id = make_workflow(tmp_path, monkeypatch)
    workflow = store.get_workflow(workflow_id)
    checkpoint = workflow["results"]
    checkpoint["qa_review"]["task_id"] = invalid_task_id
    assert store.claim_workflow_recovery(workflow_id)
    store.update_workflow(workflow_id, "running", results=checkpoint)
    store.update_workflow(workflow_id, "unknown", error="simulated invalid QA task id")

    with pytest.raises(HTTPException) as exc:
        await app_module.recover_agent_workflow(workflow_id)

    assert exc.value.status_code == 409
    assert store.get_workflow(workflow_id)["status"] == "unknown"


@pytest.mark.asyncio
async def test_recovery_rejects_qa_checkpoint_pointing_to_non_qa_task(
    tmp_path, monkeypatch
):
    store, workflow_id, task_id, _qa_id = make_workflow(tmp_path, monkeypatch)
    workflow = store.get_workflow(workflow_id)
    checkpoint = workflow["results"]
    checkpoint["qa_review"]["task_id"] = task_id
    # Keep the referenced task queued so a status-only check would accept it.
    assert store.get_task(task_id)["status"] == "queued"
    assert store.claim_workflow_recovery(workflow_id)
    store.update_workflow(workflow_id, "running", results=checkpoint)
    store.update_workflow(workflow_id, "unknown", error="simulated QA identity corruption")

    with pytest.raises(HTTPException) as exc:
        await app_module.recover_agent_workflow(workflow_id)

    assert exc.value.status_code == 409
    assert store.get_workflow(workflow_id)["status"] == "unknown"
    assert store.get_task(task_id)["status"] == "queued"


@pytest.mark.asyncio
async def test_recovery_rejects_checkpoint_task_id_for_different_specialist(
    tmp_path, monkeypatch
):
    store, workflow_id, task_id, qa_id = make_workflow(tmp_path, monkeypatch)
    other_task = store.create_task("[research] Different task", "Unrelated task")
    workflow = store.get_workflow(workflow_id)
    checkpoint = workflow["results"]
    checkpoint["tasks"][0]["task_id"] = other_task["id"]
    assert other_task["status"] == "queued"
    assert store.claim_workflow_recovery(workflow_id)
    store.update_workflow(workflow_id, "running", results=checkpoint)
    store.update_workflow(workflow_id, "unknown", error="simulated task identity corruption")

    with pytest.raises(HTTPException) as exc:
        await app_module.recover_agent_workflow(workflow_id)

    assert exc.value.status_code == 409
    assert store.get_workflow(workflow_id)["status"] == "unknown"
    assert store.get_task(other_task["id"])["status"] == "queued"
    assert store.get_task(task_id)["status"] == "queued"
    assert store.get_task(qa_id)["status"] == "queued"


@pytest.mark.asyncio
async def test_recovery_rejects_checkpoint_pointing_to_different_qa_task(
    tmp_path, monkeypatch
):
    store, workflow_id, _task_id, _qa_id = make_workflow(tmp_path, monkeypatch)
    other_qa_task = store.create_task(
        "[qa_security] Review another workflow", "Unrelated QA task"
    )
    workflow = store.get_workflow(workflow_id)
    checkpoint = workflow["results"]
    checkpoint["qa_review"]["task_id"] = other_qa_task["id"]
    assert other_qa_task["status"] == "queued"
    assert store.claim_workflow_recovery(workflow_id)
    store.update_workflow(workflow_id, "running", results=checkpoint)
    store.update_workflow(workflow_id, "unknown", error="simulated QA task substitution")

    with pytest.raises(HTTPException) as exc:
        await app_module.recover_agent_workflow(workflow_id)

    assert exc.value.status_code == 409
    assert store.get_workflow(workflow_id)["status"] == "unknown"
    assert store.get_task(other_qa_task["id"])["status"] == "queued"


@pytest.mark.asyncio
async def test_recovery_cancellation_persists_unknown_workflow_checkpoint(
    tmp_path, monkeypatch
):
    store, workflow_id, task_id, _qa_id = make_workflow(
        tmp_path, monkeypatch, qa=False
    )

    async def cancelled_run(task_id_arg):
        assert task_id_arg == task_id
        assert store.claim_task(task_id_arg)
        raise asyncio.CancelledError()

    import asyncio

    monkeypatch.setattr(app_module, "run_agent_task", cancelled_run)

    with pytest.raises(asyncio.CancelledError):
        await app_module.recover_agent_workflow(workflow_id)

    saved = store.get_workflow(workflow_id)
    assert saved["status"] == "unknown"
    assert "cancelled" in saved["error_text"].lower()
    assert saved["results"]["tasks"][0]["status"] == "unknown"
    assert store.get_task(task_id)["status"] == "running"


@pytest.mark.asyncio
# Recovery timeout cases assert the final persisted workflow status.
async def test_recovery_task_timeout_marks_task_unknown_without_replay(
    tmp_path, monkeypatch
):
    store, workflow_id, task_id, qa_id = make_workflow(tmp_path, monkeypatch)
    monkeypatch.setenv("AETHER_AGENT_TASK_TIMEOUT_SECONDS", "1")
    never_release = __import__("asyncio").Event()
    executed = []

    async def fake_run(task_id_arg):
        executed.append(task_id_arg)
        task = store.get_task(task_id_arg)
        assert task is not None
        assert store.claim_task(task_id_arg)
        if task_id_arg == task_id:
            await never_release.wait()
        store.set_task_status(task_id_arg, "succeeded", result={"answer": ("{\"verdict\":\"PASS\",\"summary\":\"Evidence is sufficient.\",\"findings\":[]}" if task["title"].startswith("[qa_security]") else "reviewed")})
        return {
            "task_id": task_id_arg, "status": "succeeded", "result": ("{\"verdict\":\"PASS\",\"summary\":\"Evidence is sufficient.\",\"findings\":[]}" if task["title"].startswith("[qa_security]") else "reviewed"),
            "tool_calls": 0, "tool_attempts": 0,
        }

    monkeypatch.setattr(app_module, "run_agent_task", fake_run)
    result = await app_module.recover_agent_workflow(workflow_id)

    assert result["status"] == "partial"
    assert "uncertain" in result["tasks"][0]["error"].lower()
    assert executed == [task_id, qa_id]
    assert store.get_task(task_id)["status"] == "unknown"
    saved = store.get_workflow(workflow_id)
    assert saved["status"] == "partial"
    assert saved["results"]["tasks"][0]["status"] == "unknown"
    diagnostics = saved["results"]["diagnostics"]
    assert diagnostics["task_counts"]["unknown"] == 1
    assert diagnostics["qa_status"] == "succeeded"
    assert diagnostics["failure_count"] == 1
    assert diagnostics["failures"][0]["category"] == "outcome_unknown"


@pytest.mark.asyncio
async def test_recovery_qa_timeout_persists_uncertain_workflow_state(
    tmp_path, monkeypatch
):
    store, workflow_id, task_id, qa_id = make_workflow(tmp_path, monkeypatch)
    monkeypatch.setenv("AETHER_AGENT_TASK_TIMEOUT_SECONDS", "1")
    never_release = __import__("asyncio").Event()

    async def fake_run(task_id_arg):
        task = store.get_task(task_id_arg)
        assert task is not None
        assert store.claim_task(task_id_arg)
        if task_id_arg == task_id:
            store.set_task_status(task_id_arg, "succeeded", result={"answer": "done"})
            return {
                "task_id": task_id_arg, "status": "succeeded", "result": "done",
                "tool_calls": 0, "tool_attempts": 0,
            }
        await never_release.wait()
        store.set_task_status(task_id_arg, "succeeded", result={"answer": ("{\"verdict\":\"PASS\",\"summary\":\"Evidence is sufficient.\",\"findings\":[]}" if task["title"].startswith("[qa_security]") else "reviewed")})
        return {
            "task_id": task_id_arg, "status": "succeeded", "result": ("{\"verdict\":\"PASS\",\"summary\":\"Evidence is sufficient.\",\"findings\":[]}" if task["title"].startswith("[qa_security]") else "reviewed"),
            "tool_calls": 0, "tool_attempts": 0,
        }

    monkeypatch.setattr(app_module, "run_agent_task", fake_run)
    result = await app_module.recover_agent_workflow(workflow_id)

    assert result["status"] == "partial"
    assert store.get_task(task_id)["status"] == "succeeded"
    assert store.get_task(qa_id)["status"] == "unknown"
    saved = store.get_workflow(workflow_id)
    assert saved["status"] == "partial"
    assert saved["results"]["qa_review"]["status"] == "unknown"


@pytest.mark.parametrize(
    ("problem", "expected_category", "expected_task_status", "expected_qa_status"),
    [
        ("task_failed", "task_failed", "failed", "succeeded"),
        ("task_unknown", "outcome_unknown", "unknown", "succeeded"),
        ("qa_failed", "qa_review_failed", "succeeded", "failed"),
    ],
)
@pytest.mark.asyncio
async def test_recovery_diagnostics_cover_failed_and_uncertain_outcomes(
    tmp_path, monkeypatch, problem, expected_category,
    expected_task_status, expected_qa_status,
):
    store, workflow_id, task_id, qa_id = make_workflow(tmp_path, monkeypatch)
    assert qa_id is not None

    async def fake_run(task_id_arg):
        task = store.get_task(task_id_arg)
        assert task is not None and task["status"] == "queued"
        assert store.claim_task(task_id_arg)
        is_problem = (
            problem == "qa_failed" and task_id_arg == qa_id
        ) or (
            problem != "qa_failed" and task_id_arg == task_id
        )
        status = (
            "failed" if is_problem and problem in {"task_failed", "qa_failed"}
            else "unknown" if is_problem and problem == "task_unknown"
            else "succeeded"
        )
        answer = "{\"verdict\":\"PASS\",\"summary\":\"Evidence is sufficient.\",\"findings\":[]}" if task["title"].startswith("[qa_security]") and status == "succeeded" else "test outcome"
        store.set_task_status(task_id_arg, status, result={"answer": answer})
        return {
            "task_id": task_id_arg,
            "status": status,
            "result": answer,
            "tool_calls": 0,
            "tool_attempts": 0,
        }

    monkeypatch.setattr(app_module, "run_agent_task", fake_run)
    result = await app_module.recover_agent_workflow(workflow_id)

    saved = store.get_workflow(workflow_id)
    diagnostics = saved["results"]["diagnostics"]
    assert result["status"] == "partial"
    assert saved["status"] == "partial"
    assert saved["results"]["tasks"][0]["status"] == expected_task_status
    assert saved["results"]["qa_review"]["status"] == expected_qa_status
    assert diagnostics["task_counts"][expected_task_status] == 1
    assert diagnostics["qa_status"] == expected_qa_status
    assert diagnostics["failure_count"] == 1
    assert diagnostics["failures"][0]["category"] == expected_category



def test_concurrent_workflow_recovery_claim_has_exactly_one_winner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    db_path = tmp_path / "concurrent-recovery.sqlite3"
    store = TaskStore(db_path)
    workflow = store.create_workflow("Concurrent recovery", [])
    store.update_workflow(
        workflow["id"], "unknown",
        results={"tasks": [], "qa_review": None},
        error="simulated interruption",
    )
    stores = [TaskStore(db_path) for _ in range(8)]
    barrier = Barrier(len(stores))

    def claim(candidate):
        barrier.wait(timeout=5)
        return candidate.claim_workflow_recovery(workflow["id"])

    with ThreadPoolExecutor(max_workers=len(stores)) as pool:
        outcomes = list(pool.map(claim, stores))

    assert outcomes.count(True) == 1
    assert outcomes.count(False) == len(stores) - 1
    assert store.get_workflow(workflow["id"])["status"] == "running"



@pytest.mark.asyncio
async def test_recovery_cancellation_after_task_success_keeps_checkpoint_recoverable(
    tmp_path, monkeypatch
):
    store, workflow_id, task_id, _qa_id = make_workflow(
        tmp_path, monkeypatch, qa=False
    )

    async def complete_then_cancel(task_id_arg):
        assert task_id_arg == task_id
        assert store.claim_task(task_id_arg)
        store.set_task_status(
            task_id_arg, "succeeded", result={"answer": "completed before cancellation"}
        )
        raise asyncio.CancelledError()

    monkeypatch.setattr(app_module, "run_agent_task", complete_then_cancel)

    with pytest.raises(asyncio.CancelledError):
        await app_module.recover_agent_workflow(workflow_id)

    saved = store.get_workflow(workflow_id)
    assert saved["status"] == "unknown"
    assert saved["results"]["tasks"][0]["status"] == "succeeded"
    assert store.get_task(task_id)["status"] == "succeeded"



@pytest.mark.asyncio
async def test_recovery_blocks_dependents_after_prerequisite_failure_and_records_diagnostics(
    tmp_path, monkeypatch
):
    store = TaskStore(tmp_path / "dependency-blocking-recovery.sqlite3")
    monkeypatch.setattr(app_module, "store", store)
    prerequisite = store.create_task("[research] Gather evidence", "Gather evidence")
    dependent = store.create_task("[coding] Prepare summary", "Use gathered evidence")
    qa = store.create_task("[qa_security] Review specialist results", "Review recovered results")
    plan = [
        {
            "title": "Gather evidence",
            "input": "Gather evidence",
            "role": "research",
            "depends_on": [],
        },
        {
            "title": "Prepare summary",
            "input": "Use gathered evidence",
            "role": "coding",
            "depends_on": [0],
        },
    ]
    checkpoint = {
        "tasks": [
            {
                "plan_index": 0,
                "task_id": prerequisite["id"],
                "title": "Gather evidence",
                "role": "research",
                "status": "queued",
                "depends_on": [],
            },
            {
                "plan_index": 1,
                "task_id": dependent["id"],
                "title": "Prepare summary",
                "role": "coding",
                "status": "queued",
                "depends_on": [0],
            },
        ],
        "qa_review": {"task_id": qa["id"], "status": "queued"},
    }
    workflow = store.create_workflow("Recover dependency chain", plan)
    store.update_workflow(
        workflow["id"], "unknown", results=checkpoint, error="simulated interruption"
    )
    executed = []

    async def fake_run(task_id_arg):
        task = store.get_task(task_id_arg)
        assert task is not None and task["status"] == "queued"
        assert store.claim_task(task_id_arg)
        executed.append(task_id_arg)
        if task_id_arg == prerequisite["id"]:
            store.set_task_status(task_id_arg, "failed", error="prerequisite failed")
            return {
                "task_id": task_id_arg,
                "status": "failed",
                "result": "",
                "tool_calls": 0,
                "tool_attempts": 0,
            }
        assert task_id_arg == qa["id"]
        answer = (
            '{"verdict":"PASS","summary":"Reviewed available results.",'
            '"findings":[]}'
        )
        store.set_task_status(task_id_arg, "succeeded", result={"answer": answer})
        return {
            "task_id": task_id_arg,
            "status": "succeeded",
            "result": answer,
            "tool_calls": 0,
            "tool_attempts": 0,
        }

    monkeypatch.setattr(app_module, "run_agent_task", fake_run)
    result = await app_module.recover_agent_workflow(workflow["id"])

    assert result["status"] == "partial"
    assert executed == [prerequisite["id"], qa["id"]]
    assert store.get_task(prerequisite["id"])["status"] == "failed"
    # Blocking is a workflow-checkpoint outcome; the unclaimed durable task stays queued.
    assert store.get_task(dependent["id"])["status"] == "queued"
    assert store.get_task(qa["id"])["status"] == "succeeded"

    saved = store.get_workflow(workflow["id"])
    by_index = {
        item["plan_index"]: item for item in saved["results"]["tasks"]
    }
    assert by_index[0]["status"] == "failed"
    assert by_index[1]["status"] == "blocked"
    assert by_index[1]["depends_on"] == [0]
    diagnostics = saved["results"]["diagnostics"]
    assert diagnostics["task_counts"]["failed"] == 1
    assert diagnostics["task_counts"]["blocked"] == 1
    assert diagnostics["qa_status"] == "succeeded"
    assert diagnostics["failure_count"] == 2
    assert {item["category"] for item in diagnostics["failures"]} == {
        "task_failed",
        "dependency_blocked",
    }


@pytest.mark.asyncio
async def test_recovery_cancellation_during_qa_persists_rebuilt_diagnostics(
    tmp_path, monkeypatch
):
    store, workflow_id, task_id, qa_id = make_workflow(tmp_path, monkeypatch)
    assert qa_id is not None

    async def complete_specialist_then_cancel_qa(task_id_arg):
        task = store.get_task(task_id_arg)
        assert task is not None and store.claim_task(task_id_arg)
        if task_id_arg == task_id:
            store.set_task_status(
                task_id_arg, "succeeded", result={"answer": "specialist evidence"}
            )
            return {
                "task_id": task_id_arg,
                "status": "succeeded",
                "result": "specialist evidence",
                "tool_calls": 0,
                "tool_attempts": 0,
            }
        assert task_id_arg == qa_id
        raise asyncio.CancelledError()

    monkeypatch.setattr(
        app_module, "run_agent_task", complete_specialist_then_cancel_qa
    )
    with pytest.raises(asyncio.CancelledError):
        await app_module.recover_agent_workflow(workflow_id)

    saved = store.get_workflow(workflow_id)
    assert saved["status"] == "unknown"
    assert saved["results"]["tasks"][0]["status"] == "succeeded"
    assert saved["results"]["qa_review"]["status"] == "unknown"
    assert store.get_task(task_id)["status"] == "succeeded"
    assert store.get_task(qa_id)["status"] == "running"
    diagnostics = saved["results"]["diagnostics"]
    assert diagnostics["task_counts"]["succeeded"] == 1
    assert diagnostics["qa_status"] == "unknown"
    assert diagnostics["qa_verdict"] == "UNVERIFIED"
    assert diagnostics["failure_count"] == 1
    assert diagnostics["failures"][0]["category"] == "qa_review_failed"
    assert diagnostics["failures"][0]["task_id"] == qa_id


@pytest.mark.asyncio
async def test_recovery_unexpected_error_rebuilds_diagnostics_from_task_journal(
    tmp_path, monkeypatch
):
    store, workflow_id, task_id, qa_id = make_workflow(tmp_path, monkeypatch)
    assert qa_id is not None

    async def fail_after_durable_task_failure(task_id_arg):
        assert task_id_arg == task_id
        assert store.claim_task(task_id_arg)
        store.set_task_status(task_id_arg, "failed", error="specialist failed")
        raise RuntimeError("simulated recovery orchestration failure")

    monkeypatch.setattr(app_module, "run_agent_task", fail_after_durable_task_failure)
    with pytest.raises(RuntimeError, match="simulated recovery orchestration failure"):
        await app_module.recover_agent_workflow(workflow_id)

    saved = store.get_workflow(workflow_id)
    assert saved["status"] == "unknown"
    assert saved["results"]["tasks"][0]["status"] == "failed"
    assert saved["results"]["qa_review"]["status"] == "queued"
    diagnostics = saved["results"]["diagnostics"]
    assert diagnostics["task_counts"]["failed"] == 1
    assert diagnostics["qa_status"] == "queued"
    assert diagnostics["failure_count"] == 1
    assert diagnostics["failures"][0]["category"] == "task_failed"
    assert diagnostics["failures"][0]["task_id"] == task_id



def test_recovery_plan_preview_is_read_only_and_marks_uncertain_tasks(tmp_path, monkeypatch):
    store, workflow_id, task_id, qa_id = make_workflow(
        tmp_path, monkeypatch, task_status="unknown"
    )
    before = store.get_workflow(workflow_id)
    preview = app_module.get_agent_workflow_recovery_plan(workflow_id)
    after = store.get_workflow(workflow_id)

    assert preview["recoverable"] is True
    assert preview["execution_started"] is False
    assert preview["state_mutated"] is False
    task = next(item for item in preview["tasks"] if item["task_id"] == task_id)
    assert task["disposition"] == "uncertain_never_replay"
    assert preview["qa_review"]["task_id"] == qa_id
    assert preview["qa_review"]["disposition"] == "eligible_to_resume"
    assert before["status"] == after["status"] == "unknown"
    assert before["results"] == after["results"]
    assert store.get_task(task_id)["status"] == "unknown"
    assert store.get_task(qa_id)["status"] == "queued"


def test_recovery_plan_preview_rejects_checkpoint_journal_mismatch(tmp_path, monkeypatch):
    store, workflow_id, task_id, _qa_id = make_workflow(tmp_path, monkeypatch)
    store.set_task_status(task_id, "running")
    preview = app_module.get_agent_workflow_recovery_plan(workflow_id)
    task = next(item for item in preview["tasks"] if item["task_id"] == task_id)
    assert preview["recoverable"] is False
    assert "checkpoint_journal_mismatch" in task["issues"]
    assert task["disposition"] == "manual_inspection_required"


def test_recovery_plan_preview_returns_404_for_missing_workflow(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "missing-preview.sqlite3")
    monkeypatch.setattr(app_module, "store", store)
    with pytest.raises(HTTPException) as error:
        app_module.get_agent_workflow_recovery_plan("missing")
    assert error.value.status_code == 404


def test_workflow_recovery_audit_is_durable_and_paginated(tmp_path, monkeypatch):
    store, workflow_id, _task_id, _qa_id = make_workflow(tmp_path, monkeypatch)
    attempt_id = store.record_workflow_recovery_event(
        workflow_id, "started", details={"queued_task_count": 1}
    )
    store.record_workflow_recovery_event(
        workflow_id, "completed", attempt_id=attempt_id,
        details={"workflow_status": "succeeded", "succeeded_task_count": 1},
    )
    first = store.list_workflow_recovery_audit(workflow_id, limit=1, offset=0)
    second = store.list_workflow_recovery_audit(workflow_id, limit=1, offset=1)
    assert first["pagination"] == {
        "limit": 1, "offset": 0, "total": 2, "returned": 1, "has_more": True
    }
    assert first["events"][0]["event_type"] == "completed"
    assert second["events"][0]["event_type"] == "started"
    assert first["events"][0]["attempt_id"] == second["events"][0]["attempt_id"] == attempt_id
    assert first["events"][0]["details"]["workflow_status"] == "succeeded"
    with pytest.raises(ValueError):
        store.list_workflow_recovery_audit(workflow_id, limit=0)


def test_workflow_recovery_audit_rejects_invalid_events_and_oversized_details(tmp_path, monkeypatch):
    store, workflow_id, _task_id, _qa_id = make_workflow(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="Invalid workflow recovery audit event"):
        store.record_workflow_recovery_event(workflow_id, "secret_payload")
    with pytest.raises(ValueError, match="exceed 4000"):
        store.record_workflow_recovery_event(workflow_id, "started", details={"note": "x" * 5000})
    with pytest.raises(KeyError):
        store.record_workflow_recovery_event("missing", "started")


def test_recovery_endpoint_writes_durable_start_and_completion_events(tmp_path, monkeypatch):
    store, workflow_id, task_id, qa_id = make_workflow(tmp_path, monkeypatch)
    async def fake_run(task_id_arg):
        task = store.get_task(task_id_arg)
        assert task is not None and task["status"] == "queued"
        assert store.claim_task(task_id_arg)
        result = (
            '{"verdict":"PASS","summary":"Evidence is sufficient.","findings":[]}'
            if task["title"].startswith("[qa_security]") else "verified"
        )
        store.set_task_status(task_id_arg, "succeeded", result={"answer": result})
        return {"task_id": task_id_arg, "status": "succeeded", "result": result, "tool_calls": 0, "tool_attempts": 0}
    monkeypatch.setattr(app_module, "run_agent_task", fake_run)
    result = asyncio.run(app_module.recover_agent_workflow(workflow_id))
    assert result["status"] == "succeeded"
    audit = store.list_workflow_recovery_audit(workflow_id)
    assert [event["event_type"] for event in reversed(audit["events"])] == ["started", "completed"]
    assert audit["events"][0]["attempt_id"] == audit["events"][1]["attempt_id"]
    assert audit["events"][0]["details"]["workflow_status"] == "succeeded"


def test_workflow_recovery_audit_api_validates_and_returns_404(tmp_path, monkeypatch):
    store, workflow_id, _task_id, _qa_id = make_workflow(tmp_path, monkeypatch)
    assert app_module.get_agent_workflow_recovery_audit(workflow_id)["pagination"]["total"] == 0
    with pytest.raises(HTTPException) as invalid:
        app_module.get_agent_workflow_recovery_audit(workflow_id, limit=0)
    assert invalid.value.status_code == 400
    with pytest.raises(HTTPException) as missing:
        app_module.get_agent_workflow_recovery_audit("missing")
    assert missing.value.status_code == 404


def test_recovery_does_not_start_tasks_when_start_audit_cannot_persist(tmp_path, monkeypatch):
    store, workflow_id, task_id, _qa_id = make_workflow(tmp_path, monkeypatch)
    async def should_not_run(_task_id):
        pytest.fail("recovery task must not run without a durable start audit")
    def fail_audit(*args, **kwargs):
        raise OSError("audit database unavailable")
    monkeypatch.setattr(app_module, "run_agent_task", should_not_run)
    monkeypatch.setattr(store, "record_workflow_recovery_event", fail_audit)
    with pytest.raises(HTTPException) as error:
        asyncio.run(app_module.recover_agent_workflow(workflow_id))
    assert error.value.status_code == 503
    assert store.get_workflow(workflow_id)["status"] == "unknown"
    assert store.get_task(task_id)["status"] == "queued"


def test_recovery_audit_enforces_attempt_lifecycle_and_single_terminal_event(tmp_path, monkeypatch):
    store, workflow_id, _task_id, _qa_id = make_workflow(tmp_path, monkeypatch)
    attempt_id = store.record_workflow_recovery_event(workflow_id, "started")
    with pytest.raises(ValueError, match="already has an event"):
        store.record_workflow_recovery_event(workflow_id, "started", attempt_id=attempt_id)
    with pytest.raises(ValueError, match="already has a terminal event"):
        store.record_workflow_recovery_event(workflow_id, "completed", attempt_id="missing-start")
    store.record_workflow_recovery_event(workflow_id, "interrupted", attempt_id=attempt_id)
    with pytest.raises(ValueError, match="already has a terminal event"):
        store.record_workflow_recovery_event(workflow_id, "completed", attempt_id=attempt_id)


def test_recovery_audit_prevents_attempt_id_reuse_across_workflows(tmp_path, monkeypatch):
    store, workflow_id, _task_id, _qa_id = make_workflow(tmp_path, monkeypatch)
    other = store.create_workflow("Other workflow", [])
    attempt_id = store.record_workflow_recovery_event(workflow_id, "started")
    with pytest.raises(ValueError, match="another workflow"):
        store.record_workflow_recovery_event(other["id"], "completed", attempt_id=attempt_id)


def test_recovery_audit_filters_by_attempt_and_event_type(tmp_path, monkeypatch):
    store, workflow_id, _task_id, _qa_id = make_workflow(tmp_path, monkeypatch)
    first = store.record_workflow_recovery_event(workflow_id, "started")
    store.record_workflow_recovery_event(workflow_id, "completed", attempt_id=first)
    second = store.record_workflow_recovery_event(workflow_id, "started")
    store.record_workflow_recovery_event(workflow_id, "interrupted", attempt_id=second)
    started = store.list_workflow_recovery_audit(workflow_id, event_type="started")
    assert started["pagination"]["total"] == 2
    assert all(event["event_type"] == "started" for event in started["events"])
    filtered = store.list_workflow_recovery_audit(workflow_id, attempt_id=second)
    assert [event["event_type"] for event in reversed(filtered["events"])] == ["started", "interrupted"]
    assert filtered["pagination"]["attempt_id"] == second


def test_recovery_audit_rejects_invalid_attempt_filter_and_event_type(tmp_path, monkeypatch):
    store, workflow_id, _task_id, _qa_id = make_workflow(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="Invalid workflow recovery attempt ID"):
        store.list_workflow_recovery_audit(workflow_id, attempt_id="")
    with pytest.raises(ValueError, match="Invalid workflow recovery audit event type"):
        store.list_workflow_recovery_audit(workflow_id, event_type="deleted")
