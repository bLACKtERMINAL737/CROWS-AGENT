from fastapi.testclient import TestClient

import crows.app as app_module
from crows.skills import SkillRegistry


TOOLS = {"workspace.list_files", "workspace.read_file", "workspace.search_text", "web.search", "web.open"}


def setup_client(monkeypatch, tmp_path):
    monkeypatch.setenv("CROWS_API_TOKEN", "skills-test-token")
    monkeypatch.setattr(app_module, "skill_registry", SkillRegistry(tmp_path / "skills", TOOLS))
    # The shared FastAPI app may retain middleware initialized by another test.
    # Rebuild it so LocalAPIProtection reads this test's patched token.
    monkeypatch.setattr(app_module.app, "middleware_stack", None)
    return TestClient(app_module.app, raise_server_exceptions=False)


def headers():
    return {"X-Crows-Token": "skills-test-token"}


def payload(**updates):
    value = {
        "id": "repo-review",
        "name": "Repository review",
        "version": "1.0.0",
        "description": "Inspect repository files without modifying them.",
        "tools": ["workspace.list_files", "workspace.read_file"],
        "permissions": ["workspace.read"],
    }
    value.update(updates)
    return value


def test_skill_api_registers_lists_and_requires_explicit_activation(monkeypatch, tmp_path):
    client = setup_client(monkeypatch, tmp_path)
    created = client.post("/skills", json=payload(), headers=headers())
    assert created.status_code == 201
    assert created.json()["enabled"] is False

    listed = client.get("/skills", headers=headers())
    assert listed.status_code == 200
    assert listed.json()["skills"][0]["id"] == "repo-review"

    denied = client.post(
        "/skills/repo-review/activate",
        json={"version": "1.0.0", "approved": False},
        headers=headers(),
    )
    assert denied.status_code == 409

    activated = client.post(
        "/skills/repo-review/activate",
        json={"version": "1.0.0", "approved": True},
        headers=headers(),
    )
    assert activated.status_code == 200
    assert activated.json()["enabled"] is True


def test_skill_api_rejects_unregistered_tools_and_supports_disable(monkeypatch, tmp_path):
    client = setup_client(monkeypatch, tmp_path)
    rejected = client.post(
        "/skills",
        json=payload(tools=["shell.run"], permissions=["workspace.read"]),
        headers=headers(),
    )
    assert rejected.status_code == 422

    client.post("/skills", json=payload(), headers=headers())
    client.post(
        "/skills/repo-review/activate",
        json={"version": "1.0.0", "approved": True},
        headers=headers(),
    )
    disabled = client.post("/skills/repo-review/disable", headers=headers())
    assert disabled.status_code == 200
    assert disabled.json() == {"id": "repo-review", "enabled": False}


def test_skill_api_requires_authentication(monkeypatch, tmp_path):
    client = setup_client(monkeypatch, tmp_path)
    assert client.get("/skills").status_code == 401
