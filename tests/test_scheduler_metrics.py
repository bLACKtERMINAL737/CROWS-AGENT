from crows.scheduler_store import SchedulerStore


def test_scheduler_metrics_aggregate_statuses_without_identifiers(tmp_path):
    store = SchedulerStore(tmp_path / "scheduler-metrics.sqlite3")
    store.create_schedule(
        "[research] First", "one", "2026-11-01T10:00:00+00:00", "metrics-1"
    )
    second = store.create_schedule(
        "[research] Second", "two", "2026-11-02T10:00:00+00:00", "metrics-2"
    )
    assert store.cancel_schedule(second["id"]) is True

    metrics = store.metrics()
    assert metrics["total"] == 2
    assert metrics["by_status"]["scheduled"] == 1
    assert metrics["by_status"]["cancelled"] == 1
    assert metrics["by_status"]["running"] == 0
    assert metrics["by_status"]["unknown"] == 0
    assert metrics["oldest_scheduled_run_at"] == "2026-11-01T10:00:00+00:00"
    assert "id" not in metrics
    assert "lease_owner" not in metrics


def test_scheduler_metrics_report_overdue_lateness_and_lease_health(tmp_path):
    from datetime import datetime, timedelta, timezone

    store = SchedulerStore(tmp_path / "scheduler-latency.sqlite3")
    now = datetime.now(timezone.utc)
    overdue = store.create_schedule(
        "[research] Overdue", "late", now - timedelta(minutes=10), "latency-overdue"
    )
    running = store.create_schedule(
        "[research] Running", "active", now - timedelta(minutes=5), "latency-running"
    )
    claimed = store.claim_due("metrics-worker", now=now, lease_seconds=120, limit=1)
    assert claimed[0]["id"] == overdue["id"]
    # Move the claimed lease close to expiry while preserving its ownership.
    with store.connect() as con:
        con.execute(
            "UPDATE scheduled_tasks SET lease_expires_at=? WHERE id=?",
            ((now + timedelta(seconds=30)).isoformat(), overdue["id"]),
        )
        con.execute(
            "UPDATE scheduled_tasks SET status='scheduled',lease_owner=NULL,"
            "lease_expires_at=NULL WHERE id=?",
            (running["id"],),
        )
        con.execute(
            "UPDATE scheduled_tasks SET status='running',lease_owner='expired-worker',"
            "lease_expires_at=? WHERE id=?",
            ((now - timedelta(seconds=1)).isoformat(), running["id"]),
        )

    metrics = store.metrics()
    assert metrics["overdue_scheduled_count"] == 0
    assert metrics["running_with_expired_lease_count"] == 1
    assert metrics["leases_expiring_within_60_seconds"] == 0
    assert metrics["oldest_running_age_seconds"] >= 0
    assert "lease_owner" not in metrics
    assert "id" not in metrics
