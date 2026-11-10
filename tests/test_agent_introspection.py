from types import SimpleNamespace

from crows import app as app_module


def test_runtime_context_reports_live_capabilities_without_secrets(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = "sk-or-v1-do-not-return-this"

    class FakeStore:
        def list_tasks(self, limit):
            assert limit == 5
            return [{
                "id": "task-123",
                "status": "succeeded",
                "created_at": "2026-11-01T10:00:00Z",
                "updated_at": "2026-11-01T10:01:00Z",
                "input": "private user input",
                "result": "private task result",
            }]

    class FakeSkills:
        def list(self):
            return [{
                "id": "workspace-reader",
                "name": "Workspace Reader",
                "version": "1.0.0",
                "enabled": True,
                "tools": ["workspace.read_file"],
                "api_key": secret,
            }]

    monkeypatch.setattr(app_module, "settings", SimpleNamespace(workspace=workspace))
    monkeypatch.setattr(app_module, "store", FakeStore())
    monkeypatch.setattr(app_module, "skill_registry", FakeSkills())
    monkeypatch.setattr(
        app_module,
        "router",
        SimpleNamespace(available_routes=lambda: [
            {"id": "primary", "model": "example/model", "configured": True},
        ]),
    )
    monkeypatch.setattr(
        app_module,
        "tools",
        SimpleNamespace(describe=lambda: [
            {"name": "workspace.read_file", "description": "Read a workspace file", "input_schema": {"type": "object"}},
            {"name": "web.search", "description": "Search the public web", "input_schema": {"type": "object"}},
        ]),
    )
    monkeypatch.setattr(
        app_module,
        "coding",
        SimpleNamespace(enable_test_runner=True, sandbox_image="crows-sandbox:test"),
    )
    monkeypatch.setattr(
        app_module,
        "get_env",
        lambda name, default="": "configured-token" if name == "CROWS_API_TOKEN" else default,
    )

    result = app_module.build_agent_runtime_context(mode="auto", task_type="coding")

    assert result["project"] == "Crows"
    assert result["mode"] == "auto"
    assert result["task_type"] == "coding"
    assert result["workspace"] == str(workspace.resolve())
    assert result["model_routes"] == [
        {"id": "primary", "model": "example/model", "configured": True},
    ]
    assert result["active_model_route"] == {
        "id": "primary", "model": "example/model", "configured": True,
    }
    assert [item["name"] for item in result["available_tools"]] == [
        "workspace.read_file", "web.search",
    ]
    assert result["skills"][0]["enabled"] is True
    assert result["recent_tasks"][0]["status"] == "succeeded"
    assert result["coding_test_runner_enabled"] is True
    assert result["api_token_required"] is True

    rendered = repr(result)
    assert secret not in rendered
    assert "configured-token" not in rendered
    assert "private user input" not in rendered
    assert "private task result" not in rendered


def test_runtime_context_degrades_safely_when_journals_are_unavailable(monkeypatch, tmp_path):
    class BrokenStore:
        def list_tasks(self, limit):
            raise OSError("database unavailable")

    class BrokenSkills:
        def list(self):
            raise OSError("registry unavailable")

    monkeypatch.setattr(app_module, "settings", SimpleNamespace(workspace=tmp_path))
    monkeypatch.setattr(app_module, "store", BrokenStore())
    monkeypatch.setattr(app_module, "skill_registry", BrokenSkills())
    monkeypatch.setattr(app_module, "router", SimpleNamespace(available_routes=lambda: []))
    monkeypatch.setattr(app_module, "tools", SimpleNamespace(describe=lambda: []))
    monkeypatch.setattr(
        app_module, "coding",
        SimpleNamespace(enable_test_runner=False, sandbox_image="crows-sandbox:latest"),
    )
    monkeypatch.setattr(app_module, "get_env", lambda name, default="": default)

    result = app_module.build_agent_runtime_context()

    assert result["skills"] == []
    assert result["recent_tasks"] == []
    assert result["model_routes"] == []
    assert result["active_model_route"]["configured"] is False
    assert result["available_tools"] == []


def test_agent_runtime_status_endpoint_returns_runtime_snapshot(monkeypatch):
    expected = {"project": "Crows", "mode": "auto", "model_routes": []}
    monkeypatch.setattr(
        app_module, "build_agent_runtime_context",
        lambda: expected,
    )

    assert app_module.agent_runtime_status() == expected



def test_runtime_context_supports_legacy_router_without_route_introspection(monkeypatch, tmp_path):
    class LegacyRouter:
        pass

    monkeypatch.setattr(app_module, "settings", SimpleNamespace(workspace=tmp_path))
    monkeypatch.setattr(app_module, "store", SimpleNamespace(list_tasks=lambda limit: []))
    monkeypatch.setattr(app_module, "skill_registry", SimpleNamespace(list=lambda: []))
    monkeypatch.setattr(app_module, "router", LegacyRouter())
    monkeypatch.setattr(
        app_module, "tools",
        SimpleNamespace(describe=lambda: [{"name": "workspace.list_files"}]),
    )
    monkeypatch.setattr(
        app_module, "coding",
        SimpleNamespace(enable_test_runner=False, sandbox_image="crows-sandbox:latest"),
    )
    monkeypatch.setattr(app_module, "get_env", lambda name, default="": default)

    result = app_module.build_agent_runtime_context()

    assert result["model_routes"] == []
    assert result["active_model_route"]["configured"] is False
    assert result["available_tools"] == [{
        "name": "workspace.list_files",
        "description": "",
        "input_schema": {},
    }]
