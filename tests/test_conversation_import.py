from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from crows import app as app_module
from crows.store import TaskStore


def archive_session(*, session_id="archive-session-1", message_id="archive-message-1"):
    created = datetime(2026, 2, 3, tzinfo=timezone.utc).isoformat()
    updated = datetime(2026, 2, 4, tzinfo=timezone.utc).isoformat()
    return {
        "id": session_id,
        "title": "Imported conversation",
        "created_at": created,
        "updated_at": updated,
        "messages": [
            {
                "id": message_id,
                "session_id": session_id,
                "role": "user",
                "content": "Crows archive restore searchable marker",
                "created_at": created,
            },
            {
                "id": message_id + "-assistant",
                "session_id": session_id,
                "role": "assistant",
                "content": "Conversation restored successfully",
                "created_at": updated,
            },
        ],
    }


def test_conversation_import_restores_messages_and_search_index(tmp_path):
    store = TaskStore(tmp_path / "conversation-import.sqlite3")

    result = store.import_sessions([archive_session()])

    assert result == {
        "imported_sessions": 1,
        "imported_messages": 2,
        "skipped_sessions": 0,
    }
    restored = store.get_session("archive-session-1")
    assert restored is not None
    messages = store.list_session_messages("archive-session-1", limit=10)
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "Crows archive restore searchable marker"
    matches = store.search_session_messages("archive restore searchable")
    assert [message["id"] for message in matches] == ["archive-message-1"]


def test_conversation_import_skips_existing_session_without_overwrite(tmp_path):
    store = TaskStore(tmp_path / "conversation-import-collision.sqlite3")
    local = store.create_session("Keep local conversation")
    store.add_session_message(local["id"], "user", "local-only marker")

    collision = archive_session(session_id=local["id"])
    result = store.import_sessions([collision])

    assert result == {
        "imported_sessions": 0,
        "imported_messages": 0,
        "skipped_sessions": 1,
    }
    assert store.get_session(local["id"])["title"] == "Keep local conversation"
    assert [item["content"] for item in store.list_session_messages(local["id"])] == [
        "local-only marker"
    ]


def test_conversation_import_skips_whole_session_on_message_id_collision(tmp_path):
    store = TaskStore(tmp_path / "conversation-message-collision.sqlite3")
    local = store.create_session("Another local conversation")
    store.add_session_message(local["id"], "user", "local message")

    collision = archive_session(session_id="different-session")
    collision["messages"][0]["id"] = store.list_session_messages(local["id"])[0]["id"]
    result = store.import_sessions([collision])

    assert result == {
        "imported_sessions": 0,
        "imported_messages": 0,
        "skipped_sessions": 1,
    }
    assert store.get_session("different-session") is None


def test_conversation_import_is_idempotent(tmp_path):
    store = TaskStore(tmp_path / "conversation-import-idempotent.sqlite3")
    archive = archive_session()

    first = store.import_sessions([archive])
    second = store.import_sessions([archive])

    assert first["imported_sessions"] == 1
    assert second == {
        "imported_sessions": 0,
        "imported_messages": 0,
        "skipped_sessions": 1,
    }


def test_conversation_import_request_rejects_invalid_format_and_naive_timestamp():
    archive = archive_session()

    with pytest.raises(ValidationError):
        app_module.ConversationImportRequest(
            format="unknown-format", version=1, sessions=[archive]
        )

    archive["messages"][0]["created_at"] = "2026-02-03T00:00:00"
    with pytest.raises(ValidationError):
        app_module.ConversationImportRequest(
            format="aether-conversation-export", version=1, sessions=[archive]
        )


def test_conversation_import_request_rejects_duplicate_ids_and_extra_fields():
    archive = archive_session()
    duplicate = {**archive, "id": "archive-session-2"}

    with pytest.raises(ValidationError):
        app_module.ConversationImportRequest(
            format="aether-conversation-export",
            version=1,
            sessions=[archive, duplicate],
        )

    invalid = {**archive, "unexpected": "field"}
    with pytest.raises(ValidationError):
        app_module.ConversationImportRequest(
            format="aether-conversation-export", version=1, sessions=[invalid]
        )


def test_conversation_import_endpoint_returns_restore_counts(monkeypatch, tmp_path):
    store = TaskStore(tmp_path / "conversation-import-endpoint.sqlite3")
    monkeypatch.setattr(app_module, "store", store)
    request = app_module.ConversationImportRequest(
        format="aether-conversation-export",
        version=1,
        sessions=[archive_session()],
    )

    result = app_module.import_sessions(request)

    assert result == {
        "imported_sessions": 1,
        "imported_messages": 2,
        "skipped_sessions": 0,
    }
