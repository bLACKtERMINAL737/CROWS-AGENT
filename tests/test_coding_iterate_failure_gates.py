import json

import pytest

from crows.coding import CodingEngine
from crows.store import TaskStore


class IterationFailureRouter:
    def __init__(self):
        self.calls = 0

    async def chat(self, message, task_type="general", **kwargs):
        self.calls += 1
        return {
            "answer": json.dumps({
                "summary": "Candidate change",
                "changes": [{
                    "path": "sample.py",
                    "content": "def value():\\n    return 2\\n",
                }],
            }),
            "route": "coding",
            "model": "fixture-model",
            "fallback_used": False,
            "attempts": 1,
        }


def _passing_result():
    return {
        "passed": True,
        "returncode": 0,
        "stdout": "===== 235 passed in 0.10s =====",
        "stderr": "",
    }


@pytest.mark.asyncio
async def test_iterative_coding_blocks_before_model_call_when_baseline_fails(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "sample.py"
    original = "def value():\\n    return 1\\n"
    source.write_text(original, encoding="utf-8")
    store = TaskStore(tmp_path / "baseline-failure.sqlite3")
    router = IterationFailureRouter()
    engine = CodingEngine(workspace, store, router, enable_test_runner=True)
    runs = []

    def failed_baseline():
        runs.append("baseline")
        return {
            "passed": False,
            "returncode": 1,
            "stdout": "===== 1 failed in 0.10s =====",
            "stderr": "baseline regression",
        }

    monkeypatch.setattr(CodingEngine, "run_tests", lambda self: failed_baseline())
    outcome = await engine.iterate_with_tests("Improve value()", ["sample.py"], 1)

    assert outcome["passed"] is False
    assert outcome["proposal"] is None
    assert "baseline tests must pass" in outcome["message"]
    assert runs == ["baseline"]
    assert router.calls == 0
    assert source.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_iterative_coding_discards_candidate_when_candidate_tests_fail(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "sample.py"
    original = "def value():\\n    return 1\\n"
    source.write_text(original, encoding="utf-8")
    store = TaskStore(tmp_path / "candidate-failure.sqlite3")
    router = IterationFailureRouter()
    engine = CodingEngine(workspace, store, router, enable_test_runner=True)
    runs = []

    def baseline_then_candidate_failure():
        runs.append("test")
        if len(runs) == 1:
            return _passing_result()
        return {
            "passed": False,
            "returncode": 1,
            "stdout": "===== 234 passed, 1 failed in 0.12s =====",
            "stderr": "candidate regression",
        }

    monkeypatch.setattr(
        CodingEngine, "run_tests",
        lambda self: baseline_then_candidate_failure(),
    )
    outcome = await engine.iterate_with_tests("Improve value()", ["sample.py"], 1)

    assert outcome["passed"] is False
    assert outcome["proposal"] is None
    assert len(outcome["rounds"]) == 1
    assert outcome["rounds"][0]["passed"] is False
    assert outcome["rounds"][0]["gate_status"] == "blocked"
    assert runs == ["test", "test"]
    assert router.calls == 1
    # Candidate edits exist only in the disposable snapshot.
    assert source.read_text(encoding="utf-8") == original
