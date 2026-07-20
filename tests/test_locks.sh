#!/bin/bash
#=========================================================================
# DDM 状态锁场景测试
#
# 运行: bash tests/test_locks.sh
#
# 覆盖:
#   1. 模块锁: 同模块同 tag 互斥
#   2. 模块锁: 不同模块不互斥
#   3. 过期锁自动清除 (PID 已死)
#   4. 过期锁 PID 检测 (活的进程不会误清)
#   5. 新鲜锁拒绝 (超时内且 PID 活着)
#   6. tag release 锁阻塞同 tag submit
#   7. tag release 锁不阻塞不同 tag submit
#   8. tag release 锁阻塞同 tag release
#   9. tag release 锁不阻塞不同 tag release
#  10. ddm check 过期锁检测
#  11. ddm check 运行中锁提示
#  12. 极端: 不同 owner 同时提交 (互斥)
#  13. 极端: release 和 submit 同一 tag (互斥)
#=========================================================================

cd "$(dirname "$0")/.."

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

pass_count=0; fail_count=0
pass() { echo -e "  ${GREEN}✓ PASS${NC} $1"; pass_count=$((pass_count+1)); }
fail() { echo -e "  ${RED}✗ FAIL${NC} $1"; fail_count=$((fail_count+1)); }
info() { echo -e "  ${CYAN}→${NC} $1"; }
banner() { echo ""; echo -e "${BOLD}${YELLOW}━━━ $1 ━━━${NC}"; echo ""; }

# ensure repo exists
mkdir -p repository/raw repository/ready
python3 -c "from ddm.storage import Storage; Storage('repository/ddm.db')" > /dev/null 2>&1 || true

# ---- helper: create fake lock ----
mk_lock() {  # $1=dir $2=name $3=pid $4=user $5=age_min_ago
    local path="${1}/${2}"
    local ts=$(date -d "${5:-0} minutes ago" +%s 2>/dev/null || python3 -c "import time; print(int(time.time())-${5:-0}*60)")
    cat > "$path" <<EOF
pid=${3:-99999}
user=${4:-test_user}
time=${ts}
EOF
    touch -t "$(date -d "@${ts}" +%Y%m%d%H%M.%S 2>/dev/null || python3 -c "import time; t=${ts}; print(time.strftime('%Y%m%d%H%M.%S', time.localtime(t)))")" "$path"
}

# ---- helper: check output ----
assert_contains() {  # $1=output $2=pattern $3=test_desc $4=expected_desc
    if echo "$1" | grep -q "$2"; then pass "$3"; else fail "$3" "$4"; fi
}

# ---- clean state ----
cleanup() { rm -f repository/raw/.lock_* repository/ready/.lock_release_*; [ -n "$SLEEP_PID" ] && kill $SLEEP_PID 2>/dev/null || true; }
trap cleanup EXIT
cleanup

# ══════════════════════════════════════════════════════════════════
banner "Test 1: 模块锁 — 同模块同 tag 互斥"
# ══════════════════════════════════════════════════════════════════
mk_lock repository/raw .lock_CPU_PV_ITER $$ live_user 0

info "submit CPU @ PV_ITER (锁由 live_user 持有, PID=$$ 活着)"
output=$(python3 -m ddm submit -m CPU -t PV_ITER -s "test" 2>&1) || true
echo "$output"
echo ""
assert_contains "$output" "lock exists" "同模块锁拒绝" "lock exists"

cleanup

# ══════════════════════════════════════════════════════════════════
banner "Test 2: 模块锁 — 不同模块不互斥"
# ══════════════════════════════════════════════════════════════════
mk_lock repository/raw .lock_CPU_PV_ITER $$ live_user 0

info "submit DDR @ PV_ITER (CPU 有锁, DDR 不应受影响)"
output=$(python3 -m ddm submit -m DDR -t PV_ITER -s "test" 2>&1) || true
echo "$output"
echo ""
assert_contains "$output" "✓ Submit successful" "不同模块不阻塞" "exit 0"

cleanup

# ══════════════════════════════════════════════════════════════════
banner "Test 3: 过期锁自动清除 — PID 已死 + 超时"
# ══════════════════════════════════════════════════════════════════
mk_lock repository/raw .lock_CPU_PV_ITER 99999 crashed_user 15

info "submit CPU (锁 15 分钟前, PID 99999 不存在 → 自动清除)"
output=$(python3 -m ddm submit -m CPU -t PV_ITER -s "test" 2>&1) || true
echo "$output"
echo ""
assert_contains "$output" "✓ Submit successful" "过期锁自动清除" "✓ Submit successful"
assert_contains "$output" "⚠.*过期锁.*自动清除" "显示过期锁警告" "过期锁警告"

cleanup

# ══════════════════════════════════════════════════════════════════
banner "Test 4: 极端 — PID 活着 + 超时 (长任务未释放)"
# ══════════════════════════════════════════════════════════════════
mk_lock repository/raw .lock_CPU_PV_ITER $$ live_user 15

info "submit CPU (锁 PID=$$ 活着, 虽然 15 分钟前 → 自动清除但有警告)"
output=$(python3 -m ddm submit -m CPU -t PV_ITER -s "test" 2>&1) || true
echo "$output"
echo ""
assert_contains "$output" "✓ Submit successful" "活 PID 也会清除" "✓ Submit successful"
assert_contains "$output" "仍在运行.*Ctrl.C" "显示运行警告" "仍在运行"

cleanup

# ══════════════════════════════════════════════════════════════════
banner "Test 5: 新鲜锁 — 超时内 + PID 活着 → 拒绝"
# ══════════════════════════════════════════════════════════════════
mk_lock repository/raw .lock_CPU_PV_ITER $$ live_user 0

info "submit CPU (锁 0 分钟前, PID=$$ 活着 → 拒绝)"
output=$(python3 -m ddm submit -m CPU -t PV_ITER -s "test" 2>&1) || true
echo "$output"
echo ""
assert_contains "$output" "lock exists" "新鲜锁拒绝" "lock exists"

cleanup

# ══════════════════════════════════════════════════════════════════
banner "Test 6: tag release 锁 — 阻塞同 tag submit"
# ══════════════════════════════════════════════════════════════════
mk_lock repository/ready .lock_release_PV_ITER $$ release_user 0

info "submit CPU @ PV_ITER (PV_ITER 正在 release)"
output=$(python3 -m ddm submit -m CPU -t PV_ITER -s "test" 2>&1) || true
echo "$output"
echo ""
assert_contains "$output" "Release.*提交被阻断" "release 锁阻塞 submit" "Release.*阻断"

cleanup

# ══════════════════════════════════════════════════════════════════
banner "Test 7: tag release 锁 — 不阻塞不同 tag submit"
# ══════════════════════════════════════════════════════════════════
mk_lock repository/ready .lock_release_PV_ITER $$ release_user 0

info "submit CPU @ PI_ITER (PV_ITER 在 release, PI_ITER 不受影响)"
output=$(python3 -m ddm submit -m CPU -t PI_ITER -s "test" 2>&1) || true
echo "$output"
echo ""
assert_contains "$output" "✓ Submit successful" "不同 tag submit 不阻塞" "✓ Submit successful"

cleanup

# ══════════════════════════════════════════════════════════════════
banner "Test 8: tag release 锁 — 阻塞同 tag release"
# ══════════════════════════════════════════════════════════════════
mk_lock repository/ready .lock_release_PV_ITER $$ release_user 0

info "release PV_ITER (PV_ITER 已在 release)"
output=$(python3 -m ddm release -t PV_ITER -m CPU -v V99 2>&1) || true
echo "$output"
echo ""
assert_contains "$output" "Release.*发布被阻断" "release 锁阻塞 release" "Release.*阻断"

cleanup

# ══════════════════════════════════════════════════════════════════
banner "Test 9: tag release 锁 — 不阻塞不同 tag release"
# ══════════════════════════════════════════════════════════════════
mk_lock repository/ready .lock_release_PV_ITER $$ release_user 0

info "release PI_ITER (PV_ITER 在 release, PI_ITER 不受影响)"
output=$(python3 -m ddm release -t PI_ITER -m CPU -v V98 2>&1) || true
echo "$output"
echo ""
assert_contains "$output" "✓ Release successful" "不同 tag release 不阻塞" "✓ Release successful"

cleanup

# ══════════════════════════════════════════════════════════════════
banner "Test 10: ddm check — 过期锁 + 运行中锁"
# ══════════════════════════════════════════════════════════════════
mk_lock repository/raw .lock_DDR_PV_ITER 99999 crashed_user 20
mk_lock repository/raw .lock_CPU_BASE_CLEAN $$ live_user 20

info "ddm check 应分别报告过期锁和运行中的锁"
output=$(python3 -m ddm check 2>&1) || true
echo "$output"
echo ""
assert_contains "$output" "进程已退出" "check 检测过期锁" "进程已退出"
assert_contains "$output" "长期运行中的锁\|仍在运行" "check 检测运行锁" "运行中的锁"

cleanup

# ══════════════════════════════════════════════════════════════════
banner "Test 11: 极端 — 不同 owner 同时提交同一模块 (互斥)"
# ══════════════════════════════════════════════════════════════════
# 用后台 sleep 模拟 wangshuai 的活进程
sleep 300 &
SLEEP_PID=$!
mk_lock repository/raw .lock_CPU_PV_ITER $SLEEP_PID wangshuai 2

info "lisi 提交 CPU (wangshuai 的 PID=$SLEEP_PID 还活着, 2 分钟前)"
output=$(python3 -m ddm submit -m CPU -t PV_ITER -s "lisi test" 2>&1) || true
echo "$output"
echo ""
assert_contains "$output" "lock exists" "不同 owner 互斥" "lock exists"

kill $SLEEP_PID 2>/dev/null || true
cleanup

# ══════════════════════════════════════════════════════════════════
banner "Test 12: 极端 — wangshuai 崩溃后 lisi 接手"
# ══════════════════════════════════════════════════════════════════
mk_lock repository/raw .lock_CPU_PV_ITER 99999 wangshuai 15

info "lisi 提交 CPU (wangshuai 已崩溃, 锁 15 分钟前 → 自动清除)"
output=$(python3 -m ddm submit -m CPU -t PV_ITER -s "lisi takeover" 2>&1) || true
echo "$output"
echo ""
assert_contains "$output" "✓ Submit successful" "崩溃后自动清除" "✓ Submit successful"
assert_contains "$output" "crashed_user\|wangshuai" "显示原用户" "user=wangshuai"

cleanup

# ══════════════════════════════════════════════════════════════════
banner "Test 13: 极端 — release 阻塞 submit (反方向)"
# ══════════════════════════════════════════════════════════════════
# 先让 submit 拿到锁, 然后 release 检查锁
mk_lock repository/raw .lock_CPU_PV_ITER $$ submit_user 1

info "release -A PV_ITER (CPU 正在 submit → 阻断)"
output=$(python3 -m ddm release -t PV_ITER -A -v V100 2>&1) || true
echo "$output"
echo ""
assert_contains "$output" "CPU.*submit.*完成" "release 被 submit 阻塞" "CPU 提交中阻断"

cleanup

# ══════════════════════════════════════════════════════════════════
banner "Summary"
# ══════════════════════════════════════════════════════════════════
echo -e "  ${GREEN}Passed: $pass_count${NC}  ${RED}Failed: $fail_count${NC}"
echo ""
exit $fail_count
