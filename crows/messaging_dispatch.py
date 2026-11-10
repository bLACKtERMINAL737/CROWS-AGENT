"""Durable, lease-based dispatch journal for verified inbound channel messages.

The journal deliberately does not execute messages itself. A runtime worker must
claim a row, apply its channel policy, and explicitly finish the lease. Expired
leases become unknown and are never replayed automatically.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class InboundDispatchError(ValueError):
    """Invalid inbound dispatch operation."""


class InboundDispatchStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS messaging_inbound_dispatch (
                    id TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL UNIQUE
                        REFERENCES messaging_messages(id) ON DELETE CASCADE,
                    conversation_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN
                        ('pending','processing','completed','failed','unknown')),
                    lease_owner TEXT,
                    lease_until TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                    diagnostic TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_inbound_dispatch_claim
                    ON messaging_inbound_dispatch(state, created_at, id);
                CREATE INDEX IF NOT EXISTS idx_inbound_dispatch_session
                    ON messaging_inbound_dispatch(channel, conversation_id, created_at);
            """)

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=10000")
        return con

    def enqueue(self, *, message_id: str, conversation_id: str,
                channel: str, session_id: str) -> dict:
        values = (message_id, conversation_id, channel, session_id)
        if any(not isinstance(v, str) or not v.strip() or len(v) > 256 for v in values):
            raise InboundDispatchError("dispatch identifiers are invalid")
        now = _now()
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute(
                "SELECT * FROM messaging_inbound_dispatch WHERE message_id=?",
                (message_id,),
            ).fetchone()
            if existing:
                row = dict(existing)
                if (row["conversation_id"], row["channel"], row["session_id"]) != (
                    conversation_id, channel, session_id
                ):
                    raise InboundDispatchError("message is already queued for a different route")
                return {**row, "duplicate": True}
            dispatch_id = str(uuid4())
            con.execute(
                """INSERT INTO messaging_inbound_dispatch
                   (id,message_id,conversation_id,channel,session_id,state,created_at,updated_at)
                   VALUES(?,?,?,?,?,'pending',?,?)""",
                (dispatch_id, message_id, conversation_id, channel, session_id, now, now),
            )
            row = con.execute(
                "SELECT * FROM messaging_inbound_dispatch WHERE id=?", (dispatch_id,)
            ).fetchone()
        return {**dict(row), "duplicate": False}

    def claim(self, *, worker_id: str, limit: int = 10,
              lease_seconds: int = 120) -> list[dict]:
        if not isinstance(worker_id, str) or not worker_id.strip() or len(worker_id) > 128:
            raise InboundDispatchError("worker_id is invalid")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise InboundDispatchError("limit must be between 1 and 100")
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 5 <= lease_seconds <= 900:
            raise InboundDispatchError("lease_seconds must be between 5 and 900")
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        lease_until = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        claimed = []
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute(
                """SELECT * FROM messaging_inbound_dispatch AS candidate
                   WHERE candidate.state='pending'
                     AND NOT EXISTS (
                         SELECT 1 FROM messaging_inbound_dispatch AS active
                         WHERE active.channel=candidate.channel
                           AND active.conversation_id=candidate.conversation_id
                           AND active.state='processing'
                     )
                     AND NOT EXISTS (
                         SELECT 1 FROM messaging_inbound_dispatch AS earlier
                         WHERE earlier.channel=candidate.channel
                           AND earlier.conversation_id=candidate.conversation_id
                           AND earlier.state='pending'
                           AND (earlier.created_at < candidate.created_at
                                OR (earlier.created_at = candidate.created_at
                                    AND earlier.id < candidate.id))
                     )
                   ORDER BY candidate.created_at,candidate.id LIMIT ?""",
                (limit,),
            ).fetchall()
            for row in rows:
                con.execute(
                    """UPDATE messaging_inbound_dispatch
                       SET state='processing', lease_owner=?, lease_until=?,
                           attempts=attempts+1, updated_at=?
                       WHERE id=? AND state='pending'""",
                    (worker_id.strip(), lease_until, now, row["id"]),
                )
                if con.execute("SELECT changes()").fetchone()[0] == 1:
                    updated = con.execute(
                        "SELECT * FROM messaging_inbound_dispatch WHERE id=?", (row["id"],)
                    ).fetchone()
                    claimed.append(dict(updated))
        return claimed

    def renew(self, dispatch_id: str, *, worker_id: str,
              lease_seconds: int = 120) -> bool:
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 5 <= lease_seconds <= 900:
            raise InboundDispatchError("lease_seconds must be between 5 and 900")
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        lease_until = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        with self.connect() as con:
            con.execute(
                """UPDATE messaging_inbound_dispatch
                   SET lease_until=?,updated_at=?
                   WHERE id=? AND state='processing' AND lease_owner=?
                     AND lease_until >= ?""",
                (lease_until, now, dispatch_id, worker_id, now),
            )
            return con.execute("SELECT changes()").fetchone()[0] == 1

    def finish(self, dispatch_id: str, *, worker_id: str, state: str,
               diagnostic: str | None = None) -> bool:
        if state not in {"completed", "failed", "unknown"}:
            raise InboundDispatchError("terminal dispatch state is invalid")
        if diagnostic is not None:
            if not isinstance(diagnostic, str) or len(diagnostic) > 1000:
                raise InboundDispatchError("diagnostic is invalid")
            diagnostic = diagnostic.strip() or None
        now = _now()
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                """SELECT lease_owner,lease_until FROM messaging_inbound_dispatch
                   WHERE id=? AND state='processing'""",
                (dispatch_id,),
            ).fetchone()
            if (row is None or row["lease_owner"] != worker_id
                    or not row["lease_until"] or row["lease_until"] < now):
                return False
            con.execute(
                """UPDATE messaging_inbound_dispatch
                   SET state=?,lease_owner=NULL,lease_until=NULL,
                       diagnostic=?,updated_at=?
                   WHERE id=? AND state='processing' AND lease_owner=?""",
                (state, diagnostic, now, dispatch_id, worker_id),
            )
            return con.execute("SELECT changes()").fetchone()[0] == 1

    def expire_leases(self) -> int:
        now = _now()
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                """UPDATE messaging_inbound_dispatch
                   SET state='unknown',lease_owner=NULL,lease_until=NULL,
                       diagnostic='Worker lease expired; inspect runtime and session state before retrying',
                       updated_at=?
                   WHERE state='processing' AND lease_until < ?""",
                (now, now),
            )
            return con.execute("SELECT changes()").fetchone()[0]

    def get(self, dispatch_id: str) -> dict | None:
        with self.connect() as con:
            row = con.execute(
                "SELECT * FROM messaging_inbound_dispatch WHERE id=?", (dispatch_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_recent(self, limit: int = 50) -> list[dict]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise InboundDispatchError("limit must be between 1 and 200")
        with self.connect() as con:
            rows = con.execute(
                """SELECT id,channel,state,attempts,created_at,updated_at
                   FROM messaging_inbound_dispatch ORDER BY created_at DESC,id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
