"""Exercise coding proposal recovery after an abrupt worker-process exit."""

import subprocess
import sys
from pathlib import Path

from crows.store import TaskStore


def test_process_crash_releases_apply_lock_and_startup_marks_proposal_for_review(
    tmp_path: Path,
):
    database = tmp_path / "crash-recovery.sqlite3"
    store = TaskStore(database)
    proposal = store.create_proposal("interrupted change", [])
    store.set_proposal_status(proposal["id"], "approved")

    child_script = """
import os
import sys
from pathlib import Path
from crows.store import TaskStore

store = TaskStore(Path(sys.argv[1]))
proposal_id = sys.argv[2]
with store.coding_apply_lock():
    assert store.claim_proposal_for_apply(proposal_id)
    os._exit(23)
"""

    child = subprocess.run(
        [sys.executable, "-c", child_script, str(database), proposal["id"]],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert child.returncode == 23, child.stderr

    interrupted = store.get_proposal(proposal["id"])
    assert interrupted is not None
    assert interrupted["status"] == "applying"
    assert interrupted["error_text"] is None

    # This is the same guarded recovery sequence used by application startup.
    with store.coding_apply_lock(blocking=False) as acquired:
        assert acquired is True
        assert store.mark_interrupted_proposals_for_review() == 1

    recovered = store.get_proposal(proposal["id"])
    assert recovered is not None
    assert recovered["status"] == "applying"
    assert "Process restarted while applying" in recovered["error_text"]
