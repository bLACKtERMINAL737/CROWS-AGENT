"""Ensure rollback does not recreate a target removed by another process."""

import hashlib
from pathlib import Path

import pytest

from crows.coding import CodingEngine, CodingError
from crows.store import TaskStore


class _UnusedRouter:
    async def chat(self, message, task_type="general"):
        raise AssertionError("apply must not invoke the model router")


def test_rollback_does_not_recreate_target_removed_by_external_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "sample.py"
    original = "VALUE = 1" + chr(10)
    proposed = "VALUE = 2" + chr(10)
    source.write_text(original, encoding="utf-8")

    store = TaskStore(tmp_path / "tasks.sqlite3")
    engine = CodingEngine(workspace, store, _UnusedRouter())
    proposal = store.create_proposal(
        "preserve external deletion during rollback",
        [{
            "path": "sample.py",
            "original_content": original,
            "original_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
            "content": proposed,
            "diff": "",
        }],
    )
    engine.approve(proposal["id"])

    def external_delete_then_fail_sync(_directory: Path) -> None:
        source.unlink()
        raise OSError("simulated directory sync failure")

    monkeypatch.setattr(engine, "_fsync_directory", external_delete_then_fail_sync)

    with pytest.raises(CodingError, match="rollback failure"):
        engine.apply(proposal["id"])

    assert not source.exists()
    saved = store.get_proposal(proposal["id"])
    assert saved is not None
    assert saved["status"] == "applying"
    assert "Rollback target became unavailable" in saved["error_text"]
    assert not list(workspace.glob(".crows-stage-*"))
    assert not list(workspace.glob(".crows-rollback-*"))
