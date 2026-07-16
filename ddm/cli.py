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

    \b
    Enable tab completion:
      eval "$(_DDM_COMPLETE=bash_source ddm)"
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
@click.option("-A", "--all", "list_all", is_flag=True, help="List all modules")
@click.option("-m", "--module", default=None, help="Filter by module")
@click.option("-v", "--verbose", is_flag=True, help="Show per-file BLAKE3 hash and timestamp")
@click.pass_context
def _list(ctx, tag, list_all, module, verbose):
    """List submitted / released data for a tag."""
    config_path = ctx.obj["config_path"]
    cfg, storage = _init_config_and_storage(config_path)

    if tag not in cfg.tag_names():
        console.print(f"[red]Error:[/] Unknown tag '{tag}'. Supported: {', '.join(cfg.tag_names())}")
        sys.exit(1)

    batches = storage.list_batches(tag=tag, module=module if not list_all else None)

    if not batches:
        console.print(f"[yellow]No records for tag=[bold]{tag}[/][/]")
        return

    if verbose:
        _list_verbose(storage, batches, tag)
    else:
        _list_summary(storage, batches, tag, cfg)


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

        table.add_row(
            b["module"],
            f"[{color}]{st}[/]",
            b.get("version", "") or "-",
            str(file_count),
            _fmt_size(total_size),
            delta_str,
            ts,
        )

    console.print(table)


def _list_verbose(storage, batches, tag):
    """Per-file detail table with BLAKE3 and timestamp."""
    for b in batches:
        files = storage.get_files(b["batch_uuid"])
        total_size = sum(f.get("file_size", 0) for f in files)
        st = b["status"]
        status_colors = {"PENDING": "yellow", "SUBMITTED": "cyan", "RELEASED": "green", "FAILED": "red"}
        color = status_colors.get(st, "white")

        console.print(f"\n[bold]{b['module']}[/]  [{color}]{st}[/]  "
                      f"v={b.get('version') or '-'}  "
                      f"files={len(files)}  size={_fmt_size(total_size)}")

        ftable = Table(show_header=True, box=None)
        ftable.add_column("File", style="dim")
        ftable.add_column("Size", justify="right")
        ftable.add_column("BLAKE3", style="dim", max_width=20)
        ftable.add_column("Timestamp")

        for f in files:
            fname = Path(f["source_path"]).name if f.get("source_path") else "-"
            fsize = _fmt_size(f.get("file_size", 0))
            blake3_short = f.get("blake3_hash", "")[:12] if f.get("blake3_hash") else "-"
            mtime_val = f.get("source_mtime", 0)
            mtime_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime_val)) if mtime_val else "-"

            ftable.add_row(fname, fsize, blake3_short, mtime_str)

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
    for label, path in [
        ("Outgoing root", cfg.outgoing_root),
        ("Repository root", cfg.repository_root),
    ]:
        p = Path(path)
        if p.exists():
            console.print(f"  [green]✓[/] {label}: {p.resolve()}")
        else:
            console.print(f"  [yellow]![/] {label} (not created yet): {p.resolve()}")

    # psutil
    try:
        import psutil
        console.print(f"  [green]✓[/] psutil available")
    except ImportError:
        console.print(f"  [red]✗[/] psutil not available")

    console.print("\n[bold green]Check complete.[/]")


if __name__ == "__main__":
    main()
