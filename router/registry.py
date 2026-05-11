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
    last_active_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_acp_session ON sessions(acp_session_id);
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


class Registry:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def insert(self, row: SessionRow) -> None:
        self.conn.execute(
            """INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row.thread_id, row.guild_id, row.channel_id, row.owner_user_id,
             row.workspace_id, row.workspace_name, row.cwd, row.monitor_surface_id,
             row.acp_session_id, row.status, row.created_at, row.last_active_at),
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
