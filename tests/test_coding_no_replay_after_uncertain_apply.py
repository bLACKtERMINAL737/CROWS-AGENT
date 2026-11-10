"""Ensure an uncertain apply is never replayed automatically."""

import hashlib
from pathlib import Path

import pytest

from crows.coding import CodingEngine, CodingError
from crows.store import TaskStore


class _UnusedRouter:
    async def chat(self, message, task_type="general"):
        return {"answer": "{}", "route": "coding", "model": "fake", "fallback_used": False, "attempts": 1}


def test_apply_is_not_replayed_after_final_status_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "sample.py"
    original = "def value():\n    return 1\n"
    updated = "def value():\n    return 2\n"
    source.write_text(original, encoding="utf-8")

    store = TaskStore(tmp_path / "tasks.sqlite3")
    engine = CodingEngine(workspace, store, _UnusedRouter())
    proposal = store.create_proposal("change value", [{
        "path": "sample.py",
        "original_content": original,
        "original_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
        "content": updated,
        "diff": "",
    }])
    engine.approve(proposal["id"])

    real_set_status = store.set_proposal_status
    failed = False

    def fail_once_on_final_applied_write(proposal_id: str, status: str, error=None):
        nonlocal failed
        if status == "applied" and not failed:
            failed = True
            raise OSError("simulated database finalization failure")
        return real_set_status(proposal_id, status, error)

    monkeypatch.setattr(store, "set_proposal_status", fail_once_on_final_applied_write)

    with pytest.raises(CodingError, match="final proposal status"):
        engine.apply(proposal["id"])

    assert source.read_text(encoding="utf-8") == updated
    before_retry = source.read_bytes()

    with pytest.raises(CodingError, match="approved"):
        engine.apply(proposal["id"])

    assert source.read_bytes() == before_retry
    saved = store.get_proposal(proposal["id"])
    assert saved is not None
    assert saved["status"] == "applying"
    assert "saving the applied status failed" in saved["error_text"]
