"""Persistent provider-neutral inbox storage.

Provider IDs are converted to opaque conversation scopes and are not persisted.
External adapters must verify webhook signatures before calling inbound APIs.
"""
from __future__ import annotations

import re
import sqlite3

from cryptography.fernet import Fernet, InvalidToken
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from .config import get_env
from .messaging import MAX_ID_CHARS, MAX_TEXT_CHARS, MessagingContractError, conversation_scope

_CHANNEL_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_CONVERSATION_RE = re.compile(r"^msg_[a-f0-9]{40}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_channel(value: str) -> str:
    if not isinstance(value, str):
        raise MessagingContractError("channel must be text")
    channel = value.strip().lower()
    if not _CHANNEL_RE.fullmatch(channel):
        raise MessagingContractError("channel is invalid")
    return channel


def _validate_text(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT_CHARS:
        raise MessagingContractError("message text must be non-empty and within the size limit")
    return value.strip()


class MessagingStore:
    """SQLite inbox/outbox journal sharing the application's database file."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS messaging_conversations (
                    id TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    display_name TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_msg_conversations_updated
                    ON messaging_conversations(updated_at DESC);
                CREATE TABLE IF NOT EXISTS messaging_messages (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES messaging_conversations(id) ON DELETE CASCADE,
                    channel TEXT NOT NULL,
                    direction TEXT NOT NULL CHECK(direction IN ('inbound','outbound')),
                    text TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('accepted','sent','delivered','failed','unknown')),
                    provider_message_id TEXT,
                    idempotency_key TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(channel, provider_message_id),
                    UNIQUE(channel, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_msg_messages_conversation
                    ON messaging_messages(conversation_id, created_at, id);
                CREATE TABLE IF NOT EXISTS messaging_delivery_targets (
                    message_id TEXT PRIMARY KEY REFERENCES messaging_messages(id) ON DELETE CASCADE,
                    ciphertext BLOB NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messaging_delivery_leases (
                    message_id TEXT PRIMARY KEY REFERENCES messaging_messages(id) ON DELETE CASCADE,
                    lease_owner TEXT NOT NULL,
                    lease_until TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_msg_delivery_leases_until
                    ON messaging_delivery_leases(lease_until);
                CREATE TABLE IF NOT EXISTS messaging_delivery_retries (
                    message_id TEXT PRIMARY KEY REFERENCES messaging_messages(id) ON DELETE CASCADE,
                    retry_count INTEGER NOT NULL DEFAULT 0 CHECK(retry_count >= 0),
                    max_retries INTEGER NOT NULL DEFAULT 3 CHECK(max_retries BETWEEN 0 AND 5),
                    next_attempt_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messaging_delivery_reconciliations (
                    id TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL REFERENCES messaging_messages(id) ON DELETE CASCADE,
                    previous_status TEXT NOT NULL,
                    new_status TEXT NOT NULL,
                    evidence TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)
        raw_key = (get_env("CROWS_MESSAGING_TARGET_KEY", "") or "").strip()
        self._target_cipher = None
        if raw_key:
            try:
                self._target_cipher = Fernet(raw_key.encode("ascii"))
            except (ValueError, UnicodeEncodeError) as exc:
                raise ValueError("CROWS_MESSAGING_TARGET_KEY must be a valid Fernet key") from exc

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=10000")
        return con

    @staticmethod
    def _scope(channel: str, provider_user_id: str) -> str:
        if not isinstance(provider_user_id, str) or not provider_user_id.strip() or len(provider_user_id) > MAX_ID_CHARS:
            raise MessagingContractError("provider_user_id is invalid")
        # Keep the established scope namespace so existing conversation IDs remain stable.
        namespace = "aether-claw"
        return conversation_scope(channel, provider_user_id, namespace=namespace)

    @property
    def delivery_targets_configured(self) -> bool:
        return self._target_cipher is not None

    def ingest(
        self, *, channel: str, provider_message_id: str, provider_user_id: str,
        text: str, received_at: str | None = None, display_name: str | None = None,
        delivery_target: str | None = None,
    ) -> dict:
        channel = _validate_channel(channel)
        text = _validate_text(text)
        if not isinstance(provider_message_id, str) or not provider_message_id.strip() or len(provider_message_id) > MAX_ID_CHARS:
            raise MessagingContractError("provider_message_id is invalid")
        provider_message_id = provider_message_id.strip()
        if received_at is None:
            received_at = _now()
        try:
            parsed = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError
        except (AttributeError, ValueError) as exc:
            raise MessagingContractError("received_at must be an ISO-8601 timestamp with timezone") from exc
        if display_name is not None:
            if not isinstance(display_name, str) or len(display_name) > 200:
                raise MessagingContractError("display_name is invalid")
            display_name = display_name.strip() or None
        if delivery_target is not None:
            if (not isinstance(delivery_target, str) or not delivery_target.strip()
                    or len(delivery_target) > MAX_ID_CHARS):
                raise MessagingContractError("delivery_target is invalid")
            delivery_target = delivery_target.strip()
            if self._target_cipher is None:
                raise MessagingContractError(
                    "CROWS_MESSAGING_TARGET_KEY is required when an inbound delivery_target is supplied"
                )
            encrypted_target = self._target_cipher.encrypt(delivery_target.encode("utf-8"))
        else:
            encrypted_target = None
        conversation_id = self._scope(channel, provider_user_id)
        now = _now()
        with self.connect() as con:
            con.execute(
                "INSERT INTO messaging_conversations(id,channel,display_name,created_at,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET display_name=COALESCE(excluded.display_name,messaging_conversations.display_name), updated_at=excluded.updated_at",
                (conversation_id, channel, display_name, now, now),
            )
            existing = con.execute(
                "SELECT * FROM messaging_messages WHERE channel=? AND provider_message_id=?",
                (channel, provider_message_id),
            ).fetchone()
            if existing:
                if encrypted_target is not None:
                    stored = con.execute(
                        "SELECT ciphertext FROM messaging_delivery_targets WHERE message_id=?",
                        (existing["id"],),
                    ).fetchone()
                    if stored is None:
                        con.execute(
                            "INSERT INTO messaging_delivery_targets(message_id,ciphertext,created_at) VALUES(?,?,?)",
                            (existing["id"], encrypted_target, now),
                        )
                    else:
                        try:
                            current_target = self._target_cipher.decrypt(
                                bytes(stored["ciphertext"])
                            ).decode("utf-8")
                        except (InvalidToken, UnicodeDecodeError) as exc:
                            raise MessagingContractError(
                                "existing inbound delivery target cannot be verified"
                            ) from exc
                        if current_target != delivery_target:
                            raise MessagingContractError(
                                "provider message ID was already used for a different delivery target"
                            )
                return {"conversation_id": conversation_id, "message": dict(existing), "duplicate": True}
            message_id = str(uuid4())
            con.execute(
                "INSERT INTO messaging_messages(id,conversation_id,channel,direction,text,status,provider_message_id,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (message_id, conversation_id, channel, "inbound", text, "accepted", provider_message_id, received_at, now),
            )
            if encrypted_target is not None:
                con.execute(
                    "INSERT INTO messaging_delivery_targets(message_id,ciphertext,created_at) VALUES(?,?,?)",
                    (message_id, encrypted_target, now),
                )
            row = con.execute("SELECT * FROM messaging_messages WHERE id=?", (message_id,)).fetchone()
        return {"conversation_id": conversation_id, "message": dict(row), "duplicate": False}

    def queue_outbound(
        self, *, channel: str, provider_user_id: str, text: str,
        idempotency_key: str, delivery_target: str | None = None,
        conversation_id: str | None = None,
    ) -> dict:
        channel = _validate_channel(channel)
        text = _validate_text(text)
        if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > MAX_ID_CHARS:
            raise MessagingContractError("idempotency_key is invalid")
        idempotency_key = idempotency_key.strip()
        if delivery_target is not None:
            if not isinstance(delivery_target, str) or not delivery_target.strip() or len(delivery_target) > MAX_ID_CHARS:
                raise MessagingContractError("delivery_target is invalid")
            delivery_target = delivery_target.strip()
        if delivery_target is not None and self._target_cipher is None:
            raise MessagingContractError(
                "CROWS_MESSAGING_TARGET_KEY is required when a delivery_target is supplied"
            )
        encrypted_target = (
            self._target_cipher.encrypt(delivery_target.encode("utf-8"))
            if delivery_target is not None else None
        )
        preserve_conversation = conversation_id is not None
        if conversation_id is None:
            conversation_id = self._scope(channel, provider_user_id)
        elif not isinstance(conversation_id, str) or not _CONVERSATION_RE.fullmatch(conversation_id):
            raise MessagingContractError("conversation_id is invalid")
        now = _now()
        with self.connect() as con:
            if preserve_conversation:
                existing_conversation = con.execute(
                    "SELECT channel FROM messaging_conversations WHERE id=?",
                    (conversation_id,),
                ).fetchone()
                if existing_conversation is None or existing_conversation["channel"] != channel:
                    raise MessagingContractError("conversation_id does not identify an existing channel conversation")
            con.execute(
                "INSERT INTO messaging_conversations(id,channel,created_at,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at",
                (conversation_id, channel, now, now),
            )
            existing = con.execute(
                "SELECT * FROM messaging_messages WHERE channel=? AND idempotency_key=?",
                (channel, idempotency_key),
            ).fetchone()
            if existing:
                if existing["conversation_id"] != conversation_id or existing["text"] != text:
                    raise MessagingContractError("idempotency_key was already used for a different message")

                stored_target = con.execute(
                    "SELECT ciphertext FROM messaging_delivery_targets WHERE message_id=?",
                    (existing["id"],),
                ).fetchone()

                if stored_target is None:
                    existing_target = None
                else:
                    if self._target_cipher is None:
                        raise MessagingContractError(
                            "existing delivery target cannot be verified for this idempotency_key"
                        )
                    try:
                        existing_target = self._target_cipher.decrypt(
                            bytes(stored_target["ciphertext"])
                        ).decode("utf-8")
                    except (InvalidToken, UnicodeDecodeError) as exc:
                        raise MessagingContractError(
                            "existing delivery target cannot be verified for this idempotency_key"
                        ) from exc

                if existing_target != delivery_target:
                    raise MessagingContractError(
                        "idempotency_key was already used for a different message or delivery target"
                    )

                return {"conversation_id": conversation_id, "message": dict(existing), "duplicate": True}
            message_id = str(uuid4())
            con.execute(
                "INSERT INTO messaging_messages(id,conversation_id,channel,direction,text,status,idempotency_key,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (message_id, conversation_id, channel, "outbound", text, "accepted", idempotency_key, now, now),
            )
            if encrypted_target is not None:
                con.execute(
                    "INSERT INTO messaging_delivery_targets(message_id,ciphertext,created_at) VALUES(?,?,?)",
                    (message_id, encrypted_target, now),
                )
            row = con.execute("SELECT * FROM messaging_messages WHERE id=?", (message_id,)).fetchone()
        return {"conversation_id": conversation_id, "message": dict(row), "duplicate": False}

    def get_delivery_target(self, message_id: str) -> str | None:
        """Decrypt a delivery target for trusted backend workers; never include it in API message views."""
        if self._target_cipher is None:
            return None
        with self.connect() as con:
            row = con.execute(
                "SELECT ciphertext FROM messaging_delivery_targets WHERE message_id=?",
                (message_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            return self._target_cipher.decrypt(bytes(row["ciphertext"])).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError):
            return None

    def claim_outbound(
        self, *, worker_id: str, limit: int = 10, lease_seconds: int = 60,
        message_id: str | None = None,
    ) -> list[dict]:
        """Atomically lease eligible outbound messages, optionally targeting one message."""
        if not isinstance(worker_id, str) or not worker_id.strip() or len(worker_id) > 128:
            raise MessagingContractError("worker_id is invalid")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 5 <= lease_seconds <= 900:
            raise ValueError("lease_seconds must be an integer between 5 and 900")
        if message_id is not None and (
            not isinstance(message_id, str) or not message_id.strip() or len(message_id) > 128
        ):
            raise MessagingContractError("message_id is invalid")
        if message_id is not None:
            message_id = message_id.strip()
        if self._target_cipher is None:
            return []
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        lease_until = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        claimed: list[dict] = []
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute(
                """SELECT m.* FROM messaging_messages m
                   JOIN messaging_delivery_targets t ON t.message_id=m.id
                   LEFT JOIN messaging_delivery_leases l ON l.message_id=m.id
                   LEFT JOIN messaging_delivery_retries r ON r.message_id=m.id
                   WHERE m.direction='outbound' AND m.status='accepted'
                     AND l.message_id IS NULL
                     AND (r.message_id IS NULL OR
                          (r.next_attempt_at <= ? AND r.retry_count <= r.max_retries))
                     AND (? IS NULL OR m.id=?)
                   ORDER BY COALESCE(r.next_attempt_at, m.created_at), m.id LIMIT ?""",
                (now, message_id, message_id, limit),
            ).fetchall()
            for row in rows:
                con.execute(
                    """INSERT INTO messaging_delivery_leases(message_id,lease_owner,lease_until,attempts,updated_at)
                       VALUES(?,?,?,?,?)
                       ON CONFLICT(message_id) DO UPDATE SET
                         lease_owner=excluded.lease_owner,
                         lease_until=excluded.lease_until,
                         attempts=messaging_delivery_leases.attempts+1,
                         updated_at=excluded.updated_at""",
                    (row["id"], worker_id.strip(), lease_until, 1, now),
                )
                claimed.append({**dict(row), "lease_owner": worker_id.strip(),
                                "lease_until": lease_until})
        return claimed

    def finish_outbound_lease(
        self, message_id: str, *, worker_id: str, status: str,
        provider_message_id: str | None = None,
    ) -> bool:
        """Finish a lease only for its current owner; never auto-requeue uncertain outcomes."""
        if status not in {"sent", "delivered", "failed", "unknown"}:
            raise MessagingContractError("invalid outbound delivery status")
        if provider_message_id is not None:
            if (
                not isinstance(provider_message_id, str)
                or not provider_message_id.strip()
                or len(provider_message_id) > MAX_ID_CHARS
            ):
                raise MessagingContractError("provider_message_id is invalid")
            provider_message_id = provider_message_id.strip()
        now = _now()
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            lease = con.execute(
                "SELECT lease_owner FROM messaging_delivery_leases WHERE message_id=?",
                (message_id,),
            ).fetchone()
            if lease is None or lease["lease_owner"] != worker_id:
                return False
            lease_row = con.execute(
                "SELECT lease_until FROM messaging_delivery_leases WHERE message_id=?",
                (message_id,),
            ).fetchone()
            if lease_row is None or lease_row["lease_until"] < now:
                return False
            try:
                con.execute(
                    """UPDATE messaging_messages SET status=?,
                       provider_message_id=COALESCE(?,provider_message_id), updated_at=?
                       WHERE id=? AND direction='outbound' AND status='accepted'""",
                    (status, provider_message_id, now, message_id),
                )
            except sqlite3.IntegrityError as exc:
                raise MessagingContractError(
                    "provider_message_id is already recorded for this channel"
                ) from exc
            changed = con.execute("SELECT changes()").fetchone()[0] == 1
            if changed:
                con.execute("DELETE FROM messaging_delivery_leases WHERE message_id=?", (message_id,))
            return changed

    def expire_outbound_leases(self) -> int:
        """Mark expired in-flight claims unknown; explicit reconciliation is required before retry."""
        now = _now()
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute(
                """SELECT message_id FROM messaging_delivery_leases
                   WHERE lease_until < ?""", (now,)
            ).fetchall()
            ids = [row["message_id"] for row in rows]
            for message_id in ids:
                con.execute(
                    """UPDATE messaging_messages SET status='unknown', updated_at=?
                       WHERE id=? AND direction='outbound' AND status='accepted'""",
                    (now, message_id),
                )
                con.execute("DELETE FROM messaging_delivery_leases WHERE message_id=?", (message_id,))
        return len(ids)

    def retry_outbound_lease(
        self, message_id: str, *, worker_id: str, delay_seconds: int,
        max_retries: int = 3,
    ) -> str:
        """Schedule a bounded retry only after a provider's explicit rate-limit response."""
        if isinstance(delay_seconds, bool) or not isinstance(delay_seconds, int) or not 1 <= delay_seconds <= 300:
            raise ValueError("delay_seconds must be an integer between 1 and 300")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or not 0 <= max_retries <= 5:
            raise ValueError("max_retries must be an integer between 0 and 5")
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        next_attempt = (now_dt + timedelta(seconds=delay_seconds)).isoformat()
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            lease = con.execute(
                "SELECT lease_owner, lease_until FROM messaging_delivery_leases WHERE message_id=?",
                (message_id,),
            ).fetchone()
            if (lease is None or lease["lease_owner"] != worker_id
                    or lease["lease_until"] < now):
                return "lease_lost"
            existing = con.execute(
                "SELECT retry_count, max_retries FROM messaging_delivery_retries WHERE message_id=?",
                (message_id,),
            ).fetchone()
            if existing is not None and existing["max_retries"] != max_retries:
                raise MessagingContractError(
                    "max_retries cannot change after retry scheduling begins"
                )
            retry_count = (existing["retry_count"] if existing else 0) + 1
            if retry_count > max_retries:
                con.execute(
                    "UPDATE messaging_messages SET status='failed', updated_at=? WHERE id=? AND status='accepted'",
                    (now, message_id),
                )
                con.execute("DELETE FROM messaging_delivery_leases WHERE message_id=?", (message_id,))
                return "exhausted"
            con.execute(
                """INSERT INTO messaging_delivery_retries(message_id,retry_count,max_retries,next_attempt_at,updated_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(message_id) DO UPDATE SET
                     retry_count=excluded.retry_count,
                     max_retries=excluded.max_retries,
                     next_attempt_at=excluded.next_attempt_at,
                     updated_at=excluded.updated_at""",
                (message_id, retry_count, max_retries, next_attempt, now),
            )
            con.execute("DELETE FROM messaging_delivery_leases WHERE message_id=?", (message_id,))
            return "scheduled"

    def reconcile_unknown(
        self, message_id: str, *, status: str, evidence: str,
        provider_message_id: str | None = None,
    ) -> dict:
        """Manually resolve an unknown delivery with an auditable operator note."""
        if status not in {"sent", "delivered", "failed"}:
            raise MessagingContractError("reconciliation status must be sent, delivered, or failed")
        if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 1000:
            raise MessagingContractError("reconciliation evidence is required and must be at most 1000 characters")
        if provider_message_id is not None and (
            not isinstance(provider_message_id, str) or not provider_message_id.strip()
            or len(provider_message_id) > MAX_ID_CHARS
        ):
            raise MessagingContractError("provider_message_id is invalid")
        now = _now()
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT status FROM messaging_messages WHERE id=? AND direction='outbound'",
                (message_id,),
            ).fetchone()
            if row is None:
                raise MessagingContractError("outbound message not found")
            if row["status"] != "unknown":
                raise MessagingContractError("only unknown messages can be reconciled")
            try:
                con.execute(
                    """UPDATE messaging_messages SET status=?,
                       provider_message_id=COALESCE(?,provider_message_id), updated_at=?
                       WHERE id=? AND status='unknown'""",
                    (status, provider_message_id, now, message_id),
                )
            except sqlite3.IntegrityError as exc:
                raise MessagingContractError(
                    "provider_message_id is already recorded for this channel"
                ) from exc
            con.execute(
                """INSERT INTO messaging_delivery_reconciliations
                   (id,message_id,previous_status,new_status,evidence,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (str(uuid4()), message_id, "unknown", status, evidence.strip(), now),
            )
            return {"message_id": message_id, "status": status, "reconciled_at": now}

    def update_outbound_status(
        self, channel: str, idempotency_key: str, status: str,
        *, provider_message_id: str | None = None,
    ) -> None:
        channel = _validate_channel(channel)
        if (
            not isinstance(idempotency_key, str)
            or not idempotency_key.strip()
            or len(idempotency_key) > MAX_ID_CHARS
        ):
            raise MessagingContractError("idempotency_key is invalid")
        idempotency_key = idempotency_key.strip()
        if status not in {"sent", "delivered", "failed", "unknown"}:
            raise MessagingContractError("invalid outbound delivery status")
        if provider_message_id is not None:
            if (
                not isinstance(provider_message_id, str)
                or not provider_message_id.strip()
                or len(provider_message_id) > MAX_ID_CHARS
            ):
                raise MessagingContractError("provider_message_id is invalid")
            provider_message_id = provider_message_id.strip()

        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                con.execute(
                    """UPDATE messaging_messages
                       SET status=?, provider_message_id=COALESCE(?, provider_message_id), updated_at=?
                       WHERE channel=? AND idempotency_key=? AND direction='outbound'
                         AND status='accepted'""",
                    (status, provider_message_id, _now(), channel, idempotency_key),
                )
            except sqlite3.IntegrityError as exc:
                raise MessagingContractError(
                    "provider_message_id is already recorded for this channel"
                ) from exc
            if con.execute("SELECT changes()").fetchone()[0] != 1:
                row = con.execute(
                    """SELECT status FROM messaging_messages
                       WHERE channel=? AND idempotency_key=? AND direction='outbound'""",
                    (channel, idempotency_key),
                ).fetchone()
                if row is None:
                    raise MessagingContractError("outbound message not found")
                raise MessagingContractError(
                    "outbound delivery status can only transition from accepted"
                )

    def get_outbound(self, channel: str, idempotency_key: str) -> dict | None:
        with self.connect() as con:
            row = con.execute(
                "SELECT * FROM messaging_messages WHERE channel=? AND idempotency_key=? AND direction='outbound'",
                (channel, idempotency_key),
            ).fetchone()
        return dict(row) if row else None

    def get_conversation(self, conversation_id: str) -> dict | None:
        if not isinstance(conversation_id, str) or not _CONVERSATION_RE.fullmatch(conversation_id):
            raise MessagingContractError("conversation ID is invalid")
        with self.connect() as con:
            row = con.execute(
                "SELECT id, channel, display_name, created_at, updated_at FROM messaging_conversations WHERE id=?",
                (conversation_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_message(self, message_id: str) -> dict | None:
        if not isinstance(message_id, str) or not message_id.strip() or len(message_id) > 128:
            raise MessagingContractError("message ID is invalid")
        with self.connect() as con:
            row = con.execute(
                "SELECT * FROM messaging_messages WHERE id=?", (message_id.strip(),)
            ).fetchone()
        return dict(row) if row else None

    def list_conversations(self, *, limit: int = 50) -> list[dict]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        with self.connect() as con:
            rows = con.execute(
                """SELECT c.id, c.channel, c.display_name, c.created_at, c.updated_at,
                          (SELECT m.text FROM messaging_messages m WHERE m.conversation_id=c.id
                           ORDER BY m.created_at DESC, m.id DESC LIMIT 1) AS last_message,
                          (SELECT m.direction FROM messaging_messages m WHERE m.conversation_id=c.id
                           ORDER BY m.created_at DESC, m.id DESC LIMIT 1) AS last_direction,
                          (SELECT m.status FROM messaging_messages m WHERE m.conversation_id=c.id
                           ORDER BY m.created_at DESC, m.id DESC LIMIT 1) AS last_status
                   FROM messaging_conversations c ORDER BY c.updated_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_messages(self, conversation_id: str, *, limit: int = 100) -> list[dict]:
        if not isinstance(conversation_id, str) or not _CONVERSATION_RE.fullmatch(conversation_id):
            raise MessagingContractError("conversation ID is invalid")
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM messaging_messages WHERE conversation_id=? ORDER BY created_at, id LIMIT ?",
                (conversation_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_outbound_attention(self, *, limit: int = 50) -> list[dict]:
        """List failed and uncertain outbound records without content or delivery targets."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        with self.connect() as con:
            rows = con.execute(
                """SELECT m.id AS message_id, m.channel, m.status,
                          m.created_at, m.updated_at,
                          COALESCE(r.retry_count, 0) AS retry_count,
                          COALESCE(r.max_retries, 0) AS max_retries,
                          r.next_attempt_at
                   FROM messaging_messages m
                   LEFT JOIN messaging_delivery_retries r ON r.message_id=m.id
                   WHERE m.direction='outbound' AND m.status IN ('failed','unknown')
                   ORDER BY m.updated_at DESC, m.id LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def delivery_metrics(self) -> dict:
        """Return aggregate outbound delivery counts without exposing message content or IDs."""
        with self.connect() as con:
            totals = con.execute(
                """SELECT status, COUNT(*) AS count
                   FROM messaging_messages
                   WHERE direction='outbound'
                   GROUP BY status"""
            ).fetchall()
            channels = con.execute(
                """SELECT channel, status, COUNT(*) AS count
                   FROM messaging_messages
                   WHERE direction='outbound'
                   GROUP BY channel, status
                   ORDER BY channel, status"""
            ).fetchall()
            recent_failures = con.execute(
                """SELECT channel, status, COUNT(*) AS count, MAX(updated_at) AS latest_at
                   FROM messaging_messages
                   WHERE direction='outbound' AND status IN ('failed','unknown')
                   GROUP BY channel, status
                   ORDER BY latest_at DESC"""
            ).fetchall()
        by_status = {row["status"]: row["count"] for row in totals}
        for status in ("accepted", "sent", "delivered", "failed", "unknown"):
            by_status.setdefault(status, 0)
        by_channel: dict[str, dict[str, int]] = {}
        for row in channels:
            by_channel.setdefault(row["channel"], {})[row["status"]] = row["count"]
        for counts in by_channel.values():
            for status in ("accepted", "sent", "delivered", "failed", "unknown"):
                counts.setdefault(status, 0)
        with self.connect() as con:
            queue = con.execute(
                """SELECT
                     SUM(CASE WHEN m.status='accepted' AND l.message_id IS NULL THEN 1 ELSE 0 END) AS pending,
                     SUM(CASE WHEN l.message_id IS NOT NULL THEN 1 ELSE 0 END) AS leased,
                     SUM(CASE WHEN m.status='unknown' THEN 1 ELSE 0 END) AS unknown,
                     MIN(CASE WHEN m.status='accepted' AND l.message_id IS NULL THEN m.created_at END) AS oldest_pending_at,
                     SUM(CASE WHEN r.message_id IS NOT NULL
                               AND m.status='accepted' AND l.message_id IS NULL
                               AND r.retry_count <= r.max_retries
                              THEN 1 ELSE 0 END) AS scheduled_retries
                   FROM messaging_messages m
                   LEFT JOIN messaging_delivery_leases l ON l.message_id=m.id
                   LEFT JOIN messaging_delivery_retries r ON r.message_id=m.id
                   WHERE m.direction='outbound'"""
            ).fetchone()
            retry_rows = con.execute(
                """SELECT COUNT(*) AS count, MIN(next_attempt_at) AS next_attempt_at
                   FROM messaging_delivery_retries r
                   JOIN messaging_messages m ON m.id=r.message_id
                   LEFT JOIN messaging_delivery_leases l ON l.message_id=m.id
                   WHERE m.status='accepted' AND l.message_id IS NULL
                     AND r.retry_count <= r.max_retries"""
            ).fetchone()
        return {
            "total_outbound": sum(by_status.values()),
            "by_status": by_status,
            "by_channel": by_channel,
            "queue": {
                "pending": queue["pending"] or 0,
                "leased": queue["leased"] or 0,
                "unknown": queue["unknown"] or 0,
                "scheduled_retries": queue["scheduled_retries"] or 0,
                "next_retry_at": retry_rows["next_attempt_at"],
                "oldest_pending_at": queue["oldest_pending_at"],
            },
            "recent_failures": [dict(row) for row in recent_failures[:20]],
        }

    def list_channels(self) -> list[str]:
        with self.connect() as con:
            rows = con.execute("SELECT DISTINCT channel FROM messaging_conversations ORDER BY channel").fetchall()
        return [row["channel"] for row in rows]
