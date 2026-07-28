# DDM Gate 插件开发指南

DDM 的门禁（Gate）系统在 `submit` 流程中，文件从 `a0.outgoing/` 拷贝到 `raw/` 后、移入 `ready/` 前自动运行一系列检查。检查通过则继续，任一失败则阻断提交。

## 目录

- [快速开始：新增一个 Python 门禁](#快速开始新增一个-python-门禁)
- [完整实例：file_freshness 门禁](#完整实例file_freshness-门禁)
  - [Python 版本](#python-版本)
  - [Bash 版本](#bash-版本)
  - [CSH/tcsh 版本](#cshtcsh-版本)
  - [配置注册](#配置注册)
  - [测试方法](#测试方法)
- [接口规范](#接口规范)
  - [命令行参数](#命令行参数)
  - [返回值协议](#返回值协议)
  - [runner 如何调用外部脚本](#runner-如何调用外部脚本)
  - [stdout / stderr](#stdout--stderr)
  - [超时](#超时)
- [Flag 文件协议（异步 / 集群场景）](#flag-文件协议异步--集群场景)
- [LSF/Grid 集群提交](#lsfgrid-集群提交)
- [调试技巧](#调试技巧)
- [注意事项汇总](#注意事项汇总)

---

## 快速开始：新增一个 Python 门禁

### Step 1 — 创建门禁脚本

在 `ddm/gates/` 下新建一个 `.py` 文件：

```python
# ddm/gates/power_check.py
"""Power integrity check gate."""
import sys
from pathlib import Path

def main():
    raw_dir = Path(sys.argv[1])   # raw/<tag>/<module>/
    module  = sys.argv[2]         # e.g. "CPU"
    tag     = sys.argv[3]         # e.g. "PV_ITER"

    # ---- 你的检查逻辑 ----
    pg_files = list(raw_dir.rglob("*.v.pg"))
    if not pg_files:
        print(f"WARNING: no .v.pg files found for {module}/{tag}")
        sys.exit(1)              # 非 0 → FAIL

    for f in pg_files:
        print(f"Checking power grid: {f.name}")
        # ... 实际的 power 分析逻辑 ...

    print(f"PASS: {len(pg_files)} power grid file(s) OK")
    sys.exit(0)                  # 0 → PASS

if __name__ == "__main__":
    main()
```

### Step 2 — 注册到 config.yaml

```yaml
defaults:
  tag:
    PV_ITER:
      gates:
        - name: verilog_syntax_check
          command: python3 -m ddm.gates.verilog_check
        - name: power_integrity_check
          command: python3 -m ddm.gates.power_check
```

- `name`：在 submit 进度条和日志中显示的名字
- `command`：runner 会拆成参数列表，追加 `raw_dir module tag` 后执行

### Step 3 — 完成

```bash
ddm submit -m CPU -t PV_ITER    # 新 gate 自动生效，无需重启
```

---

## 完整实例：file_freshness 门禁

功能：检查一次提交中所有文件的 mtime 是否在 N 小时内。有 stale 文件则阻断提交，全部 fresh 则放行。

### Python 版本

`ddm/gates/file_freshness.py`：

```python
"""File freshness gate — reject files whose mtime is older than N hours."""
import sys
import time
from pathlib import Path

MAX_AGE_HOURS = 2.0    # 超过此时间的文件视为 stale

def _fmt_age(seconds: float) -> str:
    if seconds < 60:       return f"{seconds:.0f}s"
    elif seconds < 3600:   return f"{seconds / 60:.1f}m"
    else:                  return f"{seconds / 3600:.1f}h"

def main():
    raw_dir = Path(sys.argv[1])
    module  = sys.argv[2]
    tag     = sys.argv[3]

    deadline = time.time() - MAX_AGE_HOURS * 3600
    files = sorted(f for f in raw_dir.rglob("*") if f.is_file())

    if not files:
        print(f"No files found in {raw_dir}")
        sys.exit(1)

    print(f"Checking {len(files)} file(s) for {module}/{tag}")
    print(f"  max age: {MAX_AGE_HOURS}h  "
          f"(deadline: {time.strftime('%H:%M:%S', time.localtime(deadline))})")
    print()

    stale = []
    ok = 0
    for f in files:
        mtime = f.stat().st_mtime
        age = time.time() - mtime
        status = "OK" if mtime >= deadline else "STALE"
        print(f"  [{status}] {f.name:30s}  "
              f"mtime={time.strftime('%H:%M:%S', time.localtime(mtime))}  "
              f"age={_fmt_age(age)}")
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
```

配置：
```yaml
gates:
  - name: file_freshness
    command: python3 -m ddm.gates.file_freshness
```

`MAX_AGE_HOURS` 可以按需调整（比如测试时设成 `0.01` = 36 秒，方便制造 stale 场景）。

### Bash 版本

`ddm/gates/file_freshness.sh`：

```bash
#!/bin/bash
# File freshness gate — standalone shell version.
set -e

RAW_DIR="$1"; MODULE="$2"; TAG="$3"
MAX_HOURS=2
DEADLINE=$(python3 -c "import time; print(time.time() - ${MAX_HOURS} * 3600)")

echo "Checking file freshness for ${MODULE}/${TAG}"
echo "  max age: ${MAX_HOURS}h"; echo ""

STALE=0; OK=0

while IFS= read -r -d '' f; do
    MTIME=$(python3 -c "import os; print(os.path.getmtime('$f'))")
    AGE=$(python3 -c "import time; print(int(time.time() - $MTIME))")
    FNAME=$(basename "$f")

    if python3 -c "exit(0 if $MTIME >= $DEADLINE else 1)"; then
        echo "  [OK]    $FNAME  age=${AGE}s"; OK=$((OK + 1))
    else
        H=$(python3 -c "print(f'{$AGE / 3600:.1f}h')")
        echo "  [STALE] $FNAME  age=$H"; STALE=$((STALE + 1))
    fi
done < <(find "$RAW_DIR" -type f -print0)

echo ""; echo "  fresh: $OK  stale: $STALE"

if [ "$STALE" -gt 0 ]; then
    echo "FAIL: $STALE file(s) older than ${MAX_HOURS}h"; exit 1
else
    echo "PASS: all files within ${MAX_HOURS}h window"; exit 0
fi
```

### CSH/tcsh 版本

`ddm/gates/file_freshness.csh`：

```csh
#!/bin/csh -f
# File freshness gate — tcsh/csh version.

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
```

CSH 脚本需要通过 **shebang** 让 OS 选择解释器（`#!/bin/csh -f`）。runner 不会对 CSH 脚本做 `sys.executable` 替换（只对 `python`/`python3` 开头的 command 做替换）。

### 配置注册

在 `config.yaml` 中三种方式任选：

```yaml
defaults:
  tag:
    PV_ITER:
      gates:
        # ---- Python 模块 gate ----
        - name: file_freshness
          command: python3 -m ddm.gates.file_freshness

        # ---- Bash 外部脚本 ----
        - name: file_freshness
          command: /path/to/ddm/gates/file_freshness.sh

        # ---- CSH 外部脚本 ----
        - name: file_freshness
          command: /path/to/ddm/gates/file_freshness.csh
```

### 测试方法

#### 1. 手动直接测试（模拟 runner 调用，调试时最常用）

Runner 实际拼接的命令是 `<command> <raw_dir> <module> <tag>`：

```bash
# 准备测试数据
mkdir -p /tmp/gate_test
touch /tmp/gate_test/fresh.v.gz
touch -t 202601010000 /tmp/gate_test/old.v.gz   # 造一个 stale 文件

# Python 版本
python3 -m ddm.gates.file_freshness /tmp/gate_test CPU PV_ITER
echo "exit: $?"       # 0 = PASS, 1 = FAIL

# Bash 版本
bash /path/to/ddm/gates/file_freshness.sh /tmp/gate_test CPU PV_ITER
echo "exit: $?"

# CSH 版本
csh /path/to/ddm/gates/file_freshness.csh /tmp/gate_test CPU PV_ITER
echo "exit: $?"
```

#### 2. 完整 submit 测试

```bash
# 刷新源文件时间戳（streaming_copy 保留 mtime，所以源文件必须新鲜）
touch ~/a0.outgoing/CPU/*

# 提交流程 → runner → gate 子进程
ddm submit -m CPU -t PV_ITER
```

输出中会看到：
```
Gates file_freshness ✓ (0.1s)    ← PASS
Gates file_freshness ✗           ← FAIL
```

#### 3. 查看 gate 日志

```bash
grep "gate\|file_freshness\|stale\|Stale" logs/ddm_*.log
```

#### 4. 查看 SQLite 事件

```bash
sqlite3 repository/ddm.db \
  "SELECT batch_uuid, event_type, substr(message,1,150)
   FROM events WHERE event_type LIKE '%gate%'
   ORDER BY created_at DESC LIMIT 10;"
```

#### 5. 调试 stderr

Runner 捕获子进程的 stderr，在 gate 失败时把前 200 字符写入 `EVENT_GATE_FAIL` 事件。如果 CSH 脚本报错，stderr 会出现在 SQLite events 表和日志中。

---

## 接口规范

### 命令行参数

Runner 拼接的实际命令：

```
<command> <raw_dir> <module> <tag>
```

例如：

```
python3 -m ddm.gates.file_freshness /nfs/ddm/repository/raw/PV_ITER/CPU CPU PV_ITER
/bin/csh /path/to/script.csh /nfs/ddm/repository/raw/PV_ITER/CPU CPU PV_ITER
```

| 参数位置 | 变量名 | 含义 | 示例 |
|----------|--------|------|------|
| 第 1 个 | `$1` / `sys.argv[1]` | raw 目录的绝对路径 | `/nfs/.../raw/PV_ITER/CPU` |
| 第 2 个 | `$2` / `sys.argv[2]` | 模块名 | `CPU` |
| 第 3 个 | `$3` / `sys.argv[3]` | tag 名 | `PV_ITER` |

### 返回值协议

Runner 的判定优先级（从高到低）：

#### 1. Flag 文件（最高优先级，见下文）

`raw_dir/.ddm_gate_<name>` 或 `raw_dir/.ddm_gate_<name>.json` 第一行（/JSON 字段）决定结果，覆盖 exit code。

#### 2. Exit Code（默认）

```
exit 0  → PASS
exit ≠0 → FAIL
```

Python: `sys.exit(0)` → PASS；非零 → FAIL。
CSH/Bash: `exit 0` → PASS；非零 → FAIL。

### runner 如何调用外部脚本

```python
# runner.py 核心逻辑 (简化)
cmd_parts = gate.command.split()           # e.g. ['/path/to/script.csh']

# 只有 python/python3 开头才替换为 sys.executable
if cmd_parts and cmd_parts[0] in ("python", "python3"):
    cmd_parts[0] = sys.executable          # → /path/to/venv/bin/python3

proc = subprocess.run(
    cmd_parts + [raw_dir, module, tag],    # 追加 3 个参数
    capture_output=True, text=True,
    timeout=300,
)
passed = proc.returncode == 0

# 然后检查 flag 文件（可选覆盖）
passed, msg = _check_flag_file(raw_dir, gate.name, passed)
```

**关键点**：

| command 开头 | 行为 |
|-------------|------|
| `python` / `python3` | runner 替换为 `sys.executable`（venv 的 Python） |
| `/path/to/script` | 不做替换，依赖 shebang（`#!/bin/csh -f` 等） |
| `bash script.sh` | bash 走 `PATH` 查找，不替换 |

对于 CSH 脚本，推荐写法：用绝对路径，shebang 声明 `#!/bin/csh -f`，runner 当作普通可执行文件调用。

### stdout / stderr

- **stdout**：子进程全部 stdout 被 runner 捕获，写入日志。gate PASS 时不显示在终端，FAIL 时 stderr 前 200 字符写入事件表。
- **stderr**：gate 失败时写入 `EVENT_GATE_FAIL` 事件。CSH 脚本的 `echo` 输出到 stdout，`echo "error" >&2` 输出到 stderr。

### 超时

默认每个 gate 300 秒（5 分钟），超过则判定 FAIL。可在 `run_gates()` 调用时配置：

```python
run_gates(gate_defs, raw_dir, module, tag, timeout=600)
```

---

## Flag 文件协议（异步 / 集群场景）

适用于：脚本提交 LSF 任务后立即返回，实际检查结果由后续流程通过 flag 文件传递。

### 协议规则

1. Gate 脚本（或后续检查任务）在 `raw_dir` 下生成 flag 文件
2. 文件名约定：`.ddm_gate_<gate_name>`（纯文本）或 `.ddm_gate_<gate_name>.json`（JSON）
3. Runner 在子进程退出后检查 flag 文件，存在则覆盖 exit code 判定

### 纯文本格式

```
.ddm_gate_<name> → 第一行以 "PASS" 或 "FAIL" 开头
```

```csh
#!/bin/csh -f
set raw_dir = $1
set flag    = "$raw_dir/.ddm_gate_custom_drc"

# 提交 LSF 任务
bsub -q normal -J drc_task /path/to/run_drc.csh $raw_dir $2 $3
echo "PASS" > $flag       # 提交成功即放行
exit 0
```

### JSON 格式（含指标）

```json
{
  "status": "pass",
  "reason": "LSF job 12345 completed successfully",
  "metrics": {
    "lsf_job_id": "12345",
    "drc_violations": 0,
    "runtime": "12m"
  }
}
```

```python
import json
flag_file.write_text(json.dumps({
    "status": "pass",
    "reason": "All checks passed",
    "metrics": {"violations": 0}
}))
```

---

## LSF/Grid 集群提交

### 模式 1：提交即过（异步）

```yaml
gates:
  - name: drc_submit
    command: python3 -m ddm.gates.lsf_submit
```

Gate 脚本只负责提交 LSF 作业，提交成功 → flag 文件写 PASS → gate PASS。实际 DRC 结果由 release 阶段或人工验证。

### 模式 2：轮询等待（同步/半同步）

```python
# ddm/gates/lsf_wait_check.py — 提交并等待完成
import sys, subprocess, time
from pathlib import Path

def main():
    raw_dir = Path(sys.argv[1])
    module  = sys.argv[2]
    tag     = sys.argv[3]

    proc = subprocess.run(
        ["bsub", "-q", "normal", "-J", f"drc_{module}",
         "/path/to/run_drc.csh", str(raw_dir), module, tag],
        capture_output=True, text=True
    )
    job_id = proc.stdout.strip().split()[1].lstrip("<").rstrip(">")
    print(f"LSF job {job_id} submitted")

    deadline = time.time() + 1800    # 最多等 30 分钟
    while time.time() < deadline:
        check = subprocess.run(
            ["bjobs", "-o", "stat", "-noheader", job_id],
            capture_output=True, text=True
        )
        s = check.stdout.strip()
        if s == "DONE":
            print(f"Job {job_id} completed"); sys.exit(0)
        elif s in ("EXIT", "UNKWN"):
            print(f"Job {job_id} failed: {s}"); sys.exit(1)
        time.sleep(30)

    print(f"Job {job_id} timeout"); sys.exit(1)

if __name__ == "__main__":
    main()
```

---

## 调试技巧

| 场景 | 命令 |
|------|------|
| 手动跑 gate | `python3 -m ddm.gates.<name> /tmp/test CPU PV_ITER` |
| 手动跑 CSH gate | `csh /path/to/gate.csh /tmp/test CPU PV_ITER` |
| 看 gate 日志 | `grep "gate\|Gate" logs/ddm_*.log` |
| 看 gate 事件 | `sqlite3 repository/ddm.db "SELECT * FROM events WHERE event_type LIKE '%gate%' ORDER BY created_at DESC LIMIT 10;"` |
| 测试 LSF | `which bsub && bsub -q normal -J test /path/to/script.sh /tmp/test CPU PV_ITER` |
| 造 stale 文件 | `touch -t 202601010000 /tmp/test/old.v.gz` |
| 刷新源文件 | `touch ~/a0.outgoing/CPU/*` |

> **关于 `touch`**：`streaming_copy` 用 `os.utime` 保留了源文件的 mtime。如果 `a0.outgoing` 里的文件创建时间很久，复制到 raw 后的文件 mtime 也会是旧的，file_freshness 会正确阻断。每次 submit 前 `touch` 一下源文件即可。

---

## 注意事项汇总

| 项目 | 说明 |
|------|------|
| 执行顺序 | gates 按配置列表顺序**串行**执行 |
| 短路 | 任一 gate FAIL，后续 gate **不再执行**，submit 整体失败 |
| 工作目录 | 子进程 cwd 是提交时的工作目录（非 raw 目录） |
| python 路径 | `python3` 开头的 command → runner 自动替换为 `sys.executable` |
| 外部脚本 | 非 python 开头 → 不做替换，需依赖 shebang 或系统 PATH |
| CSH 脚本 | 推荐 `#!/bin/csh -f` shebang + 绝对路径 |
| stdout | 子进程 stdout 被 runner 捕获，写入日志，不在终端显示 |
| stderr | gate FAIL 时前 200 字符写入 `EVENT_GATE_FAIL` |
| mtime | raw 文件的 mtime 来自 `streaming_copy` 的 `os.utime`（= 源文件 mtime） |
| 环境变量 | 子进程继承父进程环境，但 venv 不在 PATH 中 |
| 权限 | gate 以触发 submit 的用户身份运行 |
