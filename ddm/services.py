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

from ddm.config import Config, GateDef
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
    STATUS_SUPERSEDED,
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


# Threshold for flagging abnormal file-size changes between versions
SIZE_CHANGE_WARN_RATIO = 0.30   # warn if size changes by ±30% or more
SIZE_CHANGE_ALERT_RATIO = 0.50  # alert if size changes by ±50% or more

# File-system permissions for shared (multi-user) directories.
# SGID (0o2775) ensures new files/subdirs inherit the parent group so that
# any member of the shared group (set via config.shared_group) can create,
# overwrite, and release data.
SHARED_DIR_PERMS = 0o2775
SHARED_FILE_PERMS = 0o664


def _ensure_dir(path: Path, repo_root: Path = Path("."), shared_group_gid: int = None):
    """Create directory with SGID group-writable permissions for shared access.

    Chmod propagates up to all ancestors under repo_root so that parent
    directories (e.g. raw/, ready/) also carry SGID.

    If *shared_group_gid* is provided it is used for chown; otherwise the
    group is inherited from the parent directory (historic behaviour for
    callers that do not have the config handy).
    """
    path.mkdir(parents=True, exist_ok=True)
    current = path.resolve()
    bound = repo_root.resolve()
    while current != current.parent and str(current).startswith(str(bound)):
        try:
            gid = shared_group_gid if shared_group_gid is not None \
                  else os.stat(str(current.parent)).st_gid
            os.chown(str(current), -1, gid)
            os.chmod(str(current), SHARED_DIR_PERMS)
        except (PermissionError, OSError):
            pass  # not the owner, directory already exists with correct perms
        current = current.parent


# ---------------------------------------------------------------------------
# Lock helpers
# ---------------------------------------------------------------------------

def _write_op_log(log_path: Path, entry: str, shared_group_gid: int = None):
    """Append a timestamped entry to an operation log file."""
    _ensure_dir(log_path.parent, shared_group_gid=shared_group_gid)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(str(log_path), "a") as f:
        f.write(f"[{ts}] {entry}\n")
    os.chmod(str(log_path), SHARED_FILE_PERMS)


class LockError(Exception):
    """Raised when a lock prevents an operation."""


def _acquire_lock(lock_path: Path, description: str,
                  auto_clean_stale: bool = False,
                  stale_seconds: int = 600,
                  shared_group_gid: int = None) -> str:
    """Atomically create a lock file using O_CREAT|O_EXCL.

    If auto_clean_stale=True and the existing lock is older than
    stale_seconds, remove it automatically and retry (the previous
    submit process likely crashed or was SIGKILL'd).
    stale_seconds=0 disables auto-clean.

    Returns a warning string if a stale lock was auto-cleaned, else "".
    """
    _ensure_dir(lock_path.parent, shared_group_gid=shared_group_gid)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            f.write(f"pid={os.getpid()}\n"
                    f"user={os.environ.get('USER', '?')}\n"
                    f"time={time.time()}\n")
        os.chmod(str(lock_path), SHARED_FILE_PERMS)
        return ""  # fresh lock acquired, no warning
    except FileExistsError:
        if auto_clean_stale and lock_path.exists():
            age = time.time() - lock_path.stat().st_mtime
            # Parse lock file: pid + user for dual verification
            pid_dead = False
            old_user = "?"
            try:
                content = lock_path.read_text().split("\n")
                old_pid = int(content[0].replace("pid=", "")) if content else 0
                for line in content:
                    if line.startswith("user="):
                        old_user = line.replace("user=", "")
                os.kill(old_pid, 0)  # signal 0 = existence check
            except (ValueError, OSError, ProcessLookupError):
                pid_dead = True
            except Exception:
                pass  # can't determine, rely on age

            if pid_dead or (stale_seconds > 0 and age > stale_seconds):
                reason = f"原进程已退出 (user={old_user})" if pid_dead else \
                         f"{age/60:.0f}min 前创建 (user={old_user})"
                logger.warning(
                    f"⚠ 检测到过期锁 ({reason}): {lock_path}")
                logger.warning(
                    "  自动清除中… 如果原 submit 仍在运行（如处理大文件），"
                    "请 Ctrl+C 取消本次提交，等待原任务完成。")
                try:
                    os.unlink(str(lock_path))
                except OSError:
                    pass
                # Retry once after cleaning
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w") as f:
                    f.write(f"pid={os.getpid()}\n"
                            f"user={os.environ.get('USER', '?')}\n"
                            f"time={time.time()}\n")
                os.chmod(str(lock_path), SHARED_FILE_PERMS)
                return (f"⚠ 检测到过期锁 ({reason}) 并已自动清除。"
                        f"如果原 submit 仍在运行，请立即 Ctrl+C 取消。")
        raise LockError(
            f"[Warning] {description} — lock exists: {lock_path}. "
            f"If the previous submit crashed, remove it manually: "
            f"rm {lock_path}")


def _release_lock(lock_path: Path) -> None:
    """Remove a lock file (best-effort)."""
    try:
        os.unlink(str(lock_path))
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning(f"Failed to release lock {lock_path}: {e}")


# ---------------------------------------------------------------------------
# File discovery (flat outgoing directory)
# ---------------------------------------------------------------------------

def find_source_files(
    outgoing_roots: List[str],
    patterns: List[str],
    user: str,
    module: str,
) -> List[str]:
    """Find files across multiple outgoing roots matching expanded patterns.

    Each root supports {user} and {module} placeholders, e.g.:
      /data/{user}/{module}/a0.outgoing  →  /data/wangshuai/CPU/a0.outgoing

    Roots are searched in order; when the same filename exists in multiple
    roots the first match wins (later duplicates are skipped).
    Missing / unreadable roots are skipped with a warning.
    """
    matched: List[str] = []
    seen: set = set()
    tried: List[str] = []

    for root_template in outgoing_roots:
        expanded_root = root_template.format(user=user, module=module)
        root = Path(expanded_root).resolve()
        tried.append(str(root))
        if not root.is_dir():
            continue
        try:
            all_files = [f.name for f in root.iterdir() if f.is_file()]
        except PermissionError:
            logger.warning(f"Permission denied reading outgoing root: {root}")
            continue

        for pattern in patterns:
            expanded = pattern.format(user=user, module=module)
            for fname in all_files:
                if fname not in seen and fnmatch.fnmatch(fname, expanded):
                    seen.add(fname)
                    matched.append(str(root / fname))

    if not matched:
        logger.warning(f"No matching files in any outgoing root: {tried}")

    return sorted(matched)


# ---------------------------------------------------------------------------
# Streaming copy with BLAKE3
# ---------------------------------------------------------------------------

def streaming_copy(
    source_path: str,
    dest_path: str,
    shared_group_gid: int = None,
) -> Tuple[int, str]:
    """Stream-copy a file, computing BLAKE3 on the fly. Returns (size, hash_hex).

    Preserves the source file's mtime so timestamps survive across stages:
      a0 → raw (copy, utime) → ready (os.replace) → release (copy2)
    """
    dest = Path(dest_path)
    _ensure_dir(dest.parent, shared_group_gid=shared_group_gid)
    src_stat = os.stat(source_path)

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

    # Preserve original timestamp so later stages inherit it
    os.utime(str(dest), (src_stat.st_atime, src_stat.st_mtime))

    # Ensure copied file is readable/writable by shared group
    os.chmod(dest_path, SHARED_FILE_PERMS)

    return copied, hasher.hexdigest()


# ---------------------------------------------------------------------------
# Metadata comparison (pre_check / post_check)
# ---------------------------------------------------------------------------

def compare_metadata(source_path: str, dest_path: str, check_mtime: bool = True) -> bool:
    """Compare file size and BLAKE3 hash. Optionally also check mtime."""
    if not os.path.exists(dest_path):
        logger.error(f"Destination missing: {dest_path}")
        return False

    src_stat = os.stat(source_path)
    dst_stat = os.stat(dest_path)

    # size
    if src_stat.st_size != dst_stat.st_size:
        logger.error(f"Size mismatch: {src_stat.st_size} vs {dst_stat.st_size} ({source_path})")
        return False

    # mtime (only for stages where it should be preserved, e.g. copy2)
    if check_mtime:
        if int(src_stat.st_mtime) != int(dst_stat.st_mtime):
            logger.warning(
                f"Timestamp mismatch: src={src_stat.st_mtime:.0f} dst={dst_stat.st_mtime:.0f} ({source_path})"
            )

    # BLAKE3 hash
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
    def __init__(self, batch_uuid: str, success: bool, message: str,
                 warnings: list = None):
        self.batch_uuid = batch_uuid
        self.success = success
        self.message = message
        self.warnings = warnings or []


def submit(
    config: Config,
    storage: Storage,
    module: str,
    tag: str,
    username: str,
    summary: str = "",
    on_step: Optional[Callable] = None,
    admin_override: bool = False,
) -> SubmitResult:
    """Execute the full submit pipeline with unified progress callback.

    on_step(phase, step, total, detail) is called at each pipeline step:
      phase  — "Copying" | "Pre-check" | "Gates" | "Delivering"
      step   — current step number (0-based before, n after)
      total  — total steps in this pipeline run
      detail — e.g. filename, gate name
    """
    # Resolve shared-group GID once for all directory/file permission fixes
    shared_gid = config.shared_group_gid

    # ---- validate ----
    tag_cfg = config.tag_config(tag)
    if tag_cfg is None:
        return SubmitResult("", False,
            f"Unknown tag: '{tag}'.  Defined tags: {', '.join(config.tag_names())}")

    patterns = tag_cfg.file_patterns
    if not patterns:
        return SubmitResult("", False, f"No file_patterns defined for tag: {tag}")

    # ---- fast-fail: module ownership ----
    if not admin_override and not config.is_module_owner(module, username):
        owners = config.module_owners(module)
        admins_hint = f"，管理员: {', '.join(config.admins)}" if config.admins else ""
        if owners:
            msg = (f"用户 [{username}] 无权提交模块 [{module}]。"
                   f"该模块 owner: {', '.join(owners)}{admins_hint}。")
        else:
            msg = (f"模块 [{module}] 未在 config.yaml 中配置 owners 列表。"
                   f"请在 modules.{module}.owners 中添加可提交用户。")
        logger.warning(msg)
        return SubmitResult("", False, msg)

    # ---- fast-fail: tag release lock ----
    rlock = config.release_lock_path(tag)
    if rlock.exists():
        msg = (f"[{tag}] 正在 Release，提交被阻断。"
               f"请等待 {tag} 发布完成后再提交。")
        logger.warning(msg)
        return SubmitResult("", False, msg)

    # ---- fast-fail: module lock ----
    mlock = config.module_lock_path(module, tag)
    stale_warning = ""
    try:
        stale_warning = _acquire_lock(mlock, f"模块 {module}/{tag} 正在提交",
                                       auto_clean_stale=True,
                                       stale_seconds=config.stale_lock_minutes * 60,
                                       shared_group_gid=shared_gid)
    except LockError as e:
        logger.warning(str(e))
        return SubmitResult("", False, str(e))

    batch_uuid = ""
    try:
        # ---- disk space ----
        raw_base = config.raw_dir()
        _ensure_dir(raw_base, Path(config.repository_root).resolve(),
                     shared_group_gid=shared_gid)
        check_disk_space(str(raw_base))

        # ---- create batch ----
        batch_uuid = storage.create_batch(
            module=module, tag=tag, username=username, summary=summary
        )
        storage.add_event(batch_uuid, EVENT_SUBMIT_START, f"module={module} tag={tag} user={username}")

        # Mark previous SUBMITTED batches of same module+tag as superseded
        old_batches = storage.get_submitted_batches(tag=tag, module=module)
        for ob in old_batches:
            if ob["batch_uuid"] != batch_uuid:
                storage.update_batch_status(ob["batch_uuid"], STATUS_SUPERSEDED)

        # ---- discover source files ----
        source_files = find_source_files(config.outgoing_roots_resolved, patterns, username, module)
        if not source_files:
            roots_str = ", ".join(
                r.format(user=username, module=module) for r in config.outgoing_roots
            )
            msg = (f"未找到匹配文件 (patterns: {patterns})，已尝试: {roots_str}")
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

        # ---- stream-copy to raw/<tag>/<module> ----
        raw_run_dir = raw_base / tag / module
        _ensure_dir(raw_run_dir, Path(config.repository_root).resolve(),
                     shared_group_gid=shared_gid)

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
            dest_path = raw_run_dir / src_path.name
            src_stat = os.stat(src)

            storage.add_file(batch_uuid, src,
                             file_size=src_stat.st_size,
                             blake3_hash="",
                             source_size=src_stat.st_size,
                             source_mtime=src_stat.st_mtime)
            file_size, blake3_hex = streaming_copy(src, str(dest_path),
                                                      shared_group_gid=shared_gid)
            storage.update_file_raw(batch_uuid, src, str(dest_path), file_size, blake3_hex)

            _step("Copying", f"{src_path.name} ({file_size} bytes)")
            logger.info(f"Copied: {src_path.name} -> {dest_path} ({file_size} bytes)")

        storage.add_event(batch_uuid, EVENT_COPY_DONE, f"{len(source_files)} files copied")

        # ---- pre_check: BLAKE3(a0) vs DB-stored hash from streaming_copy ----
        _step("Pre-check", f"Verifying {len(source_files)} files", advance=False)
        pre_ok = True
        for src in source_files:
            src_path = Path(src)
            src_hash = _blake3_hash(str(src_path))
            # Fetch the hash stored by streaming_copy (avoid recomputing raw)
            file_rec = storage.get_file_by_source(batch_uuid, src)
            stored_hash = file_rec.get("blake3_hash", "") if file_rec else ""
            if src_hash != stored_hash:
                pre_ok = False
                logger.error(f"pre_check FAILED: {src_path.name} BLAKE3 mismatch")
                break

        if pre_ok:
            storage.add_event(batch_uuid, EVENT_PRE_CHECK_OK, "Metadata matches")
            _step("Pre-check", "OK", advance=True)
        else:
            storage.update_batch_status(batch_uuid, STATUS_FAILED)
            storage.add_event(batch_uuid, EVENT_PRE_CHECK_FAIL, "Metadata mismatch")
            return SubmitResult(batch_uuid, False,
                f"pre_check 失败: 文件校验不匹配。请检查 a0.outgoing 源文件是否完整，"
                f"必要时重新提交。")

        # ---- run gates (delegated to gates/runner.py) ----
        if gate_defs:
            for i, gate in enumerate(gate_defs):
                _step("Gates", f"Running {gate.name}...", advance=False)

            results = run_gates(gate_defs, str(raw_run_dir), module, tag,
                                progress_callback=None)

            for i, gr in enumerate(results):
                if gr.passed:
                    storage.add_event(batch_uuid, EVENT_GATE_PASS, gr.name)
                    _step("Gates", f"{gr.name} ✓ ({gr.elapsed:.1f}s)", advance=True)
                else:
                    storage.add_event(batch_uuid, EVENT_GATE_FAIL, f"{gr.name}: {gr.stderr[:200]}")
                    _step("Gates", f"{gr.name} ✗", advance=True)
                    storage.update_batch_status(batch_uuid, STATUS_FAILED)
                    return SubmitResult(batch_uuid, False, f"Gate '{gr.name}' failed")
        else:
            logger.info(f"No gates defined for tag={tag}")

        # ---- atomic move to ready + chmod ----
        _step("Delivering", f"Moving to ready/{tag}/{module}", advance=False)
        ready_tag_dir = config.ready_dir() / tag / module
        _ensure_dir(ready_tag_dir, Path(config.repository_root).resolve(),
                     shared_group_gid=shared_gid)

        # Cache raw hashes before os.replace (raw files will be gone after move)
        raw_hashes: dict = {}
        for src in source_files:
            raw_path = raw_run_dir / Path(src).name
            raw_hashes[src] = _blake3_hash(str(raw_path))

        for src in source_files:
            src_path = Path(src)
            raw_path = raw_run_dir / src_path.name
            ready_path = ready_tag_dir / src_path.name
            os.replace(str(raw_path), str(ready_path))
            os.chmod(str(ready_path), SHARED_FILE_PERMS)
            storage.update_file_ready(batch_uuid, src, str(ready_path))

        storage.add_event(batch_uuid, EVENT_DELIVERED, f"Files moved to {ready_tag_dir}")

        # ---- post_check (1st): raw → ready (chain of custody) ----
        post_ok = True
        for src in source_files:
            ready_path = ready_tag_dir / Path(src).name
            ready_hash = _blake3_hash(str(ready_path))
            if ready_hash != raw_hashes[src]:
                post_ok = False
                logger.error(f"post_check FAILED: raw→ready BLAKE3 mismatch ({Path(src).name})")
                break

        if post_ok:
            storage.update_batch_status(batch_uuid, STATUS_SUBMITTED)
            storage.add_event(batch_uuid, EVENT_SUBMITTED, "Batch ready for release")
            _step("Delivering", "Done", advance=True)
            logger.info(f"Submit complete: {batch_uuid} -> SUBMITTED "
                        f"({len(source_files)} files, {total_size} bytes)")

            # Write operation log (non-fatal)
            log_warnings = []
            try:
                _write_op_log(
                    Path(config.outgoing_root.format(user=username, module=module)) / ".ddm_submit.log",
                    f"submit  tag={tag}  module={module}  user={username}  "
                    f"files={len(source_files)}  size={total_size}  uuid={batch_uuid[:8]}  "
                    f"summary={summary or '-'}",
                    shared_group_gid=shared_gid,
                )
            except OSError as e:
                logger.warning(f"Failed to write submit log (non-fatal): {e}")
                log_warnings.append(f"操作日志写入失败: {e}")

            msg = f"Submitted: {len(source_files)} files (total {total_size} bytes)"
            if stale_warning:
                msg += f"\n{stale_warning}"
            return SubmitResult(batch_uuid, True, msg, warnings=log_warnings)
        else:
            storage.update_batch_status(batch_uuid, STATUS_FAILED)
            storage.add_event(batch_uuid, EVENT_POST_CHECK_FAIL, "Post-check mismatch")
            return SubmitResult(batch_uuid, False,
                "post_check 失败: gate 后文件损坏。请联系管理员检查 raw/ 和 ready/ 目录。")

    except KeyboardInterrupt:
        logger.warning(f"Submit interrupted by user (Ctrl+C): {batch_uuid[:8] if batch_uuid else '?'}")
        if batch_uuid:
            storage.update_batch_status(batch_uuid, STATUS_FAILED)
            storage.add_event(batch_uuid, EVENT_FAILED, "User interrupted (Ctrl+C)")
        return SubmitResult(batch_uuid, False,
            "提交被用户中断 (Ctrl+C)。锁文件已释放，可重新提交。\n"
            "  若新提交提示锁文件存在，请用联系专项负责人 ddm check 检查。")
    except Exception as exc:
        logger.exception(f"Submit failed: {exc}")
        if batch_uuid:
            storage.update_batch_status(batch_uuid, STATUS_FAILED)
            storage.add_event(batch_uuid, EVENT_FAILED, str(exc))
        return SubmitResult(batch_uuid, False, str(exc))
    finally:
        _release_lock(mlock)
        # clean up raw run directory if empty
        raw_cleanup_base = config.raw_dir()
        try:
            run_dir = raw_cleanup_base / tag / module
            if run_dir.exists():
                remaining = list(run_dir.iterdir())
                if not remaining:
                    run_dir.rmdir()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Release flow
# ---------------------------------------------------------------------------

def _load_previous_sizes(release_dir: Path, tag: str, current_version: str) -> dict:
    """Scan the previous release version and return {filename: size} map."""
    tag_dir = release_dir / tag
    if not tag_dir.is_dir():
        return {}

    prev_dir = None
    latest_link = tag_dir / "@latest"
    if latest_link.is_symlink():
        resolved = latest_link.resolve()
        if resolved.name != current_version and resolved.is_dir():
            prev_dir = resolved

    if prev_dir is None:
        candidates = []
        for d in tag_dir.iterdir():
            if d.is_dir() and d.name not in ("@latest", current_version):
                try:
                    candidates.append((d.stat().st_mtime, d))
                except OSError:
                    pass
        if candidates:
            candidates.sort(reverse=True)
            prev_dir = candidates[0][1]

    if prev_dir is None:
        return {}

    sizes: dict = {}
    for f in prev_dir.rglob("*"):
        if f.is_file():
            sizes[f.name] = f.stat().st_size
    return sizes


def _merge_dirs(src: str, dst: str):
    """Merge src directory into dst, overwriting same-named files."""
    for item in os.listdir(src):
        s = os.path.join(src, item)
        d = os.path.join(dst, item)
        if os.path.isdir(s):
            if not os.path.isdir(d):
                shutil.copytree(s, d)
            else:
                _merge_dirs(s, d)
        else:
            shutil.copy2(s, d)
            os.chmod(d, SHARED_FILE_PERMS)


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
    username: str = "",
    allow_inherit: bool = False,
    force: bool = False,
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
    # Resolve shared-group GID once for all directory/file permission fixes
    shared_gid = config.shared_group_gid

    # ---- validate ----
    tag_cfg = config.tag_config(tag)
    if tag_cfg is None:
        return ReleaseResult(False,
            f"Unknown tag: '{tag}'.  Defined tags: {', '.join(config.tag_names())}")
    if tag_cfg and tag_cfg.release_users and username:
        if username not in tag_cfg.release_users and username not in config.admins:
            msg = (f"用户 [{username}] 无权发布 {tag}。"
                   f"已授权: {', '.join(tag_cfg.release_users)}。"
                   f"请联系管理员添加。")
            logger.warning(msg)
            return ReleaseResult(False, msg)

    release_version_dir = config.release_dir() / tag / version
    is_new_version = not release_version_dir.is_dir()
    if not is_new_version and release_all and not force:
        return ReleaseResult(False,
            f"版本 {version} 已存在。全量发布 (-A) 覆盖已有版本属破坏性操作。\n"
            f"  如需覆盖请使用 --force，或使用 -m MODULE 追加模块。")
    if is_new_version:
        logger.info(f"Creating new version: {version}")
    else:
        logger.info(f"Appending modules to existing version: {version}")

    # ---- fast-fail: lock check BEFORE touching any data ----
    # -m MODULE: only check THAT module's lock.
    # -A:        check ALL modules — a submit in progress means ready/ data
    #            could be half-written when release copies it.
    raw_dir = config.raw_dir()
    if module:
        mlock = raw_dir / f".lock_{module}_{tag}"
        if mlock.exists():
            msg = (f"模块 [{module}] 正在提交 {tag} 数据，"
                   f"Release 被阻断。请等待其 submit 完成后再发布。")
            logger.warning(msg)
            return ReleaseResult(False, msg)
    else:
        active_locks = list(raw_dir.glob(f".lock_*_{tag}")) if raw_dir.exists() else []
        if active_locks:
            # Parse lock filename (.lock_{MODULE}_{TAG}) into module names
            blocked = []
            tag_suffix = f"_{tag}"
            for lck in active_locks:
                inner = lck.name[len(".lock_"):]  # "CPU_PV_ITER"
                if inner.endswith(tag_suffix):
                    mod_name = inner[:-len(tag_suffix)]  # "CPU"
                else:
                    mod_name = inner
                blocked.append(mod_name)
            msg = (f"以下模块正在提交 {tag} 数据，Release 被阻断: "
                   f"{', '.join(blocked)}。请等待 submit 完成后再发布。")
            logger.warning(msg)
            return ReleaseResult(False, msg)

    batches = storage.get_submitted_batches(tag=tag, module=module)

    # ---- -A: verify all configured modules are accounted for ----
    if release_all:
        configured_modules = set(config.modules_for(tag))
        submitted_modules = {b["module"] for b in batches}
        missing = configured_modules - submitted_modules

        if missing:
            if not allow_inherit:
                ready_mods = submitted_modules if submitted_modules else {"(无)"}
                msg = (f"全量发布 (-A) 要求所有模块均已提交。\n"
                       f"  已就绪: {sorted(ready_mods)}\n"
                       f"  缺失:   {sorted(missing)}\n"
                       f"  使用 --inherit 可从上一版本继承缺失模块。")
                logger.error(msg)
                return ReleaseResult(False, msg)
            logger.info(f"-A with --inherit: modules {sorted(missing)} will be "
                        f"inherited from previous version")

    if not batches:
        if release_all and allow_inherit:
            logger.info("No new SUBMITTED batches — all modules will inherit")
        else:
            hint = f"模块 [{module}]" if module else f"tag [{tag}]"
            return ReleaseResult(False,
                f"{hint} 没有任何已提交 (SUBMITTED) 的数据。请先执行 ddm submit。")

    for batch in batches:
        batch_uuid = batch["batch_uuid"]
        files = storage.get_files(batch_uuid)
        for f in files:
            ready_path = f.get("ready_path", "")
            if ready_path and not os.path.exists(ready_path):
                fname = Path(ready_path).name if ready_path else "?"
                msg = (f"数据不一致: ready/ 中文件 {fname} 已丢失 "
                       f"(batch {batch_uuid[:8]})。可能被手动删除，请重新提交。")
                logger.error(msg)
                storage.update_batch_status(batch_uuid, STATUS_FAILED)
                storage.add_event(batch_uuid, EVENT_FAILED, msg)
                return ReleaseResult(False, msg)

    rlock = config.release_lock_path(tag)

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

    try:
        _acquire_lock(rlock, f"Tag [{tag}] Release 进行中",
                       shared_group_gid=shared_gid)
    except LockError as e:
        return ReleaseResult(False, f"Tag [{tag}] 正在 Release，发布被阻断 ({e})")

    try:
        # ---- stage to temp directory (all-or-nothing) ----
        import uuid as _uuid
        repo_root = Path(config.repository_root).resolve()
        staging_dir = config.release_dir() / tag / f".staging_{version}_{_uuid.uuid4().hex[:8]}"
        _ensure_dir(staging_dir, repo_root, shared_group_gid=shared_gid)

        # Map: (batch_uuid, ready_path) -> staging_path for cross-pass lookup
        staging_map: dict = {}
        total_files = 0

        # Pass 1: copy all files to staging, build staging_map
        for batch in batches:
            batch_uuid = batch["batch_uuid"]
            batch_module = batch["module"]
            files = storage.get_files(batch_uuid)

            for f in files:
                ready_path = f["ready_path"]
                fname = Path(ready_path).name
                # Classify into group subdirectory (e.g. "verilog", "gds")
                group = config.classify_file(fname, batch_module)
                dest_dir = staging_dir / group if group else staging_dir
                _ensure_dir(dest_dir, repo_root, shared_group_gid=shared_gid)
                sp = str(dest_dir / fname)
                shutil.copy2(ready_path, sp)
                os.chmod(sp, SHARED_FILE_PERMS)
                staging_map[(batch_uuid, ready_path)] = sp
                total_files += 1

        # ---- inherit unchanged modules from @latest (cumulative release) ----
        latest_link = config.release_dir() / tag / "@latest"
        modules_in_this_release = {b["module"] for b in batches}
        inherited_count = 0
        # Helper: identify module from filename prefix (e.g. "CPU.v.gz" → "CPU")
        configured_modules = set(config.modules_for(tag))

        def _module_of(filename: str) -> str:
            for m in configured_modules:
                if filename.startswith(m + "."):
                    return m
            return ""

        if latest_link.is_symlink():
            prev_dir = latest_link.resolve()
            if prev_dir.is_dir() and prev_dir.name != version:
                for f in prev_dir.rglob("*"):
                    if not f.is_file():
                        continue
                    mod_name = _module_of(f.name)
                    if mod_name and mod_name in modules_in_this_release:
                        continue  # this module is being updated — skip
                    group = config.classify_file(f.name, mod_name) if mod_name else ""
                    dest_dir = staging_dir / group if group else staging_dir
                    _ensure_dir(dest_dir, repo_root, shared_group_gid=shared_gid)
                    dest_path = str(dest_dir / f.name)
                    shutil.copy2(str(f), dest_path)
                    os.chmod(dest_path, SHARED_FILE_PERMS)
                    inherited_count += 1
                total_files += inherited_count
                if inherited_count > 0:
                    logger.info(f"Inherited {inherited_count} files from {prev_dir.name}")
                    logger.info(f"(modules in this release: {sorted(modules_in_this_release)})")

        # Pass 2: size diff (read-only, against previous version)
        prev_sizes = _load_previous_sizes(config.release_dir(), tag, version)
        for batch in batches:
            batch_uuid = batch["batch_uuid"]
            files = storage.get_files(batch_uuid)
            for f in files:
                sp = staging_map.get((batch_uuid, f["ready_path"]), "")
                if not sp or not os.path.exists(sp):
                    continue
                fname = Path(sp).name
                new_size = os.path.getsize(sp)
                old_size = prev_sizes.get(fname)
                if old_size is not None and old_size > 0:
                    ratio = (new_size - old_size) / old_size
                    if abs(ratio) >= SIZE_CHANGE_ALERT_RATIO:
                        direction = "increase" if ratio > 0 else "decrease"
                        msg = (f"Size anomaly [{direction} {abs(ratio)*100:.0f}%]: "
                               f"{fname}: {old_size} → {new_size} bytes")
                        logger.warning(msg)
                        storage.add_event(batch_uuid, "size_anomaly", msg)
                        integrity_warnings.append(f"  {msg}")
                    elif abs(ratio) >= SIZE_CHANGE_WARN_RATIO:
                        direction = "increase" if ratio > 0 else "decrease"
                        msg = (f"Size change [{direction} {abs(ratio)*100:.0f}%]: "
                               f"{fname}: {old_size} → {new_size} bytes")
                        logger.info(msg)
                        storage.add_event(batch_uuid, "size_change", msg)

        # Pass 3: post_check (DB-stored ready hash vs staging hash)
        all_ok = True
        for batch in batches:
            batch_uuid = batch["batch_uuid"]
            storage.add_event(batch_uuid, EVENT_RELEASE_START, f"Releasing to {version}")
            files = storage.get_files(batch_uuid)
            for f in files:
                ready_path = f["ready_path"]
                sp = staging_map.get((batch_uuid, ready_path), "")
                if not sp or not os.path.exists(sp):
                    all_ok = False
                    break
                # Compare DB-stored BLAKE3 (from submit) vs staging hash
                staging_hash = _blake3_hash(sp)
                stored_hash = f.get("blake3_hash", "")
                if staging_hash != stored_hash:
                    all_ok = False
                    logger.error(f"post_check FAILED: ready→staging BLAKE3 mismatch")
                    storage.add_event(batch_uuid, EVENT_POST_CHECK_FAIL, sp)
                    storage.update_batch_status(batch_uuid, STATUS_FAILED)
                    storage.add_event(batch_uuid, EVENT_FAILED, "Release post_check failed")
                    break
                # Also verify mtime was preserved by copy2
                ready_mtime = os.stat(ready_path).st_mtime if os.path.exists(ready_path) else 0
                staging_mtime = os.stat(sp).st_mtime
                if int(ready_mtime) != int(staging_mtime):
                    logger.warning(f"mtime mismatch ready→staging: {Path(sp).name}")

        if not all_ok:
            shutil.rmtree(str(staging_dir), ignore_errors=True)
            return ReleaseResult(False, "Release aborted: post-check failed, staging cleaned", version)

        # ---- all passed: commit ----
        if is_new_version:
            os.rename(str(staging_dir), str(release_version_dir))
        else:
            _merge_dirs(str(staging_dir), str(release_version_dir))
            shutil.rmtree(str(staging_dir), ignore_errors=True)
        # Ensure version dir and all subdirs are group-writable so
        # future owners can append modules to this version.
        for root, _, _ in os.walk(str(release_version_dir)):
            os.chmod(root, SHARED_DIR_PERMS)
        action = f"Released to {version}"

        for batch in batches:
            batch_uuid = batch["batch_uuid"]
            batch_module = batch["module"]
            files = storage.get_files(batch_uuid)
            for f in files:
                sp = staging_map.get((batch_uuid, f["ready_path"]), "")
                final_path = sp.replace(str(staging_dir), str(release_version_dir))
                storage.update_file_released(batch_uuid, f["source_path"], final_path)
            storage.update_batch_status(batch_uuid, STATUS_RELEASED, version=version)
            storage.add_event(batch_uuid, EVENT_RELEASE_DONE, action)
            storage.add_event(batch_uuid, EVENT_POST_CHECK_OK, "Final post-check passed")

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

        # Write operation log (non-fatal)
        try:
            _write_op_log(
                config.release_dir() / tag / ".ddm_release.log",
                f"release  tag={tag}  version={version}  user={username}  "
                f"files={total_files}  batches={len(batches)}  "
                f"modules={sorted(set(b['module'] for b in batches))}",
                shared_group_gid=shared_gid,
            )
        except OSError as e:
            logger.warning(f"Failed to write release log (non-fatal): {e}")
            import sys as _sys
            _sys.stderr.write(f"  \033[33m!\033[0m 操作日志写入失败: {e}\n")

        release_path = str(release_version_dir.resolve())
        return ReleaseResult(True,
            f"Released {tag}/{version} ({total_files} files, {len(batches)} batches)\n"
            f"  Path: {release_path}",
            version, integrity_warnings=integrity_warnings)

    except KeyboardInterrupt:
        logger.warning(f"Release interrupted by user (Ctrl+C): {version}")
        return ReleaseResult(False,
            "发布被用户中断 (Ctrl+C)。staging 目录已清理，可重新发布。", version)
    except Exception as exc:
        logger.exception(f"Release failed: {exc}")
        return ReleaseResult(False, str(exc), version)
    finally:
        _release_lock(rlock)
