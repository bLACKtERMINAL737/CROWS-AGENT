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


def test_cross_session_search_indexes_existing_messages_once(tmp_path):
    db_path = tmp_path / "legacy-cross-session-search.sqlite3"
    store = TaskStore(db_path)
    session = store.create_session("Legacy")
    message = store.add_session_message(session["id"], "user", "Legacy Straße archive")
    with store.connect() as con:
        con.execute("DELETE FROM session_message_search_terms")
        con.execute("DELETE FROM session_message_search_index_meta")
    migrated = TaskStore(db_path)
    assert migrated.search_session_messages("STRASSE") [0]["id"] == message["id"]
    with migrated.connect() as con:
        count = con.execute(
            "SELECT COUNT(*) FROM session_message_search_terms WHERE message_id=?",
            (message["id"],),
        ).fetchone()[0]
        version = con.execute(
            "SELECT version FROM session_message_search_index_meta"
        ).fetchone()[0]
    assert count > 0
    assert version == 1


def test_cross_session_search_indexes_single_messages_and_atomic_turns(tmp_path):
    store = TaskStore(tmp_path / "indexed-writes.sqlite3")
    session = store.create_session("Writes")
    single = store.add_session_message(session["id"], "user", "Crows deployment marker")
    turn = store.add_session_turn(
        session["id"], "Remember the workflow marker", "Workflow marker saved"
    )
    assert store.search_session_messages("deployment") [0]["id"] == single["id"]
    assert {item["id"] for item in store.search_session_messages("workflow")} == {
        turn[0]["id"], turn[1]["id"]
    }


def test_cross_session_search_index_cascades_on_session_delete(tmp_path):
    store = TaskStore(tmp_path / "indexed-delete.sqlite3")
    session = store.create_session("Delete")
    message = store.add_session_message(session["id"], "user", "temporary indexed phrase")
    assert store.delete_session(session["id"])
    with store.connect() as con:
        assert con.execute(
            "SELECT 1 FROM session_message_search_terms WHERE message_id=?",
            (message["id"],),
        ).fetchone() is None


def test_cross_session_search_uses_term_index_without_message_table_scan(tmp_path):
    store = TaskStore(tmp_path / "indexed-query-plan.sqlite3")
    session = store.create_session("Plan")
    store.add_session_message(session["id"], "user", "query plan indexed target")
    terms = sorted({"query", "plan"})
    with store.connect() as con:
        con.execute(
            "CREATE TEMP TABLE query_session_message_terms "
            "(term TEXT PRIMARY KEY) WITHOUT ROWID"
        )
        con.executemany(
            "INSERT INTO query_session_message_terms(term) VALUES(?)",
            [(term,) for term in terms],
        )
        plan = [
            row["detail"] for row in con.execute(
                "EXPLAIN QUERY PLAN SELECT indexed.message_id, COUNT(*) AS score "
                "FROM query_session_message_terms AS query "
                "CROSS JOIN session_message_search_terms AS indexed "
                "INDEXED BY idx_session_message_search_terms_term "
                "WHERE indexed.term=query.term "
                "GROUP BY indexed.message_id"
            )
        ]
    assert any(
        "SEARCH indexed USING COVERING INDEX idx_session_message_search_terms_term" in item
        for item in plan
    )
    assert not any("SCAN session_messages" in item for item in plan)
