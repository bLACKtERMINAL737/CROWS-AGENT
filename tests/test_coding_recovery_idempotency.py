"""Verify interrupted-proposal recovery is safe to repeat."""

from pathlib import Path

from crows.store import TaskStore


def test_interrupted_proposal_recovery_is_idempotent_and_preserves_diagnostics(
    tmp_path: Path,
):
    store = TaskStore(tmp_path / "recovery-idempotency.sqlite3")

    first = store.create_proposal("first interrupted proposal", [])
    second = store.create_proposal("second interrupted proposal", [])
    pending = store.create_proposal("still pending proposal", [])

    for proposal in (first, second):
        store.set_proposal_status(proposal["id"], "approved")
        assert store.claim_proposal_for_apply(proposal["id"])

    store.set_proposal_status(
        second["id"],
        "applying",
        error="Rollback could not restore one workspace file; inspect it manually.",
    )

    assert store.mark_interrupted_proposals_for_review() == 2

    first_after = store.get_proposal(first["id"])
    second_after = store.get_proposal(second["id"])
    pending_after = store.get_proposal(pending["id"])

    assert first_after is not None
    assert first_after["status"] == "applying"
    assert "Process restarted while applying" in first_after["error_text"]

    assert second_after is not None
    assert second_after["status"] == "applying"
    assert second_after["error_text"] == (
        "Rollback could not restore one workspace file; inspect it manually."
    )

    assert pending_after is not None
    assert pending_after["status"] == "pending"
    assert pending_after["error_text"] is None

    # A second startup must not erase diagnostics or transition proposals.
    assert store.mark_interrupted_proposals_for_review() == 2
    assert store.get_proposal(first["id"])["error_text"] == first_after["error_text"]
    assert store.get_proposal(second["id"])["error_text"] == second_after["error_text"]
