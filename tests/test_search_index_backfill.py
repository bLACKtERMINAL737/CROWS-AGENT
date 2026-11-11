from crows.store import TaskStore


def test_search_index_backfill_processes_multiple_bounded_batches(tmp_path):
    db_path = tmp_path / "large-search-index-backfill.sqlite3"
    store = TaskStore(db_path)
    session = store.create_session("Legacy search history")
    now = "2026-01-01T00:00:00+00:00"
    count = 1001

    with store.connect() as con:
        con.executemany(
            "INSERT INTO session_messages(id, session_id, role, content, created_at) "
            "VALUES(?, ?, 'user', ?, ?)",
            [
                (f"legacy-message-{index}", session["id"],
                 f"messagebatchmarker{index}", now)
                for index in range(count)
            ],
        )
        con.executemany(
            "INSERT INTO memories(id, category, content, source, tags_json, created_at, updated_at) "
            "VALUES(?, 'note', ?, 'user', '[]', ?, ?)",
            [
                (f"legacy-memory-{index}", f"memorybatchmarker{index}", now, now)
                for index in range(count)
            ],
        )
        con.execute("DELETE FROM session_message_search_terms")
        con.execute("DELETE FROM session_message_search_index_meta")
        con.execute("DELETE FROM memory_search_terms")
        con.execute("DELETE FROM memory_search_index_meta")

    migrated = TaskStore(db_path)

    message_results = migrated.search_session_messages("messagebatchmarker1000")
    memory_results = migrated.search_memories("memorybatchmarker1000")
    assert [item["id"] for item in message_results] == ["legacy-message-1000"]
    assert [item["id"] for item in memory_results] == ["legacy-memory-1000"]

    with migrated.connect() as con:
        assert con.execute(
            "SELECT COUNT(*) FROM session_message_search_terms"
        ).fetchone()[0] > count
        assert con.execute(
            "SELECT COUNT(*) FROM memory_search_terms"
        ).fetchone()[0] > count
