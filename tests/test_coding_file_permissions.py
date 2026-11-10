"""Verify coding apply and rollback preserve existing file permissions."""

import hashlib
import os
import stat
from pathlib import Path

import pytest

from crows.coding import CodingEngine, CodingError
from crows.store import TaskStore


class _UnusedRouter:
    async def chat(self, message, task_type="general"):
        raise AssertionError("apply must not invoke the model router")


def _make_proposal(tmp_path: Path):
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
        "preserve file mode",
        [{
            "path": "sample.py",
            "original_content": original,
            "original_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
            "content": proposed,
            "diff": "",
        }],
    )
    engine.approve(proposal["id"])
    return source, original, proposed, store, engine, proposal


def test_apply_preserves_existing_file_permissions(tmp_path: Path):
    if os.name == "nt":
        pytest.skip("Windows exposes only the read-only file attribute through chmod")
    source, _original, proposed, _store, engine, proposal = _make_proposal(tmp_path)

    result = engine.apply(proposal["id"])

    assert result["status"] == "applied"
    assert source.read_text(encoding="utf-8") == proposed
    assert stat.S_IMODE(source.stat().st_mode) == 0o640


def test_successful_rollback_restores_original_file_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    if os.name == "nt":
        pytest.skip("Windows exposes only the read-only file attribute through chmod")
    source, original, _proposed, store, engine, proposal = _make_proposal(tmp_path)
    calls = 0

    def fail_first_directory_sync(_directory: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated apply directory sync failure")

    monkeypatch.setattr(engine, "_fsync_directory", fail_first_directory_sync)

    with pytest.raises(CodingError, match="Apply failed"):
        engine.apply(proposal["id"])

    assert source.read_text(encoding="utf-8") == original
    assert stat.S_IMODE(source.stat().st_mode) == 0o640
    saved = store.get_proposal(proposal["id"])
    assert saved is not None
    assert saved["status"] == "approved"
