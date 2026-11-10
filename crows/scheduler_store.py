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

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=10000")
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def create_schedule(
        self, title: str, input_text: str, run_at: str | datetime,
        idempotency_key: str,
    ) -> dict[str, Any]:
        title, input_text, idempotency_key = title.strip(), input_text.strip(), idempotency_key.strip()
        if not title or not input_text or not idempotency_key:
            raise ValueError("title, input_text, and idempotency_key must not be blank")
        if len(title) > 200 or len(input_text) > 20_000 or len(idempotency_key) > 256:
            raise ValueError("schedule field exceeds its maximum length")
        due = _utc(run_at)
        now, schedule_id = datetime.now(timezone.utc).isoformat(), str(uuid4())
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute(
                "SELECT * FROM scheduled_tasks WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if existing:
                if (existing["title"], existing["input_text"], existing["run_at"]) != (title, input_text, due):
                    raise ValueError("idempotency key already belongs to a different schedule")
                return dict(existing)
            con.execute(
                "INSERT INTO scheduled_tasks "
                "(id,idempotency_key,title,input_text,run_at,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'scheduled',?,?)",
                (schedule_id, idempotency_key, title, input_text, due, now, now),
            )
            row = con.execute("SELECT * FROM scheduled_tasks WHERE id=?", (schedule_id,)).fetchone()
            return dict(row)

    def get_schedule(self, schedule_id: str) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM scheduled_tasks WHERE id=?", (schedule_id,)).fetchone()
        return dict(row) if row else None

    def list_schedules(self, limit: int = 50) -> list[dict[str, Any]]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM scheduled_tasks ORDER BY run_at, created_at LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

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
                "SELECT id FROM scheduled_tasks WHERE status='scheduled' AND run_at<=? "
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
            cursor = con.execute(
                "UPDATE scheduled_tasks SET status=?,task_id=?,result_json=?,error_text=?,"
                "lease_owner=NULL,lease_expires_at=NULL,updated_at=? "
                "WHERE id=? AND status='running' AND lease_owner=? AND lease_expires_at>?",
                (status, task_id, result_json, error_text, instant, schedule_id, worker_id, instant),
            )
            return cursor.rowcount == 1

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
            total = con.execute("SELECT COUNT(*) AS count FROM scheduled_tasks").fetchone()["count"]
        counts = {status: 0 for status in sorted(SCHEDULE_STATUSES)}
        counts.update({row["status"]: row["count"] for row in rows})
        return {
            "total": total,
            "by_status": counts,
            "oldest_scheduled_run_at": oldest_scheduled,
            "oldest_running_updated_at": oldest_running,
            "oldest_unknown_updated_at": oldest_unknown,
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
