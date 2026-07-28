"""File freshness gate — reject files whose mtime is older than 2 hours.

This demonstrates the standard Python gate interface.

Runner passes: <raw_dir> <module> <tag>
  sys.argv[1] — raw/PV_ITER/CPU
  sys.argv[2] — CPU
  sys.argv[3] — PV_ITER
"""
import os
import sys
import time
from pathlib import Path

# ---- configurable ----
MAX_AGE_HOURS = 2.0
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

    deadline = time.time() - MAX_AGE_HOURS * 3600

    # Collect all regular files under raw_dir
    files = sorted(f for f in raw_dir.rglob("*") if f.is_file())
    if not files:
        print(f"No files found in {raw_dir}")
        sys.exit(1)

    print(f"Checking {len(files)} file(s) for {module}/{tag}")
    print(f"  max age: {MAX_AGE_HOURS}h  (deadline: {time.strftime('%H:%M:%S', time.localtime(deadline))})")
    print()

    stale = []
    ok = 0
    for f in files:
        mtime = f.stat().st_mtime
        age = time.time() - mtime
        status = "OK" if mtime >= deadline else "STALE"
        print(f"  [{status}] {f.name:30s}  mtime={time.strftime('%H:%M:%S', time.localtime(mtime))}  age={_fmt_age(age)}")
        if mtime >= deadline:
            ok += 1
        else:
            stale.append((f.name, _fmt_age(age)))

    print()
    print(f"  fresh: {ok}  stale: {len(stale)}")

    if stale:
        print(f"FAIL: {len(stale)} file(s) older than {MAX_AGE_HOURS}h:")
        for name, age in stale:
            print(f"       {name} ({age} old)")
        sys.exit(1)
    else:
        print(f"PASS: all {ok} file(s) within {MAX_AGE_HOURS}h window")
        sys.exit(0)


if __name__ == "__main__":
    main()
