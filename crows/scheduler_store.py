"""Durable, opt-in scheduling primitives backed by SQLite.

This module only stores and claims scheduled work. It deliberately does not
execute tasks; callers must apply the agent's normal authorization and approval
policy before dispatching a claimed item.
"""
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4


SCHEDULE_STATUSES = {"scheduled", "running", "completed", "failed", "cancelled", "unknown"}


def _utc(value: str | datetime) -> str:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


class SchedulerStore:
    """SQLite schedule journal with atomic due claims and conservative recovery."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("""
                CREATE TABLE IF NOT EXISTS scheduled_tasks (
                  id TEXT PRIMARY KEY,
                  idempotency_key TEXT NOT NULL UNIQUE,
                  title TEXT NOT NULL,
                  input_text TEXT NOT NULL,
                  run_at TEXT NOT NULL,
                  status TEXT NOT NULL CHECK(status IN
                    ('scheduled','running','completed','failed','cancelled','unknown')),
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  lease_owner TEXT,
                  lease_expires_at TEXT,
                  task_id TEXT,
                  result_json TEXT,
                  error_text TEXT
                )
            """)
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_due "
                "ON scheduled_tasks(status, run_at)"
            )
            con.execute("""
                CREATE TABLE IF NOT EXISTS schedule_reconciliation_audit (
                  id TEXT PRIMARY KEY,
                  schedule_id TEXT NOT NULL REFERENCES scheduled_tasks(id),
                  note TEXT NOT NULL,
                  evidence TEXT NOT NULL,
                  created_at TEXT NOT NULL
                )
            """)
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_schedule_audit_schedule "
                "ON schedule_reconciliation_audit(schedule_id, created_at)"
            )
            columns = {
                row["name"] for row in con.execute("PRAGMA table_info(scheduled_tasks)")
            }
            if "retry_count" not in columns:
                con.execute(
                    "ALTER TABLE scheduled_tasks ADD COLUMN retry_count "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            for name, declaration in {
                "is_paused": "INTEGER NOT NULL DEFAULT 0",
                "recurrence_interval_seconds": "INTEGER",
                "max_occurrences": "INTEGER",
                "occurrence_count": "INTEGER NOT NULL DEFAULT 0",
            }.items():
                if name not in columns:
                    con.execute(f"ALTER TABLE scheduled_tasks ADD COLUMN {name} {declaration}")
            con.execute("""
                CREATE TABLE IF NOT EXISTS schedule_execution_history (
                  id TEXT PRIMARY KEY,
                  schedule_id TEXT NOT NULL REFERENCES scheduled_tasks(id),
                  occurrence_number INTEGER NOT NULL,
                  task_id TEXT,
                  status TEXT NOT NULL,
                  scheduled_for TEXT NOT NULL,
                  finished_at TEXT NOT NULL,
                  result_json TEXT,
                  error_text TEXT,
                  UNIQUE(schedule_id, occurrence_number)
                )
            """)
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_schedule_execution_history_schedule "
                "ON schedule_execution_history(schedule_id, occurrence_number)"
            )
            con.execute("""
                CREATE TABLE IF NOT EXISTS schedule_retry_audit (
                  id TEXT PRIMARY KEY,
                  schedule_id TEXT NOT NULL REFERENCES scheduled_tasks(id),
                  previous_task_id TEXT,
                  reason TEXT NOT NULL,
                  requested_run_at TEXT NOT NULL,
                  retry_number INTEGER NOT NULL,
                  created_at TEXT NOT NULL
                )
            """)
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_schedule_retry_audit_schedule "
                "ON schedule_retry_audit(schedule_id, created_at)"
            )

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=10000")
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def create_schedule(
        self, title: str, input_text: str, run_at: str | datetime,
        idempotency_key: str, *,
        recurrence_interval_seconds: int | None = None,
        max_occurrences: int | None = None,
    ) -> dict[str, Any]:
        title, input_text, idempotency_key = title.strip(), input_text.strip(), idempotency_key.strip()
        if not title or not input_text or not idempotency_key:
            raise ValueError("title, input_text, and idempotency_key must not be blank")
        if len(title) > 200 or len(input_text) > 20_000 or len(idempotency_key) > 256:
            raise ValueError("schedule field exceeds its maximum length")
        due = _utc(run_at)
        if (recurrence_interval_seconds is None) != (max_occurrences is None):
            raise ValueError("recurrence_interval_seconds and max_occurrences must be supplied together")
        if recurrence_interval_seconds is not None and not 60 <= recurrence_interval_seconds <= 31_536_000:
            raise ValueError("recurrence_interval_seconds must be between 60 and 31536000")
        if max_occurrences is not None and not 2 <= max_occurrences <= 1000:
            raise ValueError("max_occurrences must be between 2 and 1000")
        now, schedule_id = datetime.now(timezone.utc).isoformat(), str(uuid4())
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute(
                "SELECT * FROM scheduled_tasks WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if existing:
                if (
                    existing["title"], existing["input_text"], existing["run_at"],
                    existing["recurrence_interval_seconds"], existing["max_occurrences"],
                ) != (
                    title, input_text, due, recurrence_interval_seconds, max_occurrences,
                ):
                    raise ValueError("idempotency key already belongs to a different schedule")
                return dict(existing)
            con.execute(
                "INSERT INTO scheduled_tasks "
                "(id,idempotency_key,title,input_text,run_at,status,created_at,updated_at,"
                "recurrence_interval_seconds,max_occurrences) "
                "VALUES(?,?,?,?,?,'scheduled',?,?,?,?)",
                (schedule_id, idempotency_key, title, input_text, due, now, now,
                 recurrence_interval_seconds, max_occurrences),
            )
            row = con.execute("SELECT * FROM scheduled_tasks WHERE id=?", (schedule_id,)).fetchone()
            return dict(row)

    def get_schedule(self, schedule_id: str) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM scheduled_tasks WHERE id=?", (schedule_id,)).fetchone()
        return dict(row) if row else None

    def list_schedules(
        self, limit: int = 50, *, status: str | None = None,
        paused: bool | None = None, overdue_before: str | datetime | None = None,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        if status is not None and status not in SCHEDULE_STATUSES:
            raise ValueError("invalid schedule status")
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if paused is not None:
            clauses.append("is_paused=?")
            params.append(int(paused))
        if overdue_before is not None:
            clauses.extend(("status='scheduled'", "is_paused=0", "run_at<=?"))
            params.append(_utc(overdue_before))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM scheduled_tasks" + where +
                " ORDER BY run_at, created_at LIMIT ?", params
            ).fetchall()
        return [dict(row) for row in rows]

    def pause_schedule(self, schedule_id: str) -> dict[str, Any] | None:
        """Pause pending work without changing its due time or recurrence progress."""
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            cursor = con.execute(
                "UPDATE scheduled_tasks SET is_paused=1,updated_at=? "
                "WHERE id=? AND status='scheduled' AND is_paused=0",
                (now, schedule_id),
            )
            if cursor.rowcount:
                return dict(con.execute("SELECT * FROM scheduled_tasks WHERE id=?",
                                        (schedule_id,)).fetchone())
            exists = con.execute("SELECT 1 FROM scheduled_tasks WHERE id=?", (schedule_id,)).fetchone()
            if exists is None:
                raise KeyError("Schedule not found")
            return None

    def resume_schedule(self, schedule_id: str) -> dict[str, Any] | None:
        """Resume pending work using its original due time (overdue work runs promptly)."""
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            cursor = con.execute(
                "UPDATE scheduled_tasks SET is_paused=0,updated_at=? "
                "WHERE id=? AND status='scheduled' AND is_paused=1",
                (now, schedule_id),
            )
            if cursor.rowcount:
                return dict(con.execute("SELECT * FROM scheduled_tasks WHERE id=?",
                                        (schedule_id,)).fetchone())
            exists = con.execute("SELECT 1 FROM scheduled_tasks WHERE id=?", (schedule_id,)).fetchone()
            if exists is None:
                raise KeyError("Schedule not found")
            return None

    def cancel_schedule(self, schedule_id: str) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as con:
            cursor = con.execute(
                "UPDATE scheduled_tasks SET status='cancelled',updated_at=? "
                "WHERE id=? AND status='scheduled'",
                (now, schedule_id),
            )
            if cursor.rowcount:
                return True
            if con.execute("SELECT 1 FROM scheduled_tasks WHERE id=?", (schedule_id,)).fetchone() is None:
                raise KeyError("Schedule not found")
            return False

    def reschedule_schedule(self, schedule_id: str, run_at: str | datetime) -> dict[str, Any] | None:
        """Change the due time only while a schedule is still pending.

        Running or terminal work is never moved: callers must reconcile an
        uncertain execution rather than silently scheduling a second attempt.
        """
        due = _utc(run_at)
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            cursor = con.execute(
                "UPDATE scheduled_tasks SET run_at=?,updated_at=? "
                "WHERE id=? AND status='scheduled'",
                (due, now, schedule_id),
            )
            if cursor.rowcount:
                row = con.execute(
                    "SELECT * FROM scheduled_tasks WHERE id=?", (schedule_id,)
                ).fetchone()
                return dict(row)
            exists = con.execute(
                "SELECT 1 FROM scheduled_tasks WHERE id=?", (schedule_id,)
            ).fetchone()
            if exists is None:
                raise KeyError("Schedule not found")
            return None

    def retry_failed_schedule(
        self, schedule_id: str, run_at: str | datetime, reason: str, *, max_retries: int = 3
    ) -> dict[str, Any] | None:
        """Explicitly requeue a definitively failed schedule with an audit record.

        Unknown/running schedules are never eligible: uncertain side effects
        must be reconciled rather than replayed.
        """
        due = _utc(run_at)
        reason = reason.strip()
        if not reason or len(reason) > 2000:
            raise ValueError("retry reason is required and must be at most 2000 characters")
        if not 1 <= max_retries <= 10:
            raise ValueError("max_retries must be between 1 and 10")
        now, audit_id = datetime.now(timezone.utc).isoformat(), str(uuid4())
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT status,task_id,retry_count FROM scheduled_tasks WHERE id=?",
                (schedule_id,),
            ).fetchone()
            if row is None:
                raise KeyError("Schedule not found")
            if row["status"] != "failed":
                return None
            if int(row["retry_count"] or 0) >= max_retries:
                return None
            retry_number = int(row["retry_count"] or 0) + 1
            con.execute(
                "UPDATE scheduled_tasks SET status='scheduled',run_at=?,updated_at=?,"
                "lease_owner=NULL,lease_expires_at=NULL,task_id=NULL,result_json=NULL,"
                "error_text=NULL,retry_count=? WHERE id=? AND status='failed'",
                (due, now, retry_number, schedule_id),
            )
            con.execute(
                "INSERT INTO schedule_retry_audit "
                "(id,schedule_id,previous_task_id,reason,requested_run_at,retry_number,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (audit_id, schedule_id, row["task_id"], reason, due, retry_number, now),
            )
            saved = con.execute(
                "SELECT * FROM scheduled_tasks WHERE id=?", (schedule_id,)
            ).fetchone()
            return dict(saved)

    def list_retry_audit(self, schedule_id: str) -> list[dict[str, Any]]:
        with self.connect() as con:
            if con.execute(
                "SELECT 1 FROM scheduled_tasks WHERE id=?", (schedule_id,)
            ).fetchone() is None:
                raise KeyError("Schedule not found")
            rows = con.execute(
                "SELECT id,schedule_id,previous_task_id,reason,requested_run_at,"
                "retry_number,created_at FROM schedule_retry_audit "
                "WHERE schedule_id=? ORDER BY created_at,id",
                (schedule_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def claim_due(self, worker_id: str, *, now: str | datetime | None = None,
                  lease_seconds: int = 60, limit: int = 10) -> list[dict[str, Any]]:
        worker_id = worker_id.strip()
        if not worker_id or len(worker_id) > 200:
            raise ValueError("worker_id must be non-blank and at most 200 characters")
        if not 1 <= lease_seconds <= 3600 or not 1 <= limit <= 100:
            raise ValueError("lease_seconds or limit is outside its allowed range")
        instant = _utc(now or datetime.now(timezone.utc))
        expires = (datetime.fromisoformat(instant) + timedelta(seconds=lease_seconds)).isoformat()
        claimed = []
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute(
                "SELECT id FROM scheduled_tasks WHERE status='scheduled' AND is_paused=0 AND run_at<=? "
                "ORDER BY run_at,created_at LIMIT ?", (instant, limit)
            ).fetchall()
            for row in rows:
                cursor = con.execute(
                    "UPDATE scheduled_tasks SET status='running',lease_owner=?,"
                    "lease_expires_at=?,updated_at=? WHERE id=? AND status='scheduled'",
                    (worker_id, expires, instant, row["id"]),
                )
                if cursor.rowcount:
                    claimed_row = con.execute(
                        "SELECT * FROM scheduled_tasks WHERE id=?", (row["id"],)
                    ).fetchone()
                    claimed.append(dict(claimed_row))
        return claimed

    def attach_task_to_claim(
        self, schedule_id: str, worker_id: str, task_id: str, *,
        now: str | datetime | None = None,
    ) -> bool:
        """Durably link a created task before execution can produce side effects."""
        task_id = task_id.strip()
        if not task_id or len(task_id) > 200:
            raise ValueError("task_id must be non-blank and at most 200 characters")
        instant = _utc(now or datetime.now(timezone.utc))
        with self.connect() as con:
            cursor = con.execute(
                "UPDATE scheduled_tasks SET task_id=?,updated_at=? "
                "WHERE id=? AND status='running' AND lease_owner=? "
                "AND lease_expires_at>? AND task_id IS NULL",
                (task_id, instant, schedule_id, worker_id, instant),
            )
            return cursor.rowcount == 1

    def renew_claim(self, schedule_id: str, worker_id: str, *,
                    lease_seconds: int = 300,
                    now: str | datetime | None = None) -> bool:
        """Extend an active lease only while its current owner still holds it."""
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        instant = _utc(now or datetime.now(timezone.utc))
        expires = (datetime.fromisoformat(instant) + timedelta(seconds=lease_seconds)).isoformat()
        with self.connect() as con:
            cursor = con.execute(
                "UPDATE scheduled_tasks SET lease_expires_at=?,updated_at=? "
                "WHERE id=? AND status='running' AND lease_owner=? AND lease_expires_at>?",
                (expires, instant, schedule_id, worker_id, instant),
            )
            return cursor.rowcount == 1

    def finish_claim(self, schedule_id: str, worker_id: str, status: str, *,
                     task_id: str | None = None, result_json: str | None = None,
                     error_text: str | None = None,
                     now: str | datetime | None = None) -> bool:
        if status not in {"completed", "failed", "unknown"}:
            raise ValueError("invalid terminal schedule status")
        instant = _utc(now or datetime.now(timezone.utc))
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT run_at,recurrence_interval_seconds,max_occurrences,occurrence_count,task_id "
                "FROM scheduled_tasks WHERE id=? AND status='running' AND lease_owner=? "
                "AND lease_expires_at>?", (schedule_id, worker_id, instant),
            ).fetchone()
            if row is None:
                return False
            linked_task_id = task_id or row["task_id"]
            interval, maximum = row["recurrence_interval_seconds"], row["max_occurrences"]
            occurrence = int(row["occurrence_count"] or 0) + 1
            if interval is not None:
                con.execute(
                    "INSERT INTO schedule_execution_history "
                    "(id,schedule_id,occurrence_number,task_id,status,scheduled_for,"
                    "finished_at,result_json,error_text) VALUES(?,?,?,?,?,?,?,?,?)",
                    (str(uuid4()), schedule_id, occurrence, linked_task_id, status,
                     row["run_at"], instant, result_json, error_text),
                )
            if status == "completed" and interval is not None and occurrence < int(maximum):
                next_run = datetime.fromisoformat(row["run_at"]) + timedelta(seconds=int(interval))
                finished = datetime.fromisoformat(instant)
                while next_run <= finished:
                    next_run += timedelta(seconds=int(interval))
                cursor = con.execute(
                    "UPDATE scheduled_tasks SET status='scheduled',run_at=?,task_id=NULL,"
                    "result_json=NULL,error_text=NULL,lease_owner=NULL,lease_expires_at=NULL,"
                    "occurrence_count=?,updated_at=? WHERE id=? AND status='running' "
                    "AND lease_owner=? AND lease_expires_at>?",
                    (next_run.isoformat(), occurrence, instant, schedule_id, worker_id, instant),
                )
            else:
                cursor = con.execute(
                    "UPDATE scheduled_tasks SET status=?,task_id=COALESCE(?,task_id),"
                    "result_json=?,error_text=?,lease_owner=NULL,lease_expires_at=NULL,"
                    "occurrence_count=CASE WHEN recurrence_interval_seconds IS NOT NULL "
                    "THEN ? ELSE occurrence_count END,updated_at=? "
                    "WHERE id=? AND status='running' AND lease_owner=? AND lease_expires_at>?",
                    (status, linked_task_id, result_json, error_text,
                     occurrence if interval is not None else 0, instant,
                     schedule_id, worker_id, instant),
                )
            return cursor.rowcount == 1

    def list_execution_history(self, schedule_id: str) -> list[dict[str, Any]]:
        with self.connect() as con:
            if con.execute("SELECT 1 FROM scheduled_tasks WHERE id=?", (schedule_id,)).fetchone() is None:
                raise KeyError("Schedule not found")
            rows = con.execute(
                "SELECT occurrence_number,task_id,status,scheduled_for,finished_at,"
                "result_json,error_text FROM schedule_execution_history "
                "WHERE schedule_id=? ORDER BY occurrence_number", (schedule_id,)
            ).fetchall()
            return [dict(row) for row in rows]

    def record_reconciliation(self, schedule_id: str, note: str, evidence: str) -> dict[str, Any]:
        note, evidence = note.strip(), evidence.strip()
        if not note or len(note) > 2000 or not evidence or len(evidence) > 4000:
            raise ValueError("note and evidence are required and must fit their size limits")
        now = datetime.now(timezone.utc).isoformat()
        audit_id = str(uuid4())
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT status FROM scheduled_tasks WHERE id=?", (schedule_id,)).fetchone()
            if row is None:
                raise KeyError("Schedule not found")
            if row["status"] != "unknown":
                raise ValueError("only unknown schedules can be reconciled")
            con.execute(
                "INSERT INTO schedule_reconciliation_audit "
                "(id,schedule_id,note,evidence,created_at) VALUES(?,?,?,?,?)",
                (audit_id, schedule_id, note, evidence, now),
            )
            saved = con.execute(
                "SELECT id,schedule_id,note,evidence,created_at "
                "FROM schedule_reconciliation_audit WHERE id=?", (audit_id,)
            ).fetchone()
            return dict(saved)

    def list_reconciliation_audit(self, schedule_id: str) -> list[dict[str, Any]]:
        with self.connect() as con:
            if con.execute("SELECT 1 FROM scheduled_tasks WHERE id=?", (schedule_id,)).fetchone() is None:
                raise KeyError("Schedule not found")
            rows = con.execute(
                "SELECT id,schedule_id,note,evidence,created_at "
                "FROM schedule_reconciliation_audit WHERE schedule_id=? ORDER BY created_at,id",
                (schedule_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def metrics(self) -> dict[str, Any]:
        """Return aggregate scheduler health without exposing schedule identifiers."""
        with self.connect() as con:
            rows = con.execute(
                "SELECT status, COUNT(*) AS count FROM scheduled_tasks GROUP BY status"
            ).fetchall()
            oldest_scheduled = con.execute(
                "SELECT MIN(run_at) AS value FROM scheduled_tasks WHERE status='scheduled'"
            ).fetchone()["value"]
            oldest_running = con.execute(
                "SELECT MIN(updated_at) AS value FROM scheduled_tasks WHERE status='running'"
            ).fetchone()["value"]
            oldest_unknown = con.execute(
                "SELECT MIN(updated_at) AS value FROM scheduled_tasks WHERE status='unknown'"
            ).fetchone()["value"]
            now = datetime.now(timezone.utc)
            lease_health = con.execute(
                "SELECT "
                "SUM(CASE WHEN lease_expires_at IS NOT NULL AND lease_expires_at<=? "
                "THEN 1 ELSE 0 END) AS expired, "
                "SUM(CASE WHEN lease_expires_at>? AND lease_expires_at<=? "
                "THEN 1 ELSE 0 END) AS expiring "
                "FROM scheduled_tasks WHERE status='running'",
                (now.isoformat(), now.isoformat(),
                 (now + timedelta(seconds=60)).isoformat()),
            ).fetchone()
            scheduled_rows = con.execute(
                "SELECT run_at FROM scheduled_tasks "
                "WHERE status='scheduled' AND is_paused=0 AND run_at<=?",
                (now.isoformat(),),
            ).fetchall()
            paused_overdue = con.execute(
                "SELECT COUNT(*) AS count FROM scheduled_tasks "
                "WHERE status='scheduled' AND is_paused=1 AND run_at<=?",
                (now.isoformat(),),
            ).fetchone()["count"]
            paused_total = con.execute(
                "SELECT COUNT(*) AS count FROM scheduled_tasks WHERE is_paused=1"
            ).fetchone()["count"]
            running_rows = con.execute(
                "SELECT updated_at FROM scheduled_tasks WHERE status='running'"
            ).fetchall()
            total = con.execute("SELECT COUNT(*) AS count FROM scheduled_tasks").fetchone()["count"]
        counts = {status: 0 for status in sorted(SCHEDULE_STATUSES)}
        counts.update({row["status"]: row["count"] for row in rows})
        lateness = sorted(
            max(0, int((now - datetime.fromisoformat(row["run_at"])).total_seconds()))
            for row in scheduled_rows
        )
        running_age = sorted(
            max(0, int((now - datetime.fromisoformat(row["updated_at"])).total_seconds()))
            for row in running_rows
        )
        return {
            "total": total,
            "by_status": counts,
            "oldest_scheduled_run_at": oldest_scheduled,
            "oldest_running_updated_at": oldest_running,
            "oldest_unknown_updated_at": oldest_unknown,
            "paused_schedule_count": int(paused_total),
            "paused_overdue_count": int(paused_overdue),
            "overdue_scheduled_count": len(lateness),
            "overdue_lateness_p50_seconds": (
                lateness[max(0, (len(lateness) + 1) // 2 - 1)] if lateness else 0
            ),
            "overdue_lateness_p95_seconds": (
                lateness[max(0, (len(lateness) * 95 + 99) // 100 - 1)] if lateness else 0
            ),
            "oldest_running_age_seconds": running_age[-1] if running_age else 0,
            "running_with_expired_lease_count": int(lease_health["expired"] or 0),
            "leases_expiring_within_60_seconds": int(lease_health["expiring"] or 0),
        }

    def mark_expired_claims_unknown(self, *, now: str | datetime | None = None) -> int:
        instant = _utc(now or datetime.now(timezone.utc))
        with self.connect() as con:
            cursor = con.execute(
                "UPDATE scheduled_tasks SET status='unknown',lease_owner=NULL,"
                "lease_expires_at=NULL,updated_at=?,"
                "error_text=COALESCE(error_text,'Worker lease expired; inspect task journal before retrying') "
                "WHERE status='running' AND lease_expires_at<=?",
                (instant, instant),
            )
            return cursor.rowcount
