from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from crows import app as app_module
from crows.store import TaskStore


def memory_record(*, memory_id="archive-memory-1", content="restore marker"):
    timestamp = datetime(2026, 1, 2, tzinfo=timezone.utc).isoformat()
    return {
        "id": memory_id,
        "category": "project",
        "content": content,
        "source": "user",
        "tags": ["archive", "restore"],
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def test_memory_import_restores_records_and_search_index(tmp_path):
    store = TaskStore(tmp_path / "memory-import.sqlite3")

    result = store.import_memories([memory_record()])

    assert result == {"imported": 1, "skipped": 0}
    restored = store.get_memory("archive-memory-1")
    assert restored is not None
    assert restored["content"] == "restore marker"
    assert restored["tags"] == ["archive", "restore"]
    assert [item["id"] for item in store.search_memories("restore marker")] == [
        "archive-memory-1"
    ]


def test_memory_import_is_idempotent_and_never_overwrites_existing_records(tmp_path):
    store = TaskStore(tmp_path / "memory-import-idempotent.sqlite3")
    store.add_memory("local", "keep local value", source="user", tags=["local"])

    archive_record = memory_record(memory_id="local-memory", content="archive value")
    store.import_memories([archive_record])
    result = store.import_memories([
        {**archive_record, "content": "attempted overwrite"}
    ])

    assert result == {"imported": 0, "skipped": 1}
    assert store.get_memory("local-memory")["content"] == "archive value"


def test_memory_import_endpoint_returns_import_and_skip_counts(monkeypatch, tmp_path):
    store = TaskStore(tmp_path / "memory-import-endpoint.sqlite3")
    monkeypatch.setattr(app_module, "store", store)
    request = app_module.MemoryImportRequest(
        format="crows-memory-export",
        version=1,
        memories=[memory_record(), memory_record()],
    )

    result = app_module.import_memories(request)

    assert result == {"imported": 1, "skipped": 1}


def test_memory_import_rejects_unknown_format_and_naive_timestamps():
    record = memory_record()

    with pytest.raises(ValidationError):
        app_module.MemoryImportRequest(
            format="unknown-memory-export",
            version=1,
            memories=[record],
        )

    with pytest.raises(ValidationError):
        app_module.MemoryImportRequest(
            format="crows-memory-export",
            version=1,
            memories=[{**record, "created_at": "2026-01-02T00:00:00"}],
        )


def test_memory_import_rejects_extra_fields():
    record = {**memory_record(), "unexpected": "not accepted"}

    with pytest.raises(ValidationError):
        app_module.MemoryImportRequest(
            format="crows-memory-export",
            version=1,
            memories=[record],
        )
