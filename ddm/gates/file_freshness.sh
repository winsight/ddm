#!/bin/bash
# File freshness gate — standalone shell version.
# Usage: file_freshness.sh <raw_dir> <module> <tag>
#
# This demonstrates the external-script gate interface (方式 A).
# Returns exit code 0 if all files under raw_dir are ≤ 2h old.

set -e

RAW_DIR="$1"
MODULE="$2"
TAG="$3"
MAX_HOURS=2
DEADLINE=$(python3 -c "import time; print(time.time() - ${MAX_HOURS} * 3600)")

echo "Checking file freshness for ${MODULE}/${TAG}"
echo "  max age: ${MAX_HOURS}h"
echo ""

STALE=0
OK=0

# Use find + stat to check each file
while IFS= read -r -d '' f; do
    MTIME=$(python3 -c "import os; print(os.path.getmtime('$f'))")
    AGE=$(python3 -c "import time; print(int(time.time() - $MTIME))")
    FNAME=$(basename "$f")

    if python3 -c "exit(0 if $MTIME >= $DEADLINE else 1)"; then
        echo "  [OK]    $FNAME  age=${AGE}s"
        OK=$((OK + 1))
    else
        H=$(python3 -c "print(f'{$AGE / 3600:.1f}h')")
        echo "  [STALE] $FNAME  age=$H"
        STALE=$((STALE + 1))
    fi
done < <(find "$RAW_DIR" -type f -print0)

echo ""
echo "  fresh: $OK  stale: $STALE"

if [ "$STALE" -gt 0 ]; then
    echo "FAIL: $STALE file(s) older than ${MAX_HOURS}h"
    exit 1
else
    echo "PASS: all files within ${MAX_HOURS}h window"
    exit 0
fi
