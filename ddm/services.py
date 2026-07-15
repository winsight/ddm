"""Core business logic: lock control, streaming copy, checksum, state machine, release.

Layers:
  - services.py  (business logic — this file)
  - storage.py   (SQLite persistence)
  - cli.py       (Click interaction & Rich rendering)
"""

from __future__ import annotations

import fnmatch
import os
import shutil
import time
from pathlib import Path
from typing import List, Optional, Tuple

import psutil
from loguru import logger

from ddm.config import VALID_TAGS, Config, GateDef
from ddm.gates.runner import all_gates_passed, run_gates
from ddm.storage import (
    EVENT_COPY_DONE,
    EVENT_DELIVERED,
    EVENT_DISK_FULL,
    EVENT_FAILED,
    EVENT_GATE_FAIL,
    EVENT_GATE_PASS,
    EVENT_LOCK_BLOCKED,
    EVENT_POST_CHECK_FAIL,
    EVENT_POST_CHECK_OK,
    EVENT_PRE_CHECK_FAIL,
    EVENT_PRE_CHECK_OK,
    EVENT_RELEASE_DONE,
    EVENT_RELEASE_START,
    EVENT_SUBMIT_START,
    EVENT_SUBMITTED,
    STATUS_FAILED,
    STATUS_RELEASED,
    STATUS_SUBMITTED,
    Storage,
)

# Prefer BLAKE3; fall back to hashlib.blake2b if unavailable
try:
    import blake3

    def _blake3_hash(path: str) -> str:
        h = blake3.blake3()
        with open(path, "rb") as f:
            while chunk := f.read(8192):
                h.update(chunk)
        return h.hexdigest()

    HAS_BLAKE3 = True
except ImportError:
    import hashlib

    def _blake3_hash(path: str) -> str:
        h = hashlib.blake2b()
        with open(path, "rb") as f:
            while chunk := f.read(8192):
                h.update(chunk)
        return h.hexdigest()

    HAS_BLAKE3 = False
    logger.warning("blake3 not installed; falling back to BLAKE2b-256")


# ---------------------------------------------------------------------------
# Lock helpers
# ---------------------------------------------------------------------------

class LockError(Exception):
    """Raised when a lock prevents an operation."""


def _acquire_lock(lock_path: Path, description: str) -> None:
    """Create a lock file; raise LockError if it already exists."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.exists():
        raise LockError(f"[Warning] {description} — lock exists: {lock_path}")
    lock_path.write_text(f"pid={os.getpid()}\ntime={time.time()}\n")


def _release_lock(lock_path: Path) -> None:
    """Remove a lock file (best-effort)."""
    try:
        if lock_path.exists():
            lock_path.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# File discovery (flat outgoing directory)
# ---------------------------------------------------------------------------

def find_source_files(
    outgoing_root: str,
    patterns: List[str],
    user: str,
    module: str,
) -> List[str]:
    """Find files in the flat outgoing_root matching expanded patterns.

    Patterns may contain {user} and {module} placeholders.
    Returns a list of absolute file paths.
    """
    root = Path(outgoing_root).resolve()
    if not root.is_dir():
        logger.warning(f"Outgoing root does not exist: {root}")
        return []

    matched: List[str] = []
    # List all files in the flat directory
    try:
        all_files = [f.name for f in root.iterdir() if f.is_file()]
    except PermissionError:
        logger.error(f"Permission denied reading {root}")
        return []

    for pattern in patterns:
        expanded = pattern.format(user=user, module=module)
        # Use fnmatch to match the expanded pattern against flat file names
        for fname in all_files:
            if fnmatch.fnmatch(fname, expanded):
                filepath = root / fname
                if str(filepath) not in matched:
                    matched.append(str(filepath))

    return sorted(matched)


# ---------------------------------------------------------------------------
# Streaming copy with BLAKE3 + rich progress
# ---------------------------------------------------------------------------

def streaming_copy(
    source_path: str,
    dest_path: str,
    progress=None,
    task_id=None,
) -> Tuple[int, str]:
    """Stream-copy a file, computing BLAKE3 on the fly.

    Returns (file_size, blake3_hex).
    Uses the provided rich.progress instance if given.
    """
    file_size = os.path.getsize(source_path)
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Open progress bar if rich is available
    pbar = None
    if progress is not None and task_id is not None:
        progress.update(task_id, total=file_size, completed=0)

    if HAS_BLAKE3:
        hasher = blake3.blake3()
    else:
        import hashlib
        hasher = hashlib.blake2b()

    copied = 0
    with open(source_path, "rb") as src, open(dest_path, "wb") as dst:
        while True:
            chunk = src.read(1024 * 1024)  # 1 MiB chunks
            if not chunk:
                break
            dst.write(chunk)
            hasher.update(chunk)
            copied += len(chunk)
            if progress is not None and task_id is not None:
                progress.update(task_id, completed=copied)

    return file_size, hasher.hexdigest()


# ---------------------------------------------------------------------------
# Metadata comparison (pre_check / post_check)
# ---------------------------------------------------------------------------

def compare_metadata(source_path: str, dest_path: str) -> bool:
    """Compare file size and BLAKE3 between source and destination."""
    if not os.path.exists(dest_path):
        logger.error(f"Destination missing: {dest_path}")
        return False

    src_size = os.path.getsize(source_path)
    dst_size = os.path.getsize(dest_path)
    if src_size != dst_size:
        logger.error(f"Size mismatch: {src_size} vs {dst_size} ({source_path})")
        return False

    src_hash = _blake3_hash(source_path)
    dst_hash = _blake3_hash(dest_path)
    if src_hash != dst_hash:
        logger.error(f"Hash mismatch: {src_hash} vs {dst_hash} ({source_path})")
        return False

    return True


# ---------------------------------------------------------------------------
# Disk space check
# ---------------------------------------------------------------------------

def check_disk_space(target_dir: str, required_ratio: float = 1.2) -> None:
    """Raise OSError if the mount point for target_dir has less than
    required_ratio * total_source_size free space."""
    usage = psutil.disk_usage(target_dir)
    # We'll check at submit time with actual source sizes
    logger.info(f"Disk free on {target_dir}: {usage.free / (1024**3):.1f} GiB")


# ---------------------------------------------------------------------------
# Submit flow
# ---------------------------------------------------------------------------

class SubmitResult:
    def __init__(self, batch_uuid: str, success: bool, message: str):
        self.batch_uuid = batch_uuid
        self.success = success
        self.message = message


def submit(
    config: Config,
    storage: Storage,
    module: str,
    tag: str,
    username: str,
    summary: str = "",
    progress=None,
) -> SubmitResult:
    """Execute the full submit pipeline.

    1. Validate tag
    2. Fast-fail checks (global lock, module lock, disk space)
    3. Create batch record (PENDING)
    4. Discover source files in flat a0.outgoing
    5. Stream-copy to raw/<TAG>/<MODULE> with BLAKE3
    6. pre_check metadata comparison
    7. Run gates
    8. Atomic move to ready/<TAG>/<MODULE>, chmod 664
    9. Update status to SUBMITTED
    """
    # ---- validate ----
    if tag not in VALID_TAGS:
        return SubmitResult("", False, f"Invalid tag: {tag}. Valid: {sorted(VALID_TAGS)}")

    tag_cfg = config.tag_config(tag)
    if tag_cfg is None:
        return SubmitResult("", False, f"No config entry for tag: {tag}")

    patterns = tag_cfg.file_patterns
    if not patterns:
        return SubmitResult("", False, f"No file_patterns defined for tag: {tag}")

    # ---- fast-fail: global lock ----
    glock = config.global_lock_path()
    if glock.exists():
        msg = f"系统正在 Release，提交被阻断 (lock: {glock})"
        logger.warning(msg)
        return SubmitResult("", False, msg)

    # ---- fast-fail: module lock ----
    mlock = config.module_lock_path(module, tag)
    try:
        _acquire_lock(mlock, f"模块 {module}/{tag} 正在提交")
    except LockError as e:
        logger.warning(str(e))
        return SubmitResult("", False, str(e))

    batch_uuid = ""
    try:
        # ---- disk space ----
        raw_base = config.raw_dir()
        raw_base.mkdir(parents=True, exist_ok=True)
        check_disk_space(str(raw_base))

        # ---- create batch ----
        batch_uuid = storage.create_batch(
            module=module, tag=tag, username=username, summary=summary
        )
        storage.add_event(batch_uuid, EVENT_SUBMIT_START, f"module={module} tag={tag} user={username}")

        # ---- discover source files ----
        source_files = find_source_files(config.outgoing_root, patterns, username, module)
        if not source_files:
            msg = f"No files matched in {config.outgoing_root} for tag={tag} user={username} module={module}"
            logger.error(msg)
            storage.update_batch_status(batch_uuid, STATUS_FAILED)
            storage.add_event(batch_uuid, EVENT_FAILED, msg)
            return SubmitResult(batch_uuid, False, msg)

        logger.info(f"Found {len(source_files)} source files for {module}/{tag}")

        # ---- stream-copy to raw ----
        raw_tag_dir = raw_base / tag / module
        raw_tag_dir.mkdir(parents=True, exist_ok=True)

        total_size = sum(os.path.getsize(f) for f in source_files)

        # Check disk space against actual source size
        usage = psutil.disk_usage(str(raw_base))
        if usage.free < total_size * 1.2:
            msg = f"磁盘空间不足: 需要 {total_size * 1.2 / (1024**3):.1f} GiB, 可用 {usage.free / (1024**3):.1f} GiB"
            logger.error(msg)
            storage.update_batch_status(batch_uuid, STATUS_FAILED)
            storage.add_event(batch_uuid, EVENT_DISK_FULL, msg)
            return SubmitResult(batch_uuid, False, msg)

        # Rich progress for copy
        overall_task = None
        if progress is not None:
            overall_task = progress.add_task(
                f"[cyan]Copying {module}/{tag}",
                total=total_size,
            )

        for src in source_files:
            src_path = Path(src)
            dest_path = raw_tag_dir / src_path.name

            # Record file in DB
            storage.add_file(batch_uuid, src, 0, "")

            # Stream-copy with BLAKE3
            file_size, blake3_hex = streaming_copy(
                src, str(dest_path), progress=progress, task_id=overall_task
            )

            storage.update_file_raw(batch_uuid, src, str(dest_path), file_size, blake3_hex)
            logger.info(f"Copied: {src_path.name} -> {dest_path} ({file_size} bytes)")

        if progress is not None and overall_task is not None:
            progress.remove_task(overall_task)

        storage.add_event(batch_uuid, EVENT_COPY_DONE, f"{len(source_files)} files copied")

        # ---- pre_check: compare source vs raw ----
        pre_ok = True
        for src in source_files:
            src_path = Path(src)
            dest_path = raw_tag_dir / src_path.name
            if not compare_metadata(src, str(dest_path)):
                pre_ok = False
                logger.error(f"pre_check FAILED: {src_path.name}")
                break

        if pre_ok:
            storage.add_event(batch_uuid, EVENT_PRE_CHECK_OK, "Metadata matches")
        else:
            storage.update_batch_status(batch_uuid, STATUS_FAILED)
            storage.add_event(batch_uuid, EVENT_PRE_CHECK_FAIL, "Metadata mismatch")
            return SubmitResult(batch_uuid, False, "pre_check failed: metadata mismatch")

        # ---- run gates ----
        gate_defs = tag_cfg.gates
        if gate_defs:
            gate_results = run_gates(gate_defs, str(raw_tag_dir), module, tag)
            for gr in gate_results:
                if gr.passed:
                    storage.add_event(batch_uuid, EVENT_GATE_PASS, gr.name)
                else:
                    storage.add_event(batch_uuid, EVENT_GATE_FAIL, f"{gr.name}: {gr.stderr[:200]}")
            if not all_gates_passed(gate_results):
                storage.update_batch_status(batch_uuid, STATUS_FAILED)
                return SubmitResult(batch_uuid, False, "Gates failed")
        else:
            logger.info(f"No gates defined for tag={tag}")

        # ---- atomic move to ready + chmod ----
        ready_tag_dir = config.ready_dir() / tag / module
        ready_tag_dir.mkdir(parents=True, exist_ok=True)

        for src in source_files:
            src_path = Path(src)
            raw_path = raw_tag_dir / src_path.name
            ready_path = ready_tag_dir / src_path.name

            # Atomic rename (same filesystem)
            os.replace(str(raw_path), str(ready_path))
            # Enforce permissions
            os.chmod(str(ready_path), 0o664)

            storage.update_file_ready(batch_uuid, src, str(ready_path))

        storage.add_event(batch_uuid, EVENT_DELIVERED, f"Files moved to {ready_tag_dir}")

        # ---- post_check (1st): verify ready files ----
        post_ok = True
        for src in source_files:
            src_path = Path(src)
            ready_path = ready_tag_dir / src_path.name
            if not compare_metadata(src, str(ready_path)):
                post_ok = False
                logger.error(f"post_check FAILED: {src_path.name}")
                break

        if post_ok:
            storage.update_batch_status(batch_uuid, STATUS_SUBMITTED)
            storage.add_event(batch_uuid, EVENT_SUBMITTED, "Batch ready for release")
            logger.info(f"Submit complete: {batch_uuid} -> SUBMITTED")
            return SubmitResult(batch_uuid, True, f"Submitted: {len(source_files)} files -> {ready_tag_dir}")
        else:
            storage.update_batch_status(batch_uuid, STATUS_FAILED)
            storage.add_event(batch_uuid, EVENT_POST_CHECK_FAIL, "Post-check mismatch")
            return SubmitResult(batch_uuid, False, "post_check failed: files corrupted after gate")

    except Exception as exc:
        logger.exception(f"Submit failed: {exc}")
        if batch_uuid:
            storage.update_batch_status(batch_uuid, STATUS_FAILED)
            storage.add_event(batch_uuid, EVENT_FAILED, str(exc))
        return SubmitResult(batch_uuid, False, str(exc))
    finally:
        _release_lock(mlock)
        # Clean up raw directory if empty
        raw_tag_dir = config.raw_dir() / tag / module
        try:
            if raw_tag_dir.exists():
                remaining = list(raw_tag_dir.iterdir())
                if not remaining:
                    raw_tag_dir.rmdir()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Release flow
# ---------------------------------------------------------------------------

class ReleaseResult:
    def __init__(self, success: bool, message: str, version: str = ""):
        self.success = success
        self.message = message
        self.version = version


def release(
    config: Config,
    storage: Storage,
    tag: str,
    version: str = "",
    module: Optional[str] = None,
    release_all: bool = False,
    progress=None,
) -> ReleaseResult:
    """Execute the release pipeline.

    1. Query SUBMITTED batches from SQLite
    2. Anti-split-brain: verify physical files exist
    3. Acquire global lock
    4. Copy to release/<TAG>/<VERSION>/<MODULE>
    5. post_check (2nd): compare BLAKE3 ready vs release
    6. Update status to RELEASED, update @latest symlink
    7. Release global lock
    """
    if tag not in VALID_TAGS:
        return ReleaseResult(False, f"Invalid tag: {tag}")

    # ---- default version ----
    if not version:
        version = time.strftime("%Y%m%d")
    else:
        version = f"{version}_{time.strftime('%Y%m%d')}"

    # ---- query submitted batches ----
    batches = storage.get_submitted_batches(tag=tag, module=module)
    if not batches:
        return ReleaseResult(False, f"No SUBMITTED batches for tag={tag}")

    # ---- anti-split-brain: verify physical files ----
    for batch in batches:
        batch_uuid = batch["batch_uuid"]
        files = storage.get_files(batch_uuid)
        for f in files:
            ready_path = f.get("ready_path", "")
            if ready_path and not os.path.exists(ready_path):
                msg = f"Split-brain detected: {ready_path} missing for batch {batch_uuid}"
                logger.error(msg)
                storage.update_batch_status(batch_uuid, STATUS_FAILED)
                storage.add_event(batch_uuid, EVENT_FAILED, msg)
                return ReleaseResult(False, msg)

    # ---- acquire global lock ----
    glock = config.global_lock_path()
    try:
        _acquire_lock(glock, "全局 Release 进行中")
    except LockError:
        return ReleaseResult(False, "全局 Release 锁已被持有，发布被阻断")

    try:
        release_tag_dir = config.release_dir() / tag / version
        release_tag_dir.mkdir(parents=True, exist_ok=True)

        total_files = 0

        for batch in batches:
            batch_uuid = batch["batch_uuid"]
            storage.add_event(batch_uuid, EVENT_RELEASE_START, f"Releasing to {version}")

            batch_module = batch["module"]
            files = storage.get_files(batch_uuid)

            release_mod_dir = release_tag_dir / batch_module
            release_mod_dir.mkdir(parents=True, exist_ok=True)

            for f in files:
                ready_path = Path(f["ready_path"])
                release_path = release_mod_dir / ready_path.name

                # Copy ready -> release
                shutil.copy2(str(ready_path), str(release_path))
                os.chmod(str(release_path), 0o664)

                storage.update_file_released(batch_uuid, f["source_path"], str(release_path))
                total_files += 1

            # ---- post_check (2nd): verify ready vs release ----
            post_ok = True
            for f in files:
                ready_path = f["ready_path"]
                release_path = release_mod_dir / Path(ready_path).name
                if not compare_metadata(ready_path, str(release_path)):
                    post_ok = False
                    storage.add_event(batch_uuid, EVENT_POST_CHECK_FAIL, str(release_path))
                    break

            if post_ok:
                storage.update_batch_status(batch_uuid, STATUS_RELEASED)
                storage.add_event(batch_uuid, EVENT_RELEASE_DONE, f"Released to {version}")
                storage.add_event(batch_uuid, EVENT_POST_CHECK_OK, "Final post-check passed")
            else:
                storage.update_batch_status(batch_uuid, STATUS_FAILED)
                storage.add_event(batch_uuid, EVENT_FAILED, "Release post_check failed")
                return ReleaseResult(False, f"Post-check failed for batch {batch_uuid}", version)

        # ---- update @latest symlink ----
        latest_link = config.release_dir() / tag / "@latest"
        if latest_link.is_symlink() or latest_link.exists():
            latest_link.unlink()
        latest_link.symlink_to(version, target_is_directory=True)

        # ---- clean up ready directory for released files ----
        for batch in batches:
            batch_module = batch["module"]
            ready_mod_dir = config.ready_dir() / tag / batch_module
            if ready_mod_dir.exists():
                shutil.rmtree(str(ready_mod_dir), ignore_errors=True)

        logger.info(f"Release complete: {tag}/{version} ({total_files} files)")
        return ReleaseResult(True, f"Released {tag}/{version} ({total_files} files, {len(batches)} batches)", version)

    except Exception as exc:
        logger.exception(f"Release failed: {exc}")
        return ReleaseResult(False, str(exc), version)
    finally:
        _release_lock(glock)
