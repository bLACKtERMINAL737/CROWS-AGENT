from crows.store import TaskStore


def test_session_messages_persist_and_title_from_first_user_message(tmp_path):
    store = TaskStore(tmp_path / 'sessions.sqlite3')
    session = store.create_session()
    store.add_session_message(session['id'], 'user', 'Help me debug this code')
    store.add_session_message(session['id'], 'assistant', 'Sure, share the error.')
    reopened = TaskStore(tmp_path / 'sessions.sqlite3')
    saved = reopened.get_session(session['id'])
    assert saved['title'] == 'Help me debug this code'
    messages = reopened.list_session_messages(session['id'])
    assert [m['role'] for m in messages] == ['user', 'assistant']
    assert messages[1]['content'] == 'Sure, share the error.'


def test_session_delete_cascades_messages(tmp_path):
    store = TaskStore(tmp_path / 'sessions.sqlite3')
    session = store.create_session('Temporary')
    store.add_session_message(session['id'], 'user', 'Delete this conversation')
    assert store.delete_session(session['id'])
    assert store.get_session(session['id']) is None
    assert store.list_session_messages(session['id']) == []


def test_session_rejects_invalid_role(tmp_path):
    store = TaskStore(tmp_path / 'sessions.sqlite3')
    session = store.create_session()
    try:
        store.add_session_message(session['id'], 'tool', 'untrusted')
    except ValueError as exc:
        assert 'role' in str(exc).lower()
    else:
        raise AssertionError('Invalid message role should be rejected')


def test_session_search_request_rejects_whitespace_only_query():
    import pytest
    from pydantic import ValidationError
    from crows.app import SessionSearchRequest

    with pytest.raises(ValidationError, match="query must not be blank"):
        SessionSearchRequest(query="   \n\t")


def test_memory_search_request_rejects_whitespace_only_query():
    import pytest
    from pydantic import ValidationError
    from crows.app import MemorySearchRequest

    with pytest.raises(ValidationError, match="query must not be blank"):
        MemorySearchRequest(query="   \n\t")


def test_session_search_request_accepts_bounded_offset():
    from crows.app import SessionSearchRequest

    request = SessionSearchRequest(query="project Crows", limit=10, offset=20)

    assert request.offset == 20


def test_session_search_request_rejects_offset_above_maximum():
    import pytest
    from pydantic import ValidationError
    from crows.app import SessionSearchRequest

    with pytest.raises(ValidationError):
        SessionSearchRequest(query="project Crows", offset=100_001)

def test_session_search_endpoint_returns_requested_page_metadata(monkeypatch):
    import crows.app as app_module
    from crows.app import SessionSearchRequest

    class SearchStore:
        def search_session_messages(self, query, *, limit, offset):
            assert query == "Crows memory"
            assert limit == 2
            assert offset == 4
            return [
                {"id": "message-5", "session_id": "session-a", "content": "Crows memory result 5"},
                {"id": "message-6", "session_id": "session-b", "content": "Crows memory result 6"},
            ]

    monkeypatch.setattr(app_module, "store", SearchStore())
    response = app_module.search_sessions(
        SessionSearchRequest(query="Crows memory", limit=2, offset=4)
    )

    assert response == {
        "messages": [
            {"id": "message-5", "session_id": "session-a", "content": "Crows memory result 5"},
            {"id": "message-6", "session_id": "session-b", "content": "Crows memory result 6"},
        ],
        "limit": 2,
        "offset": 4,
    }


def test_session_search_endpoint_returns_empty_page_at_end(monkeypatch):
    import crows.app as app_module
    from crows.app import SessionSearchRequest

    class SearchStore:
        def search_session_messages(self, query, *, limit, offset):
            assert offset == 20
            return []

    monkeypatch.setattr(app_module, "store", SearchStore())
    response = app_module.search_sessions(
        SessionSearchRequest(query="Crows", limit=20, offset=20)
    )

    assert response == {"messages": [], "limit": 20, "offset": 20}

