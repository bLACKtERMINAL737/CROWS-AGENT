from pathlib import Path
import pytest

from crows.coding import CodingEngine, CodingError
from crows.store import TaskStore


class FakeRouter:
    def __init__(self, answer):
        self.answer = answer
    async def chat(self, message, task_type="general"):
        return {"answer": self.answer, "route": "coding", "model": "fake", "fallback_used": False, "attempts": 1}


def make_engine(tmp_path: Path, answer: str):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "sample.py"
    source.write_text("def value():\n    return 1\n", encoding="utf-8")
    store = TaskStore(tmp_path / "crows.sqlite3")
    engine = CodingEngine(workspace, store, FakeRouter(answer))
    return engine, store, workspace, source


@pytest.mark.asyncio
async def test_proposal_is_pending_and_does_not_change_files(tmp_path: Path):
    answer = '{"summary":"change value","changes":[{"path":"sample.py","content":"def value():\\n    return 2\\n"}]}'
    engine, store, workspace, source = make_engine(tmp_path, answer)
    before = source.read_text(encoding="utf-8")
    proposal = await engine.propose("Change value to 2", ["sample.py"])
    assert proposal["status"] == "pending"
    assert source.read_text(encoding="utf-8") == before
    assert "return 2" in proposal["changes"][0]["diff"]




@pytest.mark.asyncio
async def test_proposal_rejects_comment_only_changes(tmp_path: Path):
    answer = (
        '{"summary":"comment cleanup","changes":['
        '{"path":"sample.py","content":"# explanation\\ndef value():\\n    return 1\\n"}]}'
    )
    engine, store, workspace, source = make_engine(tmp_path, answer)
    before = source.read_text(encoding="utf-8")

    with pytest.raises(CodingError, match="only comment or whitespace"):
        await engine.propose("Improve value behavior", ["sample.py"])

    assert source.read_text(encoding="utf-8") == before
    assert store.list_proposals() == []

@pytest.mark.asyncio
async def test_proposal_rejects_unchanged_content(tmp_path: Path):
    answer = (
        '{"summary":"pretend change","changes":['
        '{"path":"sample.py","content":"def value():\\n    return 1\\n"}]}'
    )
    engine, store, workspace, source = make_engine(tmp_path, answer)
    before = source.read_text(encoding="utf-8")

    with pytest.raises(CodingError, match="no actual changes"):
        await engine.propose("Change value", ["sample.py"])

    assert source.read_text(encoding="utf-8") == before
    assert store.list_proposals() == []

def test_cannot_apply_before_approval(tmp_path: Path):
    engine, store, workspace, source = make_engine(tmp_path, "{}")
    proposal = store.create_proposal("test", [{
        "path": "sample.py",
        "original_content": source.read_text(encoding="utf-8"),
        "original_sha256": __import__("hashlib").sha256(source.read_bytes()).hexdigest(),
        "content": "def value():\n    return 2\n",
        "diff": "",
    }])
    with pytest.raises(CodingError, match="approved"):
        engine.apply(proposal["id"])
    assert "return 1" in source.read_text(encoding="utf-8")





def test_apply_rejects_legacy_proposal_targeting_sensitive_file(tmp_path: Path):
    import hashlib

    engine, store, workspace, _ = make_engine(tmp_path, "{}")
    secret = workspace / ".env"
    original = "SERVICE_TOKEN=must-not-be-overwritten\n"
    secret.write_text(original, encoding="utf-8")
    proposal = store.create_proposal("legacy sensitive proposal", [{
        "path": ".env",
        "original_content": original,
        "original_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
        "content": "SERVICE_TOKEN=changed\n",
        "diff": "",
    }])

    # Simulate a proposal persisted before the source-reading denylist existed.
    engine.approve(proposal["id"])
    with pytest.raises(CodingError, match="Sensitive file cannot be sent"):
        engine.apply(proposal["id"])

    assert secret.read_text(encoding="utf-8") == original
    assert store.get_proposal(proposal["id"])["status"] == "approved"


def test_approved_proposal_applies_atomically_and_marks_applied(tmp_path: Path):
    engine, store, workspace, source = make_engine(tmp_path, "{}")
    original = source.read_text(encoding="utf-8")
    import hashlib
    proposal = store.create_proposal("test", [{
        "path": "sample.py",
        "original_content": original,
        "original_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
        "content": "def value():\n    return 2\n",
        "diff": "",
    }])
    engine.approve(proposal["id"])
    result = engine.apply(proposal["id"])
    assert result["status"] == "applied"
    assert "return 2" in source.read_text(encoding="utf-8")
    assert store.get_proposal(proposal["id"])["status"] == "applied"



def test_apply_rolls_back_replaced_files_on_replace_failure(tmp_path: Path, monkeypatch):
    import hashlib
    import os

    engine, store, workspace, source = make_engine(tmp_path, "{}")
    second = workspace / "second.py"
    second.write_text("VALUE = 1\n", encoding="utf-8")

    original_first = source.read_text(encoding="utf-8")
    original_second = second.read_text(encoding="utf-8")

    proposal = store.create_proposal("two-file change", [
        {
            "path": "sample.py",
            "original_content": original_first,
            "original_sha256": hashlib.sha256(original_first.encode("utf-8")).hexdigest(),
            "content": "def value():\n    return 2\n",
            "diff": "",
        },
        {
            "path": "second.py",
            "original_content": original_second,
            "original_sha256": hashlib.sha256(original_second.encode("utf-8")).hexdigest(),
            "content": "VALUE = 2\n",
            "diff": "",
        },
    ])
    engine.approve(proposal["id"])

    replace_name = "replace" if os.name == "nt" else "rename"
    real_replace = getattr(os, replace_name)
    calls = 0

    def fail_second_replace(src, dst, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated replace failure")
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr("crows.coding.os." + replace_name, fail_second_replace)

    with pytest.raises(CodingError, match="Apply failed"):
        engine.apply(proposal["id"])

    assert source.read_text(encoding="utf-8") == original_first
    assert second.read_text(encoding="utf-8") == original_second
    assert store.get_proposal(proposal["id"])["status"] == "approved"




def test_apply_syncs_parent_directory_after_each_replacement(tmp_path: Path, monkeypatch):
    import hashlib

    engine, store, workspace, source = make_engine(tmp_path, "{}")
    second = workspace / "second.py"
    second.write_text("VALUE = 1\\n", encoding="utf-8")
    originals = [source.read_text(encoding="utf-8"), second.read_text(encoding="utf-8")]
    proposal = store.create_proposal("durable two-file change", [
        {"path": "sample.py", "original_content": originals[0],
         "original_sha256": hashlib.sha256(originals[0].encode()).hexdigest(),
         "content": "def value():\\n    return 2\\n", "diff": ""},
        {"path": "second.py", "original_content": originals[1],
         "original_sha256": hashlib.sha256(originals[1].encode()).hexdigest(),
         "content": "VALUE = 2\\n", "diff": ""},
    ])
    engine.approve(proposal["id"])
    synced = []
    monkeypatch.setattr(engine, "_fsync_directory", lambda directory: synced.append(directory))

    result = engine.apply(proposal["id"])

    assert result["status"] == "applied"
    assert synced == [workspace, workspace]


def test_directory_sync_failure_rolls_back_replaced_file(tmp_path: Path, monkeypatch):
    import hashlib

    engine, store, workspace, source = make_engine(tmp_path, "{}")
    original = source.read_text(encoding="utf-8")
    proposal = store.create_proposal("directory sync failure", [{
        "path": "sample.py",
        "original_content": original,
        "original_sha256": hashlib.sha256(original.encode()).hexdigest(),
        "content": "def value():\\n    return 2\\n",
        "diff": "",
    }])
    engine.approve(proposal["id"])
    calls = 0

    def fail_first_sync(directory):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated directory fsync failure")

    monkeypatch.setattr(engine, "_fsync_directory", fail_first_sync)

    with pytest.raises(CodingError, match="Apply failed"):
        engine.apply(proposal["id"])

    assert source.read_text(encoding="utf-8") == original
    assert store.get_proposal(proposal["id"])["status"] == "approved"
    assert calls == 2



def test_apply_reports_rollback_failure_without_hiding_original_error(
    tmp_path: Path, monkeypatch
):
    import hashlib
    import os

    engine, store, workspace, source = make_engine(tmp_path, "{}")
    second = workspace / "second.py"
    second.write_text("VALUE = 1\n", encoding="utf-8")

    original_first = source.read_text(encoding="utf-8")
    original_second = second.read_text(encoding="utf-8")

    proposal = store.create_proposal("two-file change", [
        {
            "path": "sample.py",
            "original_content": original_first,
            "original_sha256": hashlib.sha256(original_first.encode("utf-8")).hexdigest(),
            "content": "def value():\n    return 2\n",
            "diff": "",
        },
        {
            "path": "second.py",
            "original_content": original_second,
            "original_sha256": hashlib.sha256(original_second.encode("utf-8")).hexdigest(),
            "content": "VALUE = 2\n",
            "diff": "",
        },
    ])
    engine.approve(proposal["id"])

    replace_name = "replace" if os.name == "nt" else "rename"
    real_replace = getattr(os, replace_name)
    calls = 0

    def fail_second_replace(src, dst, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated replacement failure")
        if calls == 3:
            raise OSError("simulated rollback failure")
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr("crows.coding.os." + replace_name, fail_second_replace)

    with pytest.raises(CodingError, match="Apply failed"):
        engine.apply(proposal["id"])

    saved = store.get_proposal(proposal["id"])
    assert saved["status"] == "applying"
    assert saved["error_text"]

def test_apply_refuses_stale_proposal(tmp_path: Path):
    engine, store, workspace, source = make_engine(tmp_path, "{}")
    original = source.read_text(encoding="utf-8")
    import hashlib
    proposal = store.create_proposal("test", [{
        "path": "sample.py",
        "original_content": original,
        "original_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
        "content": "def value():\n    return 2\n",
        "diff": "",
    }])
    engine.approve(proposal["id"])
    source.write_text("def value():\n    return 9\n", encoding="utf-8")
    with pytest.raises(CodingError, match="changed since proposal"):
        engine.apply(proposal["id"])
    assert "return 9" in source.read_text(encoding="utf-8")


def test_test_runner_is_disabled_by_default(tmp_path: Path):
    engine, _, _, _ = make_engine(tmp_path, "{}")
    with pytest.raises(CodingError, match="disabled"):
        engine.run_tests()


def test_coding_engine_rejects_path_escape(tmp_path: Path):
    engine, _, _, _ = make_engine(tmp_path, "{}")
    with pytest.raises(CodingError, match="escapes"):
        engine.read_sources(["../outside.py"])


def test_coding_engine_rejects_symlink_paths(tmp_path: Path):
    engine, _, workspace, source = make_engine(tmp_path, "{}")
    linked = workspace / "linked.py"
    try:
        linked.symlink_to(source)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks are unavailable in this environment")
    with pytest.raises(CodingError, match="Symlink"):
        engine.read_sources(["linked.py"])


@pytest.mark.asyncio
async def test_proposal_cannot_modify_unselected_file(tmp_path: Path):
    answer = '{"summary":"bad proposal","changes":[{"path":"other.py","content":"print(1)"}]}'
    engine, _, workspace, source = make_engine(tmp_path, answer)
    (workspace / "other.py").write_text("print(0)\n", encoding="utf-8")
    with pytest.raises(CodingError, match="unselected file"):
        await engine.propose("Change sample", ["sample.py"])


@pytest.mark.asyncio
async def test_iterative_loop_returns_pending_proposal_after_sandbox_pass(tmp_path: Path, monkeypatch):
    first = '{"summary":"first attempt","changes":[{"path":"sample.py","content":"def value():\\n    return 2\\n"}]}'
    second = '{"summary":"fixed attempt","changes":[{"path":"sample.py","content":"def value():\\n    return 3\\n"}]}'

    class SequenceRouter:
        def __init__(self):
            self.answers = [first, second]
            self.prompts = []
        async def chat(self, message, task_type="general"):
            self.prompts.append(message)
            return {"answer": self.answers.pop(0), "route": "coding", "model": "fake", "fallback_used": False, "attempts": 1}

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "sample.py"
    original = "def value():\n    return 1\n"
    source.write_text(original, encoding="utf-8")
    store = TaskStore(tmp_path / "crows.sqlite3")
    router = SequenceRouter()
    engine = CodingEngine(workspace, store, router, enable_test_runner=True)

    results = iter([
        {"passed": True, "returncode": 0, "stdout": "===== 2 passed in 0.1s =====", "stderr": ""},
        {"passed": False, "returncode": 1, "stdout": "assert 2 == 3", "stderr": ""},
        {"passed": True, "returncode": 0, "stdout": "===== 2 passed in 0.1s =====", "stderr": ""},
    ])
    monkeypatch.setattr(CodingEngine, "run_tests", lambda self: next(results))

    outcome = await engine.iterate_with_tests("Make value correct", ["sample.py"], max_rounds=2)

    assert outcome["passed"] is True
    assert len(outcome["rounds"]) == 2
    assert outcome["proposal"]["status"] == "pending"
    assert "return 3" in outcome["proposal"]["changes"][0]["content"]
    assert source.read_text(encoding="utf-8") == original
    proposals = store.list_proposals()
    assert sum(p["status"] == "rejected" for p in proposals) == 2
    assert any("PREVIOUS SANDBOX TEST FAILURE" in prompt for prompt in router.prompts)


@pytest.mark.asyncio
async def test_iterative_loop_never_proposes_failed_candidate(tmp_path: Path, monkeypatch):
    answer = '{"summary":"bad attempt","changes":[{"path":"sample.py","content":"def value():\\n    return 2\\n"}]}'
    engine, store, workspace, source = make_engine(tmp_path, answer)
    engine.enable_test_runner = True
    monkeypatch.setattr(CodingEngine, "run_tests", lambda self: {
        "passed": False, "returncode": 1, "stdout": "", "stderr": "assertion failed"
    })

    outcome = await engine.iterate_with_tests("Try a change", ["sample.py"], max_rounds=1)

    assert outcome["passed"] is False
    assert outcome["proposal"] is None
    assert source.read_text(encoding="utf-8") == "def value():\n    return 1\n"
    assert all(p["status"] == "rejected" for p in store.list_proposals())


def test_proposal_store_round_trip(tmp_path: Path):
    store = TaskStore(tmp_path / "crows.sqlite3")
    created = store.create_proposal("summary", [{"path": "sample.py", "content": "print(1)"}])
    assert created["status"] == "pending"
    assert created["changes"][0]["path"] == "sample.py"
    store.set_proposal_status(created["id"], "approved")
    assert store.get_proposal(created["id"])["status"] == "approved"

@pytest.mark.asyncio
async def test_proposal_rejects_duplicate_paths(tmp_path: Path):
    answer = (
        '{"summary":"duplicate change","changes":['
        '{"path":"sample.py","content":"def value():\\n    return 2\\n"},'
        '{"path":"sample.py","content":"def value():\\n    return 3\\n"}]}'
    )
    engine, store, workspace, source = make_engine(tmp_path, answer)
    before = source.read_text(encoding="utf-8")

    with pytest.raises(CodingError, match="duplicate change"):
        await engine.propose("Change value", ["sample.py"])

    assert source.read_text(encoding="utf-8") == before
    assert store.list_proposals() == []


@pytest.mark.parametrize("status", ["approved", "rejected", "applied"])
def test_reject_only_allows_pending_proposals(tmp_path: Path, status: str):
    engine, store, workspace, source = make_engine(tmp_path, "{}")
    original = source.read_text(encoding="utf-8")
    import hashlib
    proposal = store.create_proposal("test", [{
        "path": "sample.py",
        "original_content": original,
        "original_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
        "content": "def value():\n    return 2\n",
        "diff": "",
    }])
    if status == "approved":
        store.set_proposal_status(proposal["id"], "approved")
    elif status == "rejected":
        store.set_proposal_status(proposal["id"], "rejected")
    else:
        store.set_proposal_status(proposal["id"], "approved")
        assert store.claim_proposal_for_apply(proposal["id"]) is True
        store.set_proposal_status(proposal["id"], "applied")

    with pytest.raises(CodingError, match="Only pending proposals can be rejected"):
        engine.reject(proposal["id"])

    assert store.get_proposal(proposal["id"])["status"] == status


def test_pending_proposal_can_be_rejected(tmp_path: Path):
    engine, store, _, _ = make_engine(tmp_path, "{}")
    proposal = store.create_proposal("test", [{"path": "sample.py", "content": "print(1)"}])

    result = engine.reject(proposal["id"])

    assert result["status"] == "rejected"

def test_applied_proposal_cannot_be_applied_twice(tmp_path: Path):
    engine, store, workspace, source = make_engine(tmp_path, "{}")
    import hashlib

    original = source.read_text(encoding="utf-8")
    proposal = store.create_proposal("test", [{
        "path": "sample.py",
        "original_content": original,
        "original_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
        "content": "def value():\n    return 2\n",
        "diff": "",
    }])

    engine.approve(proposal["id"])
    engine.apply(proposal["id"])
    applied_content = source.read_text(encoding="utf-8")

    with pytest.raises(CodingError, match="approved"):
        engine.apply(proposal["id"])

    assert source.read_text(encoding="utf-8") == applied_content
    assert store.get_proposal(proposal["id"])["status"] == "applied"


def test_rejected_proposal_cannot_be_approved(tmp_path: Path):
    engine, store, workspace, source = make_engine(tmp_path, "{}")
    proposal = store.create_proposal("test", [{
        "path": "sample.py",
        "original_content": source.read_text(encoding="utf-8"),
        "original_sha256": __import__("hashlib").sha256(
            source.read_bytes()
        ).hexdigest(),
        "content": "def value():\n    return 2\n",
        "diff": "",
    }])

    engine.reject(proposal["id"])

    with pytest.raises(CodingError, match="Only pending proposals"):
        engine.approve(proposal["id"])

    assert store.get_proposal(proposal["id"])["status"] == "rejected"
    assert "return 1" in source.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("initial", "target", "error"),
    [
        ("pending", "applied", None),
        ("rejected", "approved", None),
        ("applied", "rejected", None),
        ("approved", "rejected", None),
        ("approved", "approved", None),
    ],
)
def test_store_rejects_invalid_proposal_transitions(
    tmp_path: Path, initial: str, target: str, error: str | None
):
    store = TaskStore(tmp_path / "crows.sqlite3")
    proposal = store.create_proposal("transition test", [])

    if initial == "approved":
        store.set_proposal_status(proposal["id"], "approved")
    elif initial == "rejected":
        store.set_proposal_status(proposal["id"], "rejected")
    elif initial == "applied":
        store.set_proposal_status(proposal["id"], "approved")
        assert store.claim_proposal_for_apply(proposal["id"]) is True
        store.set_proposal_status(proposal["id"], "applied")

    before = store.get_proposal(proposal["id"])["status"]

    with pytest.raises(ValueError, match="Invalid proposal status transition"):
        store.set_proposal_status(proposal["id"], target, error=error)

    assert store.get_proposal(proposal["id"])["status"] == before


def test_store_allows_recording_apply_failure_without_changing_status(tmp_path: Path):
    store = TaskStore(tmp_path / "crows.sqlite3")
    proposal = store.create_proposal("apply failure", [])
    store.set_proposal_status(proposal["id"], "approved")

    store.set_proposal_status(
        proposal["id"], "approved", error="Apply failed; inspect workspace"
    )

    saved = store.get_proposal(proposal["id"])
    assert saved["status"] == "approved"
    assert saved["error_text"] == "Apply failed; inspect workspace"




def test_store_updates_apply_failure_error_text(tmp_path: Path):
    store = TaskStore(tmp_path / "crows.sqlite3")
    proposal = store.create_proposal("apply failure", [])
    store.set_proposal_status(proposal["id"], "approved")

    store.set_proposal_status(
        proposal["id"], "approved", error="first failure"
    )
    store.set_proposal_status(
        proposal["id"], "approved", error="second failure"
    )

    saved = store.get_proposal(proposal["id"])
    assert saved["status"] == "approved"
    assert saved["error_text"] == "second failure"

def test_store_rejects_unknown_proposal_id(tmp_path: Path):
    store = TaskStore(tmp_path / "crows.sqlite3")

    with pytest.raises(KeyError, match="Proposal not found"):
        store.set_proposal_status("missing-proposal-id", "approved")


@pytest.mark.asyncio
async def test_apply_reports_error_if_final_applied_status_update_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    engine, store, workspace, source = make_engine(
        tmp_path,
        '{"summary":"change value","changes":[{"path":"sample.py","content":"def value():\\n    return 2\\n"}]}',
    )
    proposal = await engine.propose("change value", ["sample.py"])
    engine.approve(proposal["id"])

    original_set_status = store.set_proposal_status

    def fail_final_applied_update(proposal_id, status, error=None):
        if status == "applied":
            raise RuntimeError("simulated database failure")
        return original_set_status(proposal_id, status, error=error)

    monkeypatch.setattr(store, "set_proposal_status", fail_final_applied_update)

    with pytest.raises(CodingError, match="final proposal status could not be saved"):
        engine.apply(proposal["id"])

    assert source.read_text(encoding="utf-8") == "def value():\n    return 2\n"
    saved = store.get_proposal(proposal["id"])
    assert saved["status"] == "applying"
    assert "simulated database failure" in saved["error_text"]


def test_concurrent_apply_requests_only_apply_proposal_once(tmp_path: Path):
    """Two engine instances sharing one local DB must not apply a proposal twice."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    import hashlib

    engine, store, workspace, source = make_engine(tmp_path, "{}")
    original = source.read_text(encoding="utf-8")
    proposal = store.create_proposal("concurrent apply", [{
        "path": "sample.py",
        "original_content": original,
        "original_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
        "content": "def value():\\n    return 2\\n",
        "diff": "",
    }])
    engine.approve(proposal["id"])

    # Use separate engine/store instances to exercise the cross-instance file lock.
    second_engine = CodingEngine(
        workspace, TaskStore(tmp_path / "crows.sqlite3"), FakeRouter("{}")
    )
    barrier = Barrier(2)

    def apply(engine_instance):
        barrier.wait(timeout=5)
        try:
            return engine_instance.apply(proposal["id"])
        except CodingError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, (engine, second_engine)))

    successes = [result for result in results if isinstance(result, dict)]
    failures = [result for result in results if isinstance(result, CodingError)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert successes[0]["status"] == "applied"
    assert source.read_text(encoding="utf-8") == "def value():\\n    return 2\\n"
    assert store.get_proposal(proposal["id"])["status"] == "applied"


def test_process_interruption_during_apply_requires_manual_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    engine, store, workspace, source = make_engine(tmp_path, "{}")
    original = source.read_text(encoding="utf-8")
    proposal = store.create_proposal("interrupted filesystem apply", [{
        "path": "sample.py",
        "original_content": original,
        "original_sha256": __import__("hashlib").sha256(original.encode("utf-8")).hexdigest(),
        "content": "def value():\\n    return 2\\n",
        "diff": "",
    }])
    engine.approve(proposal["id"])

    def interrupt_after_first_replace(_directory: Path) -> None:
        raise KeyboardInterrupt("simulated process interruption")

    monkeypatch.setattr(engine, "_fsync_directory", interrupt_after_first_replace)

    with pytest.raises(KeyboardInterrupt, match="simulated process interruption"):
        engine.apply(proposal["id"])

    # The process stopped after the rename: do not pretend the proposal completed
    # or make it eligible for an automatic retry.
    assert source.read_text(encoding="utf-8") == "def value():\\n    return 2\\n"
    assert store.get_proposal(proposal["id"])["status"] == "applying"

    restarted_store = TaskStore(store.db_path)
    assert restarted_store.mark_interrupted_proposals_for_review() == 1
    recovered = restarted_store.get_proposal(proposal["id"])
    assert recovered["status"] == "applying"
    assert "Process restarted while applying" in recovered["error_text"]
    with pytest.raises(ValueError, match="Invalid proposal status transition"):
        restarted_store.set_proposal_status(proposal["id"], "approved")


def test_interrupted_apply_is_flagged_for_manual_review_after_restart(tmp_path: Path):
    store = TaskStore(tmp_path / "crows.sqlite3")
    proposal = store.create_proposal("interrupted apply", [])
    store.set_proposal_status(proposal["id"], "approved")
    assert store.claim_proposal_for_apply(proposal["id"]) is True

    # A fresh store simulates process restart against the same database.
    restarted_store = TaskStore(tmp_path / "crows.sqlite3")
    count = restarted_store.mark_interrupted_proposals_for_review()

    saved = restarted_store.get_proposal(proposal["id"])
    assert count == 1
    assert saved["status"] == "applying"
    assert "Process restarted while applying" in saved["error_text"]
    # Recovery must never silently re-approve uncertain filesystem changes.
    with pytest.raises(ValueError, match="Invalid proposal status transition"):
        restarted_store.set_proposal_status(proposal["id"], "approved")


@pytest.mark.parametrize(
    "relative",
    [
        ".env",
        ".env.local",
        ".ENV.production",
        ".ssh/id_ed25519",
        ".aws/credentials",
        ".azure/accessTokens.json",
        ".gnupg/private-keys-v1.d/key",
        ".netrc",
        ".npmrc",
        "credentials.json",
        "service-secret.txt",
        "private.pem",
        "signing.key",
        ".envrc",
        "id_ecdsa",
        "id_dsa",
        "application_default_credentials.json",
        "access_token.json",
        "refresh_token",
        "api_key.txt",
        "service_private_key.json",
        "config/secrets/database.yml",
        "config/credentials/production.yml",
    ],
)
def test_coding_source_reader_rejects_sensitive_files_before_reading(
    tmp_path: Path, relative: str
):
    engine, _, workspace, _ = make_engine(tmp_path, "{}")
    secret = workspace / relative
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("DO_NOT_SEND_THIS_SECRET", encoding="utf-8")

    with pytest.raises(CodingError, match="Sensitive file cannot be sent"):
        engine.read_sources([relative])


def test_replace_in_workspace_uses_directory_relative_rename(tmp_path: Path):
    engine, _store, workspace, _source = make_engine(tmp_path, "{}")
    target_dir = workspace / "nested"
    target_dir.mkdir()
    target = target_dir / "sample.py"
    target.write_text("old", encoding="utf-8")
    staged = target_dir / ".stage"
    staged.write_text("new", encoding="utf-8")

    engine._replace_in_workspace("nested/sample.py", staged)

    assert target.read_text(encoding="utf-8") == "new"
    assert not staged.exists()


def test_replace_in_workspace_rejects_symlink_parent(tmp_path: Path):
    engine, _store, workspace, _source = make_engine(tmp_path, "{}")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sample.py").write_text("outside", encoding="utf-8")
    (workspace / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises((OSError, CodingError)):
        engine._replace_in_workspace("linked/sample.py", outside / "sample.py")

    assert (outside / "sample.py").read_text(encoding="utf-8") == "outside"


@pytest.mark.asyncio
async def test_new_file_proposal_is_pending_and_does_not_create_file(tmp_path: Path):
    answer = (
        '{"summary":"add helper module","changes":['
        '{"path":"helper.py","content":"def answer():\\n    return 42\\n"}]}'
    )
    engine, store, workspace, _ = make_engine(tmp_path, answer)
    proposal = await engine.propose("Add a helper module", [], new_paths=["helper.py"])

    assert proposal["status"] == "pending"
    assert proposal["changes"][0]["original_exists"] is False
    assert "+++ b/helper.py" in proposal["changes"][0]["diff"]
    assert not (workspace / "helper.py").exists()


@pytest.mark.asyncio
async def test_new_file_proposal_rejects_existing_or_sensitive_targets(tmp_path: Path):
    engine, store, workspace, _ = make_engine(
        tmp_path,
        '{"summary":"bad","changes":[{"path":"helper.py","content":"VALUE = 1\\n"}]}',
    )
    (workspace / "helper.py").write_text("existing\\n", encoding="utf-8")
    with pytest.raises(CodingError, match="already exists"):
        await engine.propose("Add helper", [], new_paths=["helper.py"])
    with pytest.raises(CodingError, match="Sensitive file"):
        await engine.propose("Add environment file", [], new_paths=[".env"])
    with pytest.raises(CodingError, match="Parent directory"):
        await engine.propose("Add nested helper", [], new_paths=["missing/helper.py"])


def test_approved_new_file_is_created_without_overwriting(tmp_path: Path):
    engine, store, workspace, _ = make_engine(tmp_path, "{}")
    content = "def answer():\n    return 42\n"
    proposal = store.create_proposal("Add helper", [{
        "path": "helper.py",
        "original_content": "",
        "original_sha256": __import__("hashlib").sha256(b"").hexdigest(),
        "original_exists": False,
        "content": content,
        "diff": "",
    }])
    engine.approve(proposal["id"])

    result = engine.apply(proposal["id"])

    assert result["status"] == "applied"
    assert result["created_files"] == ["helper.py"]
    assert (workspace / "helper.py").read_text(encoding="utf-8") == content


def test_new_file_apply_refuses_a_concurrent_existing_target(tmp_path: Path):
    engine, store, workspace, _ = make_engine(tmp_path, "{}")
    proposal = store.create_proposal("Add helper", [{
        "path": "helper.py",
        "original_content": "",
        "original_sha256": __import__("hashlib").sha256(b"").hexdigest(),
        "original_exists": False,
        "content": "VALUE = 1\\n",
        "diff": "",
    }])
    engine.approve(proposal["id"])
    target = workspace / "helper.py"
    target.write_text("external content\n", encoding="utf-8")

    with pytest.raises(CodingError, match="no longer available|appeared before apply"):
        engine.apply(proposal["id"])

    assert target.read_text(encoding="utf-8") == "external content\n"
    assert store.get_proposal(proposal["id"])["status"] == "approved"


def test_created_file_is_removed_if_later_change_fails(tmp_path: Path, monkeypatch):
    import hashlib
    import os

    engine, store, workspace, source = make_engine(tmp_path, "{}")
    original = source.read_text(encoding="utf-8")
    proposal = store.create_proposal("Create then fail", [
        {
            "path": "created.py", "original_content": "",
            "original_sha256": hashlib.sha256(b"").hexdigest(),
            "original_exists": False, "content": "VALUE = 1\\n", "diff": "",
        },
        {
            "path": "sample.py", "original_content": original,
            "original_sha256": hashlib.sha256(original.encode()).hexdigest(),
            "content": "def value():\\n    return 2\\n", "diff": "",
        },
    ])
    engine.approve(proposal["id"])
    replace_name = "replace" if os.name == "nt" else "rename"
    real_rename = getattr(os, replace_name)
    calls = 0

    def fail_second_replace(src, dst, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated replacement failure")
        return real_rename(src, dst, **kwargs)

    monkeypatch.setattr("crows.coding.os." + replace_name, fail_second_replace)

    with pytest.raises(CodingError, match="Apply failed"):
        engine.apply(proposal["id"])

    assert not (workspace / "created.py").exists()
    assert source.read_text(encoding="utf-8") == original
    assert store.get_proposal(proposal["id"])["status"] == "approved"



def test_concurrent_approve_and_reject_only_one_transition_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Approval and rejection racing on separate stores must have one winner."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    engine, store, workspace, _ = make_engine(tmp_path, "{}")
    second_store = TaskStore(tmp_path / "crows.sqlite3")
    second_engine = CodingEngine(workspace, second_store, FakeRouter("{}"))
    proposal = store.create_proposal("concurrent decision", [])
    barrier = Barrier(2)

    first_set_status = store.set_proposal_status
    second_set_status = second_store.set_proposal_status

    def synchronized_first(proposal_id, status, error=None):
        barrier.wait(timeout=5)
        return first_set_status(proposal_id, status, error=error)

    def synchronized_second(proposal_id, status, error=None):
        barrier.wait(timeout=5)
        return second_set_status(proposal_id, status, error=error)

    monkeypatch.setattr(store, "set_proposal_status", synchronized_first)
    monkeypatch.setattr(second_store, "set_proposal_status", synchronized_second)

    def decide(engine_instance, decision):
        try:
            if decision == "approve":
                return engine_instance.approve(proposal["id"])
            return engine_instance.reject(proposal["id"])
        except CodingError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda args: decide(*args),
            ((engine, "approve"), (second_engine, "reject")),
        ))

    successes = [result for result in results if isinstance(result, dict)]
    conflicts = [result for result in results if isinstance(result, CodingError)]

    assert len(successes) == 1
    assert len(conflicts) == 1
    assert successes[0]["status"] in {"approved", "rejected"}
    assert store.get_proposal(proposal["id"])["status"] == successes[0]["status"]
