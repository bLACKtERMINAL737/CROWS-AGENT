"""Regression tests for durable workflow request idempotency."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from crows.store import IdempotencyConflictError, TaskStore


def test_same_idempotency_key_and_request_returns_original_workflow(tmp_path: Path):
    store = TaskStore(tmp_path / "workflow-idempotency.sqlite3")
    plan = [{"title": "Inspect", "input": "Read", "role": "research", "depends_on": []}]

    first, created = store.create_workflow_idempotent(
        "request-1", "fingerprint-a", "Inspect repository", plan
    )
    second, created_again = store.create_workflow_idempotent(
        "request-1", "fingerprint-a", "Inspect repository", plan
    )

    assert created is True
    assert created_again is False
    assert second["id"] == first["id"]
    assert len(store.list_workflows()) == 1


def test_reusing_idempotency_key_for_different_request_conflicts(tmp_path: Path):
    store = TaskStore(tmp_path / "workflow-idempotency-conflict.sqlite3")
    plan = [{"title": "Inspect", "input": "Read", "role": "research", "depends_on": []}]
    original, created = store.create_workflow_idempotent(
        "request-2", "fingerprint-a", "Inspect repository", plan
    )
    assert created is True

    with pytest.raises(IdempotencyConflictError):
        store.create_workflow_idempotent(
            "request-2", "fingerprint-b", "Inspect another repository", plan
        )

    assert len(store.list_workflows()) == 1
    assert store.get_workflow(original["id"])["status"] == "running"


def test_concurrent_duplicate_requests_create_only_one_workflow(tmp_path: Path):
    store = TaskStore(tmp_path / "workflow-idempotency-concurrent.sqlite3")
    plan = [{"title": "Inspect", "input": "Read", "role": "research", "depends_on": []}]

    def create():
        return store.create_workflow_idempotent(
            "request-3", "fingerprint-a", "Inspect repository", plan
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _index: create(), range(8)))

    workflow_ids = {workflow["id"] for workflow, _created in results}
    created_count = sum(1 for _workflow, created in results if created)
    assert len(workflow_ids) == 1
    assert created_count == 1
    assert len(store.list_workflows()) == 1


def test_terminal_workflow_is_returned_without_reopening_on_retry(tmp_path: Path):
    store = TaskStore(tmp_path / "workflow-idempotency-terminal.sqlite3")
    plan = [{"title": "Inspect", "input": "Read", "role": "research", "depends_on": []}]
    workflow, created = store.create_workflow_idempotent(
        "request-4", "fingerprint-a", "Inspect repository", plan
    )
    assert created is True
    store.update_workflow(workflow["id"], "succeeded", results={"tasks": [], "qa_review": None})

    replay, replay_created = store.create_workflow_idempotent(
        "request-4", "fingerprint-a", "Inspect repository", plan
    )

    assert replay_created is False
    assert replay["id"] == workflow["id"]
    assert replay["status"] == "succeeded"
    assert len(store.list_workflows()) == 1
