"""Ensure rollback never overwrites edits made after Crows replaced a file."""

import hashlib
from pathlib import Path

import pytest

from crows.coding import CodingEngine, CodingError
from crows.store import TaskStore


class _UnusedRouter:
    async def chat(self, message, task_type="general"):
        raise AssertionError("apply must not invoke the model router")


def test_rollback_preserves_external_edit_to_already_replaced_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "sample.py"
    original = "VALUE = 1" + chr(10)
    proposed = "VALUE = 2" + chr(10)
    external = "VALUE = 99" + chr(10)
    source.write_text(original, encoding="utf-8")

    store = TaskStore(tmp_path / "tasks.sqlite3")
    engine = CodingEngine(workspace, store, _UnusedRouter())
    proposal = store.create_proposal(
        "preserve concurrent external edit during rollback",
        [{
            "path": "sample.py",
            "original_content": original,
            "original_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
            "content": proposed,
            "diff": "",
        }],
    )
    engine.approve(proposal["id"])

    def external_edit_then_fail_sync(directory: Path) -> None:
        source.write_text(external, encoding="utf-8")
        raise OSError("simulated directory sync failure")

    monkeypatch.setattr(engine, "_fsync_directory", external_edit_then_fail_sync)

    with pytest.raises(CodingError, match="rollback failure"):
        engine.apply(proposal["id"])

    # The rollback must not clobber a newer edit, even though the proposal
    # cannot be safely finalized and therefore remains in manual review.
    assert source.read_text(encoding="utf-8") == external
    saved = store.get_proposal(proposal["id"])
    assert saved is not None
    assert saved["status"] == "applying"
    assert "refused to overwrite an external edit" in saved["error_text"]
    assert not list(workspace.glob(".crows-stage-*"))
    assert not list(workspace.glob(".crows-rollback-*"))
