"""CLI interaction layer — Click commands with Rich terminal output."""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

import click
from loguru import logger
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from ddm.config import VALID_TAGS, Config
from ddm.services import HAS_BLAKE3, release, submit
from ddm.storage import Storage
from ddm.version import __version__

console = Console()

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _init_config_and_storage(config_path: str):
    """Load config and initialize storage. Returns (Config, Storage)."""
    cfg = Config(config_path)
    storage = Storage(cfg.db_path())
    return cfg, storage


def _setup_logger(cfg: Config, console_output: bool = True):
    """Configure loguru. When console_output=False, only log to file."""
    log_dir = Path(cfg.log_path())
    log_dir.mkdir(parents=True, exist_ok=True)
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


def _resolve_config_path(path: str) -> str:
    """If running as a PyInstaller binary, resolve relative paths against the
    bundled data directory. Otherwise use the path as-is."""
    if getattr(sys, "frozen", False):
        bundled = os.path.join(sys._MEIPASS, path)
        if os.path.exists(bundled):
            return bundled
    return path


class TagParamType(click.ParamType):
    """Click parameter type that provides completion for tag names."""
    name = "tag"

    def shell_complete(self, ctx, param, incomplete):
        from ddm.config import Config
        config_path = ctx.params.get("config", "config/config.yaml")
        try:
            cfg = Config(config_path)
        except Exception:
            cfg = None
        tags = cfg.tag_names() if cfg else sorted(VALID_TAGS)
        return [click.shell_completion.CompletionItem(t) for t in tags if t.startswith(incomplete)]


TAG_TYPE = TagParamType()


@click.group()
@click.option(
    "-c", "--config",
    default="config/config.yaml",
    show_default=True,
    help="Path to YAML config file",
)
@click.pass_context
def main(ctx, config):
    """DDM — EDA Data Delivery Manager.

    Manage PV/PI data delivery pipeline with gates, locks, and audit trails.
    """
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = _resolve_config_path(config)


# ---------------------------------------------------------------------------
# submit
# ---------------------------------------------------------------------------


@main.command(name="submit")
@click.option("-m", "--module", required=True, help="Module name (e.g. CPU, DDR)")
@click.option(
    "-t", "--tag", "tag",
    required=True, type=TAG_TYPE,
    help=f"Data tag: {', '.join(sorted(VALID_TAGS))}",
)
@click.option("-s", "--summary", default="", help="Submission summary / notes")
@click.pass_context
def _submit(ctx, module, tag, summary):
    """Submit module data from a0.outgoing through gates to ready/."""
    config_path = ctx.obj["config_path"]
    logger.remove()  # suppress default stderr before any output
    cfg, storage = _init_config_and_storage(config_path)
    _setup_logger(cfg, console_output=False)

    username = os.environ.get("USER", "unknown")

    if tag not in cfg.tag_names():
        console.print(f"[red]Error:[/] Unknown tag '{tag}'. Known: {', '.join(cfg.tag_names())}")
        sys.exit(1)

    console.print(f"\n[bold cyan]Submit[/] module=[bold]{module}[/] tag=[bold]{tag}[/] user=[bold]{username}[/]")

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
    )

    if pbar is not None:
        pbar.stop()

    if result.success:
        console.print(f"\n[bold green]✓[/] Submit successful")
        console.print(f"  {result.message}")
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
@click.option("-t", "--tag", required=True, type=TAG_TYPE, help=f"Tag: {', '.join(sorted(VALID_TAGS))}")
@click.option("-A", "--all", "release_all", is_flag=True, help="Release all configured modules for this tag")
@click.option("-m", "--module", default=None, help="Release specific module (auto-inherits others)")
@click.option("-v", "--version", default="", help="Version label (default: date stamp)")
@click.option("--inherit", is_flag=True, help="Allow -A to inherit unsubmitted modules from previous version")
@click.pass_context
def _release(ctx, tag, release_all, module, version, inherit):
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
    """Human-readable file size."""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n}{unit}"
        n //= 1024
    return f"{n}TB"


def _fmt_delta(new: int, old: int) -> str:
    """Format size change as +X% or -X%."""
    if old == 0:
        return "-"
    pct = (new - old) / old * 100
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.0f}%"


@main.command(name="list")
@click.option("-t", "--tag", required=True, type=TAG_TYPE, help=f"Tag: {', '.join(sorted(VALID_TAGS))}")
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
                prev_total[b["batch_uuid"]] = sum(f.get("file_size", 0) for f in pfiles)
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
        "PENDING": "yellow", "SUBMITTED": "cyan",
        "RELEASED": "green", "FAILED": "red",
    }

    for b in batches:
        files = storage.get_files(b["batch_uuid"])
        file_count = len(files)
        total_size = sum(f.get("file_size", 0) for f in files)

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
        total_size = sum(f.get("file_size", 0) for f in files)
        st = b["status"]
        status_colors = {"PENDING": "yellow", "SUBMITTED": "cyan", "RELEASED": "green", "FAILED": "red"}
        color = status_colors.get(st, "white")

        # Build previous file sizes for delta
        prev_file_sizes: dict = {}
        if st == "RELEASED":
            prev_batches = storage.list_batches(tag=tag, module=b["module"], status="RELEASED")
            for pb in prev_batches:
                if pb["batch_uuid"] != b["batch_uuid"] and pb["created_at"] < b["created_at"]:
                    for pf in storage.get_files(pb["batch_uuid"]):
                        pname = Path(pf["source_path"]).name if pf.get("source_path") else ""
                        prev_file_sizes[pname] = pf.get("file_size", 0)
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
            fsize = f.get("file_size", 0)
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
def _complete_commands():
    """Print command names for csh complete script (one per line)."""
    for cmd in sorted(main.commands):
        if not cmd.startswith("_"):
            click.echo(cmd)


@main.command("__complete_tags", hidden=True)
@click.pass_context
def _complete_tags(ctx):
    """Print tag names for csh complete script (one per line)."""
    config_path = ctx.obj.get("config_path", "config/config.yaml")
    try:
        cfg = Config(config_path)
        for tag in sorted(cfg.tag_names()):
            click.echo(tag)
    except Exception:
        pass


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


def _cli_entry():
    """Top-level entry: handle -V/--version before Click parsing."""
    if len(sys.argv) >= 2 and sys.argv[1] in ("-V", "--version"):
        import platform as _platform
        console.print(f"[bold cyan]ddm[/] [green]{__version__}[/]")
        console.print(f"  python:   {_platform.python_version()}")
        console.print(f"  platform: {_platform.system()} {_platform.machine()}")
        sys.exit(0)
    main()


if __name__ == "__main__":
    _cli_entry()
