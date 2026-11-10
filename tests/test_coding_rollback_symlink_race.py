"""Ensure rollback refuses a target swapped to a symlink after replacement."""

import hashlib
from pathlib import Path

import pytest

from crows.coding import CodingEngine, CodingError
from crows.store import TaskStore


class _UnusedRouter:
    async def chat(self, message, task_type="general"):
        raise AssertionError("apply must not invoke the model router")


def test_rollback_refuses_symlink_swap_before_restoring_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "sample.py"
    outside = tmp_path / "outside.py"
    original = "VALUE = 1" + chr(10)
    proposed = "VALUE = 2" + chr(10)
    external = "DO_NOT_TOUCH = True" + chr(10)
    source.write_text(original, encoding="utf-8")
    outside.write_text(external, encoding="utf-8")

    store = TaskStore(tmp_path / "tasks.sqlite3")
    engine = CodingEngine(workspace, store, _UnusedRouter())
    proposal = store.create_proposal(
        "detect symlink swap during rollback",
        [{
            "path": "sample.py",
            "original_content": original,
            "original_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
            "content": proposed,
            "diff": "",
        }],
    )
    engine.approve(proposal["id"])

    def swap_target_then_fail_sync(_directory: Path) -> None:
        source.unlink()
        source.symlink_to(outside)
        raise OSError("simulated apply directory sync failure")

    monkeypatch.setattr(engine, "_fsync_directory", swap_target_then_fail_sync)

    with pytest.raises(CodingError, match="rollback failure"):
        engine.apply(proposal["id"])

    assert source.is_symlink()
    assert outside.read_text(encoding="utf-8") == external
    saved = store.get_proposal(proposal["id"])
    assert saved is not None
    assert saved["status"] == "applying"
    assert "Symlink paths are not supported" in saved["error_text"]
    assert not list(workspace.glob(".crows-stage-*"))
    assert not list(workspace.glob(".crows-rollback-*"))
