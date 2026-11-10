from fastapi.testclient import TestClient

from crows import app as app_module


def test_agent_execution_metrics_api_returns_aggregate_contract(monkeypatch):
    expected = {
        "tasks": {
            "queued": 2,
            "running": 1,
            "succeeded": 3,
            "failed": 0,
            "unknown": 1,
        },
        "workflows": {
            "running": 1,
            "succeeded": 2,
            "partial": 0,
            "failed": 0,
            "unknown": 1,
        },
        "tool_executions": {
            "running": 0,
            "succeeded": 4,
            "failed": 1,
            "unknown": 0,
        },
        "oldest_queued_task_at": "2026-10-30T00:00:00+00:00",
        "oldest_unknown_workflow_updated_at": "2026-10-30T00:01:00+00:00",
    }
    monkeypatch.setenv("AETHER_API_TOKEN", "test-token")
    monkeypatch.setattr(
        app_module.store, "execution_metrics", lambda: expected
    )

    with TestClient(
        app_module.app,
        base_url="http://127.0.0.1",
        headers={
            "Origin": "http://127.0.0.1",
            "Host": "127.0.0.1",
            "X-Aether-Token": "test-token",
        },
        client=("127.0.0.1", 12345),
    ) as client:
        response = client.get("/agents/execution/metrics")

    assert response.status_code == 200
    assert response.json() == expected
    assert "task_id" not in response.text
    assert "workflow_id" not in response.text
