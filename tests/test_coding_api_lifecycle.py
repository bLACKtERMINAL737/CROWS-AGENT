import json

import pytest
from httpx import ASGITransport, AsyncClient

from crows import app as app_module
from crows.coding import CodingEngine
from crows.store import TaskStore


class ProposalRouter:
    def __init__(self):
        self.calls = []

    async def chat(self, message, task_type="general", **kwargs):
        self.calls.append((message, task_type))
        return {
            "answer": json.dumps({
                "summary": "Update the sample value",
                "changes": [{
                    "path": "sample.py",
                    "content": "def value():\n    return 2\n",
                }],
            }),
            "route": "coding",
            "model": "fixture-model",
            "fallback_used": False,
            "attempts": 1,
        }


@pytest.mark.asyncio
async def test_coding_proposal_review_approval_and_apply_api_lifecycle(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "sample.py"
    original = "def value():\n    return 1\n"
    source.write_text(original, encoding="utf-8")

    store = TaskStore(tmp_path / "coding-api-lifecycle.sqlite3")
    model_router = ProposalRouter()
    coding = CodingEngine(workspace, store, model_router)
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "coding", coding)
    monkeypatch.setenv("CROWS_API_TOKEN", "coding-lifecycle-test-token")
    monkeypatch.setattr(app_module.app, "middleware_stack", None)

    async with AsyncClient(
        transport=ASGITransport(app=app_module.app),
        base_url="http://127.0.0.1:8000",
        headers={"X-Crows-Token": "coding-lifecycle-test-token"},
    ) as client:
        created = await client.post(
            "/coding/proposals",
            json={
                "instruction": "Change value() to return 2",
                "paths": ["sample.py"],
            },
        )
        assert created.status_code == 200
        proposal = created.json()
        proposal_id = proposal["id"]
        assert proposal["status"] == "pending"
        assert source.read_text(encoding="utf-8") == original

        reviewed = await client.get(f"/coding/proposals/{proposal_id}")
        assert reviewed.status_code == 200
        assert reviewed.json()["status"] == "pending"
        assert "return 2" in reviewed.json()["changes"][0]["diff"]
        assert source.read_text(encoding="utf-8") == original

        premature_apply = await client.post(
            f"/coding/proposals/{proposal_id}/apply"
        )
        assert premature_apply.status_code == 409
        assert source.read_text(encoding="utf-8") == original

        approved = await client.post(
            f"/coding/proposals/{proposal_id}/approve"
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"
        assert source.read_text(encoding="utf-8") == original

        applied = await client.post(
            f"/coding/proposals/{proposal_id}/apply"
        )
        assert applied.status_code == 200
        assert applied.json()["status"] == "applied"
        assert source.read_text(encoding="utf-8") == (
            "def value():\n    return 2\n"
        )

        duplicate_apply = await client.post(
            f"/coding/proposals/{proposal_id}/apply"
        )
        assert duplicate_apply.status_code == 409
        assert source.read_text(encoding="utf-8") == (
            "def value():\n    return 2\n"
        )

    saved = store.get_proposal(proposal_id)
    assert saved["status"] == "applied"
    assert model_router.calls
    assert model_router.calls[0][1] == "coding"
