"""SQLite-backed session registry."""
from __future__ import annotations
import sqlite3, time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    thread_id        TEXT PRIMARY KEY,
    guild_id         TEXT,
    channel_id       TEXT,
    owner_user_id    TEXT NOT NULL,
    workspace_id     TEXT NOT NULL,
    workspace_name   TEXT,
    cwd              TEXT NOT NULL,
    monitor_surface_id TEXT,
    acp_session_id   TEXT,
    status           TEXT NOT NULL,           -- starting|active|dead
    created_at       INTEGER NOT NULL,
    last_active_at   INTEGER NOT NULL,
    backend          TEXT NOT NULL DEFAULT 'cmux',
    abandoned_surface_id TEXT,
    last_backend_error TEXT,
    restore_attempt_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_acp_session ON sessions(acp_session_id);

CREATE TABLE IF NOT EXISTS inbound_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id       TEXT NOT NULL,
    author_user_id  TEXT NOT NULL,
    content         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    retry_count     INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    created_at      INTEGER NOT NULL,
    source_created_at INTEGER,
    discord_message_id TEXT,
    next_attempt_at INTEGER NOT NULL,
    delivered_at    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_inbound_pending
    ON inbound_messages(status, next_attempt_at, created_at);
CREATE INDEX IF NOT EXISTS idx_inbound_thread_status
    ON inbound_messages(thread_id, status);

CREATE TABLE IF NOT EXISTS inbound_message_dedup (
    thread_id          TEXT NOT NULL,
    discord_message_id TEXT NOT NULL,
    message_row_id     INTEGER NOT NULL,
    created_at         INTEGER NOT NULL,
    PRIMARY KEY (thread_id, discord_message_id)
);
"""


@dataclass
class SessionRow:
    thread_id: str
    guild_id: str | None
    channel_id: str
    owner_user_id: str
    workspace_id: str
    workspace_name: str | None
    cwd: str
    monitor_surface_id: str | None
    acp_session_id: str | None
    status: str
    created_at: int
    last_active_at: int
    backend: str = "cmux"
    abandoned_surface_id: str | None = None
    last_backend_error: str | None = None
    restore_attempt_at: int | None = None


@dataclass
class InboundMessageRow:
    id: int
    thread_id: str
    author_user_id: str
    content: str
    status: str
    retry_count: int
    last_error: str | None
    created_at: int
    source_created_at: int | None
    discord_message_id: str | None
    next_attempt_at: int
    delivered_at: int | None


class Registry:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate_sessions()
        self._migrate_inbound_messages()
        self.conn.commit()

    def _migrate_sessions(self) -> None:
        cur = self.conn.execute("PRAGMA table_info(sessions)")
        columns = {r["name"] for r in cur.fetchall()}
        if "backend" not in columns:
            self.conn.execute(
                "ALTER TABLE sessions ADD COLUMN backend TEXT NOT NULL DEFAULT 'cmux'")
        if "abandoned_surface_id" not in columns:
            self.conn.execute(
                "ALTER TABLE sessions ADD COLUMN abandoned_surface_id TEXT")
        if "last_backend_error" not in columns:
            self.conn.execute(
                "ALTER TABLE sessions ADD COLUMN last_backend_error TEXT")
        if "restore_attempt_at" not in columns:
            self.conn.execute(
                "ALTER TABLE sessions ADD COLUMN restore_attempt_at INTEGER")
        self.conn.execute(
            "UPDATE sessions SET backend='cmux' WHERE backend IS NULL OR backend=''")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_backend ON sessions(backend)")

    def _migrate_inbound_messages(self) -> None:
        cur = self.conn.execute("PRAGMA table_info(inbound_messages)")
        columns = {r["name"] for r in cur.fetchall()}
        if "source_created_at" not in columns:
            self.conn.execute(
                "ALTER TABLE inbound_messages ADD COLUMN source_created_at INTEGER")
            self.conn.execute(
                """UPDATE inbound_messages
                   SET source_created_at=created_at
                   WHERE source_created_at IS NULL""")
        if "discord_message_id" not in columns:
            self.conn.execute(
                "ALTER TABLE inbound_messages ADD COLUMN discord_message_id TEXT")
        self.conn.execute(
            """INSERT OR IGNORE INTO inbound_message_dedup (
                   thread_id, discord_message_id, message_row_id, created_at
               )
               SELECT thread_id, discord_message_id, MIN(id), MIN(created_at)
               FROM inbound_messages
               WHERE discord_message_id IS NOT NULL
               GROUP BY thread_id, discord_message_id"""
        )

    def insert(self, row: SessionRow) -> None:
        # INSERT OR REPLACE so re-attaching to a thread whose row still
        # exists (e.g. status='dead' from a prior crash) doesn't raise.
        self.conn.execute(
            """INSERT OR REPLACE INTO sessions (
                   thread_id, guild_id, channel_id, owner_user_id,
                   workspace_id, workspace_name, cwd, monitor_surface_id,
                   acp_session_id, status, created_at, last_active_at,
                   backend, abandoned_surface_id, last_backend_error,
                   restore_attempt_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row.thread_id, row.guild_id, row.channel_id, row.owner_user_id,
             row.workspace_id, row.workspace_name, row.cwd, row.monitor_surface_id,
             row.acp_session_id, row.status, row.created_at, row.last_active_at,
             row.backend, row.abandoned_surface_id, row.last_backend_error,
             row.restore_attempt_at),
        )
        self.conn.commit()

    def get_by_thread(self, thread_id: str) -> SessionRow | None:
        cur = self.conn.execute("SELECT * FROM sessions WHERE thread_id=?", (thread_id,))
        r = cur.fetchone()
        return SessionRow(**dict(r)) if r else None

    def list_active(self) -> list[SessionRow]:
        cur = self.conn.execute("SELECT * FROM sessions WHERE status='active'")
        return [SessionRow(**dict(r)) for r in cur.fetchall()]

    def update_status(self, thread_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE sessions SET status=?, last_active_at=? WHERE thread_id=?",
            (status, int(time.time()), thread_id),
        )
        self.conn.commit()

    def touch(self, thread_id: str) -> None:
        self.conn.execute(
            "UPDATE sessions SET last_active_at=? WHERE thread_id=?",
            (int(time.time()), thread_id),
        )
        self.conn.commit()

    def set_acp_session(self, thread_id: str, acp_session_id: str) -> None:
        self.conn.execute(
            "UPDATE sessions SET acp_session_id=?, status='active' WHERE thread_id=?",
            (acp_session_id, thread_id),
        )
        self.conn.commit()

    def set_backend(self, thread_id: str, backend: str, *,
                    surface_id: str | None = None,
                    abandoned_surface_id: str | None = None,
                    last_error: str | None = None,
                    restore_attempt_at: int | None = None) -> None:
        """Update the active delivery backend for a session.

        `backend` is intentionally stored as text instead of an enum so older
        databases can be migrated in-place and inspected with sqlite tools.
        The bridge currently uses: cmux, wire, restoring.
        """
        if backend not in {"cmux", "wire", "restoring"}:
            raise ValueError(f"invalid backend: {backend}")
        self.conn.execute(
            """UPDATE sessions
               SET backend=?, monitor_surface_id=?, abandoned_surface_id=?,
                   last_backend_error=?, restore_attempt_at=?,
                   last_active_at=?
               WHERE thread_id=?""",
            (backend, surface_id, abandoned_surface_id,
             last_error[:1000] if last_error else None, restore_attempt_at,
             int(time.time()), thread_id),
        )
        self.conn.commit()

    def update_surface(self, thread_id: str, surface_id: str | None) -> None:
        self.conn.execute(
            """UPDATE sessions
               SET monitor_surface_id=?, last_active_at=?
               WHERE thread_id=?""",
            (surface_id, int(time.time()), thread_id),
        )
        self.conn.commit()

    def set_backend_error(self, thread_id: str, error: str | None) -> None:
        self.conn.execute(
            """UPDATE sessions
               SET last_backend_error=?, last_active_at=?
               WHERE thread_id=?""",
            (error[:1000] if error else None, int(time.time()), thread_id),
        )
        self.conn.commit()

    def enqueue_message(self, *, thread_id: str, author_user_id: str,
                        content: str, source_created_at: int | None = None,
                        discord_message_id: str | None = None) -> int:
        now = int(time.time())
        source_ts = now if source_created_at is None else int(source_created_at)
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            if discord_message_id:
                dedup = self.conn.execute(
                    """INSERT OR IGNORE INTO inbound_message_dedup (
                           thread_id, discord_message_id, message_row_id,
                           created_at
                       )
                       VALUES (?, ?, 0, ?)""",
                    (thread_id, discord_message_id, now),
                )
                if dedup.rowcount == 0:
                    row = self.conn.execute(
                        """SELECT message_row_id FROM inbound_message_dedup
                           WHERE thread_id=? AND discord_message_id=?""",
                        (thread_id, discord_message_id),
                    ).fetchone()
                    self.conn.commit()
                    return int(row["message_row_id"])

            cur = self.conn.execute(
                """INSERT INTO inbound_messages
                   (thread_id, author_user_id, content, status, retry_count,
                    last_error, created_at, source_created_at,
                    discord_message_id, next_attempt_at, delivered_at)
                   VALUES (?, ?, ?, 'pending', 0, NULL, ?, ?, ?, ?, NULL)""",
                (thread_id, author_user_id, content, now, source_ts,
                 discord_message_id, now),
            )
            message_id = int(cur.lastrowid)
            if discord_message_id:
                self.conn.execute(
                    """UPDATE inbound_message_dedup
                       SET message_row_id=?
                       WHERE thread_id=? AND discord_message_id=?""",
                    (message_id, thread_id, discord_message_id),
                )
            self.conn.commit()
            return message_id
        except Exception:
            self.conn.rollback()
            raise

    def list_pending_messages(self, *, limit: int = 20,
                              now: int | None = None) -> list[InboundMessageRow]:
        ts = int(time.time()) if now is None else now
        cur = self.conn.execute(
            """SELECT * FROM inbound_messages
               WHERE status='pending' AND next_attempt_at<=?
               ORDER BY created_at ASC, id ASC
               LIMIT ?""",
            (ts, limit),
        )
        return [InboundMessageRow(**dict(r)) for r in cur.fetchall()]

    def claim_pending_messages(self, *, limit: int = 20,
                               now: int | None = None,
                               lease_seconds: int = 120) -> list[InboundMessageRow]:
        ts = int(time.time()) if now is None else now
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute(
                """UPDATE inbound_messages
                   SET status='pending'
                   WHERE status='inflight' AND next_attempt_at<=?""",
                (ts,),
            )
            cur = self.conn.execute(
                """SELECT * FROM inbound_messages
                   WHERE status='pending' AND next_attempt_at<=?
                   ORDER BY created_at ASC, id ASC
                   LIMIT ?""",
                (ts, limit),
            )
            rows = cur.fetchall()
            ids = [int(r["id"]) for r in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                self.conn.execute(
                    f"""UPDATE inbound_messages
                        SET status='inflight', next_attempt_at=?
                        WHERE id IN ({placeholders}) AND status='pending'""",
                    (ts + lease_seconds, *ids),
                )
            self.conn.commit()
            return [InboundMessageRow(**dict(r)) for r in rows]
        except Exception:
            self.conn.rollback()
            raise

    def release_message(self, message_id: int, *, delay: int = 1) -> None:
        now = int(time.time())
        self.conn.execute(
            """UPDATE inbound_messages
               SET status='pending', next_attempt_at=?
               WHERE id=? AND status='inflight'""",
            (now + max(0, delay), message_id),
        )
        self.conn.commit()

    def mark_message_delivered(self, message_id: int) -> None:
        now = int(time.time())
        self.conn.execute(
            """UPDATE inbound_messages
               SET status='delivered', delivered_at=?, last_error=NULL
               WHERE id=?""",
            (now, message_id),
        )
        self.conn.commit()

    def mark_message_failed(self, message_id: int, error: str, *,
                            terminal: bool = False) -> None:
        now = int(time.time())
        status = "failed" if terminal else "pending"
        retry_sql = "retry_count + 1"
        # Keep transient retry delays bounded; cmux outages should recover
        # promptly but not spin the event loop.
        cur = self.conn.execute(
            "SELECT retry_count FROM inbound_messages WHERE id=?",
            (message_id,),
        )
        row = cur.fetchone()
        retry_count = int(row["retry_count"]) + 1 if row else 1
        delay = 0 if terminal else min(60, 2 ** min(retry_count, 6))
        self.conn.execute(
            f"""UPDATE inbound_messages
                SET status=?, retry_count={retry_sql}, last_error=?,
                    next_attempt_at=?
                WHERE id=?""",
            (status, error[:1000], now + delay, message_id),
        )
        self.conn.commit()

    def mark_message_skipped_stale(self, message_id: int, error: str) -> None:
        now = int(time.time())
        self.conn.execute(
            """UPDATE inbound_messages
               SET status='skipped_stale', last_error=?, next_attempt_at=?
               WHERE id=?""",
            (error[:1000], now, message_id),
        )
        self.conn.commit()

    def fail_pending_for_thread(self, thread_id: str, reason: str) -> None:
        now = int(time.time())
        self.conn.execute(
            """UPDATE inbound_messages
               SET status='failed', last_error=?, next_attempt_at=?
               WHERE thread_id=? AND status IN ('pending', 'inflight')""",
            (reason[:1000], now, thread_id),
        )
        self.conn.commit()

    def count_pending_messages(self, thread_id: str | None = None) -> int:
        if thread_id is None:
            cur = self.conn.execute(
                "SELECT COUNT(*) AS n FROM inbound_messages WHERE status='pending'")
        else:
            cur = self.conn.execute(
                """SELECT COUNT(*) AS n FROM inbound_messages
                   WHERE status='pending' AND thread_id=?""",
                (thread_id,),
            )
        return int(cur.fetchone()["n"])

    def last_delivery_error(self, thread_id: str | None = None) -> str | None:
        if thread_id is None:
            cur = self.conn.execute(
                """SELECT last_error FROM inbound_messages
                   WHERE last_error IS NOT NULL
                   ORDER BY id DESC LIMIT 1""")
        else:
            cur = self.conn.execute(
                """SELECT last_error FROM inbound_messages
                   WHERE thread_id=? AND last_error IS NOT NULL
                   ORDER BY id DESC LIMIT 1""",
                (thread_id,),
            )
        row = cur.fetchone()
        return str(row["last_error"]) if row and row["last_error"] else None
