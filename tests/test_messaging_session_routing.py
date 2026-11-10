from concurrent.futures import ThreadPoolExecutor

import pytest

from crows.store import TaskStore


def test_messaging_conversation_resolves_to_one_persistent_agent_session(tmp_path):
    db = tmp_path / "routing.sqlite3"
    store = TaskStore(db)
    conversation_id = "msg_" + "a" * 40

    first = store.get_or_create_messaging_session(conversation_id, "telegram")
    second = TaskStore(db).get_or_create_messaging_session(conversation_id, "telegram")

    assert first["id"] == second["id"]
    assert first["title"] == "Telegram conversation"
    assert store.get_messaging_session(conversation_id, "telegram")["id"] == first["id"]


def test_messaging_session_routes_are_isolated_by_channel(tmp_path):
    store = TaskStore(tmp_path / "routing.sqlite3")
    conversation_id = "msg_" + "b" * 40

    telegram = store.get_or_create_messaging_session(conversation_id, "telegram")
    discord = store.get_or_create_messaging_session(conversation_id, "discord")

    assert telegram["id"] != discord["id"]


def test_messaging_session_route_creation_is_concurrency_safe(tmp_path):
    db = tmp_path / "routing.sqlite3"
    conversation_id = "msg_" + "c" * 40

    def resolve(_):
        return TaskStore(db).get_or_create_messaging_session(conversation_id, "telegram")["id"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        session_ids = list(pool.map(resolve, range(16)))

    assert len(set(session_ids)) == 1


@pytest.mark.parametrize(
    ("conversation_id", "channel"),
    [
        ("raw-provider-id", "telegram"),
        ("msg_" + "d" * 40, "../telegram"),
        ("msg_" + "D" * 40, "telegram"),
    ],
)
def test_messaging_session_route_rejects_invalid_scope(tmp_path, conversation_id, channel):
    store = TaskStore(tmp_path / "routing.sqlite3")
    with pytest.raises(ValueError):
        store.get_or_create_messaging_session(conversation_id, channel)
