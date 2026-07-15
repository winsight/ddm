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
    ctx.obj["config_path"] = config


# ---------------------------------------------------------------------------
# submit
# ---------------------------------------------------------------------------


@main.command(name="submit")
@click.option("-m", "--module", required=True, help="Module name (e.g. CPU, DDR)")
@click.option(
    "-t", "--tag",
    required=True,
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
@click.option("-t", "--tag", required=True, help=f"Tag: {', '.join(sorted(VALID_TAGS))}")
@click.option("-A", "--all", "release_all", is_flag=True, help="Release all modules")
@click.option("-m", "--module", default=None, help="Release specific module")
@click.option("-v", "--version", default="", help="Version label (default: date stamp)")
@click.pass_context
def _release(ctx, tag, release_all, module, version):
    """Release submitted data to release/ directory."""
    config_path = ctx.obj["config_path"]
    cfg, storage = _init_config_and_storage(config_path)
    _setup_logger(cfg)

    if tag not in cfg.tag_names():
        console.print(f"[red]Error:[/] Unknown tag '{tag}'")
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


@main.command(name="list")
@click.option("-t", "--tag", required=True, help=f"Tag: {', '.join(sorted(VALID_TAGS))}")
@click.option("-A", "--all", "list_all", is_flag=True, help="List all modules")
@click.option("-m", "--module", default=None, help="Filter by module")
@click.pass_context
def _list(ctx, tag, list_all, module):
    """List submitted / released data for a tag."""
    config_path = ctx.obj["config_path"]
    cfg, storage = _init_config_and_storage(config_path)

    if tag not in cfg.tag_names():
        console.print(f"[red]Error:[/] Unknown tag '{tag}'")
        sys.exit(1)

    batches = storage.list_batches(tag=tag, module=module if not list_all else None)

    if not batches:
        console.print(f"[yellow]No records for tag=[bold]{tag}[/][/]")
        return

    table = Table(title=f"Records for tag: {tag}")
    table.add_column("UUID", style="dim", max_width=10)
    table.add_column("Module")
    table.add_column("User")
    table.add_column("Status")
    table.add_column("Version")
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
            b["module"],
            b["username"],
            f"[{color}]{st}[/]",
            b.get("version", "") or "-",
            b.get("summary", "") or "-",
            ts,
        )

    console.print(table)


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
