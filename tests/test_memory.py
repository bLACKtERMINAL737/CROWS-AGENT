import pytest

from crows import app as app_module
from crows.store import TaskStore


def test_memory_add_search_and_delete(tmp_path):
    store = TaskStore(tmp_path / 'memory.sqlite3')
    created = store.add_memory('preference', 'Use concise explanations in Bangla', tags=['language', 'style'])
    assert created['source'] == 'user'
    assert created['tags'] == ['language', 'style']
    found = store.search_memories('Bangla concise')
    assert found[0]['id'] == created['id']
    assert store.delete_memory(created['id']) is True
    assert store.get_memory(created['id']) is None


def test_memory_category_filter(tmp_path):
    store = TaskStore(tmp_path / 'memory.sqlite3')
    store.add_memory('project', 'Aether coding agent')
    store.add_memory('preference', 'Use local-first tools')
    assert len(store.list_memories(category='project')) == 1
    assert store.search_memories('nothing matches') == []


class CapturingRouter:
    def __init__(self):
        self.memories = None

    async def chat(self, message, task_type, *, history, memories, model_route=None, allow_fallback=True):
        self.memories = memories
        return {"answer": "I will keep the explanation concise and in Bangla."}


@pytest.mark.asyncio
async def test_chat_opt_in_memory_reaches_router_and_persists_turn(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "chat-memory.sqlite3")
    saved = store.add_memory(
        "preference",
        "Use concise explanations in Bangla",
        tags=["language", "style"],
    )
    router = CapturingRouter()
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "router", router)

    result = await app_module.chat(
        app_module.ChatRequest(
            message="Explain this topic",
            use_saved_memories=True,
        )
    )

    assert result["memory_context_used"] is True
    assert router.memories
    assert any(item["id"] == saved["id"] for item in router.memories)
    messages = store.list_session_messages(result["session_id"])
    assert [(item["role"], item["content"]) for item in messages] == [
        ("user", "Explain this topic"),
        ("assistant", "I will keep the explanation concise and in Bangla."),
    ]



@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Remember this code: CLAW-7391", "CLAW-7391"),
        ("Please remember that I prefer short answers", "I prefer short answers"),
        ("মনে রাখো: Maximu Agent", "Maximu Agent"),
        ("মনে রেখো আমার প্রজেক্টের নাম AETHER", "আমার প্রজেক্টের নাম AETHER"),
    ],
)
def test_extract_explicit_memory_request(message, expected):
    assert app_module.extract_explicit_memory_request(message) == expected


@pytest.mark.parametrize(
    "message",
    [
        "What code did I ask you to remember?",
        "I remember the code CLAW-7391",
        "Can you explain how memory works?",
        "Please remember",
    ],
)
def test_extract_explicit_memory_request_ignores_non_requests(message):
    assert app_module.extract_explicit_memory_request(message) is None


@pytest.mark.asyncio
async def test_chat_explicit_remember_request_saves_memory_and_confirms(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "explicit-memory.sqlite3")
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "router", CapturingRouter())

    result = await app_module.chat(
        app_module.ChatRequest(message="Remember this code: CLAW-7391")
    )

    saved = store.list_memories(limit=10)
    assert len(saved) == 1
    assert saved[0]["content"] == "CLAW-7391"
    assert saved[0]["source"] == "explicit_chat_request"
    assert "CLAW-7391" in result["answer"]


@pytest.mark.asyncio
async def test_chat_question_about_memory_does_not_save_anything(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "no-auto-memory.sqlite3")
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "router", CapturingRouter())

    await app_module.chat(
        app_module.ChatRequest(message="What code did I ask you to remember?")
    )

    assert store.list_memories(limit=10) == []

def test_memory_recall_question_prioritizes_explicitly_saved_memory(tmp_path):
    store = TaskStore(tmp_path / "recall.sqlite3")
    store.add_memory(
        "preference",
        "Aether could refer to a few things—please clarify what you mean.",
        source="user",
    )
    saved = store.add_memory(
        "note",
        "CLAW-7391",
        source="explicit_chat_request",
        tags=["chat", "user-requested"],
    )

    results = store.search_memories("What code did I ask you to remember?", limit=5)

    assert results
    assert results[0]["id"] == saved["id"]

@pytest.mark.asyncio
async def test_chat_returns_only_saved_code_for_explicit_code_recall(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "concise-recall.sqlite3")
    store.add_memory(
        "note",
        "Maximu Agent",
        source="explicit_chat_request",
        tags=["chat", "user-requested"],
    )
    store.add_memory(
        "note",
        "CLAW-7391",
        source="explicit_chat_request",
        tags=["chat", "user-requested"],
    )
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "router", CapturingRouter())

    result = await app_module.chat(
        app_module.ChatRequest(
            message="What code did I ask you to remember?",
            use_saved_memories=True,
        )
    )

    assert result["answer"] == "CLAW-7391"


@pytest.mark.asyncio
async def test_chat_does_not_force_code_recall_without_memory_opt_in(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "recall-opt-out.sqlite3")
    store.add_memory(
        "note",
        "CLAW-7391",
        source="explicit_chat_request",
        tags=["chat", "user-requested"],
    )
    monkeypatch.setattr(app_module, "store", store)
    router = CapturingRouter()
    monkeypatch.setattr(app_module, "router", router)

    result = await app_module.chat(
        app_module.ChatRequest(
            message="What code did I ask you to remember?",
            use_saved_memories=False,
        )
    )

    assert result["answer"] == "I will keep the explanation concise and in Bangla."


@pytest.mark.asyncio
async def test_exact_missing_identifier_never_returns_unrelated_saved_code(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "exact-missing-code.sqlite3")
    store.add_memory(
        "note",
        "AETHER_MEMORY_CHECK_4827",
        source="explicit_chat_request",
        tags=["chat", "user-requested"],
    )
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "router", CapturingRouter())

    result = await app_module.chat(
        app_module.ChatRequest(
            message=(
                "What exact secret code did I save as "
                "AETHER_MEMORY_NONEXISTENT_92741? If you cannot find it in saved memory, "
                "say you cannot find it. Do not guess."
            ),
            use_saved_memories=True,
        )
    )

    assert result["answer"] == "I couldn't find that exact code in saved memory."
    assert "AETHER_MEMORY_CHECK_4827" not in result["answer"]


@pytest.mark.asyncio
async def test_exact_identifier_recall_without_memory_opt_in_fails_closed(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "exact-code-opt-out.sqlite3")
    store.add_memory(
        "note",
        "AETHER_MEMORY_CHECK_4827",
        source="explicit_chat_request",
        tags=["chat", "user-requested"],
    )
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "router", CapturingRouter())

    result = await app_module.chat(
        app_module.ChatRequest(
            message=(
                "What exact secret code did I save as "
                "AETHER_MEMORY_NONEXISTENT_92741? If you cannot find it in saved memory, "
                "say you cannot find it. Do not guess."
            ),
            use_saved_memories=False,
        )
    )

    assert result["answer"] == (
        "Saved memory is not enabled for this request, so I can't verify that exact code."
    )
    assert "AETHER_MEMORY_CHECK_4827" not in result["answer"]


@pytest.mark.asyncio
async def test_chat_general_recall_returns_explicitly_saved_project_fact(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "general-recall.sqlite3")
    store.add_memory(
        "note",
        "My project name is Maximu Agent",
        source="explicit_chat_request",
        tags=["chat", "user-requested"],
    )
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "router", CapturingRouter())

    result = await app_module.chat(
        app_module.ChatRequest(
            message="What did I ask you to remember about my project?",
            use_saved_memories=True,
        )
    )

    assert result["answer"] == "My project name is Maximu Agent"


@pytest.mark.asyncio
async def test_chat_general_recall_does_not_invent_missing_facts(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "general-recall-empty.sqlite3")
    store.add_memory(
        "note",
        "My project name is Maximu Agent",
        source="explicit_chat_request",
        tags=["chat", "user-requested"],
    )
    monkeypatch.setattr(app_module, "store", store)
    router = CapturingRouter()
    monkeypatch.setattr(app_module, "router", router)

    result = await app_module.chat(
        app_module.ChatRequest(
            message="What city did I ask you to remember?",
            use_saved_memories=True,
        )
    )

    assert result["answer"] == "I couldn't find that information in saved memory."


def test_memory_recall_accepts_do_you_have_question():
    memory = {
        "source": "explicit_chat_request",
        "content": (
            "this test fact for the next message in this session: "
            "AETHER_MEMORY_CHECK_4827"
        ),
    }

    answer = app_module._memory_recall_answer(
        "Do you have the saved test fact AETHER_MEMORY_CHECK_4827? "
        "If available in saved memory, return the exact fact.",
        [memory],
    )

    assert answer == memory["content"]


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("What city did I ask you to remember?", True),
        ("What project name did I save?", True),
        ("What exact secret code did I save as AETHER_MEMORY_MISSING_123?", True),
        ("What is the capital of France?", False),
        ("Explain how memory works.", False),
    ],
)
def test_is_memory_recall_question(message, expected):
    assert app_module._is_memory_recall_question(message) is expected


@pytest.mark.asyncio
async def test_chat_general_recall_missing_fact_fails_closed(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "general-recall-missing.sqlite3")
    store.add_memory(
        "note",
        "My project name is Maximu Agent",
        source="explicit_chat_request",
        tags=["chat", "user-requested"],
    )
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "router", CapturingRouter())

    result = await app_module.chat(
        app_module.ChatRequest(
            message="What city did I ask you to remember?",
            use_saved_memories=True,
        )
    )

    assert result["answer"] == "I couldn't find that information in saved memory."


def test_memory_search_finds_matching_record_beyond_latest_500(tmp_path):
    store = TaskStore(tmp_path / "large-memory.sqlite3")
    old_memory = store.add_memory("project", "CROWS_ARCHIVE_MARKER_7319")

    for index in range(501):
        store.add_memory("note", f"Unrelated archive entry {index}")

    # Make the target deterministically older than every distractor.
    with store.connect() as con:
        con.execute(
            "UPDATE memories SET updated_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", old_memory["id"]),
        )

    results = store.search_memories("CROWS_ARCHIVE_MARKER_7319")

    assert results
    assert results[0]["id"] == old_memory["id"]
    assert results[0]["content"] == "CROWS_ARCHIVE_MARKER_7319"

def test_list_memories_bulk_loads_records_without_per_record_queries(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "bulk-memory.sqlite3")
    older = store.add_memory("project", "Older project fact", tags=["archive"])
    newer = store.add_memory("preference", "Newer preference", tags=["style"])

    def unexpected_get_memory(_memory_id):
        raise AssertionError("list_memories must not issue one query per record")

    monkeypatch.setattr(store, "get_memory", unexpected_get_memory)

    all_memories = store.list_memories(limit=None)
    assert [item["id"] for item in all_memories] == [newer["id"], older["id"]]
    assert all_memories[1]["tags"] == ["archive"]

    project_memories = store.list_memories(limit=None, category="project")
    assert [item["id"] for item in project_memories] == [older["id"]]

    limited = store.list_memories(limit=1)
    assert [item["id"] for item in limited] == [newer["id"]]



def test_memory_search_index_backfills_existing_records_and_tracks_writes(tmp_path):
    database = tmp_path / "memory-index-migration.sqlite3"
    store = TaskStore(database)
    old = store.add_memory("project", "Legacy CROWS_INDEX_MIGRATION_7319")
    with store.connect() as con:
        con.execute("DROP TABLE memory_search_terms")
        con.execute("DROP TABLE memory_search_index_meta")

    # Reopening an older database rebuilds the index from durable memory rows.
    migrated = TaskStore(database)
    results = migrated.search_memories("CROWS_INDEX_MIGRATION_7319")
    assert results and results[0]["id"] == old["id"]

    created = migrated.add_memory(
        "note", "Fresh indexed fact ORBIT_SEARCH_4821", tags=["archive-tag"]
    )
    assert migrated.search_memories("ORBIT_SEARCH_4821")[0]["id"] == created["id"]
    assert migrated.search_memories("archive-tag")[0]["id"] == created["id"]

    assert migrated.delete_memory(created["id"]) is True
    assert migrated.search_memories("ORBIT_SEARCH_4821") == []


def test_memory_search_index_preserves_unicode_whole_word_matching(tmp_path):
    store = TaskStore(tmp_path / "unicode-memory-index.sqlite3")
    saved = store.add_memory("note", "İstanbul CROWS-indexed")
    assert store.search_memories("İSTANBUL")[0]["id"] == saved["id"]
    assert store.search_memories("art") == []



def test_update_memory_replaces_search_terms_and_preserves_unchanged_fields(tmp_path):
    store = TaskStore(tmp_path / "update-memory.sqlite3")
    saved = store.add_memory(
        "project", "Old RETIREDALPHA7319", source="explicit_chat_request",
        tags=["LEGACYTAGALPHA"],
    )

    updated = store.update_memory(
        saved["id"], category="preference",
        content="New FRESHBETA4821", tags=["CURRENTTAGBETA"],
    )

    assert updated["category"] == "preference"
    assert updated["content"] == "New FRESHBETA4821"
    assert updated["source"] == "explicit_chat_request"
    assert updated["tags"] == ["CURRENTTAGBETA"]
    assert store.search_memories("RETIREDALPHA7319") == []
    assert store.search_memories("LEGACYTAGALPHA") == []
    assert store.search_memories("FRESHBETA4821")[0]["id"] == saved["id"]
    assert store.search_memories("CURRENTTAGBETA")[0]["id"] == saved["id"]


def test_update_memory_can_change_one_field_and_returns_none_for_missing_id(tmp_path):
    store = TaskStore(tmp_path / "partial-update-memory.sqlite3")
    saved = store.add_memory("note", "Keep this content", tags=["keep-tag"])

    updated = store.update_memory(saved["id"], category="preference")

    assert updated["category"] == "preference"
    assert updated["content"] == "Keep this content"
    assert updated["tags"] == ["keep-tag"]
    assert store.update_memory("missing-memory-id", content="Not found") is None


def test_update_memory_rejects_blank_values_without_mutating_record(tmp_path):
    store = TaskStore(tmp_path / "invalid-update-memory.sqlite3")
    saved = store.add_memory("note", "Original searchable marker")

    with pytest.raises(ValueError, match="category must not be blank"):
        store.update_memory(saved["id"], category="   ")

    unchanged = store.get_memory(saved["id"])
    assert unchanged["category"] == "note"
    assert unchanged["content"] == "Original searchable marker"
    assert store.search_memories("Original searchable marker")[0]["id"] == saved["id"]


def test_memory_update_api_rejects_empty_patch_and_updates_saved_record(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "memory-update-api.sqlite3")
    saved = store.add_memory("note", "BEFOREALPHA7319")
    monkeypatch.setattr(app_module, "store", store)

    with pytest.raises(app_module.HTTPException) as empty_error:
        app_module.update_memory(saved["id"], app_module.MemoryUpdateRequest())
    assert empty_error.value.status_code == 400

    updated = app_module.update_memory(
        saved["id"],
        app_module.MemoryUpdateRequest(content="AFTERBETA4821", tags=["edited"]),
    )
    assert updated["content"] == "AFTERBETA4821"
    assert store.search_memories("BEFOREALPHA7319") == []
    assert store.search_memories("AFTERBETA4821")[0]["id"] == saved["id"]

    with pytest.raises(app_module.HTTPException) as missing_error:
        app_module.update_memory(
            "missing-memory-id",
            app_module.MemoryUpdateRequest(content="No record"),
        )
    assert missing_error.value.status_code == 404



def test_memory_search_candidate_lookup_uses_term_index(tmp_path):
    store = TaskStore(tmp_path / "memory-search-query-plan.sqlite3")
    store.add_memory("project", "CROWS_QUERY_PLAN_MARKER_7319")

    with store.connect() as con:
        con.execute(
            "CREATE TEMP TABLE query_memory_terms(term TEXT PRIMARY KEY) WITHOUT ROWID"
        )
        con.execute(
            "INSERT INTO query_memory_terms(term) VALUES(?)",
            ("crows_query_plan_marker_7319",),
        )
        plan = con.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT indexed.memory_id, COUNT(*) AS matched_terms
            FROM query_memory_terms AS matched
            CROSS JOIN memory_search_terms AS indexed
            WHERE indexed.term=matched.term
            GROUP BY indexed.memory_id
            """
        ).fetchall()

    details = [row["detail"] for row in plan]
    assert any(
        "SEARCH indexed USING COVERING INDEX idx_memory_search_terms_term" in detail
        for detail in details
    ), details


def test_memory_search_does_not_fallback_scan_explicit_memories(tmp_path):
    store = TaskStore(tmp_path / "memory-search-no-fallback.sqlite3")
    store.add_memory(
        "note",
        "CROWS_EXPLICIT_RECALL_ONLY_92841",
        source="explicit_chat_request",
    )

    assert store.search_memories("unrelated ordinary lookup") == []


def test_memory_recall_intent_keeps_explicit_memory_candidates(tmp_path):
    store = TaskStore(tmp_path / "memory-search-recall.sqlite3")
    saved = store.add_memory(
        "note",
        "CROWS_EXPLICIT_RECALL_ONLY_92841",
        source="explicit_chat_request",
    )

    results = store.search_memories("What code did I ask you to remember?")

    assert results
    assert results[0]["id"] == saved["id"]
