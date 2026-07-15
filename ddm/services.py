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
from typing import Callable, List, Optional, Tuple

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
    """Find files in the flat outgoing_root matching expanded patterns."""
    root = Path(outgoing_root).resolve()
    if not root.is_dir():
        logger.warning(f"Outgoing root does not exist: {root}")
        return []

    matched: List[str] = []
    try:
        all_files = [f.name for f in root.iterdir() if f.is_file()]
    except PermissionError:
        logger.error(f"Permission denied reading {root}")
        return []

    for pattern in patterns:
        expanded = pattern.format(user=user, module=module)
        for fname in all_files:
            if fnmatch.fnmatch(fname, expanded):
                filepath = root / fname
                if str(filepath) not in matched:
                    matched.append(str(filepath))

    return sorted(matched)


# ---------------------------------------------------------------------------
# Streaming copy with BLAKE3
# ---------------------------------------------------------------------------

def streaming_copy(
    source_path: str,
    dest_path: str,
) -> Tuple[int, str]:
    """Stream-copy a file, computing BLAKE3 on the fly. Returns (size, hash_hex)."""
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

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

    return copied, hasher.hexdigest()


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
    """Raise OSError if mount point has insufficient free space."""
    usage = psutil.disk_usage(target_dir)
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
    on_step: Optional[Callable] = None,
) -> SubmitResult:
    """Execute the full submit pipeline with unified progress callback.

    on_step(phase, step, total, detail) is called at each pipeline step:
      phase  — "Copying" | "Pre-check" | "Gates" | "Delivering"
      step   — current step number (0-based before, n after)
      total  — total steps in this pipeline run
      detail — e.g. filename, gate name
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

        gate_defs = tag_cfg.gates
        # total steps = copy (n files) + pre_check (1) + gates (n) + deliver (1)
        total_steps = len(source_files) + len(gate_defs) + 2
        step = 0

        def _step(phase: str, detail: str = "", advance: bool = True):
            nonlocal step
            if advance:
                step += 1
            if on_step:
                on_step(phase, step, total_steps, detail)

        logger.info(f"Found {len(source_files)} source files for {module}/{tag}")

        # ---- stream-copy to raw ----
        raw_tag_dir = raw_base / tag / module
        raw_tag_dir.mkdir(parents=True, exist_ok=True)

        total_size = sum(os.path.getsize(f) for f in source_files)

        usage = psutil.disk_usage(str(raw_base))
        if usage.free < total_size * 1.2:
            msg = f"磁盘空间不足: 需要 {total_size * 1.2 / (1024**3):.1f} GiB, 可用 {usage.free / (1024**3):.1f} GiB"
            logger.error(msg)
            storage.update_batch_status(batch_uuid, STATUS_FAILED)
            storage.add_event(batch_uuid, EVENT_DISK_FULL, msg)
            return SubmitResult(batch_uuid, False, msg)

        for src in source_files:
            src_path = Path(src)
            dest_path = raw_tag_dir / src_path.name

            storage.add_file(batch_uuid, src, 0, "")
            file_size, blake3_hex = streaming_copy(src, str(dest_path))
            storage.update_file_raw(batch_uuid, src, str(dest_path), file_size, blake3_hex)

            _step("Copying", f"{src_path.name} ({file_size} bytes)")
            logger.info(f"Copied: {src_path.name} -> {dest_path} ({file_size} bytes)")

        storage.add_event(batch_uuid, EVENT_COPY_DONE, f"{len(source_files)} files copied")

        # ---- pre_check ----
        _step("Pre-check", f"Verifying {len(source_files)} files", advance=False)
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
            _step("Pre-check", "OK", advance=True)
        else:
            storage.update_batch_status(batch_uuid, STATUS_FAILED)
            storage.add_event(batch_uuid, EVENT_PRE_CHECK_FAIL, "Metadata mismatch")
            return SubmitResult(batch_uuid, False, "pre_check failed: metadata mismatch")

        # ---- run gates ----
        if gate_defs:
            for i, gate in enumerate(gate_defs):
                _step("Gates", f"Running {gate.name}...", advance=False)
                logger.info(f"Running gate '{gate.name}': {gate.command}")
                t0 = time.time()
                try:
                    import subprocess
                    raw_path = raw_tag_dir.resolve()
                    proc = subprocess.run(
                        gate.command.split() + [str(raw_path), module, tag],
                        capture_output=True, text=True, timeout=300,
                    )
                    elapsed = time.time() - t0
                    passed = proc.returncode == 0
                    if passed:
                        logger.info(f"Gate '{gate.name}' PASSED ({elapsed:.1f}s)")
                        storage.add_event(batch_uuid, EVENT_GATE_PASS, gate.name)
                        _step("Gates", f"{gate.name} ✓ ({elapsed:.1f}s)", advance=True)
                    else:
                        logger.error(f"Gate '{gate.name}' FAILED: {proc.stderr[:200]}")
                        storage.add_event(batch_uuid, EVENT_GATE_FAIL, f"{gate.name}: {proc.stderr[:200]}")
                        _step("Gates", f"{gate.name} ✗", advance=True)
                        storage.update_batch_status(batch_uuid, STATUS_FAILED)
                        return SubmitResult(batch_uuid, False, f"Gate '{gate.name}' failed")
                except Exception as exc:
                    logger.error(f"Gate '{gate.name}' ERROR: {exc}")
                    storage.add_event(batch_uuid, EVENT_GATE_FAIL, str(exc))
                    _step("Gates", f"{gate.name} ✗", advance=True)
                    storage.update_batch_status(batch_uuid, STATUS_FAILED)
                    return SubmitResult(batch_uuid, False, str(exc))
        else:
            logger.info(f"No gates defined for tag={tag}")

        # ---- atomic move to ready + chmod ----
        _step("Delivering", f"Moving to ready/{tag}/{module}", advance=False)
        ready_tag_dir = config.ready_dir() / tag / module
        ready_tag_dir.mkdir(parents=True, exist_ok=True)

        for src in source_files:
            src_path = Path(src)
            raw_path = raw_tag_dir / src_path.name
            ready_path = ready_tag_dir / src_path.name
            os.replace(str(raw_path), str(ready_path))
            os.chmod(str(ready_path), 0o664)
            storage.update_file_ready(batch_uuid, src, str(ready_path))

        storage.add_event(batch_uuid, EVENT_DELIVERED, f"Files moved to {ready_tag_dir}")

        # ---- post_check (1st) ----
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
            _step("Delivering", "Done", advance=True)
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
    def __init__(self, success: bool, message: str, version: str = "",
                 integrity_warnings: Optional[List[str]] = None):
        self.success = success
        self.message = message
        self.version = version
        self.integrity_warnings = integrity_warnings or []


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

    if not version:
        version = time.strftime("%Y%m%d")
    else:
        version = f"{version}_{time.strftime('%Y%m%d')}"

    batches = storage.get_submitted_batches(tag=tag, module=module)
    if not batches:
        return ReleaseResult(False, f"No SUBMITTED batches for tag={tag}")

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

    glock = config.global_lock_path()

    # ---- integrity check: audit previously RELEASED versions ----
    integrity_warnings: List[str] = []
    released_batches = storage.list_batches(tag=tag, status=STATUS_RELEASED)
    if released_batches:
        missing_versions: set = set()
        missing_details: List[str] = []
        for b in released_batches:
            b_uuid = b["batch_uuid"]
            files = storage.get_files(b_uuid)
            for f in files:
                rp = f.get("release_path", "")
                if rp and not os.path.exists(rp):
                    version_tag = b.get("version", "unknown")
                    missing_versions.add(version_tag)
                    missing_details.append(f"  {rp}")
                    storage.add_event(
                        b_uuid, "release_file_missing",
                        f"Physically deleted: {rp} (version: {version_tag})"
                    )
                    logger.warning(f"Release file missing — may have been rm -rf'd: {rp}")
        if missing_versions:
            msg = (f"Integrity warning: {len(missing_versions)} previously-released "
                   f"version(s) have missing files: {sorted(missing_versions)}")
            integrity_warnings = [msg] + missing_details
            logger.warning(msg)

    # ---- fast-fail: check no module is currently submitting ----
    raw_dir = config.raw_dir()
    if raw_dir.exists():
        active_locks = list(raw_dir.glob(f".lock_*_{tag}"))
        if active_locks:
            lock_names = [lck.name for lck in active_locks]
            msg = f"模块正在提交中，Release 被阻断 (active locks: {lock_names})"
            logger.warning(msg)
            return ReleaseResult(False, msg)

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
                shutil.copy2(str(ready_path), str(release_path))
                os.chmod(str(release_path), 0o664)
                storage.update_file_released(batch_uuid, f["source_path"], str(release_path))
                total_files += 1

            post_ok = True
            for f in files:
                ready_path = f["ready_path"]
                release_path = release_mod_dir / Path(ready_path).name
                if not compare_metadata(ready_path, str(release_path)):
                    post_ok = False
                    storage.add_event(batch_uuid, EVENT_POST_CHECK_FAIL, str(release_path))
                    break

            if post_ok:
                storage.update_batch_status(batch_uuid, STATUS_RELEASED, version=version)
                storage.add_event(batch_uuid, EVENT_RELEASE_DONE, f"Released to {version}")
                storage.add_event(batch_uuid, EVENT_POST_CHECK_OK, "Final post-check passed")
            else:
                storage.update_batch_status(batch_uuid, STATUS_FAILED)
                storage.add_event(batch_uuid, EVENT_FAILED, "Release post_check failed")
                return ReleaseResult(False, f"Post-check failed for batch {batch_uuid}", version)

        latest_link = config.release_dir() / tag / "@latest"
        if latest_link.is_symlink() or latest_link.exists():
            latest_link.unlink()
        latest_link.symlink_to(version, target_is_directory=True)

        for batch in batches:
            batch_module = batch["module"]
            ready_mod_dir = config.ready_dir() / tag / batch_module
            if ready_mod_dir.exists():
                shutil.rmtree(str(ready_mod_dir), ignore_errors=True)

        logger.info(f"Release complete: {tag}/{version} ({total_files} files)")
        return ReleaseResult(True, f"Released {tag}/{version} ({total_files} files, {len(batches)} batches)",
                            version, integrity_warnings=integrity_warnings)

    except Exception as exc:
        logger.exception(f"Release failed: {exc}")
        return ReleaseResult(False, str(exc), version)
    finally:
        _release_lock(glock)
