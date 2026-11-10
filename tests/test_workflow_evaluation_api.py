from fastapi.testclient import TestClient

from crows import app as app_module


def test_workflow_evaluation_endpoint_returns_safe_deterministic_report(tmp_path, monkeypatch):
    from crows.store import TaskStore

    store = TaskStore(tmp_path / "evaluation.sqlite3")
    workflow = store.create_workflow(
        "Private workflow goal",
        [{"title": "Private task title", "input": "Private task input", "role": "research"}],
    )
    store.update_workflow(
        workflow["id"],
        "succeeded",
        results={
            "tasks": [{
                "task_id": "private-task-id",
                "title": "Private task title",
                "status": "succeeded",
                "tool_calls": 1,
                "tool_attempts": 2,
            }],
            "qa_review": {
                "status": "succeeded",
                "result": '{"verdict":"PASS","summary":"ok","findings":[]}',
            },
        },
    )
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.delenv("CROWS_API_TOKEN", raising=False)
    monkeypatch.setenv("AETHER_API_TOKEN", "evaluation-token")
    # Middleware captures its token when Starlette first builds the app stack.
    # Rebuild it so this test does not inherit a stack created by another test.
    monkeypatch.setattr(app_module.app, "middleware_stack", None)

    with TestClient(
        app_module.app,
        base_url="http://127.0.0.1",
        headers={
            "Origin": "http://127.0.0.1",
            "Host": "127.0.0.1",
            "X-Aether-Token": "evaluation-token",
        },
        client=("127.0.0.1", 12345),
    ) as client:
        response = client.get(f"/agents/workflows/{workflow['id']}/evaluation")
        missing = client.get("/agents/workflows/missing/evaluation")

    assert response.status_code == 200
    payload = response.json()
    assert payload["evaluation"]["outcome"] == "passed"
    assert payload["evaluation"]["qa_verdict"] == "PASS"
    assert payload["evaluation"]["tool_calls"] == 1
    assert payload["evaluation"]["tool_attempts"] == 2
    assert "Private workflow goal" not in response.text
    assert "Private task title" not in response.text
    assert "private-task-id" not in response.text
    assert missing.status_code == 404
