"""File consistency gate — reject batches whose file mtimestamps span too wide.

A submission should contain files generated in the same tool run.  If one
file's mtime is hours older than the rest, it likely belongs to a different
version and the batch is inconsistent.
"""
import sys
import time
from pathlib import Path

# ---- configurable ----
MAX_SPAN_HOURS = 2.0    # max allowed spread between oldest and newest mtime
# ---------------------


def _fmt_age(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        return f"{seconds / 60:.1f}m"
    else:
        return f"{seconds / 3600:.1f}h"


def main():
    raw_dir = Path(sys.argv[1])
    module  = sys.argv[2]
    tag     = sys.argv[3]

    files = sorted(f for f in raw_dir.rglob("*") if f.is_file())
    if len(files) < 2:
        print(f"Only {len(files)} file(s) — nothing to compare, PASS")
        sys.exit(0)

    # Collect mtime for every file
    mtimestamps = []
    for f in files:
        mt = f.stat().st_mtime
        mtimestamps.append((f.name, mt))

    # Find spread
    oldest = min(mtimestamps, key=lambda x: x[1])
    newest = max(mtimestamps, key=lambda x: x[1])
    span = newest[1] - oldest[1]

    print(f"Checking time consistency for {module}/{tag}")
    print(f"  {len(files)} files  |  max allowed spread: {MAX_SPAN_HOURS}h")
    print()

    for fname, mt in sorted(mtimestamps, key=lambda x: x[1]):
        mark = ""
        if (fname, mt) == oldest:
            mark = " ← oldest"
        elif (fname, mt) == newest:
            mark = " ← newest"
        print(f"  {time.strftime('%H:%M:%S', time.localtime(mt))}  {fname:30s}{mark}")

    print()
    print(f"  span: {_fmt_age(span)}  (oldest → newest)")

    if span > MAX_SPAN_HOURS * 3600:
        print(f"FAIL: file mtimestamps span {_fmt_age(span)} > {MAX_SPAN_HOURS}h.")
        print(f"      Oldest: {oldest[0]} ({time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(oldest[1]))})")
        print(f"      Newest: {newest[0]} ({time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(newest[1]))})")
        print(f"      These files may belong to different versions — re-generate together.")
        sys.exit(1)
    else:
        print(f"PASS: time spread {_fmt_age(span)} within {MAX_SPAN_HOURS}h window")
        sys.exit(0)


if __name__ == "__main__":
    main()
