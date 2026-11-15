import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from crows import app as app_module
from crows.coding import CodingEngine
from crows.security import LocalAPIProtection
from crows.store import TaskStore


API_TOKEN = "crows-coding-iterate-token"


class IterationRouter:
    def __init__(self):
        self.prompts = []

    async def chat(self, message, task_type="general", **kwargs):
        self.prompts.append(message)
        return {
            "answer": json.dumps({
                "summary": "Return the corrected value",
                "changes": [{
                    "path": "sample.py",
                    "content": "def value():\\n    return 7\\n",
                }],
            }),
            "route": "coding",
            "model": "fixture-coding-model",
            "fallback_used": False,
            "attempts": 1,
        }


@pytest.mark.asyncio
async def test_iterative_coding_api_returns_reviewable_proposal_after_test_pass(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "sample.py"
    original = "def value():\\n    return 1\\n"
    source.write_text(original, encoding="utf-8")

    store = TaskStore(tmp_path / "coding-iterate.sqlite3")
    model_router = IterationRouter()
    coding = CodingEngine(
        workspace, store, model_router, enable_test_runner=True
    )
    test_runs = []

    def deterministic_test_run():
        test_runs.append(True)
        return {
            "passed": True,
            "returncode": 0,
            "stdout": "2 passed in 0.08s",
            "stderr": "",
        }

    monkeypatch.setattr(coding, "run_tests", deterministic_test_run)
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "coding", coding)
    monkeypatch.setenv("CROWS_API_TOKEN", API_TOKEN)

    isolated_app = FastAPI()
    isolated_app.router.routes = list(app_module.app.router.routes)
    isolated_app.add_middleware(LocalAPIProtection)

    async with AsyncClient(
        transport=ASGITransport(app=isolated_app),
        base_url="http://127.0.0.1:8000",
        headers={"X-Crows-Token": API_TOKEN},
    ) as client:
        response = await client.post(
            "/coding/iterate",
            json={
                "instruction": "Make value() return 7",
                "paths": ["sample.py"],
                "max_rounds": 1,
            },
        )

    assert response.status_code == 200
    outcome = response.json()
    assert outcome["passed"] is True
    assert outcome["proposal"]["status"] == "pending"
    assert outcome["rounds"][0]["test_result"]["passed"] is True
    assert len(test_runs) == 1
    assert model_router.prompts
    assert model_router.prompts[0].find("Make value() return 7") >= 0

    # Iteration creates a reviewable proposal; it never applies the patch.
    assert source.read_text(encoding="utf-8") == original
    saved = store.get_proposal(outcome["proposal"]["id"])
    assert saved["status"] == "pending"
    assert "return 7" in saved["changes"][0]["content"]
