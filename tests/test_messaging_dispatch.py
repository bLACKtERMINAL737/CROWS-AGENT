from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import crows.messaging_dispatch as dispatch_module

from crows.messaging_dispatch import InboundDispatchError, InboundDispatchStore
from crows.messaging_store import MessagingStore


def _queued(tmp_path: Path):
    db = tmp_path / "dispatch.sqlite3"
    inbox = MessagingStore(db)
    inbound = inbox.ingest(
        channel="telegram", provider_message_id="provider-1",
        provider_user_id="chat-1", text="hello",
    )
    dispatch = InboundDispatchStore(db)
    route = dispatch.enqueue(
        message_id=inbound["message"]["id"],
        conversation_id=inbound["conversation_id"],
        channel="telegram", session_id="session-1",
    )
    return dispatch, route


def test_enqueue_is_idempotent_and_rejects_route_rebinding(tmp_path):
    dispatch, route = _queued(tmp_path)
    duplicate = dispatch.enqueue(
        message_id=route["message_id"], conversation_id=route["conversation_id"],
        channel="telegram", session_id="session-1",
    )
    assert duplicate["duplicate"] is True
    assert duplicate["id"] == route["id"]
    with pytest.raises(InboundDispatchError):
        dispatch.enqueue(
            message_id=route["message_id"], conversation_id=route["conversation_id"],
            channel="discord", session_id="other-session",
        )


def test_only_one_concurrent_worker_claims_a_dispatch(tmp_path):
    dispatch, route = _queued(tmp_path)
    def claim(worker):
        return dispatch.claim(worker_id=worker, limit=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ("worker-a", "worker-b")))
    claimed = [item for batch in results for item in batch]
    assert len(claimed) == 1
    assert claimed[0]["id"] == route["id"]
    assert claimed[0]["state"] == "processing"


def test_only_current_lease_owner_can_finish(tmp_path):
    dispatch, route = _queued(tmp_path)
    claimed = dispatch.claim(worker_id="worker-a", limit=1)[0]
    assert dispatch.finish(route["id"], worker_id="worker-b", state="completed") is False
    assert dispatch.finish(route["id"], worker_id="worker-a", state="completed") is True
    assert dispatch.get(route["id"])["state"] == "completed"


def test_expired_dispatch_becomes_unknown_and_is_not_replayed(tmp_path):
    dispatch, route = _queued(tmp_path)
    dispatch.claim(worker_id="worker-a", limit=1)
    with dispatch.connect() as con:
        con.execute(
            "UPDATE messaging_inbound_dispatch SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?",
            (route["id"],),
        )
    assert dispatch.expire_leases() == 1
    assert dispatch.get(route["id"])["state"] == "unknown"
    assert dispatch.claim(worker_id="worker-b", limit=1) == []


def test_dispatch_metrics_omit_message_and_session_content(tmp_path):
    dispatch, route = _queued(tmp_path)
    item = dispatch.list_recent()
    assert item == [{
        "id": route["id"], "channel": "telegram", "state": "pending",
        "attempts": 0, "created_at": route["created_at"], "updated_at": route["updated_at"],
    }]


def test_expired_dispatch_with_checkpointed_reply_returns_to_pending(tmp_path):
    dispatch, route = _queued(tmp_path)
    dispatch.claim(worker_id="worker-a", limit=1)
    assert dispatch.save_reply(route["id"], worker_id="worker-a", reply_text="saved reply")
    with dispatch.connect() as con:
        con.execute("UPDATE messaging_inbound_dispatch SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?", (route["id"],))
    assert dispatch.expire_leases() == 1
    recovered = dispatch.get(route["id"])
    assert recovered["state"] == "pending" and recovered["reply_text"] == "saved reply"
    reclaimed = dispatch.claim(worker_id="worker-b", limit=1)
    assert len(reclaimed) == 1 and reclaimed[0]["reply_text"] == "saved reply"


def test_expired_dispatch_without_reply_remains_unknown(tmp_path):
    dispatch, route = _queued(tmp_path)
    dispatch.claim(worker_id="worker-a", limit=1)
    with dispatch.connect() as con:
        con.execute("UPDATE messaging_inbound_dispatch SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?", (route["id"],))
    assert dispatch.expire_leases() == 1
    assert dispatch.get(route["id"])["state"] == "unknown"



def test_concurrent_dispatch_store_initialization_serializes_schema_migration(
    tmp_path,
):
    db = tmp_path / "concurrent-dispatch-init.sqlite3"
    MessagingStore(db)

    def initialize(_):
        return InboundDispatchStore(db)

    with ThreadPoolExecutor(max_workers=8) as pool:
        stores = list(pool.map(initialize, range(8)))

    assert len(stores) == 8
    with stores[0].connect() as con:
        columns = {
            row["name"]
            for row in con.execute("PRAGMA table_info(messaging_inbound_dispatch)")
        }
    assert "reply_text" in columns



def test_lease_renewal_is_fenced_to_current_unexpired_owner(tmp_path):
    dispatch, route = _queued(tmp_path)
    claimed = dispatch.claim(worker_id="worker-a", limit=1, lease_seconds=30)[0]

    assert dispatch.renew(route["id"], worker_id="worker-b", lease_seconds=60) is False
    assert dispatch.renew(route["id"], worker_id="worker-a", lease_seconds=60) is True
    renewed = dispatch.get(route["id"])
    assert renewed["lease_owner"] == "worker-a"
    assert renewed["lease_until"] > claimed["lease_until"]


def test_expired_lease_cannot_be_revived_by_renewal_or_finished(tmp_path):
    dispatch, route = _queued(tmp_path)
    dispatch.claim(worker_id="worker-a", limit=1)
    with dispatch.connect() as con:
        con.execute(
            "UPDATE messaging_inbound_dispatch SET lease_until=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", route["id"]),
        )

    assert dispatch.renew(route["id"], worker_id="worker-a") is False
    assert dispatch.finish(
        route["id"], worker_id="worker-a", state="completed"
    ) is False
    assert dispatch.get(route["id"])["state"] == "processing"


def test_dispatch_without_reply_cannot_be_requeued_as_pending(tmp_path):
    dispatch, route = _queued(tmp_path)
    dispatch.claim(worker_id="worker-a", limit=1)

    assert dispatch.finish(
        route["id"], worker_id="worker-a", state="pending",
        diagnostic="No durable reply checkpoint exists.",
    ) is False
    saved = dispatch.get(route["id"])
    assert saved["state"] == "processing"
    assert saved["reply_text"] is None
    assert saved["lease_owner"] == "worker-a"


def test_checkpointed_reply_is_immutable_for_a_dispatch(tmp_path):
    dispatch, route = _queued(tmp_path)
    dispatch.claim(worker_id="worker-a", limit=1)

    assert dispatch.save_reply(
        route["id"], worker_id="worker-a", reply_text="first durable answer"
    ) is True
    assert dispatch.save_reply(
        route["id"], worker_id="worker-a", reply_text="different answer"
    ) is False
    assert dispatch.get(route["id"])["reply_text"] == "first durable answer"


def test_expired_checkpointed_lease_can_be_recovered_only_once(tmp_path):
    dispatch, route = _queued(tmp_path)
    dispatch.claim(worker_id="worker-a", limit=1)
    assert dispatch.save_reply(
        route["id"], worker_id="worker-a", reply_text="saved answer"
    )
    with dispatch.connect() as con:
        con.execute(
            "UPDATE messaging_inbound_dispatch SET lease_until=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", route["id"]),
        )

    assert dispatch.expire_leases() == 1
    assert dispatch.expire_leases() == 0
    recovered = dispatch.claim(worker_id="worker-b", limit=1)
    assert len(recovered) == 1
    assert recovered[0]["reply_text"] == "saved answer"
    assert dispatch.claim(worker_id="worker-c", limit=1) == []

