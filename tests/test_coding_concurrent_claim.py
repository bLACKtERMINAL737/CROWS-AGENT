"""Verify concurrent workers cannot both claim one coding proposal."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from crows.store import TaskStore


def test_only_one_concurrent_worker_can_claim_approved_proposal(tmp_path: Path):
    store = TaskStore(tmp_path / "concurrent-claim.sqlite3")
    proposal = store.create_proposal("concurrent apply claim", [])
    store.set_proposal_status(proposal["id"], "approved")

    start = Barrier(2)

    def claim():
        start.wait(timeout=5)
        return store.claim_proposal_for_apply(proposal["id"])

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: claim(), range(2)))

    assert sorted(results) == [False, True]
    saved = store.get_proposal(proposal["id"])
    assert saved is not None
    assert saved["status"] == "applying"
