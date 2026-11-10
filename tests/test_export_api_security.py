from fastapi import FastAPI
from fastapi.testclient import TestClient

from crows import app as app_module
from crows.security import LocalAPIProtection
from crows.store import TaskStore


def make_client(monkeypatch, tmp_path):
    monkeypatch.setenv("CROWS_API_TOKEN", "coverage-audit-test-token")
    store = TaskStore(tmp_path / "exports.sqlite3")
    monkeypatch.setattr(app_module, "store", store)
    isolated_app = FastAPI()
    isolated_app.router.routes = list(app_module.app.router.routes)
    isolated_app.add_middleware(LocalAPIProtection)
    return TestClient(
        isolated_app,
        base_url="http://127.0.0.1:8000",
        raise_server_exceptions=False,
    ), store


def test_memory_and_conversation_exports_require_authentication(monkeypatch, tmp_path):
    client, store = make_client(monkeypatch, tmp_path)
    store.add_memory("note", "private memory marker")
    session = store.create_session("Private conversation")
    store.add_session_message(session["id"], "user", "private conversation marker")

    memory_response = client.get("/memory/export")
    conversation_response = client.get("/sessions/export")

    assert memory_response.status_code == 401
    assert conversation_response.status_code == 401
    assert "private memory marker" not in memory_response.text
    assert "private conversation marker" not in conversation_response.text


def test_authenticated_exports_return_persisted_data(monkeypatch, tmp_path):
    client, store = make_client(monkeypatch, tmp_path)
    store.add_memory("note", "private memory marker")
    session = store.create_session("Private conversation")
    store.add_session_message(session["id"], "user", "private conversation marker")
    headers = {"X-Crows-Token": "coverage-audit-test-token"}

    memory_response = client.get("/memory/export", headers=headers)
    conversation_response = client.get("/sessions/export", headers=headers)

    assert memory_response.status_code == 200
    assert memory_response.json()["format"] == "aether-memory-export"
    assert memory_response.json()["version"] == 1
    assert any(item["content"] == "private memory marker"
               for item in memory_response.json()["memories"])

    assert conversation_response.status_code == 200
    assert conversation_response.json()["format"] == "aether-conversation-export"
    assert conversation_response.json()["version"] == 1
    exported = conversation_response.json()["sessions"]
    assert len(exported) == 1
    assert exported[0]["id"] == session["id"]
    assert exported[0]["messages"][0]["content"] == "private conversation marker"


def test_authenticated_conversation_export_rejects_out_of_range_limit(monkeypatch, tmp_path):
    client, _store = make_client(monkeypatch, tmp_path)
    headers = {"X-Crows-Token": "coverage-audit-test-token"}

    response = client.get("/sessions/export?limit=0", headers=headers)

    assert response.status_code == 400
