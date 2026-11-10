"""Verify multi-file apply rolls back earlier replacements on a later stale target."""

import hashlib
from pathlib import Path

import pytest

from crows.coding import CodingEngine, CodingError
from crows.store import TaskStore


class _UnusedRouter:
    async def chat(self, message, task_type="general"):
        raise AssertionError("apply must not invoke the model router")


def test_later_stale_target_rolls_back_earlier_replacement_and_preserves_external_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = workspace / "first.py"
    second = workspace / "second.py"
    first_original = "VALUE = 1\n"
    first_proposed = "VALUE = 2\n"
    second_original = "VALUE = 10\n"
    second_external = "VALUE = 99\n"
    second_proposed = "VALUE = 20\n"
    first.write_text(first_original, encoding="utf-8")
    second.write_text(second_original, encoding="utf-8")

    store = TaskStore(tmp_path / "tasks.sqlite3")
    engine = CodingEngine(workspace, store, _UnusedRouter())
    changes = [
        {
            "path": "first.py",
            "original_content": first_original,
            "original_sha256": hashlib.sha256(first_original.encode()).hexdigest(),
            "content": first_proposed,
            "diff": "",
        },
        {
            "path": "second.py",
            "original_content": second_original,
            "original_sha256": hashlib.sha256(second_original.encode()).hexdigest(),
            "content": second_proposed,
            "diff": "",
        },
    ]
    proposal = store.create_proposal("multi-file stale race", changes)
    engine.approve(proposal["id"])

    real_read_text = Path.read_text
    second_reads = 0

    def edit_second_after_staging_read(path, *args, **kwargs):
        nonlocal second_reads
        result = real_read_text(path, *args, **kwargs)
        if path == second:
            second_reads += 1
            if second_reads == 1:
                second.write_text(second_external, encoding="utf-8")
        return result

    monkeypatch.setattr(Path, "read_text", edit_second_after_staging_read)

    with pytest.raises(CodingError, match="changed while proposal was being applied"):
        engine.apply(proposal["id"])

    assert first.read_text(encoding="utf-8") == first_original
    assert second.read_text(encoding="utf-8") == second_external
    saved = store.get_proposal(proposal["id"])
    assert saved is not None
    assert saved["status"] == "approved"
    assert "changed while proposal was being applied" in saved["error_text"]
    assert not list(workspace.glob(".crows-stage-*"))
    assert not list(workspace.glob(".crows-rollback-*"))
