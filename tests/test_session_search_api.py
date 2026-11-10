import pytest
from fastapi import HTTPException

from crows import app as app_module
from crows.store import TaskStore


def test_session_search_endpoint_returns_cross_session_matches(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "session-search-api.sqlite3")
    session = store.create_session("Project notes")
    store.add_session_message(session["id"], "user", "Remember the Lobster workflow pipeline")
    monkeypatch.setattr(app_module, "store", store)

    result = app_module.search_sessions(
        app_module.SessionSearchRequest(query="lobster workflow", limit=5)
    )

    assert len(result["messages"]) == 1
    assert result["messages"][0]["session_title"] == "Project notes"


def test_session_search_endpoint_rejects_invalid_limits():
    with pytest.raises(Exception):
        app_module.SessionSearchRequest(query="hello", limit=0)
