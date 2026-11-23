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
    local = store.add_memory("local", "keep local value", source="user", tags=["local"])

    collision = memory_record(memory_id=local["id"], content="attempted overwrite")
    collision_result = store.import_memories([collision])
    assert collision_result == {"imported": 0, "skipped": 1}
    assert store.get_memory(local["id"])["content"] == "keep local value"

    archive_record = memory_record(memory_id="archive-memory-2", content="archive value")
    first_result = store.import_memories([archive_record])
    retry_result = store.import_memories([
        {**archive_record, "content": "attempted overwrite"}
    ])

    assert first_result == {"imported": 1, "skipped": 0}
    assert retry_result == {"imported": 0, "skipped": 1}
    assert store.get_memory("archive-memory-2")["content"] == "archive value"


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



def test_memory_import_rejects_aggregate_content_over_budget():
    records = [
        memory_record(memory_id=f"large-memory-{index}", content="x" * 4_000)
        for index in range(4_001)
    ]

    with pytest.raises(ValidationError, match="16-million-character"):
        app_module.MemoryImportRequest(
            format="crows-memory-export", version=1, memories=records
        )
