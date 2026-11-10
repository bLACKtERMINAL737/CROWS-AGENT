from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

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
