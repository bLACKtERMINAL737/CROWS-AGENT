import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from crows.coding import CodingEngine, CodingError
from crows.store import TaskStore


class UnusedRouter:
    async def chat(self, message, task_type="general"):
        raise AssertionError("These tests must not call the model router")


def make_proposal(tmp_path: Path, status: str = "pending"):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "sample.py"
    original = "VALUE = 1\n"
    source.write_text(original, encoding="utf-8")
    store = TaskStore(tmp_path / "crows.sqlite3")
    engine = CodingEngine(workspace, store, UnusedRouter())
    proposal = store.create_proposal(
        "change value",
        [{
            "path": "sample.py",
            "original_content": original,
            "original_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
            "content": "VALUE = 2\n",
            "diff": "",
        }],
    )
    if status != "pending":
        store.set_proposal_status(proposal["id"], status)
    return engine, store, source, proposal


@pytest.mark.parametrize("status", ["pending", "rejected"])
def test_apply_never_changes_files_without_explicit_approval(
    tmp_path: Path, status: str
):
    engine, store, source, proposal = make_proposal(tmp_path, status=status)
    before = source.read_text(encoding="utf-8")

    with pytest.raises(CodingError, match="explicitly approved"):
        engine.apply(proposal["id"])

    assert source.read_text(encoding="utf-8") == before
    assert store.get_proposal(proposal["id"])["status"] == status


def test_applied_proposal_cannot_be_applied_a_second_time(tmp_path: Path):
    engine, store, source, proposal = make_proposal(tmp_path)
    engine.approve(proposal["id"])

    first_result = engine.apply(proposal["id"])
    assert first_result["status"] == "applied"
    applied_content = source.read_text(encoding="utf-8")
    assert applied_content == "VALUE = 2\n"

    with pytest.raises(CodingError, match="explicitly approved"):
        engine.apply(proposal["id"])

    assert source.read_text(encoding="utf-8") == applied_content
    assert store.get_proposal(proposal["id"])["status"] == "applied"



def test_rejecting_non_pending_proposal_is_a_conflict(monkeypatch):
    from fastapi import HTTPException
    import crows.app as app_module

    class AlreadyApprovedCoding:
        def reject(self, proposal_id):
            raise CodingError("only pending proposals can be rejected")

    monkeypatch.setattr(app_module, "coding", AlreadyApprovedCoding())

    with pytest.raises(HTTPException) as exc_info:
        app_module.reject_coding_proposal("proposal-123")

    assert exc_info.value.status_code == 409


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_status_transition_race_is_reported_as_coding_conflict(
    tmp_path: Path, monkeypatch, action: str
):
    engine, store, _source, proposal = make_proposal(tmp_path)

    def competing_transition(proposal_id: str, status: str, error=None):
        # Simulate another request winning the atomic DB transition after
        # this request's initial pending-status check.
        raise ValueError(
            f"Invalid proposal status transition: approved -> {status}"
        )

    monkeypatch.setattr(store, "set_proposal_status", competing_transition)

    with pytest.raises(CodingError, match="Invalid proposal status transition"):
        getattr(engine, action)(proposal["id"])

    assert store.get_proposal(proposal["id"])["status"] == "pending"



def test_concurrent_approve_and_reject_only_allow_one_transition(
    tmp_path: Path, monkeypatch
):
    engine, store, _source, proposal = make_proposal(tmp_path)
    original_get_proposal = store.get_proposal
    both_read_pending = threading.Barrier(2)
    thread_state = threading.local()

    def synchronized_get_proposal(proposal_id: str):
        result = original_get_proposal(proposal_id)
        # Ensure both operations pass their initial status check before either
        # attempts the atomic database transition.
        if not getattr(thread_state, "initial_read_done", False):
            thread_state.initial_read_done = True
            if result is not None and result["status"] == "pending":
                both_read_pending.wait(timeout=5)
        return result

    monkeypatch.setattr(store, "get_proposal", synchronized_get_proposal)

    def decide(action):
        try:
            action(proposal["id"])
            return "success"
        except CodingError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        approve_future = executor.submit(decide, engine.approve)
        reject_future = executor.submit(decide, engine.reject)
        outcomes = [
            approve_future.result(timeout=10),
            reject_future.result(timeout=10),
        ]

    assert outcomes.count("success") == 1
    assert outcomes.count("conflict") == 1
    assert store.get_proposal(proposal["id"])["status"] in {"approved", "rejected"}


def test_apply_rechecks_each_file_before_replacement_and_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import crows.coding as coding_module

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = workspace / "first.py"
    second = workspace / "second.py"
    first.write_text("FIRST = 1\\n", encoding="utf-8")
    second.write_text("SECOND = 1\\n", encoding="utf-8")
    store = TaskStore(tmp_path / "crows.sqlite3")
    engine = CodingEngine(workspace, store, UnusedRouter())
    proposal = store.create_proposal(
        "update two values",
        [
            {
                "path": "first.py",
                "original_content": "FIRST = 1\\n",
                "original_sha256": hashlib.sha256(b"FIRST = 1\\n").hexdigest(),
                "content": "FIRST = 2\\n",
                "diff": "",
            },
            {
                "path": "second.py",
                "original_content": "SECOND = 1\\n",
                "original_sha256": hashlib.sha256(b"SECOND = 1\\n").hexdigest(),
                "content": "SECOND = 2\\n",
                "diff": "",
            },
        ],
    )
    engine.approve(proposal["id"])

    replace_name = "replace" if coding_module.os.name == "nt" else "rename"
    real_rename = getattr(coding_module.os, replace_name)
    changed_second = False

    def rename_with_concurrent_edit(source, destination, **kwargs):
        nonlocal changed_second
        if Path(destination).name == first.name and not changed_second:
            result = real_rename(source, destination, **kwargs)
            second.write_text("SECOND = 99\\n", encoding="utf-8")
            changed_second = True
            return result
        return real_rename(source, destination, **kwargs)

    monkeypatch.setattr(coding_module.os, replace_name, rename_with_concurrent_edit)

    with pytest.raises(CodingError, match="File changed while proposal was being applied"):
        engine.apply(proposal["id"])

    assert first.read_text(encoding="utf-8") == "FIRST = 1\\n"
    assert second.read_text(encoding="utf-8") == "SECOND = 99\\n"
    assert store.get_proposal(proposal["id"])["status"] == "approved"


@pytest.mark.asyncio
async def test_coding_proposal_api_returns_pending_proposal_without_applying(
    monkeypatch: pytest.MonkeyPatch,
):
    from httpx import ASGITransport, AsyncClient
    import crows.app as app_module

    class FakeCoding:
        async def propose(self, instruction, paths, new_paths=None):
            assert instruction == "Change behavior"
            assert paths == ["sample.py"]
            return {"id": "proposal-api-1", "status": "pending", "changes": []}

    monkeypatch.setenv("AETHER_API_TOKEN", "test-token")
    monkeypatch.setattr(app_module, "coding", FakeCoding())
    async with AsyncClient(
        transport=ASGITransport(app=app_module.app),
        base_url="http://127.0.0.1:8000",
        headers={"X-Aether-Token": "test-token"},
    ) as client:
        response = await client.post(
            "/coding/proposals",
            json={"instruction": "Change behavior", "paths": ["sample.py"]},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "pending"


@pytest.mark.asyncio
async def test_coding_proposal_api_maps_validation_error_to_client_error(
    monkeypatch: pytest.MonkeyPatch,
):
    from httpx import ASGITransport, AsyncClient
    import crows.app as app_module

    class InvalidCoding:
        async def propose(self, instruction, paths, new_paths=None):
            raise CodingError("Select between 1 and 8 files")

    monkeypatch.setenv("AETHER_API_TOKEN", "test-token")
    monkeypatch.setattr(app_module, "coding", InvalidCoding())
    async with AsyncClient(
        transport=ASGITransport(app=app_module.app),
        base_url="http://127.0.0.1:8000",
        headers={"X-Aether-Token": "test-token"},
    ) as client:
        response = await client.post(
            "/coding/proposals",
            json={"instruction": "Change behavior", "paths": ["sample.py"]},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "Select between 1 and 8 files"


@pytest.mark.asyncio
async def test_coding_apply_api_maps_missing_proposal_to_not_found(
    monkeypatch: pytest.MonkeyPatch,
):
    from httpx import ASGITransport, AsyncClient
    import crows.app as app_module

    class MissingProposalCoding:
        def apply(self, proposal_id):
            raise KeyError("Proposal not found")

    monkeypatch.setenv("AETHER_API_TOKEN", "test-token")
    monkeypatch.setattr(app_module, "coding", MissingProposalCoding())
    async with AsyncClient(
        transport=ASGITransport(app=app_module.app),
        base_url="http://127.0.0.1:8000",
        headers={"X-Aether-Token": "test-token"},
    ) as client:
        response = await client.post("/coding/proposals/missing/apply")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_coding_apply_api_maps_approval_conflict_to_conflict_status(
    monkeypatch: pytest.MonkeyPatch,
):
    from httpx import ASGITransport, AsyncClient
    import crows.app as app_module

    class UnapprovedCoding:
        def apply(self, proposal_id):
            raise CodingError("Proposal must be explicitly approved before applying")

    monkeypatch.setenv("AETHER_API_TOKEN", "test-token")
    monkeypatch.setattr(app_module, "coding", UnapprovedCoding())
    async with AsyncClient(
        transport=ASGITransport(app=app_module.app),
        base_url="http://127.0.0.1:8000",
        headers={"X-Aether-Token": "test-token"},
    ) as client:
        response = await client.post("/coding/proposals/proposal-api-2/apply")

    assert response.status_code == 409
    assert "explicitly approved" in response.json()["detail"]


@pytest.mark.asyncio
async def test_coding_apply_api_does_not_leak_unexpected_exception_details(
    monkeypatch: pytest.MonkeyPatch,
):
    from httpx import ASGITransport, AsyncClient
    import crows.app as app_module

    class BrokenCoding:
        def apply(self, proposal_id):
            raise OSError("secret-path /Users/private/.env")

    monkeypatch.setenv("AETHER_API_TOKEN", "test-token")
    monkeypatch.setattr(app_module, "coding", BrokenCoding())
    async with AsyncClient(
        transport=ASGITransport(app=app_module.app),
        base_url="http://127.0.0.1:8000",
        headers={"X-Aether-Token": "test-token"},
    ) as client:
        response = await client.post("/coding/proposals/proposal-api-3/apply")

    assert response.status_code == 502
    assert "secret-path" not in response.text
    assert "/Users/private" not in response.text
