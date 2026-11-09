"""Verify coding apply locks coordinate independent operating-system processes."""

import subprocess
import sys
from pathlib import Path

from crows.store import TaskStore


def test_coding_apply_lock_excludes_an_independent_process(tmp_path: Path):
    database = tmp_path / "cross-process.sqlite3"
    store = TaskStore(database)
    child_script = """
import sys
import time
from pathlib import Path
from crows.store import TaskStore

store = TaskStore(Path(sys.argv[1]))
with store.coding_apply_lock() as acquired:
    print("LOCK_ACQUIRED" if acquired else "LOCK_NOT_ACQUIRED", flush=True)
    time.sleep(1.5)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", child_script, str(database)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "LOCK_ACQUIRED"

        with store.coding_apply_lock(blocking=False) as acquired:
            assert acquired is False

        assert child.wait(timeout=5) == 0
        with store.coding_apply_lock(blocking=False) as acquired:
            assert acquired is True
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)
        if child.stdout is not None:
            child.stdout.close()
        if child.stderr is not None:
            child.stderr.close()



def test_runtime_process_lock_excludes_an_independent_process(tmp_path: Path):
    database = tmp_path / "runtime-owner.sqlite3"
    store = TaskStore(database)
    child_script = """
import sys
from pathlib import Path
from crows.store import TaskStore

store = TaskStore(Path(sys.argv[1]))
with store.runtime_process_lock(blocking=False) as acquired:
    print("RUNTIME_LOCK_ACQUIRED" if acquired else "RUNTIME_LOCK_BUSY", flush=True)
"""
    with store.runtime_process_lock(blocking=False) as acquired:
        assert acquired is True
        child = subprocess.run(
            [sys.executable, "-c", child_script, str(database)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert child.returncode == 0, child.stderr
        assert child.stdout.strip() == "RUNTIME_LOCK_BUSY"

    with store.runtime_process_lock(blocking=False) as acquired:
        assert acquired is True
