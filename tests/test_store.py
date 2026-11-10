from pathlib import Path
from crows.store import TaskStore


def test_task_and_tool_execution_persist(tmp_path: Path):
    store = TaskStore(tmp_path / "test.sqlite3")
    task = store.create_task("test", "do a test")
    assert task["status"] == "queued"
    store.set_task_status(task["id"], "running")
    execution_id = store.begin_tool(task["id"], "workspace.list_files", {"path": "."})
    store.finish_tool(execution_id, "succeeded", result={"entries": []})
    store.set_task_status(task["id"], "succeeded", result={"ok": True})
    reopened = TaskStore(tmp_path / "test.sqlite3")
    assert reopened.get_task(task["id"])["status"] == "succeeded"
    records = reopened.list_tool_executions(task["id"])
    assert records[0]["status"] == "succeeded"
    assert '"entries": []' in records[0]["result_json"]


def test_running_work_is_marked_unknown_after_restart(tmp_path: Path):
    store = TaskStore(tmp_path / "test.sqlite3")
    task = store.create_task("crash test", "simulate restart")
    store.set_task_status(task["id"], "running")
    store.begin_tool(task["id"], "example", {})
    assert store.mark_interrupted_as_unknown() == 2
    assert store.get_task(task["id"])["status"] == "unknown"
    assert store.list_tool_executions(task["id"])[0]["status"] == "unknown"


def test_legacy_proposal_schema_migration_preserves_data(tmp_path: Path):
    import json
    import sqlite3

    db_path = tmp_path / "legacy.sqlite3"
    changes = [{
        "path": "sample.py",
        "original_content": "old",
        "original_sha256": "abc123",
        "content": "new",
        "diff": "-old\n+new",
    }]

    with sqlite3.connect(db_path) as con:
        con.execute("""
            CREATE TABLE coding_proposals (
                id TEXT PRIMARY KEY,
                summary TEXT NOT NULL,
                changes_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('pending', 'approved', 'rejected', 'applied')
                ),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                error_text TEXT
            )
        """)
        con.execute(
            """
            INSERT INTO coding_proposals
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-proposal",
                "Legacy proposal",
                json.dumps(changes),
                "approved",
                "2026-01-01T00:00:00+00:00",
                "2026-01-02T00:00:00+00:00",
                "previous error",
            ),
        )

    # Opening the legacy DB should perform any required schema migration.
    store = TaskStore(db_path)
    saved = store.get_proposal("legacy-proposal")

    assert saved is not None
    assert saved["id"] == "legacy-proposal"
    assert saved["summary"] == "Legacy proposal"
    assert saved["changes"] == changes
    assert saved["status"] == "approved"
    assert saved["created_at"] == "2026-01-01T00:00:00+00:00"
    assert saved["updated_at"] == "2026-01-02T00:00:00+00:00"
    assert saved["error_text"] == "previous error"

    with store.connect() as con:
        con.execute(
            "UPDATE coding_proposals SET status='applying' WHERE id=?",
            ("legacy-proposal",),
        )

    assert store.get_proposal("legacy-proposal")["status"] == "applying"


def test_proposal_can_only_be_claimed_once_for_apply(tmp_path: Path):
    store = TaskStore(tmp_path / "claim.sqlite3")
    proposal = store.create_proposal("Atomic claim test", [{
        "path": "sample.py",
        "original_content": "old",
        "original_sha256": "abc123",
        "content": "new",
        "diff": "-old\\n+new",
    }])

    store.set_proposal_status(proposal["id"], "approved")

    assert store.claim_proposal_for_apply(proposal["id"]) is True
    assert store.claim_proposal_for_apply(proposal["id"]) is False

    saved = store.get_proposal(proposal["id"])
    assert saved is not None
    assert saved["status"] == "applying"
    assert saved["error_text"] is None


def test_failed_proposal_migration_rolls_back_without_data_loss(
    tmp_path: Path, monkeypatch
):
    import json
    import sqlite3

    db_path = tmp_path / "migration-failure.sqlite3"
    changes = [{"path": "sample.py", "content": "preserve me"}]

    with sqlite3.connect(db_path) as con:
        con.execute("""
            CREATE TABLE coding_proposals (
                id TEXT PRIMARY KEY,
                summary TEXT NOT NULL,
                changes_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('pending', 'approved', 'rejected', 'applied')
                ),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                error_text TEXT
            )
        """)
        con.execute(
            "INSERT INTO coding_proposals VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "keep-this-proposal",
                "Preserve this proposal",
                json.dumps(changes),
                "approved",
                "created",
                "updated",
                "existing error",
            ),
        )

    original_connect = TaskStore.connect

    class FailingConnection:
        def __init__(self, connection):
            self.connection = connection

        def __enter__(self):
            self.connection.__enter__()
            return self

        def __exit__(self, *args):
            return self.connection.__exit__(*args)

        def execute(self, sql, *args):
            if "ALTER TABLE coding_proposals_new RENAME TO coding_proposals" in sql:
                raise sqlite3.OperationalError("injected migration failure")
            return self.connection.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    def failing_connect(self):
        return FailingConnection(original_connect(self))

    monkeypatch.setattr(TaskStore, "connect", failing_connect)

    import pytest

    with pytest.raises(sqlite3.OperationalError, match="injected migration failure"):
        TaskStore(db_path)

    monkeypatch.setattr(TaskStore, "connect", original_connect)

    with sqlite3.connect(db_path) as con:
        schema = con.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='coding_proposals'"
        ).fetchone()
        assert schema is not None
        assert "'applying'" not in schema[0]

        row = con.execute(
            "SELECT id, summary, changes_json, status, created_at, "
            "updated_at, error_text FROM coding_proposals "
            "WHERE id='keep-this-proposal'"
        ).fetchone()
        assert row == (
            "keep-this-proposal",
            "Preserve this proposal",
            json.dumps(changes),
            "approved",
            "created",
            "updated",
            "existing error",
        )

        leftover = con.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='coding_proposals_new'"
        ).fetchone()
        assert leftover is None


def test_interrupted_applying_proposals_are_flagged_for_manual_review(tmp_path: Path):
    store = TaskStore(tmp_path / "test.sqlite3")
    applying = store.create_proposal("interrupted", [{"path": "sample.py"}])
    approved = store.create_proposal("not interrupted", [])
    store.set_proposal_status(applying["id"], "approved")
    assert store.claim_proposal_for_apply(applying["id"]) is True
    store.set_proposal_status(approved["id"], "approved")

    changed = store.mark_interrupted_proposals_for_review()

    assert changed == 1
    saved = store.get_proposal(applying["id"])
    assert saved is not None
    assert saved["status"] == "applying"
    assert "Process restarted while applying" in saved["error_text"]
    assert store.get_proposal(approved["id"])["status"] == "approved"


def test_interrupted_proposal_review_preserves_existing_error(tmp_path: Path):
    store = TaskStore(tmp_path / "test.sqlite3")
    proposal = store.create_proposal("interrupted", [])
    store.set_proposal_status(proposal["id"], "approved")
    assert store.claim_proposal_for_apply(proposal["id"]) is True
    store.set_proposal_status(proposal["id"], "applying", error="rollback failed")

    assert store.mark_interrupted_proposals_for_review() == 1
    saved = store.get_proposal(proposal["id"])
    assert saved["status"] == "applying"
    assert saved["error_text"] == "rollback failed"


def test_coding_apply_lock_excludes_other_store_instances(tmp_path: Path):
    db_path = tmp_path / "shared.sqlite3"
    first = TaskStore(db_path)
    second = TaskStore(db_path)

    with first.coding_apply_lock() as acquired:
        assert acquired is True
        with second.coding_apply_lock(blocking=False) as acquired_elsewhere:
            assert acquired_elsewhere is False

    with second.coding_apply_lock(blocking=False) as acquired_after_release:
        assert acquired_after_release is True


def test_claim_task_is_atomic_and_only_claims_queued_tasks(tmp_path: Path):
    store = TaskStore(tmp_path / "claim.sqlite3")
    task = store.create_task("queued task", "run once")
    assert store.claim_task(task["id"]) is True
    assert store.get_task(task["id"])["status"] == "running"
    assert store.claim_task(task["id"]) is False
    assert store.get_task(task["id"])["status"] == "running"


def test_claim_task_raises_for_missing_task(tmp_path: Path):
    import pytest

    store = TaskStore(tmp_path / "claim-missing.sqlite3")
    with pytest.raises(KeyError, match="Task not found"):
        store.claim_task("missing-id")

def test_session_search_matches_complete_words_not_substrings(tmp_path: Path):
    store = TaskStore(tmp_path / "session-search.sqlite3")
    session = store.create_session("Word boundary search")
    store.add_session_message(session["id"], "user", "The party starts at noon.")
    store.add_session_message(session["id"], "assistant", "A piece of art is on the wall.")

    results = store.search_session_messages("art")

    assert len(results) == 1
    assert results[0]["content"] == "A piece of art is on the wall."


def test_session_search_finds_hyphenated_identifier_components(tmp_path: Path):
    store = TaskStore(tmp_path / "session-search.sqlite3")
    session = store.create_session("Identifier search")
    store.add_session_message(session["id"], "user", "The project is AETHER-CLAW.")

    results = store.search_session_messages("CLAW")

    assert len(results) == 1
    assert results[0]["content"] == "The project is AETHER-CLAW."


def test_session_search_ignores_punctuation_only_query(tmp_path: Path):
    store = TaskStore(tmp_path / "session-search.sqlite3")
    session = store.create_session("Punctuation search")
    store.add_session_message(session["id"], "user", "A useful saved conversation.")

    assert store.search_session_messages("... --- !!!") == []



def test_set_task_status_raises_for_missing_task(tmp_path: Path):
    import pytest

    store = TaskStore(tmp_path / "missing-task.sqlite3")
    with pytest.raises(KeyError, match="Task not found"):
        store.set_task_status("missing-id", "failed", error="should not be lost")


def test_set_task_status_persists_result_and_error(tmp_path: Path):
    import json

    store = TaskStore(tmp_path / "task-status.sqlite3")
    task = store.create_task("status test", "verify status persistence")

    store.set_task_status(task["id"], "failed", result={"attempt": 1}, error="expected failure")

    saved = store.get_task(task["id"])
    assert saved["status"] == "failed"
    assert json.loads(saved["result_json"]) == {"attempt": 1}
    assert saved["error_text"] == "expected failure"


def test_finish_tool_raises_for_missing_execution(tmp_path: Path):
    import pytest

    store = TaskStore(tmp_path / "missing-tool-execution.sqlite3")
    with pytest.raises(KeyError, match="Tool execution not found"):
        store.finish_tool("missing-execution-id", "failed", error="should not be lost")


def test_finish_tool_persists_result_and_error(tmp_path: Path):
    import json

    store = TaskStore(tmp_path / "tool-execution-status.sqlite3")
    task = store.create_task("tool status test", "verify tool result persistence")
    execution_id = store.begin_tool(task["id"], "workspace.read_file", {"path": "notes.txt"})

    store.finish_tool(execution_id, "failed", result={"attempt": 1}, error="file unavailable")

    saved = store.list_tool_executions(task["id"])[0]
    assert saved["status"] == "failed"
    assert json.loads(saved["result_json"]) == {"attempt": 1}
    assert saved["error_text"] == "file unavailable"


def test_workflow_plan_and_checkpoints_persist_across_store_reopen(tmp_path: Path):
    db_path = tmp_path / "workflow.sqlite3"
    store = TaskStore(db_path)
    plan = [
        {"title": "Inspect code", "input": "Read modules and report evidence", "role": "research"},
        {"title": "Review risks", "input": "Review the findings", "role": "qa_security"},
    ]
    workflow = store.create_workflow("Improve reliability", plan)

    assert workflow["status"] == "running"
    assert workflow["plan"] == plan
    assert workflow["results"] == {"tasks": [], "qa_review": None}

    checkpoint = {
        "tasks": [
            {"task_id": "task-1", "title": "Inspect code", "role": "research", "status": "succeeded"},
            {"task_id": "task-2", "title": "Review risks", "role": "qa_security", "status": "queued"},
        ],
        "qa_review": None,
    }
    store.update_workflow(workflow["id"], "running", results=checkpoint)
    store = TaskStore(db_path)

    saved = store.get_workflow(workflow["id"])
    assert saved is not None
    assert saved["goal"] == "Improve reliability"
    assert saved["plan"] == plan
    assert saved["results"] == checkpoint

    final_checkpoint = {
        "tasks": checkpoint["tasks"],
        "qa_review": {"task_id": "qa-1", "status": "succeeded", "result": "No blocking issues"},
    }
    store.update_workflow(workflow["id"], "succeeded", results=final_checkpoint)
    assert TaskStore(db_path).get_workflow(workflow["id"])["status"] == "succeeded"


def test_interrupted_running_workflow_is_marked_unknown(tmp_path: Path):
    store = TaskStore(tmp_path / "interrupted-workflow.sqlite3")
    workflow = store.create_workflow(
        "Interrupted goal",
        [{"title": "Inspect", "input": "Inspect safely", "role": "research"}],
    )
    store.update_workflow(
        workflow["id"],
        "running",
        results={"tasks": [{"task_id": "task-1", "status": "succeeded"}], "qa_review": None},
    )

    assert store.mark_interrupted_as_unknown() == 1
    saved = store.get_workflow(workflow["id"])
    assert saved is not None
    assert saved["status"] == "unknown"
    assert "inspect saved task checkpoints" in saved["error_text"]
    assert saved["results"]["tasks"][0]["status"] == "succeeded"


def test_workflow_updates_validate_status_and_missing_ids(tmp_path: Path):
    import pytest

    store = TaskStore(tmp_path / "workflow-validation.sqlite3")
    workflow = store.create_workflow("Goal", [{"title": "Inspect", "input": "Do work", "role": "research"}])

    with pytest.raises(ValueError, match="Invalid workflow status"):
        store.update_workflow(workflow["id"], "queued")

    with pytest.raises(KeyError, match="Workflow not found"):
        store.update_workflow("missing-workflow", "failed", error="not found")

    with pytest.raises(ValueError, match="limit"):
        store.list_workflows(0)



def test_workflow_status_cannot_regress_after_completion(tmp_path: Path):
    import pytest

    store = TaskStore(tmp_path / "workflow-terminal-status.sqlite3")
    workflow = store.create_workflow(
        "Finish once",
        [{"title": "Inspect", "input": "Inspect safely", "role": "research"}],
    )
    store.update_workflow(
        workflow["id"], "succeeded",
        results={"tasks": [], "qa_review": {"status": "succeeded"}},
    )

    with pytest.raises(ValueError, match="Invalid workflow status transition from succeeded to running"):
        store.update_workflow(workflow["id"], "running", error=None)

    saved = store.get_workflow(workflow["id"])
    assert saved is not None
    assert saved["status"] == "succeeded"


def test_unknown_workflow_requires_atomic_recovery_claim(tmp_path: Path):
    import pytest

    store = TaskStore(tmp_path / "workflow-recovery-claim.sqlite3")
    workflow = store.create_workflow(
        "Interrupted",
        [{"title": "Inspect", "input": "Inspect safely", "role": "research"}],
    )
    store.update_workflow(workflow["id"], "unknown", error="interrupted")

    with pytest.raises(ValueError, match="Invalid workflow status transition from unknown to running"):
        store.update_workflow(workflow["id"], "running")

    assert store.claim_workflow_recovery(workflow["id"]) is True
    assert store.claim_workflow_recovery(workflow["id"]) is False
    assert store.get_workflow(workflow["id"])["status"] == "running"



def test_task_terminal_status_cannot_be_reopened_by_stale_update(tmp_path: Path):
    import json
    import pytest

    store = TaskStore(tmp_path / "task-terminal-status.sqlite3")
    task = store.create_task("Finish once", "protect terminal journal state")
    store.set_task_status(task["id"], "succeeded", result={"answer": "done"})

    with pytest.raises(ValueError, match="Invalid task status transition from succeeded to running"):
        store.set_task_status(task["id"], "running", error=None)

    with pytest.raises(ValueError, match="Invalid task status transition from succeeded to unknown"):
        store.set_task_status(task["id"], "unknown", error="stale cancellation")

    saved = store.get_task(task["id"])
    assert saved is not None
    assert saved["status"] == "succeeded"
    assert json.loads(saved["result_json"]) == {"answer": "done"}


def test_unknown_task_cannot_be_marked_succeeded_by_late_response(tmp_path: Path):
    import pytest

    store = TaskStore(tmp_path / "task-unknown-status.sqlite3")
    task = store.create_task("Uncertain", "late response must not erase uncertainty")
    store.set_task_status(task["id"], "unknown", error="execution outcome uncertain")

    with pytest.raises(ValueError, match="Invalid task status transition from unknown to succeeded"):
        store.set_task_status(task["id"], "succeeded", result={"late": True})

    saved = store.get_task(task["id"])
    assert saved is not None
    assert saved["status"] == "unknown"
    assert saved["error_text"] == "execution outcome uncertain"



def test_running_task_cannot_regress_to_queued(tmp_path: Path):
    import pytest

    store = TaskStore(tmp_path / "task-monotonic-status.sqlite3")
    task = store.create_task("In flight", "do not return to queue after claiming")
    assert store.claim_task(task["id"]) is True

    with pytest.raises(ValueError, match="Invalid task status transition from running to queued"):
        store.set_task_status(task["id"], "queued")

    saved = store.get_task(task["id"])
    assert saved is not None
    assert saved["status"] == "running"



def test_finished_tool_execution_cannot_be_rewritten_by_stale_update(tmp_path: Path):
    import json
    import pytest

    store = TaskStore(tmp_path / "tool-terminal-guard.sqlite3")
    task = store.create_task("Tool completed", "preserve the original result")
    execution_id = store.begin_tool(task["id"], "workspace.read_file", {"path": "safe.txt"})
    store.finish_tool(execution_id, "succeeded", result={"content": "original"})

    with pytest.raises(
        ValueError,
        match="Invalid tool execution status transition from succeeded to failed",
    ):
        store.finish_tool(execution_id, "failed", result={"content": "stale"}, error="late error")

    saved = store.list_tool_executions(task["id"])[0]
    assert saved["status"] == "succeeded"
    assert json.loads(saved["result_json"]) == {"content": "original"}
    assert saved["error_text"] is None


def test_unknown_tool_execution_cannot_be_overwritten_by_late_success(tmp_path: Path):
    import pytest

    store = TaskStore(tmp_path / "tool-unknown-guard.sqlite3")
    task = store.create_task("Tool uncertain", "do not hide uncertain execution")
    execution_id = store.begin_tool(task["id"], "workspace.read_file", {"path": "safe.txt"})
    store.finish_tool(execution_id, "unknown", error="outcome uncertain")

    with pytest.raises(
        ValueError,
        match="Invalid tool execution status transition from unknown to succeeded",
    ):
        store.finish_tool(execution_id, "succeeded", result={"content": "late result"})

    saved = store.list_tool_executions(task["id"])[0]
    assert saved["status"] == "unknown"
    assert saved["error_text"] == "outcome uncertain"



def test_same_state_task_update_preserves_existing_result_and_error(tmp_path: Path):
    import json

    store = TaskStore(tmp_path / "same-state-task-preserve.sqlite3")
    task = store.create_task("Preserve task journal", "same-state update omits optional fields")
    store.set_task_status(
        task["id"], "failed", result={"reason": "original"}, error="original failure"
    )

    store.set_task_status(task["id"], "failed")

    saved = store.get_task(task["id"])
    assert saved is not None
    assert saved["status"] == "failed"
    assert json.loads(saved["result_json"]) == {"reason": "original"}
    assert saved["error_text"] == "original failure"


def test_same_state_tool_update_preserves_existing_result_and_error(tmp_path: Path):
    import json

    store = TaskStore(tmp_path / "same-state-tool-preserve.sqlite3")
    task = store.create_task("Preserve tool journal", "same-state update omits optional fields")
    execution_id = store.begin_tool(task["id"], "workspace.read_file", {"path": "safe.txt"})
    store.finish_tool(
        execution_id, "failed", result={"reason": "original"}, error="original failure"
    )

    store.finish_tool(execution_id, "failed")

    saved = store.list_tool_executions(task["id"])[0]
    assert saved["status"] == "failed"
    assert json.loads(saved["result_json"]) == {"reason": "original"}
    assert saved["error_text"] == "original failure"


def test_same_state_workflow_update_preserves_existing_error(tmp_path: Path):
    store = TaskStore(tmp_path / "same-state-workflow-preserve.sqlite3")
    workflow = store.create_workflow("Preserve workflow journal", [])
    store.update_workflow(
        workflow["id"], "running", results={"tasks": [], "qa_review": None}, error="original warning"
    )

    store.update_workflow(
        workflow["id"], "running", results={"tasks": [], "qa_review": None}
    )

    saved = store.get_workflow(workflow["id"])
    assert saved is not None
    assert saved["status"] == "running"
    assert saved["error_text"] == "original warning"



def test_concurrent_task_claim_has_exactly_one_winner(tmp_path: Path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    store = TaskStore(tmp_path / "concurrent-task-claim.sqlite3")
    task = store.create_task("Concurrent claim", "only one worker may claim this")
    workers = 8
    barrier = Barrier(workers)

    def claim():
        barrier.wait()
        return store.claim_task(task["id"])

    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(lambda _: claim(), range(workers)))

    assert outcomes.count(True) == 1
    assert outcomes.count(False) == workers - 1
    saved = store.get_task(task["id"])
    assert saved is not None
    assert saved["status"] == "running"


def test_concurrent_workflow_recovery_claim_has_exactly_one_winner(tmp_path: Path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    store = TaskStore(tmp_path / "concurrent-workflow-recovery-claim.sqlite3")
    workflow = store.create_workflow("Concurrent recovery", [])
    store.update_workflow(
        workflow["id"],
        "unknown",
        results={"tasks": [], "qa_review": None},
        error="previous process interrupted",
    )
    workers = 8
    barrier = Barrier(workers)

    def claim():
        barrier.wait()
        return store.claim_workflow_recovery(workflow["id"])

    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(lambda _: claim(), range(workers)))

    assert outcomes.count(True) == 1
    assert outcomes.count(False) == workers - 1
    saved = store.get_workflow(workflow["id"])
    assert saved is not None
    assert saved["status"] == "running"



def test_concurrent_proposal_apply_claim_has_exactly_one_winner(tmp_path: Path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    store = TaskStore(tmp_path / "concurrent-proposal-claim.sqlite3")
    proposal = store.create_proposal("Concurrent apply claim", [])
    store.set_proposal_status(proposal["id"], "approved")
    workers = 8
    barrier = Barrier(workers)

    def claim():
        barrier.wait()
        return store.claim_proposal_for_apply(proposal["id"])

    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(lambda _: claim(), range(workers)))

    assert outcomes.count(True) == 1
    assert outcomes.count(False) == workers - 1
    saved = store.get_proposal(proposal["id"])
    assert saved is not None
    assert saved["status"] == "applying"



def test_terminal_task_same_state_update_cannot_replace_outcome(tmp_path: Path):
    import json

    store = TaskStore(tmp_path / "terminal-task-immutable.sqlite3")
    task = store.create_task("Immutable task outcome", "keep the first final result")
    store.set_task_status(
        task["id"], "failed", result={"attempt": 1}, error="original failure"
    )

    store.set_task_status(
        task["id"], "failed", result={"attempt": 2}, error="late failure"
    )

    saved = store.get_task(task["id"])
    assert saved is not None
    assert saved["status"] == "failed"
    assert json.loads(saved["result_json"]) == {"attempt": 1}
    assert saved["error_text"] == "original failure"


def test_terminal_tool_same_state_update_cannot_replace_outcome(tmp_path: Path):
    import json

    store = TaskStore(tmp_path / "terminal-tool-immutable.sqlite3")
    task = store.create_task("Immutable tool outcome", "keep the first final result")
    execution_id = store.begin_tool(task["id"], "workspace.read_file", {"path": "safe.txt"})
    store.finish_tool(
        execution_id, "succeeded", result={"content": "original"}, error="original note"
    )

    store.finish_tool(
        execution_id, "succeeded", result={"content": "late"}, error="late note"
    )

    saved = store.list_tool_executions(task["id"])[0]
    assert saved["status"] == "succeeded"
    assert json.loads(saved["result_json"]) == {"content": "original"}
    assert saved["error_text"] == "original note"


def test_terminal_workflow_same_state_update_cannot_replace_checkpoint(tmp_path: Path):
    import json

    store = TaskStore(tmp_path / "terminal-workflow-immutable.sqlite3")
    workflow = store.create_workflow("Immutable workflow outcome", [])
    store.update_workflow(
        workflow["id"], "succeeded",
        results={"tasks": [{"status": "succeeded"}], "qa_review": {"status": "succeeded"}},
        error="original diagnostic",
    )

    store.update_workflow(
        workflow["id"], "succeeded",
        results={"tasks": [{"status": "failed"}], "qa_review": {"status": "failed"}},
        error="late diagnostic",
    )

    saved = store.get_workflow(workflow["id"])
    assert saved is not None
    assert saved["status"] == "succeeded"
    assert saved["results"] == {
        "tasks": [{"status": "succeeded"}],
        "qa_review": {"status": "succeeded"},
    }
    assert saved["error_text"] == "original diagnostic"



def test_execution_metrics_report_aggregate_states_without_task_content(tmp_path):
    store = TaskStore(tmp_path / "execution-metrics.sqlite3")
    queued = store.create_task("queued secret title", "private input")
    running = store.create_task("running task", "private input")
    assert store.claim_task(running["id"])
    succeeded = store.create_task("done", "private input")
    assert store.claim_task(succeeded["id"])
    store.set_task_status(succeeded["id"], "succeeded", result={"private": "result"})
    workflow = store.create_workflow("private workflow goal", [])
    store.update_workflow(workflow["id"], "unknown", results={"private": "checkpoint"})
    tool_task = store.create_task("tool parent", "private")
    execution_id = store.begin_tool(tool_task["id"], "private_tool", {"secret": "input"})
    store.finish_tool(execution_id, "failed", error="private error")

    metrics = store.execution_metrics()
    assert metrics["tasks"] == {
        "queued": 2, "running": 1, "succeeded": 1, "failed": 0, "unknown": 0
    }
    assert metrics["workflows"]["unknown"] == 1
    assert metrics["tool_executions"]["failed"] == 1
    assert metrics["oldest_queued_task_at"] == queued["created_at"]
    serialized = str(metrics)
    for secret in ("private input", "private workflow goal", "private error", "private_tool"):
        assert secret not in serialized



def test_concurrent_task_claim_has_exactly_one_winner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    store = TaskStore(tmp_path / "concurrent-task-claim.sqlite3")
    task = store.create_task("One execution", "Must be claimed once")
    stores = [TaskStore(store.db_path) for _ in range(8)]
    barrier = Barrier(len(stores))

    def claim(candidate):
        barrier.wait(timeout=5)
        return candidate.claim_task(task["id"])

    with ThreadPoolExecutor(max_workers=len(stores)) as pool:
        outcomes = list(pool.map(claim, stores))

    assert outcomes.count(True) == 1
    assert outcomes.count(False) == len(stores) - 1
    assert store.get_task(task["id"])["status"] == "running"


def test_stale_task_status_write_cannot_reopen_unknown_task(tmp_path):
    import pytest

    store = TaskStore(tmp_path / "stale-task-write.sqlite3")
    task = store.create_task("Interrupted", "Do not replay")
    assert store.claim_task(task["id"])
    store.set_task_status(task["id"], "unknown", error="worker interrupted")

    with pytest.raises(ValueError, match="Invalid task status transition"):
        store.set_task_status(task["id"], "running")
    with pytest.raises(ValueError, match="Invalid task status transition"):
        store.set_task_status(task["id"], "succeeded", result={"late": True})

    saved = store.get_task(task["id"])
    assert saved["status"] == "unknown"
    assert saved["result_json"] is None
    assert saved["error_text"] == "worker interrupted"


def test_interrupted_recovery_marks_running_tool_unknown_without_replay(tmp_path):
    store = TaskStore(tmp_path / "interrupted-tool.sqlite3")
    task = store.create_task("Tool interruption", "Call tool once")
    assert store.claim_task(task["id"])
    execution_id = store.begin_tool(task["id"], "external.write", {"value": "x"})

    assert store.mark_interrupted_as_unknown() == 2
    saved_task = store.get_task(task["id"])
    saved_tool = store.list_tool_executions(task["id"])[0]

    assert saved_task["status"] == "unknown"
    assert saved_tool["id"] == execution_id
    assert saved_tool["status"] == "unknown"
    assert saved_tool["result_json"] is None


def test_terminal_task_result_is_not_overwritten_by_late_same_state_write(tmp_path):
    store = TaskStore(tmp_path / "terminal-task-write.sqlite3")
    task = store.create_task("First result wins", "Prevent late overwrite")
    assert store.claim_task(task["id"])
    store.set_task_status(task["id"], "succeeded", result={"answer": "first"})
    store.set_task_status(task["id"], "succeeded", result={"answer": "late"})

    saved = store.get_task(task["id"])
    assert saved["status"] == "succeeded"
    assert saved["result_json"] == '{"answer": "first"}'



def test_workflow_control_is_durable_and_enforces_safe_transitions(tmp_path: Path):
    db_path = tmp_path / "workflow-control.sqlite3"
    store = TaskStore(db_path)
    workflow = store.create_workflow("Control lifecycle", [])
    workflow_id = workflow["id"]

    assert workflow["control_state"] == "running"
    assert store.set_workflow_control(workflow_id, "paused") is True
    reopened = TaskStore(db_path)
    assert reopened.get_workflow(workflow_id)["control_state"] == "paused"

    assert reopened.set_workflow_control(workflow_id, "running") is True
    assert reopened.set_workflow_control(workflow_id, "cancel_requested") is True
    assert reopened.set_workflow_control(workflow_id, "running") is False
    assert reopened.set_workflow_control(workflow_id, "completed") is True
    assert reopened.set_workflow_control(workflow_id, "running") is False
    assert reopened.get_workflow(workflow_id)["control_state"] == "completed"


def test_legacy_workflow_without_control_row_remains_readable(tmp_path: Path):
    store = TaskStore(tmp_path / "legacy-workflow.sqlite3")
    workflow = store.create_workflow("Legacy", [])
    with store.connect() as con:
        con.execute("DELETE FROM workflow_controls WHERE workflow_id=?", (workflow["id"],))

    saved = store.get_workflow(workflow["id"])
    assert saved is not None
    assert saved["control_state"] == "running"
