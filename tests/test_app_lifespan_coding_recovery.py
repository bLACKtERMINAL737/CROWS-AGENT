"""Integration tests for Crows' real FastAPI startup recovery lifecycle."""

import asyncio
from pathlib import Path

import crows.app as app_module
from crows.store import TaskStore


def _make_interrupted_proposal(store: TaskStore) -> str:
    proposal = store.create_proposal("startup recovery integration", [])
    store.set_proposal_status(proposal["id"], "approved")
    assert store.claim_proposal_for_apply(proposal["id"])
    return proposal["id"]


def _run_lifespan() -> None:
    async def run() -> None:
        async with app_module.lifespan(app_module.app):
            pass

    asyncio.run(run())


def test_real_app_lifespan_marks_interrupted_proposal_for_manual_review(
    tmp_path: Path, monkeypatch
):
    store = TaskStore(tmp_path / "lifespan.sqlite3")
    proposal_id = _make_interrupted_proposal(store)
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setenv("CROWS_MESSAGING_WORKER_ENABLED", "false")
    monkeypatch.setenv("CROWS_SCHEDULER_WORKER_ENABLED", "false")

    _run_lifespan()

    proposal = store.get_proposal(proposal_id)
    assert proposal is not None
    assert proposal["status"] == "applying"
    assert "Process restarted while applying" in proposal["error_text"]


def test_real_app_lifespan_skips_recovery_while_another_process_holds_lock(
    tmp_path: Path, monkeypatch
):
    store = TaskStore(tmp_path / "lifespan-lock.sqlite3")
    proposal_id = _make_interrupted_proposal(store)
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setenv("CROWS_MESSAGING_WORKER_ENABLED", "false")
    monkeypatch.setenv("CROWS_SCHEDULER_WORKER_ENABLED", "false")

    with store.coding_apply_lock():
        _run_lifespan()

    proposal = store.get_proposal(proposal_id)
    assert proposal is not None
    assert proposal["status"] == "applying"
    assert proposal["error_text"] is None

    # Once the active owner releases the lock, a later startup can recover it.
    _run_lifespan()
    recovered = store.get_proposal(proposal_id)
    assert recovered is not None
    assert recovered["status"] == "applying"
    assert "Process restarted while applying" in recovered["error_text"]
