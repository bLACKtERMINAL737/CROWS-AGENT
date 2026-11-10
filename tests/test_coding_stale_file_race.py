"""Verify apply refuses a workspace edit made after initial validation."""

import hashlib
from pathlib import Path

import pytest

from crows.coding import CodingEngine, CodingError
from crows.store import TaskStore


class _UnusedRouter:
    async def chat(self, message, task_type="general"):
        raise AssertionError("apply must not invoke the model router")


def test_apply_rejects_file_changed_after_staging_before_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "sample.py"
    original = "def value():\n    return 1\n"
    concurrent_edit = "def value():\n    return 99\n"
    proposed = "def value():\n    return 2\n"
    target.write_text(original, encoding="utf-8")

    store = TaskStore(tmp_path / "tasks.sqlite3")
    engine = CodingEngine(workspace, store, _UnusedRouter())
    proposal = store.create_proposal("stale apply race", [{
        "path": "sample.py",
        "original_content": original,
        "original_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
        "content": proposed,
        "diff": "",
    }])
    engine.approve(proposal["id"])

    real_read_text = Path.read_text
    reads = 0

    def concurrent_edit_after_initial_read(path, *args, **kwargs):
        nonlocal reads
        result = real_read_text(path, *args, **kwargs)
        if path == target:
            reads += 1
            if reads == 1:
                target.write_text(concurrent_edit, encoding="utf-8")
        return result

    monkeypatch.setattr(Path, "read_text", concurrent_edit_after_initial_read)

    with pytest.raises(CodingError, match="changed while proposal was being applied"):
        engine.apply(proposal["id"])

    assert reads >= 2
    assert target.read_text(encoding="utf-8") == concurrent_edit
    saved = store.get_proposal(proposal["id"])
    assert saved is not None
    assert saved["status"] == "approved"
    assert "changed while proposal was being applied" in saved["error_text"]
