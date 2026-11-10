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
