import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .file_lock import file_lock
from .memory_search import MEMORY_SEARCH_RECALL_SQL, MEMORY_SEARCH_SQL

STATUSES = {"queued", "running", "succeeded", "failed", "unknown"}

# Bound startup memory while rebuilding lexical indexes for existing databases.
_SEARCH_INDEX_BACKFILL_BATCH_SIZE = 500


class IdempotencyConflictError(ValueError):
    """Raised when a key is reused for a different workflow request."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _memory_search_tokens(*values: str) -> set[str]:
    """Return normalized whole-word tokens using the same Unicode rules as search."""
    return {
        token.casefold()
        for value in values
        for token in re.findall(r"\w+", value, flags=re.UNICODE)
        if token
    }


class TaskStore:
    """Durable task journal; never blindly replay work with uncertain outcomes."""
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._migrate_coding_proposals()

    @contextmanager
    def coding_apply_lock(self, blocking: bool = True):
        """Coordinate coding apply and startup recovery across local OS processes.

        The lock is released by the OS if a process exits or crashes. This is
        intended for local filesystems; do not place the database on a network
        filesystem.
        """
        lock_path = self.db_path.with_name(self.db_path.name + ".coding-apply.lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            with file_lock(fd, blocking=blocking) as acquired:
                yield acquired
        finally:
            os.close(fd)

    @contextmanager
    def runtime_process_lock(self, blocking: bool = True):
        """Own this database for one FastAPI server process lifetime.

        A second server process using the same SQLite database must fail
        startup rather than reconcile another process's live task journal.
        The OS releases this lock after a crash. This is intended for local
        filesystems; do not place the database on a network filesystem.
        """
        lock_path = self.db_path.with_name(self.db_path.name + ".runtime.lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            with file_lock(fd, blocking=blocking) as acquired:
                yield acquired
        finally:
            os.close(fd)

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=10)
        con.row_factory = sqlite3.Row
        # SQLite's built-in lower() is ASCII-only unless built with ICU.
        # Register Python casefold so cross-session search handles Unicode
        # consistently with the ranking logic below.
        con.create_function(
            "crows_casefold",
            1,
            lambda value: value.casefold() if isinstance(value, str) else "",
            deterministic=True,
        )
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=10000")
        return con

    def _init_db(self) -> None:
        with self.connect() as con:
            # SQLite may return SQLITE_BUSY immediately for journal_mode=WAL
            # when multiple Windows threads construct stores simultaneously.
            # Retry this one-time database mode negotiation; normal queries
            # continue to use SQLite's configured busy_timeout.
            deadline = time.monotonic() + 15.0
            while True:
                try:
                    con.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
            con.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
              id TEXT PRIMARY KEY, title TEXT NOT NULL, input_text TEXT NOT NULL,
              status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed','unknown')),
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL, result_json TEXT, error_text TEXT
            );
            CREATE TABLE IF NOT EXISTS tool_executions (
              id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), tool_name TEXT NOT NULL,
              input_json TEXT NOT NULL,
              status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed','unknown')),
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL, result_json TEXT, error_text TEXT
            );
            CREATE TABLE IF NOT EXISTS coding_proposals (
              id TEXT PRIMARY KEY, summary TEXT NOT NULL, changes_json TEXT NOT NULL,
              status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected','applied')),
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL, error_text TEXT
            );
            CREATE TABLE IF NOT EXISTS memories (
              id TEXT PRIMARY KEY, category TEXT NOT NULL, content TEXT NOT NULL,
              source TEXT NOT NULL DEFAULT 'user', tags_json TEXT NOT NULL DEFAULT '[]',
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memory_search_terms (
              memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
              term TEXT NOT NULL,
              PRIMARY KEY(memory_id, term)
            );
            CREATE INDEX IF NOT EXISTS idx_memory_search_terms_term
              ON memory_search_terms(term, memory_id);
            CREATE INDEX IF NOT EXISTS idx_memories_source_id
              ON memories(source, id);
            CREATE TABLE IF NOT EXISTS memory_search_index_meta (
              version INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
              id TEXT PRIMARY KEY, title TEXT NOT NULL,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agent_workflows (
              id TEXT PRIMARY KEY,
              goal TEXT NOT NULL,
              status TEXT NOT NULL CHECK(status IN ('running','succeeded','partial','failed','unknown')),
              plan_json TEXT NOT NULL,
              results_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              error_text TEXT
            );
            CREATE TABLE IF NOT EXISTS workflow_controls (
              workflow_id TEXT PRIMARY KEY REFERENCES agent_workflows(id) ON DELETE CASCADE,
              state TEXT NOT NULL CHECK(state IN ('running','paused','cancel_requested','completed')),
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workflow_idempotency (
              idempotency_key TEXT PRIMARY KEY,
              request_hash TEXT NOT NULL,
              workflow_id TEXT NOT NULL REFERENCES agent_workflows(id),
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messaging_session_routes (
              route_id TEXT PRIMARY KEY,
              conversation_id TEXT NOT NULL,
              channel TEXT NOT NULL,
              session_id TEXT NOT NULL UNIQUE REFERENCES sessions(id) ON DELETE CASCADE,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(conversation_id, channel)
            );
            CREATE INDEX IF NOT EXISTS idx_messaging_session_routes_channel
              ON messaging_session_routes(channel, updated_at DESC);
            CREATE TABLE IF NOT EXISTS session_messages (
              id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
              role TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
              content TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_session_messages_session_created
              ON session_messages(session_id, created_at);
            CREATE TABLE IF NOT EXISTS session_message_search_terms (
              message_id TEXT NOT NULL REFERENCES session_messages(id) ON DELETE CASCADE,
              term TEXT NOT NULL,
              PRIMARY KEY(message_id, term)
            );
            CREATE INDEX IF NOT EXISTS idx_session_message_search_terms_term
              ON session_message_search_terms(term, message_id);
            CREATE TABLE IF NOT EXISTS session_message_search_index_meta (
              version INTEGER NOT NULL
            );
            """)
            # Backfill existing messages once. The version marker keeps later
            # startups independent of conversation history size.
            message_index_version = con.execute(
                "SELECT version FROM session_message_search_index_meta LIMIT 1"
            ).fetchone()
            if message_index_version is None or message_index_version["version"] < 1:
                con.execute("DELETE FROM session_message_search_terms")
                message_cursor = con.execute(
                    "SELECT id, content FROM session_messages"
                )
                while message_batch := message_cursor.fetchmany(_SEARCH_INDEX_BACKFILL_BATCH_SIZE):
                    indexed_terms = [
                        (message["id"], token)
                        for message in message_batch
                        for token in _memory_search_tokens(message["content"])
                    ]
                    if indexed_terms:
                        con.executemany(
                            "INSERT OR IGNORE INTO session_message_search_terms(message_id, term) "
                            "VALUES(?, ?)",
                            indexed_terms,
                        )
                con.execute("DELETE FROM session_message_search_index_meta")
                con.execute("INSERT INTO session_message_search_index_meta(version) VALUES(1)")

            # Backfill the token index once for databases created before indexed
            # memory search. The metadata row makes startup independent of memory
            # count after the initial migration.
            index_version = con.execute(
                "SELECT version FROM memory_search_index_meta LIMIT 1"
            ).fetchone()
            if index_version is None or index_version["version"] < 1:
                con.execute("DELETE FROM memory_search_terms")
                memory_cursor = con.execute(
                    "SELECT id, category, content, tags_json FROM memories"
                )
                while memory_batch := memory_cursor.fetchmany(_SEARCH_INDEX_BACKFILL_BATCH_SIZE):
                    indexed_terms = []
                    for memory in memory_batch:
                        tags = json.loads(memory["tags_json"])
                        indexed_terms.extend(
                            (memory["id"], token)
                            for token in _memory_search_tokens(
                                memory["category"], memory["content"], *tags
                            )
                        )
                    if indexed_terms:
                        con.executemany(
                            "INSERT OR IGNORE INTO memory_search_terms(memory_id, term) VALUES(?, ?)",
                            indexed_terms,
                        )
                con.execute("DELETE FROM memory_search_index_meta")
                con.execute("INSERT INTO memory_search_index_meta(version) VALUES(1)")

            route_columns = con.execute("PRAGMA table_info(messaging_session_routes)").fetchall()
            if route_columns and not any(row["name"] == "route_id" for row in route_columns):
                con.execute("ALTER TABLE messaging_session_routes RENAME TO messaging_session_routes_legacy")
                con.execute("""
                    CREATE TABLE messaging_session_routes (
                      route_id TEXT PRIMARY KEY,
                      conversation_id TEXT NOT NULL,
                      channel TEXT NOT NULL,
                      session_id TEXT NOT NULL UNIQUE REFERENCES sessions(id) ON DELETE CASCADE,
                      created_at TEXT NOT NULL,
                      updated_at TEXT NOT NULL,
                      UNIQUE(conversation_id, channel)
                    )
                """)
                con.execute("""
                    INSERT INTO messaging_session_routes
                      (route_id, conversation_id, channel, session_id, created_at, updated_at)
                    SELECT channel || ':' || conversation_id, conversation_id, channel,
                           session_id, created_at, updated_at
                    FROM messaging_session_routes_legacy
                """)
                con.execute("DROP TABLE messaging_session_routes_legacy")
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_messaging_session_routes_channel "
                    "ON messaging_session_routes(channel, updated_at DESC)"
                )

    def create_task(self, title: str, input_text: str) -> dict[str, Any]:
        now, task_id = utc_now(), str(uuid4())
        with self.connect() as con:
            con.execute("INSERT INTO tasks(id,title,input_text,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                        (task_id, title, input_text, "queued", now, now))
        return self.get_task(task_id)  # type: ignore[return-value]

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def list_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def claim_task(self, task_id: str) -> bool:
        """Atomically transition a queued task to running; only one caller can claim it."""
        with self.connect() as con:
            cursor = con.execute(
                "UPDATE tasks SET status='running',updated_at=?,result_json=NULL,error_text=NULL "
                "WHERE id=? AND status='queued'",
                (utc_now(), task_id),
            )
            if cursor.rowcount == 1:
                return True
            row = con.execute("SELECT id FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError("Task not found")
            return False

    def set_task_status(self, task_id: str, status: str, result: Any = None, error: str | None = None) -> None:
        if status not in STATUSES:
            raise ValueError("Invalid task status")
        encoded_result = json.dumps(result, ensure_ascii=False) if result is not None else None
        with self.connect() as con:
            # Queued/running work may be finalized, but a terminal or uncertain
            # task must never be silently reopened by a stale request. Same-state
            # writes remain allowed so callers can enrich the existing journal row.
            cursor = con.execute(
                "UPDATE tasks SET status=?,updated_at=?,"
                "result_json=CASE WHEN status=? THEN COALESCE(result_json,?) ELSE ? END,"
                "error_text=CASE WHEN status=? THEN COALESCE(error_text,?) ELSE ? END "
                "WHERE id=? AND (status='queued' OR "
                "(status='running' AND ? != 'queued') OR status=?)",
                (
                    status, utc_now(),
                    status, encoded_result, encoded_result,
                    status, error, error,
                    task_id, status, status,
                ),
            )
            if cursor.rowcount == 0:
                row = con.execute(
                    "SELECT status FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                if row is None:
                    raise KeyError("Task not found")
                raise ValueError(
                    f"Invalid task status transition from {row['status']} to {status}"
                )

    def create_workflow(self, goal: str, plan: list[dict[str, Any]]) -> dict[str, Any]:
        """Persist a workflow plan before any specialist task starts."""
        workflow_id, now = str(uuid4()), utc_now()
        with self.connect() as con:
            con.execute(
                "INSERT INTO agent_workflows(id,goal,status,plan_json,results_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    workflow_id, goal, "running",
                    json.dumps(plan, ensure_ascii=False),
                    json.dumps({"tasks": [], "qa_review": None}, ensure_ascii=False),
                    now, now,
                ),
            )
        with self.connect() as con:
            con.execute(
                "INSERT INTO workflow_controls(workflow_id,state,updated_at) VALUES(?,?,?)",
                (workflow_id, "running", now),
            )
        return self.get_workflow(workflow_id)  # type: ignore[return-value]

    def get_idempotent_workflow(
        self, idempotency_key: str, request_hash: str
    ) -> dict[str, Any] | None:
        """Return the workflow for a matching key, or reject key reuse."""
        with self.connect() as con:
            row = con.execute(
                "SELECT request_hash, workflow_id FROM workflow_idempotency "
                "WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise IdempotencyConflictError(
                "Idempotency key was already used for a different workflow request"
            )
        workflow = self.get_workflow(str(row["workflow_id"]))
        if workflow is None:
            raise RuntimeError("Idempotent workflow record disappeared")
        return workflow

    def create_workflow_idempotent(
        self,
        idempotency_key: str,
        request_hash: str,
        goal: str,
        plan: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], bool]:
        """Atomically create one workflow per key and request fingerprint.

        Returns the existing workflow for a repeated identical request. A key
        reused with different request content is rejected without changing state.
        """
        now, candidate_id = utc_now(), str(uuid4())
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute(
                "SELECT request_hash, workflow_id FROM workflow_idempotency "
                "WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise IdempotencyConflictError(
                        "Idempotency key was already used for a different workflow request"
                    )
                workflow_id = str(existing["workflow_id"])
                created = False
            else:
                con.execute(
                    "INSERT INTO agent_workflows"
                    "(id,goal,status,plan_json,results_json,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        candidate_id, goal, "running",
                        json.dumps(plan, ensure_ascii=False),
                        json.dumps({"tasks": [], "qa_review": None}, ensure_ascii=False),
                        now, now,
                    ),
                )
                con.execute(
                    "INSERT INTO workflow_controls(workflow_id,state,updated_at) "
                    "VALUES(?,?,?)",
                    (candidate_id, "running", now),
                )
                con.execute(
                    "INSERT INTO workflow_idempotency"
                    "(idempotency_key,request_hash,workflow_id,created_at) "
                    "VALUES(?,?,?,?)",
                    (idempotency_key, request_hash, candidate_id, now),
                )
                workflow_id = candidate_id
                created = True
        workflow = self.get_workflow(workflow_id)
        if workflow is None:
            raise RuntimeError("Idempotent workflow record disappeared")
        return workflow, created

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute(
                "SELECT * FROM agent_workflows WHERE id=?", (workflow_id,)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["plan"] = json.loads(result.pop("plan_json"))
        result["results"] = json.loads(result.pop("results_json"))
        result["control_state"] = self.get_workflow_control(workflow_id)
        return result

    def list_workflows(self, limit: int = 50) -> list[dict[str, Any]]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        with self.connect() as con:
            rows = con.execute(
                "SELECT id FROM agent_workflows ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            workflow for row in rows
            if (workflow := self.get_workflow(row["id"])) is not None
        ]

    def get_workflow_control(self, workflow_id: str) -> str:
        with self.connect() as con:
            row = con.execute(
                "SELECT state FROM workflow_controls WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()
            workflow = con.execute(
                "SELECT status FROM agent_workflows WHERE id=?",
                (workflow_id,),
            ).fetchone()
        if workflow is None:
            raise KeyError("Workflow not found")
        # Older persisted workflows predate the control table. Keep them readable.
        if row is None:
            return "running" if workflow["status"] == "running" else "completed"
        return str(row["state"])

    def set_workflow_control(self, workflow_id: str, state: str) -> bool:
        """Atomically change a live workflow control state; terminal states cannot reopen."""
        if state not in {"running", "paused", "cancel_requested", "completed"}:
            raise ValueError("Invalid workflow control state")
        with self.connect() as con:
            row = con.execute(
                "SELECT state FROM workflow_controls WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()
            if row is None:
                raise KeyError("Workflow not found")
            current = row["state"]
            allowed = {
                "running": {"paused", "cancel_requested", "completed"},
                "paused": {"running", "cancel_requested", "completed"},
                "cancel_requested": {"cancel_requested", "completed"},
                "completed": {"completed"},
            }
            if state not in allowed[current]:
                return False
            con.execute(
                "UPDATE workflow_controls SET state=?,updated_at=? WHERE workflow_id=?",
                (state, utc_now(), workflow_id),
            )
            return True

    def execution_metrics(self) -> dict[str, Any]:
        """Return aggregate task/workflow/tool health without exposing task content or IDs."""
        with self.connect() as con:
            task_rows = con.execute(
                "SELECT status, COUNT(*) AS count FROM tasks GROUP BY status"
            ).fetchall()
            workflow_rows = con.execute(
                "SELECT status, COUNT(*) AS count FROM agent_workflows GROUP BY status"
            ).fetchall()
            tool_rows = con.execute(
                "SELECT status, COUNT(*) AS count FROM tool_executions GROUP BY status"
            ).fetchall()
            task_age = con.execute(
                "SELECT MIN(created_at) AS oldest_queued_at FROM tasks WHERE status='queued'"
            ).fetchone()
            workflow_age = con.execute(
                "SELECT MIN(updated_at) AS oldest_unknown_at FROM agent_workflows WHERE status='unknown'"
            ).fetchone()
        def counts(rows: list[sqlite3.Row], statuses: tuple[str, ...]) -> dict[str, int]:
            result = {row["status"]: row["count"] for row in rows}
            for status in statuses:
                result.setdefault(status, 0)
            return result
        return {
            "tasks": counts(task_rows, ("queued", "running", "succeeded", "failed", "unknown")),
            "workflows": counts(workflow_rows, ("running", "succeeded", "partial", "failed", "unknown")),
            "tool_executions": counts(tool_rows, ("running", "succeeded", "failed", "unknown")),
            "oldest_queued_task_at": task_age["oldest_queued_at"],
            "oldest_unknown_workflow_updated_at": workflow_age["oldest_unknown_at"],
        }

    def claim_workflow_recovery(self, workflow_id: str) -> bool:
        """Atomically claim an unknown workflow for one explicit recovery attempt."""
        with self.connect() as con:
            cursor = con.execute(
                "UPDATE agent_workflows SET status='running',updated_at=?,error_text=NULL "
                "WHERE id=? AND status='unknown'",
                (utc_now(), workflow_id),
            )
            if cursor.rowcount == 1:
                con.execute(
                    "UPDATE workflow_controls SET state='running',updated_at=? WHERE workflow_id=?",
                    (utc_now(), workflow_id),
                )
                return True
            row = con.execute(
                "SELECT id FROM agent_workflows WHERE id=?", (workflow_id,)
            ).fetchone()
            if row is None:
                raise KeyError("Workflow not found")
            return False

    def update_workflow(
        self,
        workflow_id: str,
        status: str,
        results: Any = None,
        error: str | None = None,
    ) -> None:
        if status not in {"running", "succeeded", "partial", "failed", "unknown"}:
            raise ValueError("Invalid workflow status")
        encoded_results = (
            json.dumps(results, ensure_ascii=False) if results is not None else None
        )
        with self.connect() as con:
            # Workflows may advance while running or remain in the same state.
            # Recovery must use claim_workflow_recovery() for the guarded unknown
            # -> running transition. Completed workflows cannot be reopened.
            cursor = con.execute(
                "UPDATE agent_workflows SET status=?,updated_at=?,"
                "results_json=CASE WHEN status IN ('succeeded','partial','failed','unknown') "
                "THEN COALESCE(results_json,?) ELSE COALESCE(?,results_json) END,"
                "error_text=CASE WHEN status=? THEN COALESCE(error_text,?) ELSE ? END "
                "WHERE id=? AND (status='running' OR status=?)",
                (
                    status, utc_now(), encoded_results, encoded_results,
                    status, error, error,
                    workflow_id, status,
                ),
            )
            if cursor.rowcount == 0:
                row = con.execute(
                    "SELECT status FROM agent_workflows WHERE id=?", (workflow_id,)
                ).fetchone()
                if row is None:
                    raise KeyError("Workflow not found")
                raise ValueError(
                    f"Invalid workflow status transition from {row['status']} to {status}"
                )

    def begin_tool(self, task_id: str, tool_name: str, tool_input: dict[str, Any]) -> str:
        execution_id, now = str(uuid4()), utc_now()
        with self.connect() as con:
            con.execute("INSERT INTO tool_executions(id,task_id,tool_name,input_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                        (execution_id, task_id, tool_name, json.dumps(tool_input, ensure_ascii=False), "running", now, now))
        return execution_id

    def finish_tool(self, execution_id: str, status: str, result: Any = None, error: str | None = None) -> None:
        if status not in {"succeeded", "failed", "unknown"}:
            raise ValueError("Invalid final tool status")
        with self.connect() as con:
            cursor = con.execute(
                "UPDATE tool_executions SET status=?,updated_at=?,"
                "result_json=CASE WHEN status=? THEN COALESCE(result_json,?) ELSE ? END,"
                "error_text=CASE WHEN status=? THEN COALESCE(error_text,?) ELSE ? END "
                "WHERE id=? AND (status='running' OR status=?)",
                (
                    status,
                    utc_now(),
                    status,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    status, error, error,
                    execution_id, status,
                ),
            )
            if cursor.rowcount == 0:
                row = con.execute(
                    "SELECT status FROM tool_executions WHERE id=?", (execution_id,)
                ).fetchone()
                if row is None:
                    raise KeyError("Tool execution not found")
                raise ValueError(
                    f"Invalid tool execution status transition from {row['status']} to {status}"
                )

    def list_tool_executions(self, task_id: str) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute("SELECT * FROM tool_executions WHERE task_id=? ORDER BY created_at", (task_id,)).fetchall()
        return [dict(row) for row in rows]

    def mark_interrupted_proposals_for_review(self) -> int:
        """Flag proposals left applying by a prior process for manual reconciliation.

        File replacement and SQLite status persistence cannot be one atomic
        transaction. Never reset these proposals to approved automatically.
        """
        now = utc_now()
        with self.connect() as con:
            cursor = con.execute(
                """
                UPDATE coding_proposals
                SET updated_at=?,
                    error_text=COALESCE(
                        error_text,
                        'Process restarted while applying. Inspect workspace files and proposal changes before any manual recovery.'
                    )
                WHERE status='applying'
                """,
                (now,),
            )
        return cursor.rowcount

    def mark_interrupted_as_unknown(self) -> int:
        now = utc_now()
        with self.connect() as con:
            tools = con.execute("UPDATE tool_executions SET status='unknown',updated_at=?,error_text=COALESCE(error_text,?) WHERE status='running'",
                                (now, "Process restarted during execution; verify effects before retrying"))
            tasks = con.execute("UPDATE tasks SET status='unknown',updated_at=?,error_text=COALESCE(error_text,?) WHERE status='running'",
                                (now, "Process restarted during task; verify effects before retrying"))
            workflows = con.execute(
                "UPDATE agent_workflows SET status='unknown',updated_at=?,"
                "error_text=COALESCE(error_text,?) WHERE status='running'",
                (now, "Process restarted during workflow; inspect saved task checkpoints before manual recovery"),
            )
        return tools.rowcount + tasks.rowcount + workflows.rowcount


    def create_proposal(self, summary: str, changes: list[dict[str, Any]]) -> dict[str, Any]:
        proposal_id, now = str(uuid4()), utc_now()
        with self.connect() as con:
            con.execute(
                "INSERT INTO coding_proposals(id,summary,changes_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (proposal_id, summary, json.dumps(changes, ensure_ascii=False), "pending", now, now),
            )
        return self.get_proposal(proposal_id)  # type: ignore[return-value]

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM coding_proposals WHERE id=?", (proposal_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["changes"] = json.loads(result.pop("changes_json"))
        return result

    def list_proposals(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute("SELECT id FROM coding_proposals ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [proposal for row in rows if (proposal := self.get_proposal(row["id"])) is not None]

    def _migrate_coding_proposals(self) -> None:
        """Add the applying state while preserving all existing proposals."""
        con = self.connect()
        try:
            row = con.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='coding_proposals'"
            ).fetchone()
            if row is None:
                return

            schema = row["sql"] or ""
            if "'applying'" in schema:
                return

            con.execute("BEGIN IMMEDIATE")
            con.execute("""
                CREATE TABLE coding_proposals_new (
                    id TEXT PRIMARY KEY,
                    summary TEXT NOT NULL,
                    changes_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(
                        status IN ('pending','approved','applying','rejected','applied')
                    ),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    error_text TEXT
                )
            """)
            con.execute("""
                INSERT INTO coding_proposals_new
                    (id, summary, changes_json, status, created_at, updated_at, error_text)
                SELECT id, summary, changes_json, status, created_at, updated_at, error_text
                FROM coding_proposals
            """)
            con.execute("DROP TABLE coding_proposals")
            con.execute("ALTER TABLE coding_proposals_new RENAME TO coding_proposals")
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def claim_proposal_for_apply(self, proposal_id: str) -> bool:
        """Atomically claim an approved proposal; only one caller can win."""
        with self.connect() as con:
            cursor = con.execute(
                """
                UPDATE coding_proposals
                SET status='applying', updated_at=?, error_text=NULL
                WHERE id=? AND status='approved'
                """,
                (utc_now(), proposal_id),
            )
            if cursor.rowcount == 1:
                return True

            row = con.execute(
                "SELECT id FROM coding_proposals WHERE id=?",
                (proposal_id,),
            ).fetchone()
            if row is None:
                raise KeyError("Proposal not found")
            return False

    def set_proposal_status(self, proposal_id: str, status: str, error: str | None = None) -> None:
        if status not in {"pending", "approved", "applying", "rejected", "applied"}:
            raise ValueError("Invalid coding proposal status")

        with self.connect() as con:
            cursor = con.execute(
                """
                UPDATE coding_proposals
                SET status=?, updated_at=?, error_text=?
                WHERE id=? AND (
                    (status='pending' AND ? IN ('approved', 'rejected'))
                    OR (status='applying' AND ?='applied')
                    OR (status='applying' AND ?='approved' AND ? IS NOT NULL)
                    OR (status='applying' AND ?='applying' AND ? IS NOT NULL)
                    OR (status='approved' AND ?='approved' AND ? IS NOT NULL)
                )
                """,
                (
                    status, utc_now(), error, proposal_id,
                    status, status, status, error, status, error, status, error,
                ),
            )
            if cursor.rowcount == 0:
                row = con.execute(
                    "SELECT status FROM coding_proposals WHERE id=?",
                    (proposal_id,),
                ).fetchone()
                if row is None:
                    raise KeyError("Proposal not found")
                raise ValueError(
                    f"Invalid proposal status transition: {row['status']} -> {status}"
                )

    def add_memory(self, category: str, content: str, source: str = "user",
                   tags: list[str] | None = None) -> dict[str, Any]:
        memory_id, now = str(uuid4()), utc_now()
        with self.connect() as con:
            clean_category = category.strip()
            clean_content = content.strip()
            clean_tags = tags or []
            con.execute(
                "INSERT INTO memories(id,category,content,source,tags_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (memory_id, clean_category, clean_content, source.strip() or "user",
                 json.dumps(clean_tags, ensure_ascii=False), now, now),
            )
            tokens = _memory_search_tokens(clean_category, clean_content, *clean_tags)
            con.executemany(
                "INSERT INTO memory_search_terms(memory_id, term) VALUES(?, ?)",
                [(memory_id, token) for token in tokens],
            )
        return self.get_memory(memory_id)  # type: ignore[return-value]

    def update_memory(
        self,
        memory_id: str,
        *,
        category: str | None = None,
        content: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Update editable memory fields and their search terms atomically."""
        with self.connect() as con:
            row = con.execute(
                "SELECT category, content, tags_json FROM memories WHERE id=?",
                (memory_id,),
            ).fetchone()
            if row is None:
                return None

            clean_category = (category if category is not None else row["category"]).strip()
            clean_content = (content if content is not None else row["content"]).strip()
            clean_tags = tags if tags is not None else json.loads(row["tags_json"])
            if not clean_category:
                raise ValueError("category must not be blank")
            if not clean_content:
                raise ValueError("content must not be blank")
            if not isinstance(clean_tags, list) or any(
                not isinstance(tag, str) or not tag.strip() for tag in clean_tags
            ):
                raise ValueError("tags must be a list of non-blank strings")
            clean_tags = [tag.strip() for tag in clean_tags]

            con.execute("DELETE FROM memory_search_terms WHERE memory_id=?", (memory_id,))
            tokens = _memory_search_tokens(clean_category, clean_content, *clean_tags)
            con.executemany(
                "INSERT INTO memory_search_terms(memory_id, term) VALUES(?, ?)",
                [(memory_id, token) for token in tokens],
            )
            con.execute(
                "UPDATE memories SET category=?, content=?, tags_json=?, updated_at=? WHERE id=?",
                (
                    clean_category,
                    clean_content,
                    json.dumps(clean_tags, ensure_ascii=False),
                    utc_now(),
                    memory_id,
                ),
            )
        return self.get_memory(memory_id)

    def get_memory(self, memory_id: str) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["tags"] = json.loads(result.pop("tags_json"))
        return result

    def list_memories(self, limit: int | None = 100, category: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as con:
            if category:
                if limit is None:
                    rows = con.execute(
                        "SELECT * FROM memories WHERE category=? ORDER BY updated_at DESC, id DESC",
                        (category,),
                    ).fetchall()
                else:
                    rows = con.execute(
                        "SELECT * FROM memories WHERE category=? ORDER BY updated_at DESC, id DESC LIMIT ?",
                        (category, limit),
                    ).fetchall()
            elif limit is None:
                rows = con.execute(
                    "SELECT * FROM memories ORDER BY updated_at DESC, id DESC"
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT * FROM memories ORDER BY updated_at DESC, id DESC LIMIT ?",
                    (limit,),
                ).fetchall()

        # Decode rows from this single result set instead of opening one
        # additional SQLite connection/query per memory (the previous N+1 path).
        memories = []
        for row in rows:
            item = dict(row)
            item["tags"] = json.loads(item.pop("tags_json"))
            memories.append(item)
        return memories

    def search_memories(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        stop_words = {
            "what", "which", "did", "do", "does", "i", "you", "my", "me",
            "the", "a", "an", "is", "was", "were", "to", "of", "for",
            "in", "on", "that", "this", "please", "ask", "asked", "remember", "recall", "memorized", "saved",
        }
        terms = [
            term for term in _memory_search_tokens(query)
            if term not in stop_words
        ]
        if limit <= 0 or not terms:
            return []

        recall_intent = bool(
            re.search(r"\b(remember|recall|memorized|saved)\b", query, re.IGNORECASE)
            and re.search(r"\b(what|which|code|thing|information|detail)\b", query, re.IGNORECASE)
        )
        with self.connect() as con:
            # A connection-local term table avoids SQLite's bound-variable limit
            # for long queries while allowing the persistent index to use its
            # (term, memory_id) index instead of scanning every saved memory.
            con.execute(
                "CREATE TEMP TABLE query_memory_terms(term TEXT PRIMARY KEY) WITHOUT ROWID"
            )
            con.executemany(
                "INSERT INTO query_memory_terms(term) VALUES(?)",
                [(term,) for term in terms],
            )
            search_sql = MEMORY_SEARCH_RECALL_SQL if recall_intent else MEMORY_SEARCH_SQL
            rows = con.execute(search_sql, (limit,)).fetchall()

        results = []
        for row in rows:
            item = dict(row)
            item.pop("search_score", None)
            item["tags"] = json.loads(item.pop("tags_json"))
            results.append(item)
        return results

    def delete_memory(self, memory_id: str) -> bool:
        with self.connect() as con:
            cursor = con.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        return cursor.rowcount > 0


    def get_or_create_messaging_session(self, conversation_id: str, channel: str) -> dict[str, Any]:
        """Resolve one durable agent session for an opaque channel conversation scope."""
        if not isinstance(conversation_id, str) or not re.fullmatch(r"msg_[a-f0-9]{40}", conversation_id):
            raise ValueError("Invalid messaging conversation ID")
        if not isinstance(channel, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", channel):
            raise ValueError("Invalid messaging channel")
        now = utc_now()
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute(
                "SELECT s.* FROM messaging_session_routes r JOIN sessions s ON s.id=r.session_id "
                "WHERE r.conversation_id=? AND r.channel=?",
                (conversation_id, channel),
            ).fetchone()
            if existing:
                return dict(existing)
            session_id = str(uuid4())
            con.execute(
                "INSERT INTO sessions(id,title,created_at,updated_at) VALUES(?,?,?,?)",
                (session_id, f"{channel.title()} conversation", now, now),
            )
            con.execute(
                "INSERT INTO messaging_session_routes(route_id,conversation_id,channel,session_id,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (f"{channel}:{conversation_id}", conversation_id, channel, session_id, now, now),
            )
            row = con.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return dict(row)

    def get_messaging_session(self, conversation_id: str, channel: str) -> dict[str, Any] | None:
        if not isinstance(conversation_id, str) or not re.fullmatch(r"msg_[a-f0-9]{40}", conversation_id):
            raise ValueError("Invalid messaging conversation ID")
        if not isinstance(channel, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", channel):
            raise ValueError("Invalid messaging channel")
        with self.connect() as con:
            row = con.execute(
                "SELECT s.* FROM messaging_session_routes r JOIN sessions s ON s.id=r.session_id "
                "WHERE r.conversation_id=? AND r.channel=?",
                (conversation_id, channel),
            ).fetchone()
        return dict(row) if row else None

    def create_session(self, title: str = "New conversation") -> dict[str, Any]:
        session_id, now = str(uuid4()), utc_now()
        with self.connect() as con:
            con.execute("INSERT INTO sessions(id,title,created_at,updated_at) VALUES(?,?,?,?)",
                        (session_id, title.strip()[:120] or "New conversation", now, now))
        return self.get_session(session_id)  # type: ignore[return-value]

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return dict(row) if row else None

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT s.*, (SELECT COUNT(*) FROM session_messages m WHERE m.session_id=s.id) AS message_count "
                "FROM sessions s ORDER BY s.updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def add_session_message(self, session_id: str, role: str, content: str) -> dict[str, Any]:
        if role not in {"user", "assistant", "system"}:
            raise ValueError("Invalid message role")
        message_id, now = str(uuid4()), utc_now()
        with self.connect() as con:
            exists = con.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not exists:
                raise KeyError("Session not found")
            latest = con.execute(
                "SELECT MAX(created_at) AS created_at FROM session_messages WHERE session_id=?",
                (session_id,),
            ).fetchone()["created_at"]
            if latest is not None and now <= latest:
                from datetime import timedelta
                latest_dt = datetime.fromisoformat(latest)
                now_dt = datetime.fromisoformat(now)
                now = max(now_dt, latest_dt + timedelta(microseconds=1)).isoformat()
            con.execute("INSERT INTO session_messages(id,session_id,role,content,created_at) VALUES(?,?,?,?,?)",
                        (message_id, session_id, role, content, now))
            con.executemany(
                "INSERT INTO session_message_search_terms(message_id, term) VALUES(?, ?)",
                [(message_id, token) for token in _memory_search_tokens(content)],
            )
            con.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))
            if role == "user":
                first_user = con.execute(
                    "SELECT content FROM session_messages WHERE session_id=? AND role='user' ORDER BY created_at LIMIT 1",
                    (session_id,),
                ).fetchone()
                if first_user:
                    title = first_user["content"].strip().replace("\\n", " ")[:80] or "New conversation"
                    con.execute("UPDATE sessions SET title=? WHERE id=? AND title='New conversation'",
                                (title, session_id))
        with self.connect() as con:
            row = con.execute("SELECT * FROM session_messages WHERE id=?", (message_id,)).fetchone()
        return dict(row)


    def add_session_turn(self, session_id: str, user_content: str, assistant_content: str) -> list[dict[str, Any]]:
        """Persist a successful user/assistant turn atomically."""
        user_id, assistant_id = str(uuid4()), str(uuid4())
        user_created_at, assistant_created_at = utc_now(), utc_now()
        if assistant_created_at <= user_created_at:
            from datetime import timedelta
            assistant_created_at = (
                datetime.fromisoformat(user_created_at) + timedelta(microseconds=1)
            ).isoformat()
        with self.connect() as con:
            exists = con.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not exists:
                raise KeyError("Session not found")
            latest = con.execute(
                "SELECT MAX(created_at) AS created_at FROM session_messages WHERE session_id=?",
                (session_id,),
            ).fetchone()["created_at"]
            if latest is not None and user_created_at <= latest:
                from datetime import timedelta
                user_created_at = (
                    datetime.fromisoformat(latest) + timedelta(microseconds=1)
                ).isoformat()
                assistant_created_at = (
                    datetime.fromisoformat(user_created_at) + timedelta(microseconds=1)
                ).isoformat()
            con.execute(
                "INSERT INTO session_messages(id,session_id,role,content,created_at) VALUES(?,?,?,?,?)",
                (user_id, session_id, "user", user_content, user_created_at),
            )
            con.execute(
                "INSERT INTO session_messages(id,session_id,role,content,created_at) VALUES(?,?,?,?,?)",
                (assistant_id, session_id, "assistant", assistant_content, assistant_created_at),
            )
            con.executemany(
                "INSERT INTO session_message_search_terms(message_id, term) VALUES(?, ?)",
                [
                    (message_id, token)
                    for message_id, content in (
                        (user_id, user_content), (assistant_id, assistant_content)
                    )
                    for token in _memory_search_tokens(content)
                ],
            )
            title = user_content.strip().replace("\\n", " ")[:80] or "New conversation"
            con.execute(
                "UPDATE sessions SET updated_at=?, title=CASE WHEN title='New conversation' THEN ? ELSE title END WHERE id=?",
                (assistant_created_at, title, session_id),
            )
            rows = con.execute(
                "SELECT * FROM session_messages WHERE id IN (?, ?) "
                "ORDER BY CASE id WHEN ? THEN 0 ELSE 1 END",
                (user_id, assistant_id, user_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_session_messages(self, session_id: str, limit: int = 100) -> list[dict[str, Any]]:
        # Use the declared primary key rather than SQLite rowid; the table may be
        # backed by a WITHOUT ROWID schema in an existing local database.
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM (SELECT * FROM session_messages WHERE session_id=? "
                "ORDER BY created_at DESC, id DESC LIMIT ?) ORDER BY created_at, id",
                (session_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def search_session_messages(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Search message text through the Unicode-normalized token index."""
        if limit <= 0:
            return []
        terms = sorted(_memory_search_tokens(query))
        if not terms:
            return []
        with self.connect() as con:
            con.execute(
                "CREATE TEMP TABLE query_session_message_terms "
                "(term TEXT PRIMARY KEY) WITHOUT ROWID"
            )
            con.executemany(
                "INSERT INTO query_session_message_terms(term) VALUES(?)",
                [(term,) for term in terms],
            )
            rows = con.execute(
                "SELECT m.id, m.session_id, s.title AS session_title, m.role, "
                "m.content, m.created_at "
                "FROM ("
                "  SELECT indexed.message_id, COUNT(*) AS score "
                "  FROM query_session_message_terms AS query "
                "  CROSS JOIN session_message_search_terms AS indexed "
                "    INDEXED BY idx_session_message_search_terms_term "
                "  WHERE indexed.term = query.term "
                "  GROUP BY indexed.message_id"
                ") AS matches "
                "JOIN session_messages AS m ON m.id = matches.message_id "
                "JOIN sessions AS s ON s.id = m.session_id "
                "ORDER BY matches.score DESC, m.created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_session(self, session_id: str) -> bool:
        with self.connect() as con:
            cursor = con.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        return cursor.rowcount > 0
