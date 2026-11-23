from crows import app as app_module


def test_conversation_export_returns_versioned_archive(monkeypatch):
    sessions = [{"id": "session-1", "title": "Example", "created_at": "now", "updated_at": "now"}]
    session = {"id": "session-1", "title": "Example", "created_at": "now", "updated_at": "now"}
    messages = [{"id": "message-1", "session_id": "session-1", "role": "user", "content": "Hello"}]

    monkeypatch.setattr(app_module.store, "list_sessions", lambda limit, offset=0: sessions)
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



def test_conversation_export_passes_session_offset(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        app_module.store,
        "list_sessions",
        lambda limit, offset=0: captured.update(limit=limit, offset=offset) or [],
    )

    result = app_module.export_sessions(limit=25, offset=50)

    assert captured == {"limit": 25, "offset": 50}
    assert result["sessions"] == []


def test_conversation_export_rejects_session_with_more_than_10000_messages(monkeypatch):
    from fastapi import HTTPException
    import pytest

    session = {"id": "large-session"}
    monkeypatch.setattr(app_module.store, "list_sessions", lambda limit, offset=0: [session])
    monkeypatch.setattr(app_module.store, "get_session", lambda session_id: dict(session))
    monkeypatch.setattr(
        app_module.store,
        "list_session_messages",
        lambda session_id, limit: [{"id": str(index)} for index in range(10_001)],
    )

    with pytest.raises(HTTPException) as exc:
        app_module.export_sessions(limit=1)

    assert exc.value.status_code == 413


def test_conversation_export_rejects_archive_over_50000_messages(monkeypatch):
    from fastapi import HTTPException
    import pytest

    sessions = [{"id": f"session-{index}"} for index in range(6)]
    messages = [{"id": str(index)} for index in range(10_000)]
    monkeypatch.setattr(app_module.store, "list_sessions", lambda limit, offset=0: sessions)
    monkeypatch.setattr(
        app_module.store,
        "get_session",
        lambda session_id: {"id": session_id},
    )
    monkeypatch.setattr(
        app_module.store,
        "list_session_messages",
        lambda session_id, limit: messages,
    )

    with pytest.raises(HTTPException) as exc:
        app_module.export_sessions(limit=6)

    assert exc.value.status_code == 413



def test_conversation_export_rejects_invalid_offset():
    from fastapi import HTTPException
    import pytest

    with pytest.raises(HTTPException) as exc:
        app_module.export_sessions(offset=-1)

    assert exc.value.status_code == 400
