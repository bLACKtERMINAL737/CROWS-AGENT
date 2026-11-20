import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from crows import app as app_module
from crows.security import LocalAPIProtection
from crows.store import TaskStore


API_TOKEN = "crows-recovery-plan-api-token"


def _isolated_app():
    isolated = FastAPI()
    isolated.router.routes = list(app_module.app.router.routes)
    isolated.add_middleware(LocalAPIProtection)
    return isolated


@pytest.mark.asyncio
async def test_recovery_plan_api_is_read_only_and_marks_uncertain_tasks(
    tmp_path, monkeypatch
):
    store = TaskStore(tmp_path / "recovery-plan-api.sqlite3")
    specialist = store.create_task("[research] Inspect files", "Read-only inspection")
    qa = store.create_task(
        "[qa_security] Review specialist results", "Review saved results"
    )
    workflow = store.create_workflow(
        "Inspect safely",
        [{
            "title": "Inspect files",
            "input": "Read-only inspection",
            "role": "research",
        }],
    )
    checkpoint = {
        "tasks": [{
            "plan_index": 0,
            "task_id": specialist["id"],
            "title": "Inspect files",
            "role": "research",
            "status": "unknown",
            "depends_on": [],
        }],
        "qa_review": {"task_id": qa["id"], "status": "queued"},
    }
    store.update_workflow(
        workflow["id"], "unknown", results=checkpoint,
        error="simulated process interruption",
    )
    # The checkpoint says the specialist was interrupted; keep its journal aligned.
    # This tests uncertain-work classification rather than mismatch handling.
    store.set_task_status(specialist["id"], "unknown")
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setenv("CROWS_API_TOKEN", API_TOKEN)
    async def should_not_execute(_task_id):
        pytest.fail("Recovery-plan preview must never execute a task")

    monkeypatch.setattr(app_module, "run_agent_task", should_not_execute)
    before_workflow = store.get_workflow(workflow["id"])
    before_specialist = store.get_task(specialist["id"])
    before_qa = store.get_task(qa["id"])

    async with AsyncClient(
        transport=ASGITransport(app=_isolated_app()),
        base_url="http://127.0.0.1:8000",
        headers={"X-Crows-Token": API_TOKEN},
    ) as client:
        response = await client.get(
            f"/agents/workflows/{workflow['id']}/recovery-plan"
        )

    assert response.status_code == 200
    preview = response.json()
    assert preview["recoverable"] is True
    assert preview["workflow_status"] == "unknown"
    assert preview["execution_started"] is False
    assert preview["state_mutated"] is False
    assert preview["task_counts_by_disposition"] == {
        "uncertain_never_replay": 1,
        "eligible_to_resume": 1,
    }
    specialist_preview = next(
        item for item in preview["tasks"] if item["task_id"] == specialist["id"]
    )
    assert specialist_preview["disposition"] == "uncertain_never_replay"
    assert preview["qa_review"]["task_id"] == qa["id"]
    assert preview["qa_review"]["disposition"] == "eligible_to_resume"

    assert store.get_workflow(workflow["id"]) == before_workflow
    assert store.get_task(specialist["id"]) == before_specialist
    assert store.get_task(qa["id"]) == before_qa


@pytest.mark.asyncio
async def test_recovery_plan_api_reports_checkpoint_journal_mismatch(
    tmp_path, monkeypatch
):
    store = TaskStore(tmp_path / "recovery-plan-mismatch-api.sqlite3")
    specialist = store.create_task("[research] Inspect files", "Read-only inspection")
    workflow = store.create_workflow(
        "Inspect safely",
        [{
            "title": "Inspect files",
            "input": "Read-only inspection",
            "role": "research",
        }],
    )
    store.update_workflow(
        workflow["id"], "unknown",
        results={
            "tasks": [{
                "plan_index": 0,
                "task_id": specialist["id"],
                "title": "Inspect files",
                "role": "research",
                "status": "queued",
                "depends_on": [],
            }],
            "qa_review": None,
        },
        error="simulated interruption",
    )
    store.set_task_status(specialist["id"], "running")
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setenv("CROWS_API_TOKEN", API_TOKEN)

    async with AsyncClient(
        transport=ASGITransport(app=_isolated_app()),
        base_url="http://127.0.0.1:8000",
        headers={"X-Crows-Token": API_TOKEN},
    ) as client:
        response = await client.get(
            f"/agents/workflows/{workflow['id']}/recovery-plan"
        )

    assert response.status_code == 200
    preview = response.json()
    assert preview["recoverable"] is False
    assert "checkpoint_journal_mismatch" in preview["tasks"][0]["issues"]
    assert preview["tasks"][0]["disposition"] == "manual_inspection_required"
    assert store.get_workflow(workflow["id"])["status"] == "unknown"
    assert store.get_task(specialist["id"])["status"] == "running"



@pytest.mark.asyncio
async def test_recovery_plan_counts_missing_qa_task_as_manual_inspection(
    tmp_path, monkeypatch
):
    store = TaskStore(tmp_path / "recovery-plan-missing-qa.sqlite3")
    workflow = store.create_workflow("Inspect safely", [])
    store.update_workflow(
        workflow["id"],
        "unknown",
        results={
            "tasks": [],
            "qa_review": {
                "task_id": "missing-qa-task",
                "status": "queued",
            },
        },
        error="simulated process interruption",
    )
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setenv("CROWS_API_TOKEN", API_TOKEN)

    async with AsyncClient(
        transport=ASGITransport(app=_isolated_app()),
        base_url="http://127.0.0.1:8000",
        headers={"X-Crows-Token": API_TOKEN},
    ) as client:
        response = await client.get(
            f"/agents/workflows/{workflow['id']}/recovery-plan"
        )

    assert response.status_code == 200
    preview = response.json()
    assert preview["recoverable"] is False
    assert preview["qa_review"]["disposition"] == "manual_inspection_required"
    assert "qa_task_missing_or_mismatched" in preview["qa_review"]["issues"]
    assert preview["task_counts_by_disposition"] == {
        "manual_inspection_required": 1,
    }
    assert preview["execution_started"] is False
    assert preview["state_mutated"] is False



@pytest.mark.asyncio
async def test_recovery_plan_rejects_qa_task_id_reused_by_specialist(
    tmp_path, monkeypatch
):
    store = TaskStore(tmp_path / "recovery-plan-duplicate-qa.sqlite3")
    shared = store.create_task(
        "[qa_security] Review specialist results", "Review saved results"
    )
    workflow = store.create_workflow(
        "Inspect safely",
        [{
            "title": "Review specialist results",
            "input": "Review saved results",
            "role": "qa_security",
        }],
    )
    store.update_workflow(
        workflow["id"],
        "unknown",
        results={
            "tasks": [{
                "plan_index": 0,
                "task_id": shared["id"],
                "title": "Review specialist results",
                "role": "qa_security",
                "status": "queued",
                "depends_on": [],
            }],
            "qa_review": {"task_id": shared["id"], "status": "queued"},
        },
        error="simulated process interruption",
    )
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setenv("CROWS_API_TOKEN", API_TOKEN)

    async with AsyncClient(
        transport=ASGITransport(app=_isolated_app()),
        base_url="http://127.0.0.1:8000",
        headers={"X-Crows-Token": API_TOKEN},
    ) as client:
        response = await client.get(
            f"/agents/workflows/{workflow['id']}/recovery-plan"
        )

    assert response.status_code == 200
    preview = response.json()
    assert preview["recoverable"] is False
    assert preview["qa_review"]["disposition"] == "manual_inspection_required"
    assert "qa_task_id_duplicates_specialist" in preview["qa_review"]["issues"]
    assert preview["task_counts_by_disposition"] == {
        "eligible_to_resume": 1,
        "manual_inspection_required": 1,
    }
    assert preview["execution_started"] is False
    assert preview["state_mutated"] is False



@pytest.mark.asyncio
async def test_recovery_plan_rejects_duplicate_specialist_task_ids(
    tmp_path, monkeypatch
):
    store = TaskStore(tmp_path / "recovery-plan-duplicate-specialist.sqlite3")
    task = store.create_task("[research] Inspect files", "Read-only inspection")
    plan_item = {
        "title": "Inspect files",
        "input": "Read-only inspection",
        "role": "research",
    }
    workflow = store.create_workflow(
        "Inspect safely", [plan_item, plan_item]
    )
    outcome = {
        "task_id": task["id"],
        "title": "Inspect files",
        "role": "research",
        "status": "queued",
        "depends_on": [],
    }
    store.update_workflow(
        workflow["id"],
        "unknown",
        results={
            "tasks": [
                {**outcome, "plan_index": 0},
                {**outcome, "plan_index": 1},
            ],
            "qa_review": None,
        },
        error="simulated process interruption",
    )
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setenv("CROWS_API_TOKEN", API_TOKEN)

    async with AsyncClient(
        transport=ASGITransport(app=_isolated_app()),
        base_url="http://127.0.0.1:8000",
        headers={"X-Crows-Token": API_TOKEN},
    ) as client:
        response = await client.get(
            f"/agents/workflows/{workflow['id']}/recovery-plan"
        )

    assert response.status_code == 200
    preview = response.json()
    assert preview["recoverable"] is False
    assert preview["tasks"][0]["disposition"] == "eligible_to_resume"
    assert preview["tasks"][1]["disposition"] == "manual_inspection_required"
    assert "missing_or_duplicate_task_id" in preview["tasks"][1]["issues"]
    assert preview["task_counts_by_disposition"] == {
        "eligible_to_resume": 1,
        "manual_inspection_required": 1,
    }
    assert preview["execution_started"] is False
    assert preview["state_mutated"] is False


@pytest.mark.asyncio
async def test_recovery_plan_rejects_cyclic_dependency_checkpoint(
    tmp_path, monkeypatch
):
    store = TaskStore(tmp_path / "recovery-plan-cycle.sqlite3")
    first = store.create_task("[research] First", "Inspect first")
    second = store.create_task("[research] Second", "Inspect second")
    plan = [
        {"title": "First", "input": "Inspect first", "role": "research"},
        {"title": "Second", "input": "Inspect second", "role": "research"},
    ]
    workflow = store.create_workflow("Inspect safely", plan)
    store.update_workflow(
        workflow["id"],
        "unknown",
        results={
            "tasks": [
                {
                    "plan_index": 0,
                    "task_id": first["id"],
                    "title": "First",
                    "role": "research",
                    "status": "queued",
                    "depends_on": [1],
                },
                {
                    "plan_index": 1,
                    "task_id": second["id"],
                    "title": "Second",
                    "role": "research",
                    "status": "queued",
                    "depends_on": [0],
                },
            ],
            "qa_review": None,
        },
        error="simulated process interruption",
    )
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setenv("CROWS_API_TOKEN", API_TOKEN)

    async with AsyncClient(
        transport=ASGITransport(app=_isolated_app()),
        base_url="http://127.0.0.1:8000",
        headers={"X-Crows-Token": API_TOKEN},
    ) as client:
        response = await client.get(
            f"/agents/workflows/{workflow['id']}/recovery-plan"
        )

    assert response.status_code == 200
    preview = response.json()
    assert preview["recoverable"] is False
    assert "dependency_graph_invalid" in preview["issues"]
    assert preview["recovery_order"] == []
    assert preview["execution_started"] is False
    assert preview["state_mutated"] is False



@pytest.mark.asyncio
async def test_recovery_plan_rejects_out_of_range_dependency_index(
    tmp_path, monkeypatch
):
    store = TaskStore(tmp_path / "recovery-plan-invalid-dependency.sqlite3")
    task = store.create_task("[research] Inspect files", "Read-only inspection")
    workflow = store.create_workflow(
        "Inspect safely",
        [{"title": "Inspect files", "input": "Read-only inspection", "role": "research"}],
    )
    store.update_workflow(
        workflow["id"],
        "unknown",
        results={
            "tasks": [{
                "plan_index": 0,
                "task_id": task["id"],
                "title": "Inspect files",
                "role": "research",
                "status": "queued",
                "depends_on": [7],
            }],
            "qa_review": None,
        },
        error="simulated process interruption",
    )
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setenv("CROWS_API_TOKEN", API_TOKEN)

    async with AsyncClient(
        transport=ASGITransport(app=_isolated_app()),
        base_url="http://127.0.0.1:8000",
        headers={"X-Crows-Token": API_TOKEN},
    ) as client:
        response = await client.get(
            f"/agents/workflows/{workflow['id']}/recovery-plan"
        )

    assert response.status_code == 200
    preview = response.json()
    assert preview["recoverable"] is False
    assert "dependency_graph_invalid" in preview["issues"]
    assert preview["recovery_order"] == []
    assert preview["tasks"][0]["disposition"] == "eligible_to_resume"
    assert preview["execution_started"] is False
    assert preview["state_mutated"] is False



@pytest.mark.asyncio
async def test_recovery_plan_rejects_boolean_dependency_index(
    tmp_path, monkeypatch
):
    store = TaskStore(tmp_path / "recovery-plan-boolean-dependency.sqlite3")
    first = store.create_task("[research] First", "Inspect first")
    second = store.create_task("[research] Second", "Inspect second")
    plan = [
        {"title": "First", "input": "Inspect first", "role": "research"},
        {"title": "Second", "input": "Inspect second", "role": "research"},
    ]
    workflow = store.create_workflow("Inspect safely", plan)
    store.update_workflow(
        workflow["id"],
        "unknown",
        results={
            "tasks": [
                {
                    "plan_index": 0,
                    "task_id": first["id"],
                    "title": "First",
                    "role": "research",
                    "status": "queued",
                    "depends_on": [True],
                },
                {
                    "plan_index": 1,
                    "task_id": second["id"],
                    "title": "Second",
                    "role": "research",
                    "status": "queued",
                    "depends_on": [],
                },
            ],
            "qa_review": None,
        },
        error="simulated process interruption",
    )
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setenv("CROWS_API_TOKEN", API_TOKEN)

    async with AsyncClient(
        transport=ASGITransport(app=_isolated_app()),
        base_url="http://127.0.0.1:8000",
        headers={"X-Crows-Token": API_TOKEN},
    ) as client:
        response = await client.get(
            f"/agents/workflows/{workflow['id']}/recovery-plan"
        )

    assert response.status_code == 200
    preview = response.json()
    assert preview["recoverable"] is False
    assert "dependency_graph_invalid" in preview["issues"]
    assert preview["recovery_order"] == []
    assert preview["execution_started"] is False
    assert preview["state_mutated"] is False



@pytest.mark.parametrize(
    "dependencies",
    [
        [-1],
        [0],
        [1, 1],
        [0, True],
        "0",
        None,
    ],
)
@pytest.mark.asyncio
async def test_recovery_plan_rejects_malformed_dependency_lists(
    dependencies, tmp_path, monkeypatch
):
    store = TaskStore(tmp_path / "recovery-plan-dependency-edge.sqlite3")
    first = store.create_task("[research] First", "Inspect first")
    second = store.create_task("[research] Second", "Inspect second")
    plan = [
        {"title": "First", "input": "Inspect first", "role": "research"},
        {"title": "Second", "input": "Inspect second", "role": "research"},
    ]
    workflow = store.create_workflow("Inspect safely", plan)
    store.update_workflow(
        workflow["id"],
        "unknown",
        results={
            "tasks": [
                {
                    "plan_index": 0,
                    "task_id": first["id"],
                    "title": "First",
                    "role": "research",
                    "status": "queued",
                    "depends_on": dependencies,
                },
                {
                    "plan_index": 1,
                    "task_id": second["id"],
                    "title": "Second",
                    "role": "research",
                    "status": "queued",
                    "depends_on": [],
                },
            ],
            "qa_review": None,
        },
        error="simulated process interruption",
    )
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setenv("CROWS_API_TOKEN", API_TOKEN)

    async with AsyncClient(
        transport=ASGITransport(app=_isolated_app()),
        base_url="http://127.0.0.1:8000",
        headers={"X-Crows-Token": API_TOKEN},
    ) as client:
        response = await client.get(
            f"/agents/workflows/{workflow['id']}/recovery-plan"
        )

    assert response.status_code == 200
    preview = response.json()
    assert preview["recoverable"] is False
    assert "dependency_graph_invalid" in preview["issues"]
    assert preview["recovery_order"] == []
    assert preview["execution_started"] is False
    assert preview["state_mutated"] is False



@pytest.mark.asyncio
async def test_recovery_preview_rejects_checkpoint_dependency_plan_mismatch(
    tmp_path, monkeypatch
):
    store = TaskStore(tmp_path / "recovery-plan-dependency-mismatch.sqlite3")
    first = store.create_task("[research] First", "Inspect first")
    second = store.create_task("[research] Second", "Inspect second")
    plan = [
        {"title": "First", "input": "Inspect first", "role": "research", "depends_on": []},
        {"title": "Second", "input": "Inspect second", "role": "research", "depends_on": [0]},
    ]
    workflow = store.create_workflow("Inspect safely", plan)
    store.update_workflow(
        workflow["id"], "unknown",
        results={"tasks": [
            {"plan_index": 0, "task_id": first["id"], "title": "First", "role": "research", "status": "queued", "depends_on": []},
            {"plan_index": 1, "task_id": second["id"], "title": "Second", "role": "research", "status": "queued", "depends_on": []},
        ], "qa_review": None},
        error="simulated process interruption",
    )
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setenv("CROWS_API_TOKEN", API_TOKEN)
    async with AsyncClient(
        transport=ASGITransport(app=_isolated_app()),
        base_url="http://127.0.0.1:8000",
        headers={"X-Crows-Token": API_TOKEN},
    ) as client:
        response = await client.get(f"/agents/workflows/{workflow['id']}/recovery-plan")
    assert response.status_code == 200
    preview = response.json()
    assert preview["recoverable"] is False
    assert "plan_dependency_mismatch" in preview["issues"]
    assert preview["recovery_order"] == []
    assert preview["execution_started"] is False
    assert preview["state_mutated"] is False


@pytest.mark.parametrize(
    ("checkpoint_overrides", "expected_issue"),
    [
        ({"plan_index": 9}, "plan_index_mismatch"),
        ({"role": "coding"}, "plan_role_mismatch"),
    ],
)
@pytest.mark.asyncio
async def test_recovery_preview_rejects_checkpoint_identity_mismatch(
    checkpoint_overrides, expected_issue, tmp_path, monkeypatch
):
    store = TaskStore(tmp_path / "recovery-plan-identity-mismatch.sqlite3")
    task = store.create_task("[research] Inspect", "Read-only inspection")
    plan = [{"title": "Inspect", "input": "Read-only inspection", "role": "research"}]
    workflow = store.create_workflow("Inspect safely", plan)
    outcome = {
        "plan_index": 0, "task_id": task["id"], "title": "Inspect",
        "role": "research", "status": "queued", "depends_on": [],
    }
    outcome.update(checkpoint_overrides)
    store.update_workflow(
        workflow["id"], "unknown",
        results={"tasks": [outcome], "qa_review": None},
        error="simulated process interruption",
    )
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setenv("CROWS_API_TOKEN", API_TOKEN)
    async with AsyncClient(
        transport=ASGITransport(app=_isolated_app()),
        base_url="http://127.0.0.1:8000",
        headers={"X-Crows-Token": API_TOKEN},
    ) as client:
        response = await client.get(f"/agents/workflows/{workflow['id']}/recovery-plan")
    assert response.status_code == 200
    preview = response.json()
    assert preview["recoverable"] is False
    assert expected_issue in preview["issues"]
    assert preview["recovery_order"] == []
    assert preview["execution_started"] is False
    assert preview["state_mutated"] is False



@pytest.mark.asyncio
async def test_recovery_preview_accepts_legacy_plan_default_role(
    tmp_path, monkeypatch
):
    store = TaskStore(tmp_path / "recovery-plan-legacy-role.sqlite3")
    task = store.create_task("[planner] Inspect", "Read-only inspection")
    workflow = store.create_workflow(
        "Legacy plan role",
        [{"title": "Inspect", "input": "Read-only inspection"}],
    )
    store.update_workflow(
        workflow["id"], "unknown",
        results={"tasks": [{
            "plan_index": 0,
            "task_id": task["id"],
            "title": "Inspect",
            "role": "planner",
            "status": "queued",
            "depends_on": [],
        }], "qa_review": None},
        error="simulated interruption of a legacy workflow",
    )
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setenv("CROWS_API_TOKEN", API_TOKEN)

    async with AsyncClient(
        transport=ASGITransport(app=_isolated_app()),
        base_url="http://127.0.0.1:8000",
        headers={"X-Crows-Token": API_TOKEN},
    ) as client:
        response = await client.get(
            f"/agents/workflows/{workflow['id']}/recovery-plan"
        )

    assert response.status_code == 200
    preview = response.json()
    assert preview["recoverable"] is True
    assert preview["issues"] == []
    assert preview["tasks"][0]["disposition"] == "eligible_to_resume"
    assert preview["recovery_order"] == [0]
    assert preview["execution_started"] is False
    assert preview["state_mutated"] is False

@pytest.mark.asyncio
async def test_recovery_http_resumes_queued_checkpoint_without_replaying_unknown(
    tmp_path, monkeypatch
):
    store = TaskStore(tmp_path / "workflow-recovery-http.sqlite3")
    queued = store.create_task("[research] Queued task", "Safe checkpointed work")
    uncertain = store.create_task("[research] Uncertain task", "May have acted already")
    store.set_task_status(uncertain["id"], "unknown", error="simulated interruption")
    workflow = store.create_workflow(
        "Recover only known-safe work",
        [
            {"title": "Queued task", "input": "Safe checkpointed work", "role": "research"},
            {"title": "Uncertain task", "input": "May have acted already", "role": "research"},
        ],
    )
    checkpoint = {
        "tasks": [
            {
                "plan_index": 0,
                "task_id": queued["id"],
                "title": "Queued task",
                "role": "research",
                "status": "queued",
                "depends_on": [],
            },
            {
                "plan_index": 1,
                "task_id": uncertain["id"],
                "title": "Uncertain task",
                "role": "research",
                "status": "unknown",
                "depends_on": [],
            },
        ],
        "qa_review": None,
    }
    store.update_workflow(
        workflow["id"], "unknown", results=checkpoint,
        error="simulated process interruption",
    )
    executed = []

    async def fake_run(task_id):
        assert task_id == queued["id"]
        assert store.claim_task(task_id)
        store.set_task_status(task_id, "succeeded", result={"answer": "verified"})
        executed.append(task_id)
        return {
            "task_id": task_id,
            "status": "succeeded",
            "result": "verified",
            "tool_calls": 0,
            "tool_attempts": 0,
        }

    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "run_agent_task", fake_run)
    monkeypatch.setenv("CROWS_API_TOKEN", API_TOKEN)

    async with AsyncClient(
        transport=ASGITransport(app=_isolated_app()),
        base_url="http://127.0.0.1:8000",
        headers={"X-Crows-Token": API_TOKEN},
    ) as client:
        response = await client.post(
            f"/agents/workflows/{workflow['id']}/recover"
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "partial"
    assert executed == [queued["id"]]
    assert store.get_task(queued["id"])["status"] == "succeeded"
    assert store.get_task(uncertain["id"])["status"] == "unknown"
    saved = store.get_workflow(workflow["id"])
    assert saved["status"] == "partial"
    assert [item["status"] for item in saved["results"]["tasks"]] == [
        "succeeded", "unknown"
    ]
    audit = store.list_workflow_recovery_audit(workflow["id"])
    assert [item["event_type"] for item in audit["events"]] == [
        "completed", "started"
    ]
    assert audit["events"][0]["attempt_id"] == audit["events"][1]["attempt_id"]


@pytest.mark.asyncio
async def test_recovery_http_rejects_non_unknown_workflow_without_execution(
    tmp_path, monkeypatch
):
    store = TaskStore(tmp_path / "workflow-recovery-http-conflict.sqlite3")
    task = store.create_task("[research] Queued task", "Must not execute")
    workflow = store.create_workflow(
        "Already active",
        [{"title": "Queued task", "input": "Must not execute", "role": "research"}],
    )
    store.update_workflow(
        workflow["id"],
        "running",
        results={
            "tasks": [{
                "plan_index": 0,
                "task_id": task["id"],
                "title": "Queued task",
                "role": "research",
                "status": "queued",
                "depends_on": [],
            }],
            "qa_review": None,
        },
    )
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setenv("CROWS_API_TOKEN", API_TOKEN)

    async def should_not_run(_task_id):
        pytest.fail("A running workflow must not enter recovery")

    monkeypatch.setattr(app_module, "run_agent_task", should_not_run)

    async with AsyncClient(
        transport=ASGITransport(app=_isolated_app()),
        base_url="http://127.0.0.1:8000",
        headers={"X-Crows-Token": API_TOKEN},
    ) as client:
        response = await client.post(
            f"/agents/workflows/{workflow['id']}/recover"
        )

    assert response.status_code == 409
    assert store.get_workflow(workflow["id"])["status"] == "running"
    assert store.get_task(task["id"])["status"] == "queued"
    assert store.list_workflow_recovery_audit(workflow["id"])["events"] == []

