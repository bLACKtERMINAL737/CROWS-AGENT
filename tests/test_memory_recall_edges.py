"""Regression coverage for saved-memory recall edge cases."""

from crows import app as app_module


def test_recall_returns_none_when_no_saved_memories_match():
    memories = [
        {
            "source": "explicit_chat_request",
            "content": "My project name is Maximu Agent",
        }
    ]

    answer = app_module._memory_recall_answer(
        "What city did I ask you to remember?",
        memories,
    )

    assert answer is None


def test_recall_ignores_non_explicit_memory_sources():
    memories = [
        {
            "source": "user",
            "content": "CLAW-7391",
        },
        {
            "source": "assistant",
            "content": "The saved code is CLAW-7391",
        },
    ]

    answer = app_module._memory_recall_answer(
        "What code did I ask you to remember?",
        memories,
    )

    assert answer is None


def test_recall_does_not_return_a_fact_for_unrelated_question():
    memories = [
        {
            "source": "explicit_chat_request",
            "content": "My project name is Maximu Agent",
        }
    ]

    answer = app_module._memory_recall_answer(
        "Can you explain how memory works?",
        memories,
    )

    assert answer is None


def test_recall_avoids_ambiguous_equal_score_matches():
    memories = [
        {
            "source": "explicit_chat_request",
            "content": "My project code is CLAW-7391",
        },
        {
            "source": "explicit_chat_request",
            "content": "My project code is CLAW-7392",
        },
    ]

    answer = app_module._memory_recall_answer(
        "What code did I ask you to remember?",
        memories,
    )

    assert answer is None


def test_recall_returns_exact_matching_saved_fact():
    fact = (
        "this test fact for the next message in this session: "
        "AETHER_MEMORY_CHECK_4827. If saved memory is disabled, say so honestly"
    )
    memories = [
        {
            "source": "explicit_chat_request",
            "content": fact,
        }
    ]

    answer = app_module._memory_recall_answer(
        "Do you have the saved test fact AETHER_MEMORY_CHECK_4827? "
        "If available in saved memory, return the exact fact.",
        memories,
    )

    assert answer == fact



def test_recall_does_not_return_an_unrelated_fact_for_missing_exact_code():
    memories = [
        {
            "source": "explicit_chat_request",
            "content": (
                "this test fact for the next message in this session: "
                "AETHER_MEMORY_CHECK_4827. If saved memory is disabled, "
                "say so honestly"
            ),
        }
    ]

    answer = app_module._memory_recall_answer(
        "What exact secret code did I save as "
        "AETHER_MEMORY_NONEXISTENT_92741? "
        "If you cannot find it in saved memory, say you cannot find it. "
        "Do not guess.",
        memories,
    )

    assert answer is None

def test_recall_allows_duplicate_rows_for_the_same_fact():
    fact = "My project name is Maximu Agent"
    memories = [
        {"source": "explicit_chat_request", "content": fact},
        {"source": "explicit_chat_request", "content": fact},
    ]

    answer = app_module._memory_recall_answer(
        "What did I ask you to remember about my project?",
        memories,
    )

    assert answer == fact

def test_memory_search_returns_empty_for_stopword_only_recall_query(tmp_path):
    from crows.store import TaskStore

    store = TaskStore(tmp_path / "memory.sqlite3")
    store.add_memory(
        category="project",
        content="Project name is Maximu Agent",
        source="explicit_chat_request",
    )

    assert store.search_memories("What did I ask you to remember?") == []


def test_memory_search_still_finds_explicit_fact_when_query_has_terms(tmp_path):
    from crows.store import TaskStore

    store = TaskStore(tmp_path / "memory.sqlite3")
    fact = store.add_memory(
        category="project",
        content="Project name is Maximu Agent",
        source="explicit_chat_request",
    )

    results = store.search_memories("Maximu Agent")

    assert [item["id"] for item in results] == [fact["id"]]


def test_memory_search_empty_and_whitespace_queries_return_no_results(tmp_path):
    from crows.store import TaskStore

    store = TaskStore(tmp_path / "memory.sqlite3")
    store.add_memory(
        category="note",
        content="Keep this explicit memory",
        source="explicit_chat_request",
    )

    assert store.search_memories("") == []
    assert store.search_memories("   ") == []
    assert store.search_memories("what did i ask") == []

def test_memory_search_matches_complete_words_not_substrings(tmp_path):
    from crows.store import TaskStore

    store = TaskStore(tmp_path / "memory.sqlite3")
    store.add_memory(category="note", content="The party starts at noon")
    store.add_memory(category="note", content="A piece of art is on the wall")

    results = store.search_memories("art")

    assert len(results) == 1
    assert results[0]["content"] == "A piece of art is on the wall"


def test_memory_search_matches_hyphenated_identifier_components(tmp_path):
    from crows.store import TaskStore

    store = TaskStore(tmp_path / "memory.sqlite3")
    fact = store.add_memory(
        category="project",
        content="AETHER-CLAW is the project identifier",
        source="explicit_chat_request",
    )

    results = store.search_memories("CLAW")

    assert [item["id"] for item in results] == [fact["id"]]

def test_memory_search_handles_unicode_casefolding_with_dotted_i(tmp_path):
    from crows.store import TaskStore

    store = TaskStore(tmp_path / "unicode-memory.sqlite3")
    fact = store.add_memory(
        category="place",
        content="İstanbul is a city",
        source="explicit_chat_request",
    )

    results = store.search_memories("İSTANBUL")

    assert any(item["id"] == fact["id"] for item in results)

