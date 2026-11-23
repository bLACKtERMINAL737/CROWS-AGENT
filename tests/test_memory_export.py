from crows import app as app_module


def test_memory_export_returns_versioned_portable_document(monkeypatch):
    memories = [
        {
            "id": "memory-1",
            "category": "preference",
            "content": "Prefer concise explanations",
            "source": "user",
            "tags": ["style"],
            "created_at": "2026-10-24T00:00:00+00:00",
            "updated_at": "2026-10-24T00:00:00+00:00",
        }
    ]
    monkeypatch.setattr(app_module.store, "list_memories", lambda limit: memories)

    result = app_module.export_memories()

    assert result == {
        "format": "crows-memory-export",
        "version": 1,
        "memories": memories,
    }


def test_memory_export_route_is_read_only_get():
    matching_routes = [
        route for route in app_module.app.routes
        if getattr(route, "path", None) == "/memory/export"
    ]

    assert len(matching_routes) == 1
    assert matching_routes[0].methods == {"GET"}



def test_memory_export_rejects_aggregate_content_over_budget(monkeypatch):
    from fastapi import HTTPException
    import pytest

    memories = [
        {"id": f"large-memory-{index}", "content": "x" * 4_000}
        for index in range(4_001)
    ]
    monkeypatch.setattr(app_module.store, "list_memories", lambda limit: memories)

    with pytest.raises(HTTPException) as exc:
        app_module.export_memories()

    assert exc.value.status_code == 413
