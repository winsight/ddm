#!/bin/csh -f
# File freshness gate — tcsh/csh version.
# Usage: file_freshness.csh <raw_dir> <module> <tag>
#
# Demonstrates external-script gate interface (方式 A) for tcsh environments.
# Returns exit 0 if all files under raw_dir are ≤ 2h old.

set raw_dir  = "$1"
set module   = "$2"
set tag      = "$3"
set max_h    = 2

set deadline = `python3 -c "import time; print(time.time() - ${max_h} * 3600)"`

echo "Checking file freshness for ${module}/${tag}  (max age: ${max_h}h)"
echo ""

set stale = 0
set ok    = 0

foreach f (`find "$raw_dir" -type f`)
    set fname   = `basename "$f"`
    set mtime   = `python3 -c "import os; print(os.path.getmtime('$f'))"`
    set age_sec = `python3 -c "import time; print(int(time.time() - $mtime))"`

    if (`python3 -c "print(1 if $mtime >= $deadline else 0)"` == 1) then
        echo "  [OK]    ${fname}  age=${age_sec}s"
        @ ok++
    else
        set age_h = `python3 -c "print(f'{$age_sec / 3600:.1f}h')"`
        echo "  [STALE] ${fname}  age=${age_h}"
        @ stale++
    endif
end

echo ""
echo "  fresh: $ok  stale: $stale"

if ($stale > 0) then
    echo "FAIL: $stale file(s) older than ${max_h}h"
    exit 1
else
    echo "PASS: all $ok file(s) within ${max_h}h window"
    exit 0
endif
