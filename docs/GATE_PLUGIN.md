# DDM Gate 插件开发指南

DDM 的门禁（Gate）系统在 `submit` 流程中，文件从 `a0.outgoing/` 拷贝到 `raw/` 后、移入 `ready/` 前自动运行一系列检查。检查通过则继续，任一失败则阻断提交。

## 目录

- [快速开始：新增一个 Python 门禁](#快速开始新增一个-python-门禁)
- [接口规范](#接口规范)
  - [命令行参数](#命令行参数)
  - [返回值协议](#返回值协议)
  - [stdout / stderr](#stdout--stderr)
  - [超时](#超时)
- [对接外部脚本](#对接外部脚本)
  - [方式 A：直接调用 (exit code)](#方式-a直接调用-exit-code)
  - [方式 B：Flag 文件协议](#方式-bflag-文件协议)
  - [方式 C：LSF/Grid 集群提交](#方式-clsfgrid-集群提交)
- [配置参考](#配置参考)
- [调试技巧](#调试技巧)

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
        # 已有的 gate
        - name: verilog_syntax_check
          command: python3 -m ddm.gates.verilog_check
        - name: drc_baseline_check
          command: python3 -m ddm.gates.drc_check
        # 新增的 gate
        - name: power_integrity_check
          command: python3 -m ddm.gates.power_check
```

- `name`：在 submit 进度条和日志中显示的名字
- `command`：runner 会拆成参数列表，追加 `raw_dir module tag` 后执行

### Step 3 — 完成

无需重启服务。下次 `ddm submit` 时新 gate 自动生效。

---

## 接口规范

### 命令行参数

Runner 拼接的实际命令是：

```
<command> <raw_dir> <module> <tag>
```

例如：

```
python3 -m ddm.gates.verilog_check /nfs/ddm/repository/raw/PV_ITER/CPU CPU PV_ITER
```

| 参数 | 含义 | 示例 |
|------|------|------|
| `sys.argv[1]` / `$1` | raw 目录的绝对路径 | `/nfs/ddm/repository/raw/PV_ITER/CPU` |
| `sys.argv[2]` / `$2` | 模块名 | `CPU` |
| `sys.argv[3]` / `$3` | tag 名 | `PV_ITER` |

> **注意**：`sys.executable` 替换：如果 command 以 `python` 或 `python3` 开头，runner 会自动替换为运行 DDM 本身的 Python 解释器。这意味着你不需要 activate venv —— gate 始终使用和 DDM 相同的 Python。

### 返回值协议

Runner 支持三种判定规则，按优先级排列：

#### 1. Exit Code（默认、最简单）

```
exit 0  → PASS
exit ≠0 → FAIL
```

Python 脚本 `sys.exit(0)` 或正常结束 → PASS；`sys.exit(1)` 或其他非零值 → FAIL。Runner 会捕获 stdout/stderr 并用 return code 判定。

#### 2. stdout 末行匹配（精确控制）

如果你想更精确地控制判定（例如 exit code 始终为 0 但要根据输出内容判定），可以在脚本最后一行输出特殊标记：

```
__DDM_RESULT__: PASS  或  __DDM_RESULT__: FAIL
```

Runner 优先检查 stdout 最后一行是否包含此模式。这在你需要区分 "脚本本身成功但检查失败" 的场景时很有用。

#### 3. Flag 文件（跨进程 / 长时间任务，见下文）

### stdout / stderr

- **stdout**：自定义输出，runner 会记录到日志。建议输出检查摘要、文件数、关键指标。
- **stderr**：前 200 字符会在 gate 失败时写入 SQLite events 表（`EVENT_GATE_FAIL`）。

### 超时

默认每个 gate 超时 300 秒（5 分钟）。超过则判定 FAIL。可以在 runner 调用时配置：

```python
run_gates(gate_defs, raw_dir, module, tag, timeout=600)  # 10 分钟
```

---

## 对接外部脚本

### 方式 A：直接调用 (exit code)

适用于：**独立的本地脚本，可以同步跑完，用 exit code 表达结果**。

```yaml
gates:
  - name: custom_drc
    command: /eda/scripts/run_drc.csh
```

`run_drc.csh` 接收 3 个参数：

```csh
#!/bin/csh -f
set raw_dir = $1
set module  = $2
set tag     = $3

cd $raw_dir
# ... 执行 DRC 检查 ...
if ($status == 0) then
    exit 0   # PASS
else
    exit 1   # FAIL
endif
```

**注意事项**：
- Runner 会 `capture_output=True`，stdout/stderr 被捕获，不会直接打印到终端
- 脚本里如果有 `setenv`、`module load` 等环境操作，需要在脚本内部完成（子进程环境隔离）
- `sys.executable` 替换只对以 `python`/`python3` 开头的 command 生效，对 `/path/to/script.csh` 不替换

### 方式 B：Flag 文件协议

适用于：**长时间任务、提交到集群的任务、或者脚本和 gate 在不同环境运行**。

#### 协议规则

1. Gate 脚本在运行结束时，在 `raw_dir` 下生成一个 flag 文件
2. Flag 文件名约定：`.ddm_gate_<gate_name>` 或 `.ddm_gate_<gate_name>.json`
3. Runner 检查 flag 文件内容判定结果

#### 简单 Flag 文件

```
协议：
  .ddm_gate_<name>     → 内容为 "PASS" 或 "FAIL\n<reason>"
  .ddm_gate_<name>.json → {"status": "pass|fail", "reason": "...", "metrics": {...}}
```

**CSH 示例**：

```csh
#!/bin/csh -f
set raw_dir = $1
set module  = $2
set tag     = $3
set flag    = "$raw_dir/.ddm_gate_custom_drc"

# 提交到 LSF
set job_id = `bsub -q normal -J drc_${module} run_drc.csh $raw_dir $module $tag | awk '{print $2}' | sed 's/[<>]//g'`
echo "Job submitted: $job_id"
echo "PASS" > $flag               # 提交成功即 PASS（异步检查）
exit 0
```

**Python 封装（推荐）**：

```python
# ddm/gates/lsf_drc_check.py
"""提交 DRC 到 LSF，写 flag 文件表示提交成功。
实际检查结果由后续的 release 阶段验证。
"""
import sys, json, subprocess
from pathlib import Path

def main():
    raw_dir = Path(sys.argv[1])
    module  = sys.argv[2]
    tag     = sys.argv[3]
    flag_file = raw_dir / f".ddm_gate_lsf_drc"

    # 提交 bsub
    result = subprocess.run(
        ["bsub", "-q", "normal", "-J", f"drc_{module}",
         "/path/to/run_drc.csh", str(raw_dir), module, tag],
        capture_output=True, text=True, timeout=30
    )

    if result.returncode != 0:
        flag_file.write_text(f"FAIL\nLSF submit error: {result.stderr}")
        sys.exit(1)

    job_id = result.stdout.strip().split()[1].lstrip("<").rstrip(">")
    print(f"DRC submitted: LSF job {job_id}")
    flag_file.write_text(f"PASS\nLSF job: {job_id}")
    sys.exit(0)

if __name__ == "__main__":
    main()
```

#### JSON Flag 文件（含指标）

```python
flag = {
    "status": "pass",
    "reason": "DRC job bsub_12345 submitted",
    "metrics": {
        "lsf_job_id": "12345",
        "queue": "normal",
        "estimated_runtime": "2h"
    }
}
flag_file.write_text(json.dumps(flag, indent=2))
```

### 方式 C：LSF/Grid 集群提交

适用于：**检查本身需要大量计算资源，必须在集群上跑**。

#### 模式 1：提交即过（异步）

```yaml
gates:
  - name: drc_submit
    command: python3 -m ddm.gates.lsf_submit  # 只负责提交
  # submit 通过后再由人工或 cron 验证实际结果
```

LSF 任务提交成功 → gate PASS。实际 DRC 结果由后续的 **release gate** 或人工 review 验证。

#### 模式 2：等待结果（同步/半同步）

```python
# ddm/gates/lsf_wait_check.py
"""提交 LSF 任务并等待完成，超时则 FAIL。"""
import sys, subprocess, time
from pathlib import Path

def main():
    raw_dir = Path(sys.argv[1])
    module  = sys.argv[2]
    tag     = sys.argv[3]

    # 1. 提交任务
    proc = subprocess.run(
        ["bsub", "-q", "normal", "-J", f"drc_{module}",
         "/path/to/run_drc.csh", str(raw_dir), module, tag],
        capture_output=True, text=True
    )
    job_id = proc.stdout.strip().split()[1].lstrip("<").rstrip(">")
    print(f"LSF job submitted: {job_id}")

    # 2. 轮询状态（最多等 30 分钟）
    deadline = time.time() + 1800
    while time.time() < deadline:
        check = subprocess.run(
            ["bjobs", "-o", "stat", "-noheader", job_id],
            capture_output=True, text=True
        )
        status = check.stdout.strip()
        if status == "DONE":
            print("DRC job completed successfully")
            sys.exit(0)
        elif status in ("EXIT", "UNKWN"):
            print(f"DRC job failed with status: {status}")
            sys.exit(1)
        time.sleep(30)

    print(f"Timeout: DRC job {job_id} still running")
    sys.exit(1)

if __name__ == "__main__":
    main()
```

#### 通用 LSF 封装模板

```python
# ddm/gates/lsf_runner.py
"""
通用 LSF 门禁封装。
在 config.yaml 中通过环境变量或额外参数配置 LSF 选项。

用法:
  python3 -m ddm.gates.lsf_runner <raw_dir> <module> <tag> \
      --cmd "/eda/scripts/drc.sh" --queue normal --timeout 3600
"""
import sys, subprocess, time, argparse
from pathlib import Path

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("raw_dir")
    parser.add_argument("module")
    parser.add_argument("tag")
    parser.add_argument("--cmd", required=True)
    parser.add_argument("--queue", default="normal")
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--wait", action="store_true",
                        help="Wait for job completion")
    return parser.parse_known_args()

def main():
    args, extra = parse_args()
    raw_dir = Path(args.raw_dir)
    module  = args.module
    tag     = args.tag

    # 构建 bsub 命令
    bsub_cmd = [
        "bsub", "-q", args.queue,
        "-J", f"ddm_gate_{module}_{tag}",
        "-o", str(raw_dir / ".lsf_%J.log"),
    ] + extra + ["--"] + args.cmd.split() + [str(raw_dir), module, tag]

    proc = subprocess.run(bsub_cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"FAIL: bsub error\n{proc.stderr}")
        sys.exit(1)

    job_id = proc.stdout.strip().split()[1].lstrip("<").rstrip(">")
    print(f"LSF job {job_id} submitted to queue '{args.queue}'")

    if not args.wait:
        print("Async mode: job submitted, check .lsf_*.log for results")
        sys.exit(0)  # 提交成功即 PASS

    # 同步等待
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        check = subprocess.run(
            ["bjobs", "-o", "stat", "-noheader", job_id],
            capture_output=True, text=True
        )
        s = check.stdout.strip()
        if s == "DONE":
            print(f"LSF job {job_id} DONE")
            sys.exit(0)
        elif s in ("EXIT", "UNKWN"):
            print(f"FAIL: LSF job {job_id} status={s}")
            sys.exit(1)
        time.sleep(15)

    print(f"FAIL: LSF job {job_id} timeout ({args.timeout}s)")
    sys.exit(1)

if __name__ == "__main__":
    main()
```

配置：

```yaml
gates:
  - name: lsf_drc
    command: python3 -m ddm.gates.lsf_runner --cmd /eda/scripts/drc.sh --wait
```

---

## 配置参考

### config.yaml 完整示例

```yaml
defaults:
  tag:
    PV_ITER:
      file_patterns:
        - "{module}.v.gz"
        - "{module}.v.pg"
      gates:
        # ---- Python 模块 gate ----
        - name: verilog_syntax_check
          command: python3 -m ddm.gates.verilog_check

        # ---- 外部 Shell 脚本 ----
        - name: custom_drc
          command: /eda/scripts/run_drc.csh

        # ---- LSF 异步提交 ----
        - name: lsf_drc_submit
          command: python3 -m ddm.gates.lsf_runner --cmd /eda/scripts/drc.sh

        # ---- 带超时控制的 gate（在包裹脚本里实现） ----
        - name: long_running_check
          command: python3 -m ddm.gates.lsf_runner --cmd /eda/scripts/heavy.sh --wait --timeout 7200
```

### 注意事项

| 项目 | 说明 |
|------|------|
| 执行顺序 | gates 按配置列表顺序**串行**执行 |
| 短路 | 任一 gate FAIL 后，后续 gate **不再执行**，submit 整体失败 |
| 环境 | 子进程继承父进程的环境变量，但 `PATH` 不会包含 venv |
| 工作目录 | 子进程的 cwd 是 raw 目录（`raw/<tag>/<module>/`） |
| python 路径 | `python3` 开头的 command 自动替换为 `sys.executable` |
| 权限 | gate 以触发 submit 的用户的身份运行 |

---

## 调试技巧

### 1. 手动测试 gate

```bash
# 模拟 runner 调用
raw_dir="/nfs/ddm/repository/raw/PV_ITER/CPU"
python3 -m ddm.gates.verilog_check "$raw_dir" CPU PV_ITER
echo "exit code: $?"
```

### 2. 查看 gate 日志

```bash
# 日志在 <log_dir>/ddm_YYYY-MM-DD.log
grep "gate\|Gate" logs/ddm_2026-07-26.log
```

### 3. 查看 gate 事件

```bash
# SQLite 中记录了每次 gate 的执行结果
sqlite3 repository/ddm.db \
  "SELECT batch_uuid, event_type, message, created_at
   FROM events WHERE event_type LIKE '%gate%'
   ORDER BY created_at DESC LIMIT 10;"
```

### 4. 添加 verbose 输出

在 gate 脚本中，所有 print 到 stdout 的内容都会被 runner 捕获并记录到日志。可以放心加调试输出：

```python
print(f"DEBUG: raw_dir={raw_dir}")
print(f"DEBUG: files={list(raw_dir.rglob('*'))}")
```

这些输出不会显示在 submit 的进度条中，但会被写入 `ddm_*.log`。

### 5. 测试 LSF 集成

```bash
# 先确保 bsub 可用
which bsub

# 手动提交测试
bsub -q normal -J test_gate /eda/scripts/run_drc.csh /tmp/test CPU PV_ITER

# 检查作业状态
bjobs -u $USER
```

---

## 常见 gate 模式

| 检查类型 | 实现方式 | 超时建议 |
|----------|---------|----------|
| 语法检查 | Python 脚本直接扫描文件 | 60s |
| 完整性验证 | Python 脚本对比 hash/size | 120s |
| DRC/LVS | LSF 提交 + flag 文件 | 3600s (异步) |
| 功耗分析 | 外部工具 + exit code | 600s |
| 时序检查 | LSF 提交 + 轮询等待 | 7200s |
| 用户自定义 | 任意可执行文件 | 按需设置 |
