import os
from fastapi.testclient import TestClient

from crows import app as app_module


def test_database_health_returns_ok_when_database_query_succeeds(monkeypatch):
    monkeypatch.setenv("AETHER_API_TOKEN", "test-token")
    monkeypatch.setattr(app_module.store, "list_tasks", lambda limit: [])

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
        response = client.get("/health/db")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "reachable"}


def test_database_health_returns_generic_error_when_database_query_fails(monkeypatch):
    def fail_query(limit):
        raise RuntimeError("sensitive database details")

    monkeypatch.setenv("AETHER_API_TOKEN", "test-token")
    monkeypatch.setattr(app_module.store, "list_tasks", fail_query)

    with TestClient(
        app_module.app,
        base_url="http://127.0.0.1",
        headers={
            "Origin": "http://127.0.0.1",
            "Host": "127.0.0.1",
            "X-Aether-Token": "test-token",
        },
        client=("127.0.0.1", 12345),
        raise_server_exceptions=False,
    ) as client:
        response = client.get("/health/db")

    assert response.status_code == 500
    assert response.json() == {"detail": "Database is not reachable."}
    assert "sensitive database details" not in response.text
