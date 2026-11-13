from fastapi.testclient import TestClient

import crows.app as app_module
from crows.scheduler_store import SchedulerStore


def make_client(monkeypatch, tmp_path):
    monkeypatch.setenv("AETHER_API_TOKEN", "test-token")
    monkeypatch.setattr(app_module, "scheduler_store", SchedulerStore(tmp_path / "scheduler.sqlite3"))
    return TestClient(app_module.app, raise_server_exceptions=False)


def auth():
    return {"X-Aether-Token": "test-token"}


def payload(**overrides):
    value = {
        "title": "Prepare weekly report",
        "input": "Summarize the project updates",
        "run_at": "2026-11-01T10:00:00+00:00",
        "idempotency_key": "weekly-report-1",
    }
    value.update(overrides)
    return value


def test_create_and_read_schedule_hides_internal_lease_and_idempotency_fields(monkeypatch, tmp_path):
    client = make_client(monkeypatch, tmp_path)
    created = client.post("/scheduler/schedules", json=payload(), headers=auth())
    assert created.status_code == 201
    item = created.json()
    assert item["status"] == "scheduled"
    assert item["title"] == "Prepare weekly report"
    assert "idempotency_key" not in item
    assert "lease_owner" not in item

    fetched = client.get(f"/scheduler/schedules/{item['id']}", headers=auth())
    assert fetched.status_code == 200
    assert fetched.json()["id"] == item["id"]


def test_create_is_idempotent_and_conflicting_key_is_rejected(monkeypatch, tmp_path):
    client = make_client(monkeypatch, tmp_path)
    first = client.post("/scheduler/schedules", json=payload(), headers=auth())
    second = client.post("/scheduler/schedules", json=payload(), headers=auth())
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]

    conflict = client.post(
        "/scheduler/schedules",
        json=payload(title="Different schedule"),
        headers=auth(),
    )
    assert conflict.status_code == 422


def test_schedule_requires_timezone_aware_run_at(monkeypatch, tmp_path):
    client = make_client(monkeypatch, tmp_path)
    response = client.post(
        "/scheduler/schedules",
        json=payload(run_at="2026-11-01T10:00:00"),
        headers=auth(),
    )
    assert response.status_code == 422


def test_list_limit_and_missing_schedule_errors(monkeypatch, tmp_path):
    client = make_client(monkeypatch, tmp_path)
    assert client.get("/scheduler/schedules?limit=201", headers=auth()).status_code == 400
    assert client.get("/scheduler/schedules/missing", headers=auth()).status_code == 404
    assert client.get("/scheduler/schedules", headers=auth()).json() == {"schedules": []}


def test_cancel_pending_schedule_and_reject_second_cancel(monkeypatch, tmp_path):
    client = make_client(monkeypatch, tmp_path)
    created = client.post("/scheduler/schedules", json=payload(), headers=auth())
    schedule_id = created.json()["id"]

    cancelled = client.delete(f"/scheduler/schedules/{schedule_id}", headers=auth())
    assert cancelled.status_code == 200
    assert cancelled.json() == {"id": schedule_id, "status": "cancelled"}
    assert client.delete(f"/scheduler/schedules/{schedule_id}", headers=auth()).status_code == 409
    assert client.delete("/scheduler/schedules/missing", headers=auth()).status_code == 404


def test_scheduler_routes_require_api_token(monkeypatch, tmp_path):
    client = make_client(monkeypatch, tmp_path)
    response = client.get("/scheduler/schedules")
    assert response.status_code == 401

def test_unknown_schedule_reconciliation_audit_api(monkeypatch, tmp_path):
    client = make_client(monkeypatch, tmp_path)
    created = client.post("/scheduler/schedules", json=payload(), headers=auth())
    schedule_id = created.json()["id"]
    scheduler = app_module.scheduler_store
    scheduler.claim_due("test-worker", now="2026-11-01T10:01:00+00:00", lease_seconds=1)
    scheduler.mark_expired_claims_unknown(now="2026-11-01T10:01:02+00:00")

    response = client.post(
        f"/scheduler/schedules/{schedule_id}/reconcile",
        json={"note": "Reviewed task journal", "evidence": "No confirmed completion found."},
        headers=auth(),
    )
    assert response.status_code == 200
    assert response.json()["schedule_id"] == schedule_id
    assert client.get(
        f"/scheduler/schedules/{schedule_id}/audit", headers=auth()
    ).json()["entries"][0]["note"] == "Reviewed task journal"
    assert scheduler.get_schedule(schedule_id)["status"] == "unknown"

    duplicate = client.post(
        f"/scheduler/schedules/{schedule_id}/reconcile",
        json={"note": "Again", "evidence": "Still uncertain."},
        headers=auth(),
    )
    assert duplicate.status_code == 200
    assert len(client.get(
        f"/scheduler/schedules/{schedule_id}/audit", headers=auth()
    ).json()["entries"]) == 2


def test_reconciliation_api_rejects_pending_schedule_and_requires_auth(monkeypatch, tmp_path):
    client = make_client(monkeypatch, tmp_path)
    created = client.post("/scheduler/schedules", json=payload(), headers=auth())
    schedule_id = created.json()["id"]
    body = {"note": "Checked", "evidence": "Not due yet"}
    assert client.post(
        f"/scheduler/schedules/{schedule_id}/reconcile", json=body, headers=auth()
    ).status_code == 409
    assert client.post(
        f"/scheduler/schedules/{schedule_id}/reconcile", json=body
    ).status_code == 401


def test_reschedule_pending_schedule_normalizes_time_and_preserves_identity(monkeypatch, tmp_path):
    client = make_client(monkeypatch, tmp_path)
    created = client.post("/scheduler/schedules", json=payload(), headers=auth())
    schedule_id = created.json()["id"]

    response = client.patch(
        f"/scheduler/schedules/{schedule_id}",
        json={"run_at": "2026-11-02T12:00:00+02:00"},
        headers=auth(),
    )

    assert response.status_code == 200
    assert response.json()["id"] == schedule_id
    assert response.json()["run_at"] == "2026-11-02T10:00:00+00:00"
    assert response.json()["status"] == "scheduled"
    assert "idempotency_key" not in response.json()


def test_reschedule_rejects_running_terminal_missing_and_naive_times(monkeypatch, tmp_path):
    client = make_client(monkeypatch, tmp_path)
    created = client.post("/scheduler/schedules", json=payload(), headers=auth())
    schedule_id = created.json()["id"]

    naive = client.patch(
        f"/scheduler/schedules/{schedule_id}",
        json={"run_at": "2026-11-02T12:00:00"},
        headers=auth(),
    )
    assert naive.status_code == 422

    scheduler = app_module.scheduler_store
    scheduler.claim_due("reschedule-worker", now="2026-11-01T10:01:00+00:00")
    running = client.patch(
        f"/scheduler/schedules/{schedule_id}",
        json={"run_at": "2026-11-03T10:00:00+00:00"},
        headers=auth(),
    )
    assert running.status_code == 409

    missing = client.patch(
        "/scheduler/schedules/missing",
        json={"run_at": "2026-11-03T10:00:00+00:00"},
        headers=auth(),
    )
    assert missing.status_code == 404


def test_reschedule_requires_authentication(monkeypatch, tmp_path):
    client = make_client(monkeypatch, tmp_path)
    response = client.patch(
        "/scheduler/schedules/missing",
        json={"run_at": "2026-11-03T10:00:00+00:00"},
    )
    assert response.status_code == 401


def test_failed_schedule_retry_api_is_audited_and_unknown_is_blocked(monkeypatch, tmp_path):
    client = make_client(monkeypatch, tmp_path)
    created = client.post("/scheduler/schedules", json=payload(), headers=auth())
    schedule_id = created.json()["id"]
    scheduler = app_module.scheduler_store
    scheduler.claim_due("retry-worker", now="2026-11-01T10:01:00+00:00", lease_seconds=300)
    assert scheduler.finish_claim(
        schedule_id, "retry-worker", "failed", task_id="task-failed-1",
        error_text="Definitive task failure",
        now="2026-11-01T10:01:30+00:00",
    )

    response = client.post(
        f"/scheduler/schedules/{schedule_id}/retry",
        json={"run_at": "2026-11-03T12:00:00+02:00", "reason": "Reviewed failed task journal"},
        headers=auth(),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "scheduled"
    assert response.json()["retry_count"] == 1
    audit = client.get(
        f"/scheduler/schedules/{schedule_id}/retry-audit", headers=auth()
    )
    assert audit.status_code == 200
    assert audit.json()["entries"][0]["previous_task_id"] == "task-failed-1"

    scheduler.claim_due("retry-worker", now="2026-11-04T10:00:00+00:00", lease_seconds=1)
    scheduler.mark_expired_claims_unknown(now="2026-11-04T10:00:02+00:00")
    blocked = client.post(
        f"/scheduler/schedules/{schedule_id}/retry",
        json={"run_at": "2026-11-05T10:00:00+00:00", "reason": "must not replay"},
        headers=auth(),
    )
    assert blocked.status_code == 409


def test_failed_schedule_retry_requires_authentication(monkeypatch, tmp_path):
    client = make_client(monkeypatch, tmp_path)
    response = client.post(
        "/scheduler/schedules/missing/retry",
        json={"run_at": "2026-11-03T10:00:00+00:00", "reason": "reviewed"},
    )
    assert response.status_code == 401


def test_retry_api_enforces_bounded_attempt_budget(monkeypatch, tmp_path):
    client = make_client(monkeypatch, tmp_path)
    created = client.post("/scheduler/schedules", json=payload(), headers=auth())
    schedule_id = created.json()["id"]
    response = client.post(
        f"/scheduler/schedules/{schedule_id}/retry",
        json={
            "run_at": "2026-11-03T10:00:00+00:00",
            "reason": "reviewed",
            "max_retries": 11,
        },
        headers=auth(),
    )
    assert response.status_code == 422



def test_recurring_schedule_requeues_after_success_and_skips_missed_ticks(tmp_path):
    store = SchedulerStore(tmp_path / "recurring.sqlite3")
    item = store.create_schedule(
        "Recurring report", "Run report", "2026-01-01T00:00:00+00:00",
        "recurring-report", recurrence_interval_seconds=3600, max_occurrences=3,
    )
    claimed = store.claim_due("worker-a", now="2026-01-01T00:01:00+00:00", lease_seconds=300)
    assert len(claimed) == 1
    assert store.attach_task_to_claim(item["id"], "worker-a", "task-1",
                                      now="2026-01-01T00:01:01+00:00")
    assert store.finish_claim(
        item["id"], "worker-a", "completed", result_json='{"ok":true}',
        now="2026-01-01T02:30:00+00:00",
    )
    saved = store.get_schedule(item["id"])
    assert saved["status"] == "scheduled"
    assert saved["occurrence_count"] == 1
    assert saved["task_id"] is None
    assert saved["run_at"] == "2026-01-01T03:00:00+00:00"
    history = store.list_execution_history(item["id"])
    assert len(history) == 1 and history[0]["task_id"] == "task-1"


def test_recurring_schedule_stops_at_occurrence_limit_and_preserves_history(tmp_path):
    store = SchedulerStore(tmp_path / "recurring-limit.sqlite3")
    item = store.create_schedule(
        "Recurring report", "Run report", "2026-01-01T00:00:00+00:00",
        "recurring-limit", recurrence_interval_seconds=3600, max_occurrences=2,
    )
    store.claim_due("worker-a", now="2026-01-01T00:01:00+00:00", lease_seconds=300)
    assert store.finish_claim(item["id"], "worker-a", "completed", task_id="task-1",
                              now="2026-01-01T00:02:00+00:00")
    store.claim_due("worker-b", now="2026-01-01T01:01:00+00:00", lease_seconds=300)
    assert store.finish_claim(item["id"], "worker-b", "completed", task_id="task-2",
                              now="2026-01-01T01:02:00+00:00")
    saved = store.get_schedule(item["id"])
    assert saved["status"] == "completed" and saved["occurrence_count"] == 2
    assert len(store.list_execution_history(item["id"])) == 2


def test_recurring_schedule_requires_both_fields_and_bounded_values(tmp_path):
    store = SchedulerStore(tmp_path / "recurring-validation.sqlite3")
    with pytest.raises(ValueError, match="supplied together"):
        store.create_schedule("Recurring", "Run", "2026-01-01T00:00:00+00:00", "bad-one",
                              recurrence_interval_seconds=3600)
    with pytest.raises(ValueError, match="between 60"):
        store.create_schedule("Recurring", "Run", "2026-01-01T00:00:00+00:00", "bad-two",
                              recurrence_interval_seconds=30, max_occurrences=2)
    with pytest.raises(ValueError, match="between 2"):
        store.create_schedule("Recurring", "Run", "2026-01-01T00:00:00+00:00", "bad-three",
                              recurrence_interval_seconds=3600, max_occurrences=1)



def test_recurring_schedule_api_exposes_bounded_configuration_and_history(monkeypatch, tmp_path):
    client = make_client(monkeypatch, tmp_path)
    created = client.post("/scheduler/schedules",
        json=payload(recurrence_interval_seconds=3600, max_occurrences=4), headers=auth())
    assert created.status_code == 201
    item = created.json()
    assert item["recurrence_interval_seconds"] == 3600
    assert item["max_occurrences"] == 4
    assert item["occurrence_count"] == 0
    history = client.get(f"/scheduler/schedules/{item['id']}/execution-history", headers=auth())
    assert history.status_code == 200 and history.json() == {"entries": []}


def test_recurring_schedule_api_rejects_partial_or_unbounded_configuration(monkeypatch, tmp_path):
    client = make_client(monkeypatch, tmp_path)
    partial = client.post("/scheduler/schedules",
        json=payload(recurrence_interval_seconds=3600), headers=auth())
    assert partial.status_code == 422
    too_many = client.post("/scheduler/schedules",
        json=payload(recurrence_interval_seconds=3600, max_occurrences=1001), headers=auth())
    assert too_many.status_code == 422
