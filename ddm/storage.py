"""SQLite persistence layer for DDM state machine.

Tables:
  batches  — submit/release batches with status (PENDING/SUBMITTED/RELEASED/FAILED)
  files    — individual file records with BLAKE3 hash and size
  events   — audit log of state transitions
"""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Optional

from loguru import logger

# Valid status values
STATUS_PENDING = "PENDING"
STATUS_SUBMITTED = "SUBMITTED"
STATUS_RELEASED = "RELEASED"
STATUS_FAILED = "FAILED"
STATUS_SUPERSEDED = "SUPERSEDED"

VALID_STATUSES = {STATUS_PENDING, STATUS_SUBMITTED, STATUS_RELEASED, STATUS_FAILED, STATUS_SUPERSEDED}

# Valid event types
EVENT_SUBMIT_START = "submit_start"
EVENT_COPY_DONE = "copy_done"
EVENT_PRE_CHECK_OK = "pre_check_ok"
EVENT_PRE_CHECK_FAIL = "pre_check_fail"
EVENT_GATE_PASS = "gate_pass"
EVENT_GATE_FAIL = "gate_fail"
EVENT_DELIVERED = "delivered"
EVENT_SUBMITTED = "submitted"
EVENT_RELEASE_START = "release_start"
EVENT_RELEASE_DONE = "release_done"
EVENT_POST_CHECK_OK = "post_check_ok"
EVENT_POST_CHECK_FAIL = "post_check_fail"
EVENT_LOCK_BLOCKED = "lock_blocked"
EVENT_DISK_FULL = "disk_full"
EVENT_FAILED = "failed"


DDL = """
CREATE TABLE IF NOT EXISTS batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_uuid  TEXT NOT NULL UNIQUE,
    module      TEXT NOT NULL,
    tag         TEXT NOT NULL,
    username    TEXT NOT NULL,
    version     TEXT,
    summary     TEXT,
    status      TEXT NOT NULL DEFAULT 'PENDING',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_uuid   TEXT NOT NULL,
    source_path  TEXT NOT NULL,
    raw_path     TEXT,
    ready_path   TEXT,
    release_path TEXT,
    file_size    INTEGER NOT NULL DEFAULT 0,
    source_size  INTEGER NOT NULL DEFAULT 0,
    source_mtime REAL NOT NULL DEFAULT 0,
    blake3_hash  TEXT,
    status       TEXT NOT NULL DEFAULT 'PENDING',
    created_at   REAL NOT NULL,
    FOREIGN KEY (batch_uuid) REFERENCES batches(batch_uuid)
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_uuid  TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    message     TEXT,
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_batches_module   ON batches(module);
CREATE INDEX IF NOT EXISTS idx_batches_tag      ON batches(tag);
CREATE INDEX IF NOT EXISTS idx_batches_status   ON batches(status);
CREATE INDEX IF NOT EXISTS idx_files_batch      ON files(batch_uuid);
CREATE INDEX IF NOT EXISTS idx_events_batch     ON events(batch_uuid);
"""


class Storage:
    """Encapsulates all SQLite CRUD for DDM state tracking."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._init_db()

    # ------------------------------------------------------------------
    # connection management
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        return dict(zip(row.keys(), row))

    @contextmanager
    def _tx(self):
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        is_new = not os.path.exists(self.db_path)
        with self._tx() as conn:
            conn.executescript(DDL)
        # Ensure DB file is group-readable (only owner can chmod)
        if is_new:
            try:
                import os as _os
                _os.chmod(self.db_path, 0o664)
            except (PermissionError, OSError):
                pass
        logger.info(f"Database initialized at {self.db_path}")

    # ------------------------------------------------------------------
    # batches
    # ------------------------------------------------------------------

    def create_batch(
        self,
        module: str,
        tag: str,
        username: str,
        summary: str = "",
        version: str = "",
    ) -> str:
        """Create a new batch and return its UUID."""
        batch_uuid = str(uuid.uuid4())
        now = time.time()
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO batches (batch_uuid, module, tag, username, version, summary, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (batch_uuid, module, tag, username, version, summary, STATUS_PENDING, now, now),
            )
        logger.info(f"Batch created: {batch_uuid} module={module} tag={tag}")
        return batch_uuid

    def update_batch_status(self, batch_uuid: str, status: str, version: str = ""):
        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid status: {status}")
        now = time.time()
        with self._tx() as conn:
            if version:
                conn.execute(
                    "UPDATE batches SET status = ?, version = ?, updated_at = ? WHERE batch_uuid = ?",
                    (status, version, now, batch_uuid),
                )
            else:
                conn.execute(
                    "UPDATE batches SET status = ?, updated_at = ? WHERE batch_uuid = ?",
                    (status, now, batch_uuid),
                )

    def get_batch(self, batch_uuid: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM batches WHERE batch_uuid = ?", (batch_uuid,)
            ).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def list_batches(
        self,
        module: Optional[str] = None,
        tag: Optional[str] = None,
        status: Optional[str] = None,
        since: Optional[float] = None,
    ) -> list[dict]:
        query = "SELECT * FROM batches WHERE 1=1"
        params: list = []
        if module:
            query += " AND module = ?"
            params.append(module)
        if tag:
            query += " AND tag = ?"
            params.append(tag)
        if status:
            query += " AND status = ?"
            params.append(status)
        if since:
            query += " AND created_at >= ?"
            params.append(since)
        query += " ORDER BY created_at DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_submitted_batches(self, tag: Optional[str] = None, module: Optional[str] = None) -> list[dict]:
        """Get SUBMITTED batches, keeping only the latest per module."""
        all_batches = self.list_batches(module=module, tag=tag, status=STATUS_SUBMITTED)
        # Deduplicate: keep only the latest batch per module
        latest = {}
        for b in all_batches:
            key = b["module"]
            if key not in latest or b["created_at"] > latest[key]["created_at"]:
                latest[key] = b
        return sorted(latest.values(), key=lambda b: b["module"])

    # ------------------------------------------------------------------
    # files
    # ------------------------------------------------------------------

    def add_file(
        self,
        batch_uuid: str,
        source_path: str,
        file_size: int = 0,
        blake3_hash: str = "",
        source_size: int = 0,
        source_mtime: float = 0.0,
    ) -> int:
        now = time.time()
        with self._tx() as conn:
            cur = conn.execute(
                """INSERT INTO files (batch_uuid, source_path, file_size, blake3_hash,
                   source_size, source_mtime, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (batch_uuid, source_path, file_size, blake3_hash,
                 source_size, source_mtime, STATUS_PENDING, now),
            )
            return cur.lastrowid

    def update_file_raw(self, batch_uuid: str, source_path: str, raw_path: str, file_size: int, blake3_hash: str):
        with self._tx() as conn:
            conn.execute(
                """UPDATE files SET raw_path = ?, file_size = ?, blake3_hash = ?, status = ?
                   WHERE batch_uuid = ? AND source_path = ?""",
                (raw_path, file_size, blake3_hash, STATUS_PENDING, batch_uuid, source_path),
            )

    def update_file_ready(self, batch_uuid: str, source_path: str, ready_path: str):
        with self._tx() as conn:
            conn.execute(
                "UPDATE files SET ready_path = ?, status = ? WHERE batch_uuid = ? AND source_path = ?",
                (ready_path, STATUS_SUBMITTED, batch_uuid, source_path),
            )

    def update_file_released(self, batch_uuid: str, source_path: str, release_path: str):
        with self._tx() as conn:
            conn.execute(
                "UPDATE files SET release_path = ?, status = ? WHERE batch_uuid = ? AND source_path = ?",
                (release_path, STATUS_RELEASED, batch_uuid, source_path),
            )

    def update_file_failed(self, batch_uuid: str, source_path: str):
        with self._tx() as conn:
            conn.execute(
                "UPDATE files SET status = ? WHERE batch_uuid = ? AND source_path = ?",
                (STATUS_FAILED, batch_uuid, source_path),
            )

    def get_file_by_source(self, batch_uuid: str, source_path: str) -> dict | None:
        """Get a single file record by batch_uuid and source_path."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM files WHERE batch_uuid = ? AND source_path = ?",
                (batch_uuid, source_path),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def get_files(self, batch_uuid: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM files WHERE batch_uuid = ?", (batch_uuid,)
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ------------------------------------------------------------------
    # events (audit log)
    # ------------------------------------------------------------------

    def add_event(self, batch_uuid: str, event_type: str, message: str = ""):
        now = time.time()
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO events (batch_uuid, event_type, message, created_at) VALUES (?, ?, ?, ?)",
                (batch_uuid, event_type, message, now),
            )
        logger.info(f"[{batch_uuid[:8]}] {event_type}: {message}")

    def get_events(self, batch_uuid: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE batch_uuid = ? ORDER BY created_at ASC",
                (batch_uuid,),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]
