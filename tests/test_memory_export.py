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
        "format": "aether-memory-export",
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
