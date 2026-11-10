"""Ensure an external chmod during apply is not overwritten by staged content."""

import hashlib
import os
import stat
import tempfile
from pathlib import Path

import pytest

import crows.coding as coding_module
from crows.coding import CodingEngine, CodingError
from crows.store import TaskStore


class _UnusedRouter:
    async def chat(self, message, task_type="general"):
        raise AssertionError("apply must not invoke the model router")


def test_apply_refuses_to_overwrite_permissions_changed_after_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    if os.name == "nt":
        pytest.skip("Windows chmod does not expose POSIX permission-mode changes")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "sample.py"
    original = "VALUE = 1" + chr(10)
    proposed = "VALUE = 2" + chr(10)
    source.write_text(original, encoding="utf-8")
    source.chmod(0o640)

    store = TaskStore(tmp_path / "tasks.sqlite3")
    engine = CodingEngine(workspace, store, _UnusedRouter())
    proposal = store.create_proposal(
        "detect concurrent permission change",
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
    changed = False

    def chmod_after_staging_file_created(*args, **kwargs):
        nonlocal changed
        result = real_mkstemp(*args, **kwargs)
        if not changed:
            source.chmod(0o600)
            changed = True
        return result

    monkeypatch.setattr(coding_module.tempfile, "mkstemp", chmod_after_staging_file_created)

    with pytest.raises(CodingError, match="permissions changed while proposal was being applied"):
        engine.apply(proposal["id"])

    assert source.read_text(encoding="utf-8") == original
    assert stat.S_IMODE(source.stat().st_mode) == 0o600
    saved = store.get_proposal(proposal["id"])
    assert saved is not None
    assert saved["status"] == "approved"
    assert not list(workspace.glob(".crows-stage-*"))
