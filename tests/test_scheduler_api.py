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
