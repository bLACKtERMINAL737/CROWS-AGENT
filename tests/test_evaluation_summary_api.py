from fastapi.testclient import TestClient

from crows import app as app_module


def test_aggregate_evaluation_endpoint_is_bounded_and_content_free(tmp_path, monkeypatch):
    from crows.store import TaskStore

    store = TaskStore(tmp_path / "evaluation-summary.sqlite3")
    workflow = store.create_workflow(
        "Private aggregate goal",
        [{"title": "Private task", "input": "Private task prompt", "role": "research"}],
    )
    store.update_workflow(
        workflow["id"],
        "succeeded",
        results={
            "tasks": [{"task_id": "private-id", "status": "succeeded", "tool_calls": 1, "tool_attempts": 2}],
            "qa_review": {"status": "succeeded", "result": '{"verdict":"PASS"}'},
        },
    )
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.delenv("CROWS_API_TOKEN", raising=False)
    monkeypatch.setenv("AETHER_API_TOKEN", "summary-token")
    monkeypatch.setattr(app_module.app, "middleware_stack", None)

    with TestClient(
        app_module.app,
        base_url="http://127.0.0.1",
        headers={
            "Origin": "http://127.0.0.1",
            "Host": "127.0.0.1",
            "X-Aether-Token": "summary-token",
        },
        client=("127.0.0.1", 12346),
    ) as client:
        response = client.get("/agents/evaluations?limit=10")
        invalid = client.get("/agents/evaluations?limit=201")

    assert response.status_code == 200
    assert response.json()["summary"]["sampled_workflows"] == 1
    assert response.json()["summary"]["pass_rate"] == 1.0
    assert response.json()["summary"]["tool_calls"] == 1
    assert "Private aggregate goal" not in response.text
    assert "Private task prompt" not in response.text
    assert "private-id" not in response.text
    assert invalid.status_code == 400
