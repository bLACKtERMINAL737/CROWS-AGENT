from crows.store import TaskStore


def test_cross_session_search_finds_messages_with_session_metadata(tmp_path):
    store = TaskStore(tmp_path / "cross-session-search.sqlite3")
    first = store.create_session("Browser automation")
    second = store.create_session("Memory system")
    store.add_session_message(first["id"], "user", "Help me automate browser tasks")
    store.add_session_message(first["id"], "assistant", "We can build browser automation safely.")
    store.add_session_message(second["id"], "user", "How should persistent memory work?")
    store.add_session_message(second["id"], "assistant", "Persistent memory stores useful context.")

    results = store.search_session_messages("persistent memory", limit=10)

    assert results
    assert all("session_id" in item and "session_title" in item for item in results)
    assert results[0]["session_id"] == second["id"]
    assert results[0]["role"] == "assistant"
    assert "Persistent memory" in results[0]["content"]


def test_cross_session_search_is_case_insensitive_and_limited(tmp_path):
    store = TaskStore(tmp_path / "cross-session-search.sqlite3")
    session = store.create_session("Search")
    store.add_session_message(session["id"], "user", "Maximu Agent browser automation")
    store.add_session_message(session["id"], "assistant", "Browser automation is useful")
    store.add_session_message(session["id"], "user", "Unrelated note")

    results = store.search_session_messages("BROWSER", limit=1)

    assert len(results) == 1
    assert "browser" in results[0]["content"].casefold()


def test_cross_session_search_empty_query_returns_no_results(tmp_path):
    store = TaskStore(tmp_path / "cross-session-search.sqlite3")

    assert store.search_session_messages("   ") == []

 
def test_cross_session_search_casefolds_unicode_consistently(tmp_path):
    store = TaskStore(tmp_path / "unicode-cross-session-search.sqlite3")
    session = store.create_session("Unicode search")
    store.add_session_message(session["id"], "user", "The road passes through Straße.")

    results = store.search_session_messages("STRASSE", limit=10)

    assert len(results) == 1
    assert "Straße" in results[0]["content"]


def test_cross_session_search_handles_unicode_casefolding_in_non_latin_text(tmp_path):
    store = TaskStore(tmp_path / "unicode-cross-session-search.sqlite3")
    session = store.create_session("Unicode search")
    store.add_session_message(session["id"], "user", "İstanbul is a city.")

    results = store.search_session_messages("İSTANBUL", limit=10)

    assert len(results) == 1
    assert "İstanbul" in results[0]["content"]

 
def test_cross_session_search_rejects_non_positive_limits(tmp_path):
    store = TaskStore(tmp_path / "cross-session-search-limits.sqlite3")
    session = store.create_session("Search limits")
    store.add_session_message(session["id"], "user", "A searchable message")

    assert store.search_session_messages("searchable", limit=0) == []
    assert store.search_session_messages("searchable", limit=-1) == []
