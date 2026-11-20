import pytest

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

@pytest.mark.asyncio
async def test_session_search_http_returns_real_sqlite_pages_without_gaps(
    tmp_path, monkeypatch
):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from crows import app as app_module
    from crows.security import LocalAPIProtection

    api_token = "crows-session-search-http-token"
    store = TaskStore(tmp_path / "session-search-http.sqlite3")
    first_session = store.create_session("First conversation")
    second_session = store.create_session("Second conversation")
    for index in range(3):
        store.add_session_message(
            first_session["id"], "user", f"shared Crows marker first {index}"
        )
    for index in range(2):
        store.add_session_message(
            second_session["id"], "assistant", f"shared Crows marker second {index}"
        )

    isolated = FastAPI()
    isolated.router.routes = list(app_module.app.router.routes)
    isolated.add_middleware(LocalAPIProtection)
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setenv("CROWS_API_TOKEN", api_token)

    async with AsyncClient(
        transport=ASGITransport(app=isolated),
        base_url="http://127.0.0.1:8000",
        headers={"X-Crows-Token": api_token},
    ) as client:
        pages = []
        for offset in (0, 2, 4):
            response = await client.post(
                "/sessions/search",
                json={"query": "shared Crows marker", "limit": 2, "offset": offset},
            )
            assert response.status_code == 200
            pages.append(response.json())

    assert [len(page["messages"]) for page in pages] == [2, 2, 1]
    paged_ids = [
        message["id"]
        for page in pages
        for message in page["messages"]
    ]
    expected_ids = [
        message["id"]
        for message in store.search_session_messages(
            "shared Crows marker", limit=10
        )
    ]
    assert paged_ids == expected_ids
    assert [page["offset"] for page in pages] == [0, 2, 4]
    assert all(page["limit"] == 2 for page in pages)
    assert {message["session_id"] for page in pages for message in page["messages"]} == {
        first_session["id"], second_session["id"]
    }


@pytest.mark.asyncio
async def test_session_search_http_rejects_out_of_range_offset(
    tmp_path, monkeypatch
):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from crows import app as app_module
    from crows.security import LocalAPIProtection

    api_token = "crows-session-search-http-token"
    store = TaskStore(tmp_path / "session-search-http-invalid.sqlite3")
    isolated = FastAPI()
    isolated.router.routes = list(app_module.app.router.routes)
    isolated.add_middleware(LocalAPIProtection)
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setenv("CROWS_API_TOKEN", api_token)

    async with AsyncClient(
        transport=ASGITransport(app=isolated),
        base_url="http://127.0.0.1:8000",
        headers={"X-Crows-Token": api_token},
    ) as client:
        response = await client.post(
            "/sessions/search",
            json={"query": "Crows", "limit": 20, "offset": 100_001},
        )

    assert response.status_code == 422

