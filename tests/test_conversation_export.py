from crows import app as app_module


def test_conversation_export_returns_versioned_archive(monkeypatch):
    sessions = [{"id": "session-1", "title": "Example", "created_at": "now", "updated_at": "now"}]
    session = {"id": "session-1", "title": "Example", "created_at": "now", "updated_at": "now"}
    messages = [{"id": "message-1", "session_id": "session-1", "role": "user", "content": "Hello"}]

    monkeypatch.setattr(app_module.store, "list_sessions", lambda limit: sessions)
    monkeypatch.setattr(app_module.store, "get_session", lambda session_id: session)
    monkeypatch.setattr(app_module.store, "list_session_messages", lambda session_id, limit: messages)

    result = app_module.export_sessions(limit=100)

    assert result == {
        "format": "aether-conversation-export",
        "version": 1,
        "sessions": [{**session, "messages": messages}],
    }


def test_conversation_export_rejects_invalid_limit():
    from fastapi import HTTPException

    try:
        app_module.export_sessions(limit=0)
    except HTTPException as exc:
        assert exc.status_code == 400
    else:
        raise AssertionError("Expected invalid export limit to be rejected")


def test_conversation_export_route_is_read_only_get():
    routes = [
        route for route in app_module.app.routes
        if getattr(route, "path", None) == "/sessions/export"
    ]
    assert len(routes) == 1
    assert routes[0].methods == {"GET"}
