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

    # lock acquisition timeout (seconds)
    _LOCK_TIMEOUT = 30

    def __init__(self, db_path: str, shared_group_name: str = "staff"):
        self.db_path = db_path
        self._shared_group_name = shared_group_name
        # Resolve GID (may raise KeyError if group does not exist)
        import grp as _grp
        self._shared_group_gid = _grp.getgrnam(shared_group_name).gr_gid
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._fix_db_dir_permissions()

        # Detect NFS and warn
        self._on_nfs = self._is_nfs(os.path.dirname(self.db_path) or ".")
        if self._on_nfs:
            logger.warning(
                f"DB directory is on NFS ({os.path.dirname(self.db_path)}). "
                f"Using DELETE journal mode + file lock to avoid corruption. "
                f"For best safety set db_path to a local path in config.yaml."
            )

        self._init_db()

    # ------------------------------------------------------------------
    # NFS detection
    # ------------------------------------------------------------------

    @staticmethod
    def _is_nfs(dirpath: str) -> bool:
        """Check whether *dirpath* resides on an NFS filesystem."""
        try:
            import subprocess
            out = subprocess.run(
                ["stat", "-f", "-c", "%T", dirpath],
                capture_output=True, text=True, timeout=5,
            )
            fstype = out.stdout.strip().lower()
            return fstype in ("nfs", "nfs4")
        except Exception:
            return False

    # ------------------------------------------------------------------
    # DB-level write lock (O_CREAT|O_EXCL — atomic even on NFS)
    # ------------------------------------------------------------------

    def _acquire_db_lock(self) -> str:
        """Create a lock file atomically.  Returns the lock path on success.

        Retries with backoff when another writer holds the lock, up to
        ``_LOCK_TIMEOUT`` seconds.  Raises ``RuntimeError`` on timeout.
        """
        lock_path = self.db_path + ".lock"
        deadline = time.time() + self._LOCK_TIMEOUT
        while True:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                with os.fdopen(fd, "w") as f:
                    f.write(f"pid={os.getpid()}\n")
                return lock_path
            except FileExistsError:
                pass  # another process holds the lock — wait

            # Check whether the lock holder is still alive
            try:
                with open(lock_path, "r") as f:
                    content = f.read()
                stale = False
                for line in content.split("\n"):
                    if line.startswith("pid="):
                        pid = int(line.split("=", 1)[1])
                        try:
                            os.kill(pid, 0)  # signal 0 = existence check
                        except OSError:
                            # Process is dead — remove stale lock
                            logger.warning(f"Removing stale DB lock (pid={pid} is dead)")
                            stale = True
                        break
                if stale:
                    try:
                        os.unlink(lock_path)
                    except OSError:
                        pass
                    continue  # jump to top of while-loop, retry O_EXCL immediately
            except Exception:
                pass

            if time.time() >= deadline:
                raise RuntimeError(
                    f"Database write lock timeout ({self._LOCK_TIMEOUT}s). "
                    f"Another submit may be in progress. "
                    f"If stuck, remove: {lock_path}"
                )
            time.sleep(1)

    def _release_db_lock(self, lock_path: str):
        """Remove the lock file (best-effort)."""
        try:
            os.unlink(lock_path)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # connection management
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # WAL uses mmap'd -shm which is broken across NFS clients.
        # DELETE mode creates a per-transaction -journal file — safe
        # when combined with the O_EXCL write lock.
        if self._on_nfs:
            conn.execute("PRAGMA journal_mode=DELETE")
            # FULL is safest across NFS power loss; NORMAL offers a good
            # trade-off and is still safer than OFF which the WAL default
            # effectively provides through checkpointing.
            conn.execute("PRAGMA synchronous=NORMAL")
        else:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        return dict(zip(row.keys(), row))

    @contextmanager
    def _tx(self):
        """Write-transaction context manager with DB-level file lock."""
        lock = self._acquire_db_lock()
        try:
            conn = self._connect()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        finally:
            self._release_db_lock(lock)

    def _init_db(self):
        is_new = not os.path.exists(self.db_path)
        with self._tx() as conn:
            conn.executescript(DDL)
        # Always refresh permissions — covers the first-creation case *and*
        # the case where a previous creator's chown/chmod was silently skipped.
        self._fix_db_permissions()
        if is_new:
            logger.info(f"Database created at {self.db_path}")
        else:
            logger.info(f"Database initialized at {self.db_path}")

    def _fix_db_dir_permissions(self):
        """Ensure the database parent directory has shared-group ownership + SGID."""
        db_dir = os.path.dirname(self.db_path) or "."
        if not os.path.isdir(db_dir):
            return
        try:
            os.chown(db_dir, -1, self._shared_group_gid)
        except (PermissionError, OSError):
            pass
        try:
            os.chmod(db_dir, 0o2775)
        except (PermissionError, OSError):
            pass

    def _fix_db_permissions(self):
        """Ensure DB file and WAL/SHM companions are group-owned and writable."""
        gid = self._shared_group_gid
        for suffix in ("", "-wal", "-shm"):
            path = self.db_path + suffix
            if not os.path.exists(path):
                continue
            try:
                os.chown(path, -1, gid)
            except (PermissionError, OSError):
                pass
            try:
                os.chmod(path, 0o664)
            except (PermissionError, OSError):
                pass

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
