from datetime import datetime, timezone, timedelta
from pathlib import Path
import pytest

from crows.scheduler_store import SchedulerStore


def test_schedule_persists_across_reopen_and_normalizes_timezone(tmp_path: Path):
    path = tmp_path / "scheduler.sqlite3"
    due = datetime(2026, 11, 1, 12, tzinfo=timezone(timedelta(hours=2)))
    saved = SchedulerStore(path).create_schedule("Daily report", "Summarize updates", due, "report-1")
    reopened = SchedulerStore(path).get_schedule(saved["id"])
    assert reopened["status"] == "scheduled"
    assert reopened["run_at"] == "2026-11-01T10:00:00+00:00"


def test_idempotency_returns_same_schedule_and_rejects_conflicting_payload(tmp_path: Path):
    store = SchedulerStore(tmp_path / "scheduler.sqlite3")
    first = store.create_schedule("One", "Do one", "2026-11-01T10:00:00+00:00", "same-key")
    second = store.create_schedule("One", "Do one", "2026-11-01T10:00:00+00:00", "same-key")
    assert first["id"] == second["id"]
    with pytest.raises(ValueError, match="different schedule"):
        store.create_schedule("Other", "Do other", "2026-11-01T10:00:00+00:00", "same-key")


def test_claim_due_is_atomic_across_store_instances(tmp_path: Path):
    path = tmp_path / "scheduler.sqlite3"
    first, second = SchedulerStore(path), SchedulerStore(path)
    first.create_schedule("Due", "Run", "2026-01-01T00:00:00+00:00", "due-1")
    now = "2026-01-02T00:00:00+00:00"
    claims_a = first.claim_due("worker-a", now=now)
    claims_b = second.claim_due("worker-b", now=now)
    assert len(claims_a) == 1
    assert claims_b == []
    assert claims_a[0]["lease_owner"] == "worker-a"


def test_only_lease_owner_can_finish_and_expired_lease_is_unknown(tmp_path: Path):
    store = SchedulerStore(tmp_path / "scheduler.sqlite3")
    item = store.create_schedule("Due", "Run", "2026-01-01T00:00:00+00:00", "due-1")
    store.claim_due("worker-a", now="2026-01-02T00:00:00+00:00", lease_seconds=10)
    assert not store.finish_claim(item["id"], "worker-b", "completed", now="2026-01-02T00:00:05+00:00")
    assert not store.finish_claim(item["id"], "worker-a", "completed", now="2026-01-02T00:00:11+00:00")
    assert store.mark_expired_claims_unknown(now="2026-01-02T00:00:11+00:00") == 1
    saved = store.get_schedule(item["id"])
    assert saved["status"] == "unknown"
    assert saved["lease_owner"] is None


def test_cancel_only_affects_pending_schedules(tmp_path: Path):
    store = SchedulerStore(tmp_path / "scheduler.sqlite3")
    item = store.create_schedule("Later", "Run", "2027-01-01T00:00:00+00:00", "later-1")
    assert store.cancel_schedule(item["id"]) is True
    assert store.cancel_schedule(item["id"]) is False
    assert store.get_schedule(item["id"])["status"] == "cancelled"


def test_schedule_rejects_naive_timestamps_and_invalid_limits(tmp_path: Path):
    store = SchedulerStore(tmp_path / "scheduler.sqlite3")
    with pytest.raises(ValueError, match="timezone"):
        store.create_schedule("No timezone", "Run", "2026-11-01T10:00:00", "naive")
    with pytest.raises(ValueError, match="limit"):
        store.list_schedules(0)
    with pytest.raises(ValueError, match="worker_id"):
        store.claim_due(" ")


def test_renew_claim_extends_only_current_unexpired_owner_lease(tmp_path):
    store = SchedulerStore(tmp_path / "renew.sqlite3")
    schedule = store.create_schedule(
        "[research] Renew lease", "Check lease ownership",
        "2026-11-01T10:00:00+00:00", "renew-lease-1"
    )
    claimed = store.claim_due(
        "worker-a", now="2026-11-01T10:01:00+00:00", lease_seconds=60
    )
    assert len(claimed) == 1
    assert store.renew_claim(
        schedule["id"], "worker-a", now="2026-11-01T10:01:30+00:00", lease_seconds=300
    ) is True
    renewed = store.get_schedule(schedule["id"])
    assert renewed["lease_expires_at"] == "2026-11-01T10:06:30+00:00"
    assert store.renew_claim(
        schedule["id"], "worker-b", now="2026-11-01T10:01:31+00:00"
    ) is False


def test_renew_claim_rejects_expired_lease_and_invalid_duration(tmp_path):
    store = SchedulerStore(tmp_path / "expired-renew.sqlite3")
    schedule = store.create_schedule(
        "[research] Expired lease", "Check expired lease",
        "2026-11-01T10:00:00+00:00", "renew-lease-2"
    )
    store.claim_due("worker-a", now="2026-11-01T10:01:00+00:00", lease_seconds=10)
    assert store.renew_claim(
        schedule["id"], "worker-a", now="2026-11-01T10:01:11+00:00"
    ) is False
    with pytest.raises(ValueError):
        store.renew_claim(schedule["id"], "worker-a", lease_seconds=0)

from datetime import datetime, timedelta, timezone

import pytest

from crows.scheduler_store import SchedulerStore


def make_unknown_schedule(store):
    due = datetime.now(timezone.utc) - timedelta(minutes=2)
    saved = store.create_schedule(
        "[research] Verify uncertain run", "Inspect the task journal",
        due, "audit-unknown-1"
    )
    store.claim_due("scheduler-worker", lease_seconds=1)
    store.mark_expired_claims_unknown(now=datetime.now(timezone.utc) + timedelta(seconds=2))
    return saved


def test_reconciliation_audit_is_append_only_and_does_not_change_unknown_state(tmp_path):
    store = SchedulerStore(tmp_path / "reconcile.sqlite3")
    schedule = make_unknown_schedule(store)

    first = store.record_reconciliation(
        schedule["id"], "Reviewed task journal", "Task remained unknown; no provider result found."
    )
    second = store.record_reconciliation(
        schedule["id"], "Checked provider logs", "No confirmed completion or safe retry evidence."
    )

    assert first["id"] != second["id"]
    entries = store.list_reconciliation_audit(schedule["id"])
    assert len(entries) == 2
    assert entries[0]["note"] == "Reviewed task journal"
    assert store.get_schedule(schedule["id"])["status"] == "unknown"


def test_reconciliation_requires_unknown_schedule_and_valid_note(tmp_path):
    store = SchedulerStore(tmp_path / "reconcile-validation.sqlite3")
    scheduled = store.create_schedule(
        "[research] Still pending", "Do not execute", "2027-01-01T00:00:00+00:00", "pending-audit"
    )
    with pytest.raises(ValueError, match="only unknown"):
        store.record_reconciliation(scheduled["id"], "Checked", "Evidence")
    with pytest.raises(ValueError, match="required"):
        store.record_reconciliation(scheduled["id"], " ", "Evidence")
    with pytest.raises(KeyError):
        store.record_reconciliation("missing", "Checked", "Evidence")
    with pytest.raises(KeyError):
        store.list_reconciliation_audit("missing")



def test_concurrent_workers_have_exactly_one_claim_winner(tmp_path: Path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    path = tmp_path / "concurrent-claims.sqlite3"
    stores = [SchedulerStore(path) for _ in range(8)]
    stores[0].create_schedule(
        "One due task", "Must have one owner", "2026-01-01T00:00:00+00:00", "one-owner"
    )
    barrier = Barrier(len(stores))

    def claim(index):
        barrier.wait(timeout=5)
        return stores[index].claim_due(
            f"worker-{index}", now="2026-01-02T00:00:00+00:00", limit=10
        )

    with ThreadPoolExecutor(max_workers=len(stores)) as pool:
        results = list(pool.map(claim, range(len(stores))))

    claimed = [item for batch in results for item in batch]
    assert len(claimed) == 1
    assert claimed[0]["lease_owner"].startswith("worker-")


def test_finish_claim_cannot_overwrite_expired_or_recovered_schedule(tmp_path: Path):
    store = SchedulerStore(tmp_path / "stale-finish.sqlite3")
    schedule = store.create_schedule(
        "Due", "Do work", "2026-01-01T00:00:00+00:00", "stale-finish"
    )
    store.claim_due(
        "worker-old", now="2026-01-02T00:00:00+00:00", lease_seconds=5
    )

    assert store.finish_claim(
        schedule["id"], "worker-old", "completed",
        now="2026-01-02T00:00:06+00:00", result_json='{"ok": true}'
    ) is False
    assert store.mark_expired_claims_unknown(
        now="2026-01-02T00:00:06+00:00"
    ) == 1
    assert store.finish_claim(
        schedule["id"], "worker-old", "completed",
        now="2026-01-02T00:00:07+00:00"
    ) is False

    saved = store.get_schedule(schedule["id"])
    assert saved["status"] == "unknown"
    assert saved["result_json"] is None
    assert saved["lease_owner"] is None


def test_expired_lease_cannot_be_resurrected_by_renewal(tmp_path: Path):
    store = SchedulerStore(tmp_path / "no-resurrection.sqlite3")
    schedule = store.create_schedule(
        "Due", "Do work", "2026-01-01T00:00:00+00:00", "no-resurrection"
    )
    store.claim_due(
        "worker-a", now="2026-01-02T00:00:00+00:00", lease_seconds=5
    )
    assert store.mark_expired_claims_unknown(
        now="2026-01-02T00:00:06+00:00"
    ) == 1

    assert store.renew_claim(
        schedule["id"], "worker-a",
        now="2026-01-02T00:00:07+00:00", lease_seconds=300
    ) is False
    assert store.get_schedule(schedule["id"])["status"] == "unknown"


def test_claim_due_respects_limit_and_due_order(tmp_path: Path):
    store = SchedulerStore(tmp_path / "claim-order.sqlite3")
    for index, run_at in enumerate((
        "2026-01-01T00:03:00+00:00",
        "2026-01-01T00:01:00+00:00",
        "2026-01-01T00:02:00+00:00",
    )):
        store.create_schedule(f"Task {index}", "Run", run_at, f"ordered-{index}")

    claimed = store.claim_due(
        "worker-a", now="2026-01-02T00:00:00+00:00", limit=2
    )
    assert [item["title"] for item in claimed] == ["Task 1", "Task 2"]
    assert store.metrics()["by_status"]["scheduled"] == 1
    assert store.metrics()["by_status"]["running"] == 2
