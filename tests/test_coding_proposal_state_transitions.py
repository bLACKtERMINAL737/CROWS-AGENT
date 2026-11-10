"""Lock down coding proposal state transitions at the persistence boundary."""

from pathlib import Path

import pytest

from crows.store import TaskStore


def _proposal_in_status(store: TaskStore, status: str) -> str:
    proposal = store.create_proposal("state transition test", [])
    if status == "pending":
        return proposal["id"]
    store.set_proposal_status(proposal["id"], "approved")
    if status == "approved":
        return proposal["id"]
    assert store.claim_proposal_for_apply(proposal["id"])
    if status == "applying":
        return proposal["id"]
    store.set_proposal_status(proposal["id"], "applied")
    return proposal["id"]


@pytest.mark.parametrize(
    ("initial", "target"),
    [
        ("applying", "pending"),
        ("applying", "rejected"),
        ("applied", "pending"),
        ("applied", "approved"),
        ("applied", "applying"),
        ("applied", "rejected"),
    ],
)
def test_proposal_cannot_leave_uncertain_or_terminal_state(
    tmp_path: Path, initial: str, target: str
):
    store = TaskStore(tmp_path / f"{initial}-{target}.sqlite3")
    proposal_id = _proposal_in_status(store, initial)

    with pytest.raises(ValueError, match="Invalid proposal status transition"):
        store.set_proposal_status(proposal_id, target)

    saved = store.get_proposal(proposal_id)
    assert saved is not None
    assert saved["status"] == initial


def test_applying_proposal_can_only_be_finalized_as_applied_without_reconciliation_note(
    tmp_path: Path,
):
    store = TaskStore(tmp_path / "applying-finalization.sqlite3")
    proposal_id = _proposal_in_status(store, "applying")

    store.set_proposal_status(proposal_id, "applied")

    saved = store.get_proposal(proposal_id)
    assert saved is not None
    assert saved["status"] == "applied"
