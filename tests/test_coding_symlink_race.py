"""Ensure a symlink swap during staging is detected before replacement."""

import hashlib
import tempfile
from pathlib import Path

import pytest

import crows.coding as coding_module
from crows.coding import CodingEngine, CodingError
from crows.store import TaskStore


class _UnusedRouter:
    async def chat(self, message, task_type="general"):
        raise AssertionError("apply must not invoke the model router")


def test_apply_rejects_target_replaced_by_symlink_during_staging(
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
        "detect symlink swap during apply",
        [{
            "path": "sample.py",
            "original_content": original,
            "original_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
            "content": proposed,
            "diff": "",
        }],
    )
    engine.approve(proposal["id"])

    real_mkstemp = tempfile.mkstemp
    swapped = False

    def swap_target_after_staging(*args, **kwargs):
        nonlocal swapped
        result = real_mkstemp(*args, **kwargs)
        if not swapped:
            source.unlink()
            source.symlink_to(outside)
            swapped = True
        return result

    monkeypatch.setattr(coding_module.tempfile, "mkstemp", swap_target_after_staging)

    with pytest.raises(CodingError, match="Symlink paths are not supported"):
        engine.apply(proposal["id"])

    assert source.is_symlink()
    assert outside.read_text(encoding="utf-8") == external
    saved = store.get_proposal(proposal["id"])
    assert saved is not None
    assert saved["status"] == "approved"
    assert not list(workspace.glob(".crows-stage-*"))
