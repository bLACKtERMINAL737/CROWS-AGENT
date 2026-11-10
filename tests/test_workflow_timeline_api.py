from fastapi.testclient import TestClient

from crows import app as app_module


def test_workflow_timeline_is_ordered_and_omits_private_content(tmp_path, monkeypatch):
    from crows.store import TaskStore

    store = TaskStore(tmp_path / "timeline.sqlite3")
    workflow = store.create_workflow(
        "Private workflow goal",
        [{"title": "Private task title", "input": "Private task input", "role": "research"}],
    )
    task = store.create_task("Private task title", "Private task input with sensitive details")
    store.set_task_status(task["id"], "running")
    execution_id = store.begin_tool(
        task["id"], "workspace.read_file", {"path": "private/secret.txt"}
    )
    store.finish_tool(
        execution_id, "succeeded", result={"content": "private raw result"}
    )
    store.set_task_status(task["id"], "succeeded", result={"answer": "private answer"})
    qa_task = store.create_task("Private QA title", "Private QA input")
    store.set_task_status(qa_task["id"], "running")
    store.set_task_status(qa_task["id"], "succeeded", result={"answer": "private QA answer"})
    store.update_workflow(
        workflow["id"],
        "succeeded",
        results={
            "tasks": [{
                "plan_index": 0,
                "task_id": task["id"],
                "title": "Private task title",
                "role": "research",
                "status": "succeeded",
            }],
            "qa_review": {
                "task_id": qa_task["id"],
                "status": "succeeded",
                "result": "private QA result",
                "verdict": "PASS",
            },
        },
    )
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.delenv("CROWS_API_TOKEN", raising=False)
    monkeypatch.setenv("AETHER_API_TOKEN", "timeline-token")
    monkeypatch.setattr(app_module.app, "middleware_stack", None)

    with TestClient(
        app_module.app,
        base_url="http://127.0.0.1",
        headers={
            "Origin": "http://127.0.0.1",
            "Host": "127.0.0.1",
            "X-Aether-Token": "timeline-token",
        },
        client=("127.0.0.1", 12345),
    ) as client:
        response = client.get(f"/agents/workflows/{workflow['id']}/timeline")
        missing = client.get("/agents/workflows/missing/timeline")

    assert response.status_code == 200
    payload = response.json()
    timeline = payload["timeline"]
    assert payload["status"] == "succeeded"
    assert any(item["event"] == "workflow.created" for item in timeline)
    assert any(item["event"] == "task.created" and item.get("phase") == "specialist" for item in timeline)
    assert any(item["event"] == "tool.started" and item["tool_name"] == "workspace.read_file" for item in timeline)
    assert any(item["event"] == "task.status" and item.get("phase") == "qa" for item in timeline)
    assert [item["at"] for item in timeline] == sorted(item["at"] for item in timeline)
    serialized = response.text
    for private_value in (
        "Private workflow goal", "Private task title", "Private task input",
        "private/secret.txt", "private raw result", "private answer",
        "private QA input", "private QA result", task["id"], qa_task["id"], execution_id,
    ):
        assert private_value not in serialized
    assert missing.status_code == 404
