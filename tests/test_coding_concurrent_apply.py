"""Exercise concurrent apply requests against the real coding engine."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import hashlib

from crows.coding import CodingEngine, CodingError
from crows.store import TaskStore


class _UnusedRouter:
    async def chat(self, message, task_type="general"):
        raise AssertionError("apply must not invoke the model router")


def test_concurrent_apply_requests_change_workspace_exactly_once(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "sample.py"
    original = "def value():\n    return 1\n"
    updated = "def value():\n    return 2\n"
    target.write_text(original, encoding="utf-8")

    store = TaskStore(tmp_path / "tasks.sqlite3")
    engine = CodingEngine(workspace, store, _UnusedRouter())
    proposal = store.create_proposal("concurrent file update", [{
        "path": "sample.py",
        "original_content": original,
        "original_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
        "content": updated,
        "diff": "",
    }])
    engine.approve(proposal["id"])

    start = Barrier(2)

    def apply():
        start.wait(timeout=5)
        try:
            return engine.apply(proposal["id"])
        except CodingError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: apply(), range(2)))

    successes = [result for result in results if isinstance(result, dict)]
    failures = [result for result in results if isinstance(result, CodingError)]

    assert len(successes) == 1
    assert len(failures) == 1
    assert target.read_text(encoding="utf-8") == updated
    saved = store.get_proposal(proposal["id"])
    assert saved is not None
    assert saved["status"] == "applied"
