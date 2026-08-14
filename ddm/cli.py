"""CLI interaction layer — Click commands with Rich terminal output.

Shell completion:
  tcsh → source ddm.complete.csh                    (方案 A: 纯原生, 零依赖)
  bash → eval "$(register-python-argcomplete ddm)"   (方案 B: argcomplete)
  zsh  → 同 bash

方案 A 和 B 的补全规则在此文件统一管理: 方案 A 通过隐藏 __complete_*
子命令暴露动态数据, 方案 B 通过 argcomplete Completer 桥接到 Click.
"""
# PYTHON_ARGCOMPLETE_OK

import os
import re
import signal
import sys
import time
from pathlib import Path

import click
from loguru import logger
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from ddm.config import Config
from ddm.services import HAS_BLAKE3, release, submit
from ddm.storage import Storage
from ddm.version import __version__, __changelog__

# ---- argcomplete (方案 B: 可选依赖, 不影响方案 A) ----
try:
    import argcomplete
    from argcomplete.completers import FilesCompleter

    HAS_ARGCOMPLETE = True
except ImportError:
    HAS_ARGCOMPLETE = False
    FilesCompleter = None  # type: ignore

console = Console()

# ---------------------------------------------------------------------------
# argcomplete → Click bridge（方案 B, 仅 argcomplete 安装后生效）
# ---------------------------------------------------------------------------


def _complete_release_versions(prefix, **kwargs):
    """Dynamic completer: scan release/ dir for version directories."""
    release_base = Path("./repository/release")
    if not release_base.is_dir():
        return []
    versions = set()
    for tag_dir in release_base.iterdir():
        if not tag_dir.is_dir():
            continue
        for vdir in tag_dir.iterdir():
            if vdir.is_dir() and not vdir.name.startswith(".") \
               and vdir.name != "@latest":
                versions.add(vdir.name)
    return sorted(v for v in versions if v.startswith(prefix))


def _complete_tags(prefix, **kwargs):
    """Dynamic completer: configured tags from config.yaml."""
    try:
        cfg = Config("config/config.yaml")
        return [t for t in sorted(cfg.tag_names()) if t.startswith(prefix)]
    except Exception:
        return []


def _complete_modules(prefix, **kwargs):
    """Dynamic completer: configured modules from config.yaml."""
    try:
        cfg = Config("config/config.yaml")
        mods = set(cfg._raw.get("modules", {}).keys())
        for tag in cfg.tag_names():
            mods.update(cfg.modules_for(tag))
        return [m for m in sorted(mods) if m.startswith(prefix)]
    except Exception:
        return []


def _argcomplete_bridge():
    """Wire argcomplete to Click's command tree.

    Called once at startup when argcomplete detects a completion context
    (env var _ARGCOMPLETE is set).  Maps Click command names to their
    known options for argcomplete to present.
    """
    if not HAS_ARGCOMPLETE:
        return

    import argparse as _argparse

    # Build a lightweight argparse that mirrors Click's command structure
    parser = _argparse.ArgumentParser(prog="ddm")

    sub = parser.add_subparsers(dest="__cmd")
    sub.required = False

    # ---- submit ----
    p_s = sub.add_parser("submit")
    p_s.add_argument("-m", "--module").completer = _complete_modules
    p_s.add_argument("-t", "--tag").completer = _complete_tags
    p_s.add_argument("-s", "--summary")
    p_s.add_argument("-c", "--config").completer = FilesCompleter(
        allowednames=("*.yaml", "*.yml"), directories=False)

    # ---- release ----
    p_r = sub.add_parser("release")
    p_r.add_argument("-t", "--tag").completer = _complete_tags
    p_r.add_argument("-A", "--all", action="store_true")
    p_r.add_argument("-m", "--module").completer = _complete_modules
    p_r.add_argument("-v", "--version").completer = _complete_release_versions
    p_r.add_argument("--inherit", action="store_true")

    # ---- status ----
    p_st = sub.add_parser("status")
    p_st.add_argument("-m", "--module").completer = _complete_modules
    p_st.add_argument("-d", "--date")

    # ---- list ----
    p_l = sub.add_parser("list")
    p_l.add_argument("-t", "--tag").completer = _complete_tags
    p_l.add_argument("-A", "--all", action="store_true")
    p_l.add_argument("-m", "--module").completer = _complete_modules
    p_l.add_argument("-v", "--verbose", action="store_true")

    # ---- check ----
    sub.add_parser("check")

    # ---- version ----
    sub.add_parser("version")

    argcomplete.autocomplete(parser)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _init_config_and_storage(config_path: str):
    """Load config and initialize storage. Returns (Config, Storage)."""
    cfg = Config(config_path)
    storage = Storage(cfg.db_path(), shared_group_name=cfg.shared_group)
    return cfg, storage


def _setup_logger(cfg: Config, console_output: bool = True):
    """Configure loguru. When console_output=False, only log to file."""
    log_dir = Path(cfg.log_path())
    log_dir.mkdir(parents=True, exist_ok=True)
    # Ensure log directory has the shared-group ownership and SGID perms
    try:
        import grp as _grp_lg
        shared_gid = _grp_lg.getgrnam(cfg.shared_group).gr_gid
        current = log_dir.resolve()
        project_root = Path(cfg._project_root).resolve() if hasattr(cfg, '_project_root') else current.parent
        while current != current.parent and str(current).startswith(str(project_root)):
            try:
                os.chown(str(current), -1, shared_gid)
                os.chmod(str(current), 0o2775)
            except (PermissionError, OSError):
                pass
            current = current.parent
    except (KeyError, Exception):
        pass  # shared group not on system — not fatal for logging
    logger.remove()
    if console_output:
        logger.add(
            sys.stderr,
            format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
            level="INFO",
        )
    logger.add(
        log_dir / "ddm_{time:YYYY-MM-DD}.log",
        rotation="10 MB",
        retention="30 days",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}",
        level="DEBUG",
    )
    # Make the daily log writable by the shared group — the first submitter
    # creates it with umask-based perms (usually 644) which blocks other
    # users' submits.  chown to shared_group + 664 lets any same-group user
    # append while keeping outsiders read-only.
    try:
        import grp as _grp_perm
        shared_gid = _grp_perm.getgrnam(cfg.shared_group).gr_gid
        today_log = log_dir / f"ddm_{time.strftime('%Y-%m-%d')}.log"
        os.chown(today_log, -1, shared_gid)
        os.chmod(today_log, 0o664)
    except OSError:
        pass


def _parse_time_filter(time_str: str) -> float:
    """Parse '5m', '24h', '3d' into seconds-since-epoch."""
    m = re.match(r"^(\d+)\s*(m|min|h|hour|d|day)$", time_str, re.IGNORECASE)
    if not m:
        raise click.BadParameter(f"Invalid time format: {time_str}. Use e.g. 5m, 24h, 3d")
    value = int(m.group(1))
    unit = m.group(2).lower()
    if unit in ("m", "min"):
        seconds = value * 60
    elif unit in ("h", "hour"):
        seconds = value * 3600
    else:
        seconds = value * 86400
    return time.time() - seconds


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


TAG_TYPE = click.STRING  # csh 补全走 complete 规则 + __complete_tags，不依赖 Click shell_complete

# ---------------------------------------------------------------------------
# role-based command visibility
# ---------------------------------------------------------------------------

# Commands visible to all users (module owners)
_PUBLIC_COMMANDS = {"submit", "status", "version"}

# Commands visible only to tag admins and global admins
_ADMIN_COMMANDS = {"release", "list", "check"}

# All registered command names (populated at import time)
_ALL_COMMANDS = _PUBLIC_COMMANDS | _ADMIN_COMMANDS


def _get_user() -> str:
    return os.environ.get("USER", "unknown")


def _load_config_safe(ctx) -> "Config | None":
    """Load config from click context, caching on ctx.obj for the request."""
    if ctx.obj is None:
        ctx.ensure_object(dict)
    if "_config" not in ctx.obj:
        try:
            config_path = ctx.obj.get("config_path") or _default_config_path()
            # Suppress "Config loaded" noise during tab completion
            _quiet = any(a.startswith("__complete") for a in sys.argv)
            ctx.obj["_config"] = Config(config_path, quiet=_quiet)
        except Exception:
            ctx.obj["_config"] = None
    return ctx.obj["_config"]


def _is_tag_admin(username: str, cfg) -> bool:
    """True if user is in admins list OR is a release_user for any tag."""
    if not cfg:
        return False
    if username in cfg.admins:
        return True
    for tag in cfg.tag_names():
        if username in cfg.release_users_for(tag):
            return True
    return False


class RoleBasedGroup(click.Group):
    """Click Group that shows different commands based on user role.

    Regular users (module owners):  submit, status, version
    Tag admins + global admins:     all commands
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Mark admin-only commands hidden at registration time.
        # They are unhidden dynamically in list_commands / get_command
        # when the current user is authorized.
        for cmd_name in _ADMIN_COMMANDS:
            if cmd_name in self.commands:
                self.commands[cmd_name].hidden = True

    def list_commands(self, ctx):
        base = super().list_commands(ctx)
        cfg = _load_config_safe(ctx)
        if _is_tag_admin(_get_user(), cfg):
            return base  # show all, including hidden
        return [c for c in base if c in _PUBLIC_COMMANDS]

    def get_command(self, ctx, cmd_name):
        cfg = _load_config_safe(ctx)
        if cmd_name in _ADMIN_COMMANDS and not _is_tag_admin(_get_user(), cfg):
            console.print(
                f"[red]Error:[/] '{cmd_name}' is restricted to tag admins.\n"
                f"  Contact admin to be added to a tag's release_users "
                f"in config.yaml."
            )
            ctx.exit(1)
        return super().get_command(ctx, cmd_name)


def _default_config_path() -> str:
    """Auto-discover config.yaml relative to the ddm package installation."""
    import ddm
    pkg_dir = Path(ddm.__file__).resolve().parent  # ddm/
    # Standard layout: <project_root>/ddm/__init__.py
    #                 <project_root>/config/config.yaml
    candidate = pkg_dir.parent / "config" / "config.yaml"
    if candidate.exists():
        return str(candidate)
    # Fallback: cwd (for development)
    return "config/config.yaml"


@click.group(cls=RoleBasedGroup)
@click.option(
    "-c", "--config",
    default=_default_config_path(),
    hidden=True,
    help="Path to YAML config file (auto-discovered)",
)
@click.pass_context
def main(ctx, config):
    """DDM — EDA Data Delivery Manager.

    Manage PV/PI data delivery pipeline with gates, locks, and audit trails.
    """
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config
    # Preload config once for subsequent role checks
    _load_config_safe(ctx)


# ---------------------------------------------------------------------------
# submit
# ---------------------------------------------------------------------------


@main.command(name="submit")
@click.option("-m", "--module", required=True, help="Module name (e.g. CPU, DDR)")
@click.option(
    "-t", "--tag", "tag",
    required=True, type=TAG_TYPE,
    help="Data tag (defined in config.yaml → defaults.tag)",
)
@click.option("-s", "--summary", default="", help="Submission summary / notes")
@click.option(
    "-u", "--user",
    default=None,
    hidden=True,
    help="Admin: submit on behalf of another user",
)
@click.pass_context
def _submit(ctx, module, tag, summary, user):
    """Submit module data from a0.outgoing through gates to ready/."""
    config_path = ctx.obj["config_path"]
    logger.remove()  # suppress default stderr before any output
    cfg, storage = _init_config_and_storage(config_path)
    _setup_logger(cfg, console_output=False)

    current_user = os.environ.get("USER", "unknown")
    username = current_user

    # Admin -u: submit on behalf of another user
    if user and user != current_user:
        if not _is_tag_admin(current_user, cfg):
            console.print(f"  [red]✗[/] -u/--user 仅限管理员使用。")
            sys.exit(1)
        username = user
        console.print(f"\n[bold cyan]Submit[/] module=[bold]{module}[/] tag=[bold]{tag}[/] "
                      f"user=[bold]{username}[/] [dim](by admin {current_user})[/]")
    else:
        console.print(f"\n[bold cyan]Submit[/] module=[bold]{module}[/] tag=[bold]{tag}[/] user=[bold]{username}[/]")

    if tag not in cfg.tag_names():
        console.print(f"[red]Error:[/] Unknown tag '{tag}'. Known: {', '.join(cfg.tag_names())}")
        sys.exit(1)

    # ---- fast-fail: group membership ----
    import grp as _grp
    try:
        _gid = _grp.getgrnam(cfg.shared_group).gr_gid
        if _gid not in os.getgroups():
            console.print(f"  [red]✗[/] 用户 {username} 不在共享组 [{cfg.shared_group}] 中。")
            console.print(f"    [dim]请联系管理员将你加入 {cfg.shared_group} 组。[/]")
            sys.exit(1)
    except KeyError:
        console.print(f"  [yellow]![/] 共享组 [{cfg.shared_group}] 不存在于系统。请联系管理员。")
        sys.exit(1)

    pbar_task = None
    pbar = None

    def on_step(phase: str, step: int, total: int, detail: str = ""):
        """Single-bar progress: called at each pipeline step."""
        nonlocal pbar_task, pbar
        if pbar is None:
            pbar = Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                TimeElapsedColumn(),
                console=console,
            )
            pbar.start()
        if pbar_task is None:
            pbar_task = pbar.add_task(
                f"[cyan]{phase}[/] {detail}".strip(),
                total=total,
            )
        pbar.update(pbar_task, completed=step, description=f"[cyan]{phase}[/] {detail}".strip())

    result = submit(
        config=cfg,
        storage=storage,
        module=module,
        tag=tag,
        username=username,
        summary=summary,
        on_step=on_step,
        admin_override=(user is not None and user != current_user),
    )

    if pbar is not None:
        pbar.stop()

    if result.success:
        console.print(f"\n[bold green]✓[/] Submit successful")
        console.print(f"  {result.message}")
        for w in result.warnings:
            console.print(f"  [yellow]![/] {w}")
    else:
        console.print(f"\n[bold red]✗[/] Submit failed: {result.message}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@main.command(name="status")
@click.option("-m", "--module", required=True, help="Module name")
@click.option("-d", "--date", "date_filter", default=None, help="Time filter: 5m, 24h, 3d")
@click.pass_context
def _status(ctx, module, date_filter):
    """Query submission status for a module."""
    config_path = ctx.obj["config_path"]
    cfg, storage = _init_config_and_storage(config_path)

    since = None
    if date_filter:
        since = _parse_time_filter(date_filter)

    batches = storage.list_batches(module=module, since=since)

    if not batches:
        console.print(f"[yellow]No records found for module=[bold]{module}[/][/]")
        return

    table = Table(title=f"Status for module: {module}")
    table.add_column("UUID", style="dim", max_width=10)
    table.add_column("Tag")
    table.add_column("User")
    table.add_column("Status")
    table.add_column("Summary")
    table.add_column("Time")

    status_colors = {
        "PENDING": "yellow",
        "SUBMITTED": "cyan",
        "SUPERSEDED": "dim",
        "RELEASED": "green",
        "FAILED": "red",
    }

    for b in batches:
        uuid_short = b["batch_uuid"][:8]
        st = b["status"]
        color = status_colors.get(st, "white")
        ts = time.strftime("%m-%d %H:%M", time.localtime(b["created_at"]))
        table.add_row(
            uuid_short,
            b["tag"],
            b["username"],
            f"[{color}]{st}[/]",
            b.get("summary", "") or "-",
            ts,
        )

    console.print(table)


# ---------------------------------------------------------------------------
# release
# ---------------------------------------------------------------------------


@main.command(name="release")
@click.option("-t", "--tag", required=True, type=TAG_TYPE, help="Tag (defined in config.yaml → defaults.tag)")
@click.option("-A", "--all", "release_all", is_flag=True, help="Release all configured modules for this tag")
@click.option("-m", "--module", default=None, help="Release specific module (auto-inherits others)")
@click.option("-v", "--version", required=True, help="Version label (e.g. V1, V2)")
@click.option("--inherit", is_flag=True, help="Allow -A to inherit unsubmitted modules from previous version")
@click.option("--force", is_flag=True, help="Force -A to overwrite an existing version")
@click.pass_context
def _release(ctx, tag, release_all, module, version, inherit, force):
    """Release submitted data to release/ directory.

    \b
    -m MODULE: release one module, inherit rest from @latest.
    -A:        release ALL configured modules (config.yaml modules list).
               Fails if any module has no SUBMITTED data (use --inherit).
    """
    config_path = ctx.obj["config_path"]
    cfg, storage = _init_config_and_storage(config_path)
    _setup_logger(cfg)

    if tag not in cfg.tag_names():
        console.print(f"[red]Error:[/] Unknown tag '{tag}'. Supported: {', '.join(cfg.tag_names())}")
        sys.exit(1)

    if not release_all and not module:
        raise click.UsageError("Specify --all or --module")

    username = os.environ.get("USER", "unknown")

    console.print(f"\n[bold magenta]Release[/] tag=[bold]{tag}[/] user=[bold]{username}[/]")

    result = release(
        config=cfg,
        storage=storage,
        tag=tag,
        version=version,
        module=module if not release_all else None,
        release_all=release_all,
        username=username,
        allow_inherit=inherit,
        force=force,
    )

    if result.success:
        console.print(f"\n[bold green]✓[/] Release successful: {result.message}")
        if result.integrity_warnings:
            console.print(f"\n[bold yellow]⚠ Integrity Warnings:[/]")
            for w in result.integrity_warnings:
                console.print(f"  {w}")
    else:
        console.print(f"\n[bold red]✗[/] Release failed: {result.message}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _fmt_size(n: int) -> str:
    """Human-readable file size with one decimal place."""
    if n == 0:
        return "0B"
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(n)
    for unit in units:
        if size < 1024:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def _fmt_delta(new: int, old: int) -> str:
    """Format size change as +X% or -X%."""
    if old == 0:
        return "-"
    pct = (new - old) / old * 100
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.0f}%"


@main.command(name="list")
@click.option("-t", "--tag", required=True, type=TAG_TYPE, help="Tag (defined in config.yaml → defaults.tag)")
@click.option("-A", "--all", "list_all", is_flag=True, help="Show all historical versions")
@click.option("-m", "--module", default=None, help="Filter by module (shows all versions)")
@click.option("-v", "--verbose", is_flag=True, help="Show per-file BLAKE3 hash and timestamp")
@click.pass_context
def _list(ctx, tag, list_all, module, verbose):
    """List data for a tag.

    \b
    Default:    latest version per module (one row per module)
    -A:         all historical versions
    -m MODULE:  all versions of a specific module
    """
    config_path = ctx.obj["config_path"]
    cfg, storage = _init_config_and_storage(config_path)

    if tag not in cfg.tag_names():
        console.print(f"[red]Error:[/] Unknown tag '{tag}'. Supported: {', '.join(cfg.tag_names())}")
        sys.exit(1)

    batches = storage.list_batches(tag=tag, module=module)

    if not batches:
        console.print(f"[yellow]No records for tag=[bold]{tag}[/][/]")
        return

    # Default: keep only latest version per module
    if not list_all and not module:
        seen = set()
        latest = []
        for b in batches:
            if b["module"] not in seen:
                seen.add(b["module"])
                latest.append(b)
        batches = latest

    # Show status summary when looking at latest (not -A)
    if not list_all:
        _list_status_summary(cfg, storage, tag, batches)

    if verbose:
        _list_verbose(storage, batches, tag)
    else:
        _list_summary(storage, batches, tag, cfg)


def _list_status_summary(cfg, storage, tag, batches):
    """Print a high-level status summary for the tag."""
    configured = set(cfg.modules_for(tag))
    if not configured:
        return

    # Find the latest released version across all batches
    all_full = storage.list_batches(tag=tag)
    released_versions = sorted(
        {b.get("version", "") for b in all_full if b["status"] == "RELEASED" and b.get("version")},
        key=lambda v: max((b["created_at"] for b in all_full if b.get("version") == v), default=0),
        reverse=True,
    )
    latest_version = released_versions[0] if released_versions else ""

    # Classify each configured module
    released_new: list = []       # module has a batch in the latest version
    released_inherited: list = [] # released but not in the latest version
    submitted: list = []          # SUBMITTED but not yet released
    failed: list = []             # last batch FAILED
    missing: list = []            # no record at all

    for mod in sorted(configured):
        mod_batches = [b for b in batches if b["module"] == mod]
        if not mod_batches:
            missing.append(mod)
            continue
        latest_b = mod_batches[0]
        if latest_b["status"] == "SUBMITTED":
            submitted.append(mod)
        elif latest_b["status"] == "FAILED":
            failed.append(mod)
        elif latest_b["status"] == "RELEASED":
            # New if this batch was released in the latest version
            if latest_b.get("version") == latest_version:
                released_new.append(mod)
            else:
                released_inherited.append(mod)

    # Print summary block
    lines: list = []
    if released_new:
        lines.append(f"  [green]已发布 ({latest_version})[/]: {', '.join(released_new)}")
    if released_inherited:
        lines.append(f"  [cyan]已发布 ({latest_version}, 继承)[/]: {', '.join(released_inherited)}")
    if submitted:
        lines.append(f"  [yellow]待发布[/]: {', '.join(submitted)}")
    if failed:
        lines.append(f"  [red]失败[/]: {', '.join(failed)}")
    if missing:
        lines.append(f"  [dim]未提交[/]: {', '.join(missing)}")

    if lines:
        console.print(f"\n[bold]{tag} Status[/]")
        for line in lines:
            console.print(line)
        console.print("")


def _list_summary(storage, batches, tag, cfg):
    """Compact table with file size and delta vs previous version."""
    # Build a map: batch_uuid -> total size of the most recent PREVIOUS version
    prev_total: dict = {}
    for b in batches:
        if b["status"] != "RELEASED":
            continue
        prev_batches = storage.list_batches(tag=tag, module=b["module"], status="RELEASED")
        # Find most recent version that is OLDER than this batch
        for pb in prev_batches:
            if pb["batch_uuid"] != b["batch_uuid"] and pb["created_at"] < b["created_at"]:
                pfiles = storage.get_files(pb["batch_uuid"])
                prev_total[b["batch_uuid"]] = sum(f.get("file_size") or f.get("source_size", 0) for f in pfiles)
                break

    table = Table(title=f"Records for tag: {tag}")
    table.add_column("Module", style="bold")
    table.add_column("Status")
    table.add_column("Version")
    table.add_column("User")
    table.add_column("Summary")
    table.add_column("Files", justify="right")
    table.add_column("Size", justify="right")
    table.add_column("Δ vs prev", justify="right")
    table.add_column("Time")

    status_colors = {
        "PENDING": "yellow", "SUBMITTED": "cyan", "SUPERSEDED": "dim",
        "RELEASED": "green", "FAILED": "red",
    }

    for b in batches:
        files = storage.get_files(b["batch_uuid"])
        file_count = len(files)
        total_size = sum(f.get("file_size") or f.get("source_size", 0) for f in files)

        st = b["status"]
        color = status_colors.get(st, "white")
        ts = time.strftime("%m-%d %H:%M", time.localtime(b["created_at"]))

        delta_str = ""
        old_total = prev_total.get(b["batch_uuid"])
        if old_total is not None and old_total > 0:
            delta_str = _fmt_delta(total_size, old_total)
        elif st == "RELEASED" and old_total is None:
            delta_str = "[dim]首次[/]"

        table.add_row(
            b["module"],
            f"[{color}]{st}[/]",
            b.get("version", "") or "-",
            b.get("username", "") or "-",
            (b.get("summary", "") or "-")[:30],
            str(file_count),
            _fmt_size(total_size),
            delta_str,
            ts,
        )

    console.print(table)


def _list_verbose(storage, batches, tag):
    """Per-file detail table with BLAKE3, timestamp, and file size delta."""
    for b in batches:
        files = storage.get_files(b["batch_uuid"])
        total_size = sum(f.get("file_size") or f.get("source_size", 0) for f in files)
        st = b["status"]
        status_colors = {"PENDING": "yellow", "SUBMITTED": "cyan", "SUPERSEDED": "dim", "RELEASED": "green", "FAILED": "red"}
        color = status_colors.get(st, "white")

        # Build previous file sizes for delta
        prev_file_sizes: dict = {}
        if st == "RELEASED":
            prev_batches = storage.list_batches(tag=tag, module=b["module"], status="RELEASED")
            for pb in prev_batches:
                if pb["batch_uuid"] != b["batch_uuid"] and pb["created_at"] < b["created_at"]:
                    for pf in storage.get_files(pb["batch_uuid"]):
                        pname = Path(pf["source_path"]).name if pf.get("source_path") else ""
                        prev_file_sizes[pname] = pf.get("file_size") or pf.get("source_size", 0)
                    break

        console.print(f"\n[bold]{b['module']}[/]  [{color}]{st}[/]  "
                      f"v={b.get('version') or '-'}  user={b.get('username','-')}  "
                      f"files={len(files)}  size={_fmt_size(total_size)}")

        ftable = Table(show_header=True, box=None)
        ftable.add_column("File", style="dim")
        ftable.add_column("Size", justify="right")
        ftable.add_column("Δ file", justify="right")
        ftable.add_column("BLAKE3", style="dim", max_width=16)
        ftable.add_column("Timestamp")

        for f in files:
            fname = Path(f["source_path"]).name if f.get("source_path") else "-"
            fsize = f.get("file_size") or f.get("source_size", 0)
            blake3_short = f.get("blake3_hash", "")[:12] if f.get("blake3_hash") else "-"
            mtime_val = f.get("source_mtime", 0)
            mtime_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime_val)) if mtime_val else "-"

            # Per-file delta
            old_fsize = prev_file_sizes.get(fname)
            if old_fsize is not None and old_fsize > 0:
                fdelta = _fmt_delta(fsize, old_fsize)
            elif old_fsize is not None and old_fsize == 0:
                fdelta = ""
            elif not prev_file_sizes:
                fdelta = ""
            else:
                fdelta = "[dim]新增[/]"

            ftable.add_row(fname, _fmt_size(fsize), fdelta, blake3_short, mtime_str)

        console.print(ftable)


# ---------------------------------------------------------------------------
# check — verify system environment
# ---------------------------------------------------------------------------


@main.command()
@click.pass_context
def check(ctx):
    """Verify system environment and dependencies."""
    config_path = ctx.obj["config_path"]

    console.print("[bold]DDM Environment Check[/]\n")

    # Config
    try:
        cfg = Config(config_path)
        console.print(f"  [green]✓[/] Config loaded: {config_path}")
        console.print(f"    Tags: {', '.join(cfg.tag_names())}")
        console.print(f"    Outgoing root: {cfg.outgoing_root}")
        console.print(f"    Repository root: {cfg.repository_root}")
    except Exception as e:
        console.print(f"  [red]✗[/] Config error: {e}")
        return

    # BLAKE3
    if HAS_BLAKE3:
        console.print("  [green]✓[/] BLAKE3 available")
    else:
        console.print("  [yellow]![/] BLAKE3 not available — using BLAKE2b-256 fallback")

    # SQLite
    try:
        storage = Storage(cfg.db_path())
        console.print(f"  [green]✓[/] SQLite ready: {cfg.db_path()}")
    except Exception as e:
        console.print(f"  [red]✗[/] SQLite error: {e}")

    # Directories
    outgoing_path = cfg.outgoing_root
    # Expand {user} placeholder with current user for existence check
    _test_user = os.environ.get("USER", "unknown")
    _expanded = outgoing_path.replace("{user}", _test_user).replace("{module}", "CPU")
    _expanded_parent = Path(_expanded).parent
    # Fall back to outgoing_root without module for checking
    _base = outgoing_path.split("{module}")[0].replace("{user}", _test_user).rstrip("/")
    _base_path = Path(_base)
    # Choose the most appropriate path to check
    if _base_path.exists():
        _check_path = _base_path
        _exists = True
    elif _expanded_parent.exists():
        _check_path = _expanded_parent
        _exists = True
    else:
        _check_path = Path(outgoing_path)
        _exists = _check_path.exists()

    for label, path, exists, show_path in [
        ("Outgoing root", outgoing_path, _exists, str(_check_path.resolve())),
        ("Repository root", cfg.repository_root, Path(cfg.repository_root).exists(),
         str(Path(cfg.repository_root).resolve())),
    ]:
        if exists:
            console.print(f"  [green]✓[/] {label}: {show_path}")
        else:
            console.print(f"  [yellow]![/] {label} (not created yet): {show_path}")

    # Shared group + owner membership verification
    import grp
    user = os.environ.get("USER", "")
    shared_group = cfg.shared_group
    group_exists = False
    user_in_group = False
    try:
        gid = grp.getgrnam(shared_group).gr_gid
        group_exists = True
        gids = os.getgroups()
        if gid in gids:
            user_in_group = True
            console.print(f"  [green]✓[/] Member of shared group: {shared_group}")
        else:
            console.print(f"  [red]✗[/] NOT in shared group '{shared_group}'. "
                          f"Contact admin to add {user} to {shared_group}.")
    except KeyError:
        console.print(f"  [yellow]![/] Shared group '{shared_group}' not found on system")

    # Check each module owner is in the shared group
    if group_exists:
        try:
            members = set(grp.getgrnam(shared_group).gr_mem)
        except Exception:
            members = set()
        owners_all = set()
        for mod in cfg._raw.get("modules", {}):
            for owner in cfg._raw["modules"][mod].get("owners", []):
                owners_all.add(owner)
        # Also check admins
        for admin in cfg.admins:
            owners_all.add(admin)

        missing = []
        for person in sorted(owners_all):
            if person not in members:
                missing.append(person)
        if missing:
            console.print(f"  [yellow]![/] These users are NOT in '{shared_group}' "
                          f"group: {', '.join(missing)}")
            console.print(f"    [dim]They will not be able to write to shared directories.[/]")
        else:
            console.print(f"  [green]✓[/] All owners/admins in shared group")

    # Stale lock detection — two-pass:
    #   PID dead → stale immediately (crash / kill -9)
    #   PID alive but lock older than stale_lock_minutes → warn (long submit)
    stale_minutes = cfg.stale_lock_minutes
    raw_dir = Path(cfg.repository_root) / "raw"
    if raw_dir.exists():
        stale = []   # definitely safe to remove (pid dead)
        alive = []   # running but lock is old (long submit?)
        now = time.time()
        for lf in raw_dir.glob(".lock_*"):
            age_min = (now - lf.stat().st_mtime) / 60
            pid = "?"
            lock_user = "?"
            try:
                content = lf.read_text().split("\n")
                pid = content[0].replace("pid=", "") if content else "?"
                for line in content:
                    if line.startswith("user="):
                        lock_user = line.replace("user=", "")
                os.kill(int(pid), 0)  # signal 0 = existence check
                pid_alive = True
            except (ValueError, OSError, ProcessLookupError):
                pid_alive = False
            except Exception:
                pid_alive = False

            if not pid_alive:
                stale.append(f"    .lock → {lf.name} (user={lock_user}, pid={pid}, 进程已退出, {age_min:.0f}min)")
            elif stale_minutes > 0 and age_min > stale_minutes:
                alive.append(f"    .lock → {lf.name} (user={lock_user}, pid={pid}, 仍在运行但 {age_min:.0f}min 未释放)")

        if stale:
            console.print(f"  [yellow]![/] Stale lock files (进程已退出，可安全清理):")
            for s in stale:
                console.print(s)
            console.print(f"    [dim]Fix: rm repository/raw/.lock_*[/]")
        if alive:
            console.print(f"  [yellow]![/] 长期运行中的锁 (请确认是否正常):")
            for s in alive:
                console.print(s)
        if not stale and not alive:
            console.print(f"  [green]✓[/] No stale module locks")

    # psutil
    try:
        import psutil
        console.print(f"  [green]✓[/] psutil available")
    except ImportError:
        console.print(f"  [red]✗[/] psutil not available")

    console.print("\n[bold green]Check complete.[/]")


# ---------------------------------------------------------------------------
# hidden completion helpers for csh/tcsh
# ---------------------------------------------------------------------------


@main.command("__complete_commands", hidden=True)
@click.pass_context
def _complete_commands(ctx):
    """Print visible command names for csh complete script (one per line)."""
    cfg = _load_config_safe(ctx)
    visible = main.list_commands(ctx)
    for cmd in sorted(visible):
        click.echo(cmd)


def _complete_quiet(cfg_path: str):
    """Load config silently for tab completion."""
    return Config(cfg_path, quiet=True)


@main.command("__complete_tags", hidden=True)
@click.pass_context
def _complete_tags(ctx):
    """Print tag names for csh complete script (one per line)."""
    config_path = ctx.obj.get("config_path", "config/config.yaml")
    try:
        cfg = _complete_quiet(config_path)
        for tag in sorted(cfg.tag_names()):
            click.echo(tag)
    except Exception:
        pass


@main.command("__complete_modules", hidden=True)
@click.pass_context
def _complete_modules(ctx):
    """Print module names for csh complete script (one per line)."""
    config_path = ctx.obj.get("config_path", "config/config.yaml")
    seen = set()
    try:
        cfg = _complete_quiet(config_path)
        # modules from top-level modules: section (owners)
        for mod in sorted(cfg._raw.get("modules", {})):
            if mod not in seen:
                click.echo(mod)
                seen.add(mod)
        # modules from each tag's modules list
        for tag in cfg.tag_names():
            for mod in cfg.modules_for(tag):
                if mod not in seen:
                    click.echo(mod)
                    seen.add(mod)
    except Exception:
        pass


@main.command("__complete_versions", hidden=True)
def _complete_versions():
    """Print release version names for csh complete script."""
    versions = _complete_release_versions("")
    for v in versions:
        click.echo(v)


# ---------------------------------------------------------------------------
# version — detailed version info
# ---------------------------------------------------------------------------


@main.command("version")
def _version():
    """Print detailed version information."""
    import platform
    import subprocess

    console.print(f"[bold cyan]ddm[/] [green]{__version__}[/]")

    # Git hash (best-effort: may not be available in binary deploy)
    try:
        here = Path(__file__).resolve().parent.parent
        git_hash = subprocess.check_output(
            ["git", "-C", str(here), "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        console.print(f"  git:      {git_hash}")
    except Exception:
        pass

    console.print(f"  python:   {platform.python_version()}")
    console.print(f"  platform: {platform.system()} {platform.machine()}")
    console.print(f"  blake3:   {'available' if HAS_BLAKE3 else 'fallback (blake2b)'}")

    # Changelog from version.py
    if __changelog__:
        console.print(f"\n[bold]Recent Changes[/]")
        for version_str, desc in __changelog__:
            console.print(f"  [cyan]{version_str}[/]  {desc}")


def _setup_signal_handling():
    """Protect long-running commands (submit/release) from spurious signals.

    KeyboardInterrupt == SIGINT.  A release should only be interrupted by a
    real Ctrl+C in an interactive terminal — NOT by background/session
    signals.  Policy:

      * Non-interactive (stdin is not a tty — cron, pipe, &, nohup,
        tmux/screen detached): ignore SIGINT and SIGHUP entirely.
      * Interactive terminal: keep SIGINT (Ctrl+C works), but ignore
        SIGHUP so closing/EOF on the terminal doesn't kill a running
        release mid-copy.
    """
    try:
        interactive = sys.stdin.isatty()
        if not interactive:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
            logger.debug("Non-interactive mode: SIGINT/SIGHUP ignored")
        else:
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
            logger.debug("Interactive mode: Ctrl+C works, SIGHUP ignored")
    except (AttributeError, ValueError, OSError):
        pass  # no stdin or unsupported platform — keep defaults


def _cli_entry():
    """Top-level entry: argcomplete activation, then -V/--version shortcut.

    _argcomplete_bridge() is a no-op unless argcomplete is installed AND
    the process is running in a shell completion context (env var
    _ARGCOMPLETE set).  In normal usage it costs one importlib check.
    """
    _setup_signal_handling()
    _argcomplete_bridge()

    if len(sys.argv) >= 2 and sys.argv[1] in ("-V", "--version"):
        import platform as _platform
        console.print(f"[bold cyan]ddm[/] [green]{__version__}[/]")
        console.print(f"  python:   {_platform.python_version()}")
        console.print(f"  platform: {_platform.system()} {_platform.machine()}")
        sys.exit(0)
    main()


if __name__ == "__main__":
    _cli_entry()
