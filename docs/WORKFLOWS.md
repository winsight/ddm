# DDM Submit & Release 完整流程

## 1. 状态机总览

一次数据交付经历三个阶段，由 `submit` 和 `release` 两个命令驱动：

```mermaid
stateDiagram-v2
    [*] --> PENDING: ddm submit 开始
    PENDING --> SUBMITTED: copy + pre_check + gates 全部通过
    PENDING --> FAILED: 任意步骤失败
    SUBMITTED --> RELEASED: ddm release + post_check 通过
    SUBMITTED --> FAILED: release post_check 失败
    RELEASED --> FAILED: rm -rf 物理删除（下次 release 检测到）
```

```
PENDING ──(copy+pre_check+gate+atomic move)──▶ SUBMITTED ──(staging+post_check+commit)──▶ RELEASED
    │                                                   │                                      │
    └── (any failure) ──────────────────────▶ FAILED ◀──┘                                      │
                                                                                               │
    RELEASED ──(物理文件被删除)──▶ release_file_missing 事件（下次 release 审计发现）
```

---

## 2. 数据流转路径

```
a0.outgoing/{user}/{module}/         ← 工程师私有源目录
        │
        │ ddm submit
        │ streaming_copy (边拷贝边 BLAKE3)
        ▼
raw/{TAG}/{MODULE}/                  ← 临时暂存区
        │
        │ pre_check: size + BLAKE3 vs a0 源文件
        │ run_gates: subprocess 黑盒门禁
        │ os.replace() 原子移动
        ▼
ready/{TAG}/{MODULE}/                ← 就绪暂存区（临界区）
        │
        │ ddm release
        │ Pass 1: copy2 → staging
        │ Pass 1b: inherit from latest
        │ Pass 2: size diff vs 上一版本
        │ Pass 3: post_check (size + mtime + BLAKE3)
        │ os.rename() / merge_dirs()
        ▼
release/{TAG}/latest → VERSION/     ← 最终发布归档
```

```mermaid
flowchart LR
    A0["a0.outgoing/<br/>{user}/{module}/"] -->|"submit<br/>streaming_copy<br/>pre_check<br/>gates<br/>os.replace"| RAW
    RAW["raw/<br/>{TAG}/{MODULE}/"] --> READY
    READY["ready/<br/>{TAG}/{MODULE}/"] -->|"release<br/>staging → commit<br/>post_check"| REL["release/{TAG}/<br/>latest → VERSION/<br/>verilog/, gds/, pg/"]

    style A0 fill:#888,stroke:#333
    style REL fill:#4a9,stroke:#333,color:#fff
```

---

## 3. Submit 流程

### 3.1 命令参数

```
ddm submit -m <MODULE> -t <TAG> [-s <SUMMARY>] [-c <CONFIG>]
```

| 参数 | 必需 | 说明 |
|------|------|------|
| `-m / --module` | 是 | 模块名（CPU, DDR, ...） |
| `-t / --tag` | 是 | 标签（PV_ITER, LVS_PASS, ...） |
| `-s / --summary` | 否 | 提交备注 |
| `-c / --config` | 否 | 指定配置文件路径 |

### 3.2 Submit 流水线

```mermaid
flowchart TD
    START(["ddm submit -m CPU -t PV_ITER"]) --> V

    subgraph VALIDATE["阶段 0: 快速校验（不涉及 I/O）"]
        V1["Tag 合法性检查<br/>VALID_TAGS 白名单"] --> V2
        V2["TagConfig 存在性检查<br/>file_patterns 不能为空"] --> V3
        V3["模块 Owner 权限检查<br/>config.modules.CPU.owners<br/>+ admins 白名单"]
    end

    V --> VALIDATE
    VALIDATE --> LOCK_CHECK

    subgraph LOCK_CHECK["阶段 1: 锁快速失败"]
        LC1{"ready/.lock_global_release<br/>存在?"}
        LC2{"raw/.lock_CPU_PV_ITER<br/>O_CREAT|O_EXCL"}
    end

    LC1 -->|"✗ 存在"| REJ1["拒绝: 系统正在 Release"]
    LC1 -->|"✓ 不存在"| LC2
    LC2 -->|"FileExistsError"| REJ2["拒绝: 模块 CPU/PV_ITER 正在提交"]
    LC2 -->|"✓ 获取成功"| PIPELINE

    subgraph PIPELINE["阶段 2: 核心流水线"]
        direction TB
        D1["磁盘空间检查<br/>raw/ 挂载点可用 > 总大小×1.2"] --> D2
        D2["find_source_files<br/>a0.outgoing/{user}/CPU/<br/>按 file_patterns 匹配"] --> D3
        D3{"匹配到文件?"}
        D3 -->|"✗ 无匹配"| REJ3["拒绝: No files matched"]
        D3 -->|"✓"| D4
        D4["streaming_copy<br/>a0 → raw/CPU/<br/>边拷贝边 BLAKE3<br/>保留源 mtime"] --> D5
        D5["pre_check<br/>raw vs a0<br/>size + BLAKE3"] --> D6
        D6{"pre_check 通过?"}
        D6 -->|"✗"| REJ4["拒绝: metadata mismatch"]
        D6 -->|"✓"| D7
        D7["run_gates<br/>subprocess 黑盒门禁<br/>verilog_check / drc_check / ..."] --> D8
        D8{"全部 gate 通过?"}
        D8 -->|"✗"| REJ5["拒绝: Gate failed"]
        D8 -->|"✓"| D9
        D9["os.replace<br/>raw/CPU/ → ready/CPU/<br/>同文件系统原子 rename"] --> D10
        D10["chmod 664<br/>设置组可读"] --> D11
        D11["post_check (1st)<br/>ready vs a0<br/>size + BLAKE3"]
    end

    PIPELINE --> POST
    POST{"post_check 通过?"}
    POST -->|"✓"| DONE
    POST -->|"✗"| REJ6["拒绝: files corrupted after gate"]

    DONE["状态 → SUBMITTED<br/>释放 .lock_CPU_PV_ITER"]

    style START fill:#4a9,stroke:#333,color:#fff
    style DONE fill:#4a9,stroke:#333,color:#fff
    style REJ1 fill:#e44,stroke:#333,color:#fff
    style REJ2 fill:#e44,stroke:#333,color:#fff
    style REJ3 fill:#e44,stroke:#333,color:#fff
    style REJ4 fill:#e44,stroke:#333,color:#fff
    style REJ5 fill:#e44,stroke:#333,color:#fff
    style REJ6 fill:#e44,stroke:#333,color:#fff
```

### 3.3 快速失败 (Fast-Fail) 顺序

Submit 把最轻量的检查放在最前面，避免做无用功：

```
1. Tag 白名单检查        ← 纯内存，零 I/O
2. TagConfig 存在性        ← 纯内存
3. 模块 Owner 权限         ← 读 config 内存中的列表 + admins 白名单
4. 全局锁检查              ← 1 次 stat()
5. 模块锁获取              ← 1 次 O_CREAT|O_EXCL (原子)
6. 磁盘空间检查            ← 1 次 statvfs()
   --- 以上失败都不写入 SQLite ---
7. find_source_files      ← 第 1 次目录扫描
8. create_batch (SQLite)  ← 此时才开始写数据库
```

### 3.4 文件发现 (find_source_files)

```python
# outgoing_root 模板展开
"./a0.outgoing/{user}/{module}"  →  "./a0.outgoing/wangshuai/CPU"

# file_patterns 模板展开（只用 {module} 命名文件）
"{module}.v.gz"  →  "CPU.v.gz"
"{module}.hier.gds"  →  "CPU.hier.gds"

# 在展开后的目录下 fnmatch 匹配
a0.outgoing/wangshuai/CPU/
    CPU.v.gz          ✓ 匹配 "CPU.v.gz"
    CPU.hier.gds      ✓ 匹配 "CPU.hier.gds"
    CPU.v.pg          ✓ 匹配 "CPU.v.pg"
    DDR.v.gz          ✗ 不匹配
```

### 3.5 门禁系统 (Gates)

门禁是配置驱动的黑盒 subprocess 调用，DDM 不关心内部逻辑：

```yaml
defaults:
  tag:
    PV_ITER:
      gates:
        - name: verilog_syntax_check
          command: python3 -m ddm.gates.verilog_check
        - name: drc_check
          command: python3 -m ddm.gates.drc_check
```

```mermaid
flowchart TD
    INPUT["raw/{TAG}/{MODULE}/<br/>目录路径"] --> G1
    subgraph RUNNER["gate runner (subprocess 调用)"]
        G1["Gate 1: verilog_check"] --> G2["Gate 2: drc_check"] --> GN["Gate N: ..."]
    end
    RUNNER --> RESULT

    RESULT{全部 passed?}
    RESULT -->|"✓"| OK["写入 event: gate_pass<br/>继续流水线"]
    RESULT -->|"✗"| FAIL["写入 event: gate_fail<br/>batch → FAILED<br/>释放模块锁"]

    style OK fill:#4a9,stroke:#333,color:#fff
    style FAIL fill:#e44,stroke:#333,color:#fff
```

**约定：**
- `exit 0` → 通过，stdout 作为详情
- `exit ≠ 0` → 失败，stderr 作为失败原因写入 event
- 串行执行，任何一个失败即停止
- 无 gate 配置的 tag 跳过此阶段

### 3.6 ready/ 目录结构

```
ready/
├── .lock_global_release          ← 全局发布锁
├── PV_ITER/
│   ├── CPU/
│   │   ├── CPU.v.gz
│   │   ├── CPU.hier.gds
│   │   └── CPU.v.pg
│   └── DDR/
│       └── ...
├── LVS_PASS/
│   └── ...
└── BASE_CLEAN/
    └── ...
```

每个文件能回溯到它的 batch：

```
SQLite files 表:
  source_path  → a0.outgoing/wangshuai/CPU/CPU.v.gz
  raw_path     → raw/PV_ITER/CPU/CPU.v.gz       (submit 中途)
  ready_path   → ready/PV_ITER/CPU/CPU.v.gz      (submit 完成后)
  release_path → release/PV_ITER/V1_20260717/verilog/CPU.v.gz  (release 后)
```

---

## 4. Release 流程

### 4.1 命令参数

```
ddm release -t <TAG> (-m <MODULE> | -A) [-v <VERSION>] [--inherit]
```

| 参数 | 必需 | 说明 |
|------|------|------|
| `-t / --tag` | 是 | 发布标签 |
| `-m / --module` | 与 -A 二选一 | 发布单个模块，其余模块自动从 latest 继承 |
| `-A / --all` | 与 -m 二选一 | 发布该 tag 配置的**全部**模块，缺一不可 |
| `-v / --version` | 否 | 版本标签，默认取当天日期（YYYYMMDD） |
| `--inherit` | 否 | 配合 -A 使用，允许从 latest 继承未提交模块 |

### 4.2 版本命名规则

```
-v V1        →  V1_20260717   （label + 日期后缀）
-v ""        →  20260717       （不传则纯日期）
```

每次 release 在 `release/<TAG>/` 下创建一个版本目录，`latest` 软链接指向最新版本。

---

### 4.3 Release 流水线

```mermaid
flowchart TD
    START(["ddm release 开始"]) --> A

    A["1. 权限检查<br/>release_users + admins"] --> B
    B["2. 查询 SUBMITTED batches<br/>(SQLite)"] --> C
    C["3. 反脑裂检查<br/>ready_path 物理文件必须存在"] --> D
    D["4. 完整性审计<br/>扫描历史 RELEASED 版本<br/>检测 rm -rf 物理删除"] --> E
    E["5. 检查模块锁<br/>raw/.lock_*_{TAG}<br/>有则拒绝（模块正在提交）"] --> F
    F["6. 获取全局锁<br/>ready/.lock_global_release<br/>阻止所有 submit"] --> G

    subgraph STAGING["三阶段 Staging（原子事务）"]
        G["Pass 1: copy2<br/>ready/ → .staging_xxx/<br/>保留 mtime"] --> H
        H["Pass 1b: 继承<br/>latest → .staging_xxx/<br/>copytree 未变更模块"] --> I
        I["Pass 2: size diff<br/>staging vs 上一版本同名文件<br/>±30% 警告 / ±50% 告警"] --> J
        J["Pass 3: post_check<br/>ready vs staging<br/>size + mtime + BLAKE3"]
    end

    J --> K{全部通过?}

    K -->|✓ 是| L["7. commit<br/>新版本: os.rename(staging, VERSION)<br/>追加: merge_dirs(staging, VERSION)"]
    K -->|✗ 否| M["清理 staging/<br/>返回 FAILED"]

    L --> N["8. 更新 latest 软链接"]
    N --> O["9. 清理 ready/{TAG}/{MODULE}/"]
    O --> P["10. 释放全局锁"]
    P --> END(["Release 完成"])

    style START fill:#4a9,stroke:#333,color:#fff
    style END fill:#4a9,stroke:#333,color:#fff
    style M fill:#e44,stroke:#333,color:#fff
    style K fill:#fa0,stroke:#333
```

---

### 4.4 三种发布模式

#### 4.4.1 单模块发布：`-m <MODULE>`

只发布指定模块的一个 SUBMITTED batch，**其他模块自动从 `latest` 继承**。

```mermaid
flowchart LR
    subgraph BEFORE["release/PV_ITER/ (发布前)"]
        direction TB
        LATEST1["latest → V1_20260710"]
        V1["V1_20260710/<br/>verilog/CPU.v.gz<br/>gds/CPU.hier.gds<br/>pg/CPU.v.pg<br/>verilog/DDR.v.gz<br/>gds/DDR.hier.gds<br/>pg/DDR.v.pg"]
    end

    subgraph AFTER["release/PV_ITER/ (发布后)"]
        direction TB
        LATEST2["latest → V2_20260717"]
        V1B["V1_20260710/<br/>verilog/CPU.v.gz gds/CPU.hier.gds pg/CPU.v.pg<br/>verilog/DDR.v.gz gds/DDR.hier.gds pg/DDR.v.pg"]
        V2["V2_20260717/<br/>verilog/CPU.v.gz ★<br/>gds/CPU.hier.gds ★<br/>pg/CPU.v.pg ★<br/>verilog/DDR.v.gz (继承)<br/>gds/DDR.hier.gds (继承)<br/>pg/DDR.v.pg (继承)"]
    end

    BEFORE -->|"ddm release -t PV_ITER -m CPU -v V2"| AFTER

    style V2 fill:#26a,stroke:#333,color:#fff
```

**逻辑：**
1. 查询 `batches WHERE tag=PV_ITER AND module=CPU AND status=SUBMITTED`
2. 只拷贝 CPU 的 ready 文件到 staging
3. 扫描 `latest`（V1_20260710）中除 CPU 之外的模块 → DDR 完整继承
4. post_check → commit → 更新 latest → 清理 ready/CPU/

#### 4.4.2 全量发布：`-A`

发布该 tag 配置中 `modules` 列表里的**所有模块**，每个模块都必须有 SUBMITTED 数据。

```mermaid
flowchart TD
    CMD["ddm release -t PV_ITER -A -v V2"] --> CHECK
    CHECK{"configured_modules<br/>minus<br/>submitted_modules<br/>为空?"}
    CHECK -->|"✓ 是"| DO["全量发布<br/>所有模块从 ready 取"]
    CHECK -->|"✗ 缺 DDR"| FAIL["✗ 拒绝<br/>Missing: ['DDR']<br/>Use --inherit"]

    style FAIL fill:#e44,stroke:#333,color:#fff
    style DO fill:#4a9,stroke:#333,color:#fff
```

```
release/PV_ITER/
├── latest → V2_20260717
├── V1_20260710/
│   ├── verilog/CPU.v.gz, DDR.v.gz
│   ├── gds/CPU.hier.gds, DDR.hier.gds
│   └── pg/CPU.v.pg, DDR.v.pg
└── V2_20260717/
    ├── verilog/CPU.v.gz, DDR.v.gz  ← ★ 新发布
    ├── gds/CPU.hier.gds, DDR.hier.gds
    └── pg/CPU.v.pg, DDR.v.pg
```

#### 4.4.3 全量发布 + 继承：`-A --inherit`

全量发布，允许某些模块没有新数据 —— 自动从 `latest` 继承。

```mermaid
flowchart LR
    subgraph BEFORE2["release/PV_ITER/ (发布前)"]
        direction TB
        L1["latest → V1_20260710"]
        OLD["V1_20260710/<br/>verilog/CPU.v.gz, DDR.v.gz<br/>gds/CPU.hier.gds, DDR.hier.gds<br/>pg/CPU.v.pg, DDR.v.pg"]
    end

    subgraph AFTER2["release/PV_ITER/ (发布后)"]
        direction TB
        L2["latest → V2_20260717"]
        OLD2["V1_20260710/<br/>verilog/CPU.v.gz, DDR.v.gz<br/>gds/CPU.hier.gds, DDR.hier.gds"]
        NEW["V2_20260717/<br/>verilog/CPU.v.gz ★新发布<br/>gds/CPU.hier.gds ★新发布<br/>verilog/DDR.v.gz (继承)<br/>gds/DDR.hier.gds (继承)"]
    end

    BEFORE2 -->|"ddm release -t PV_ITER -A -v V2 --inherit<br/>(仅 CPU 有 SUBMITTED)"| AFTER2

    style NEW fill:#26a,stroke:#333,color:#fff
```

---

### 4.5 版本继承机制

#### 继承触发条件

| 模式 | 何时继承 |
|------|----------|
| `-m MODULE` | 始终继承（除了指定模块外的所有模块） |
| `-A` | 不继承，缺少任何模块直接报错 |
| `-A --inherit` | 只继承配置中有但本次未 SUBMITTED 的模块 |

#### 继承逻辑

```mermaid
flowchart TD
    START2["staging_dir 已构建<br/>modules_in_this_release = {CPU}"] --> Q

    Q{"latest 软链接存在<br/>且不是当前版本?"}
    Q -->|"✗ 否"| SKIP["跳过继承"]
    Q -->|"✓ 是"| LOOP

    subgraph LOOP["遍历 latest 的每个模块目录"]
        direction TB
        R{"mod ∈<br/>modules_in_this_release?"}
        R -->|"✓ 是 (CPU)"| SKIP_MOD["跳过<br/>（本 release 有新版本）"]
        R -->|"✗ 否 (DDR, GPU...)"| COPY["copytree 整个模块目录 → staging<br/>chmod 664"]
    end

    LOOP --> DONE["继承完成"]

    style COPY fill:#26a,stroke:#333,color:#fff
    style SKIP_MOD fill:#888,stroke:#333
    style SKIP fill:#888,stroke:#333
```

**关键规则：按文件类型分组**
- 文件通过 `file_groups` 配置映射到 verilog/gds/pg 等子目录，各模块文件在 group 内混合存放
- 文件名自带模块前缀（`CPU.v.gz`），无需模块层级目录区分
- 未匹配 file_groups 的文件直接放在版本根目录

---

### 4.6 三阶段 Staging 提交

为保障原子性，release 先将所有文件写入临时 staging 目录，全部校验通过后再一次性提交：

```mermaid
flowchart LR
    READY["ready/{TAG}/{MODULE}/"] -->|"Pass 1: shutil.copy2<br/>保留 mtime"| STG[".staging_xxx/{MODULE}/"]
    LATEST["latest 其他模块"] -->|"Pass 1b: shutil.copytree<br/>模块粒度继承"| STG
    STG -->|"Pass 2: 与上一版本同名文件<br/>比较 size diff"| DIFF{±30%?}
    DIFF -->|< 30%| OK1["正常"]
    DIFF -->|30%~50%| WARN["size_change 事件"]
    DIFF -->|> 50%| ALERT["size_anomaly 告警"]
    STG -->|"Pass 3: size + mtime + BLAKE3<br/>ready vs staging"| CHECK2{全部通过?}
    CHECK2 -->|✓| COMMIT["os.rename(staging, VERSION)<br/>原子提交"]
    CHECK2 -->|✗| CLEAN["清理 staging<br/>返回 FAILED"]

    style COMMIT fill:#4a9,stroke:#333,color:#fff
    style CLEAN fill:#e44,stroke:#333,color:#fff
    style ALERT fill:#e44,stroke:#333,color:#fff
    style WARN fill:#fa0,stroke:#333
```

#### post_check 三要素

```mermaid
flowchart TD
    SRC["ready_path<br/>（就绪区源文件）"] --> SIZE{"size 相同?"}
    SRC --> MTIME{"mtime 一致?"}
    SRC --> HASH{"BLAKE3 hash 相同?"}

    SIZE -->|✓| P1["pass"]
    SIZE -->|✗| F1["fail"]
    MTIME -->|✓| P2["pass"]
    MTIME -->|✗| F2["fail"]
    HASH -->|✓| P3["pass"]
    HASH -->|✗| F3["fail"]

    P1 & P2 & P3 --> OK["全部通过 ✓"]
    F1 --> BAD["校验失败 ✗"]
    F2 --> BAD
    F3 --> BAD

    style OK fill:#4a9,stroke:#333,color:#fff
    style BAD fill:#e44,stroke:#333,color:#fff
```

---

### 4.7 发布到已有版本（追加/覆盖）

**`-m MODULE`**：始终允许追加到已有版本（增量更新，添加一个模块的数据）。

**`-A`**：全量发布到已有版本是破坏性操作（覆盖所有模块），默认**拒绝**。

```mermaid
flowchart TD
    VCHECK{"release_version_dir<br/>已存在?"}
    VCHECK -->|"✗ 新版本"| NEW_VER["staging/ ──os.rename()──▶ VERSION/<br/>原子创建整个版本目录"]
    VCHECK -->|"✓ 已存在"| MODE{"-m MODULE 还是 -A?"}
    MODE -->|"-m MODULE"| MERGE["_merge_dirs() → VERSION/<br/>追加此模块，保留其他模块"]
    MODE -->|"-A"| BLOCK{"--force?"}
    BLOCK -->|"✗ 否"| REJECT["✗ 拒绝<br/>版本 X 已存在。全量发布 (-A)<br/>覆盖已有版本属破坏性操作。<br/>如需覆盖请使用 --force。"]
    BLOCK -->|"✓ 是"| OVERWRITE["_merge_dirs() → VERSION/<br/>覆盖所有模块"]

    subgraph MERGE_DETAIL["_merge_dirs 详细行为"]
        direction TB
        M1["同名文件 → shutil.copy2 覆盖"]
        M2["新文件 → 直接复制"]
        M3["旧文件 → 保留不动"]
    end

    style REJECT fill:#e44,stroke:#333,color:#fff
```
    end

    style NEW_VER fill:#26a,stroke:#333,color:#fff
    style MERGE fill:#a4a,stroke:#333,color:#fff
```

---

### 4.8 完整性审计

每次 release 都会扫描**历史所有 RELEASED 状态**的 batch，检测物理文件是否被 `rm -rf` 删除：

```mermaid
flowchart TD
    SCAN["遍历 SQLite<br/>batches WHERE status=RELEASED"] --> CHECK
    CHECK{"release_path<br/>物理文件存在?"}
    CHECK -->|"✓"| NEXT["继续下一个"]
    CHECK -->|"✗"| RECORD["记录 event: release_file_missing<br/>写入 integrity_warnings"]
    RECORD --> NEXT
    NEXT --> DONE["审计完成<br/>warnings 随结果返回"]

    style RECORD fill:#fa0,stroke:#333
```

---

## 5. 锁机制

锁机制同时服务于 submit 和 release，是整个系统的并发控制基础。

### 5.1 原子锁实现

锁的核心实现在 `ddm/services.py`：

```python
def _acquire_lock(lock_path: Path, description: str) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            f.write(f"pid={os.getpid()}\ntime={time.time()}\n")
    except FileExistsError:
        raise LockError(f"... lock exists: {lock_path}")
```

**`O_CREAT | O_EXCL` 是 POSIX 内核级原子操作**——不是"先检查再创建"（那才有 TOCTOU 竞态），而是创建和存在性检查作为一个不可分割的系统调用完成。内核保证：无论多少进程同时调用，**恰好一个进程创建成功**。

### 5.2 并发 Submit 同一模块不会冲突

两个用户同时 `ddm submit -m CPU -t PV_ITER`：

```mermaid
sequenceDiagram
    participant A as 进程 A (wangshuai)
    participant K as 内核 VFS
    participant B as 进程 B (lisi)
    participant F as raw/.lock_CPU_PV_ITER

    Note over A,B: 时间线：两个进程几乎同时到达

    A->>K: os.open("raw/.lock_CPU_PV_ITER", O_CREAT|O_EXCL)
    B->>K: os.open("raw/.lock_CPU_PV_ITER", O_CREAT|O_EXCL)

    Note over K: 内核串行化这两个调用<br/>第一个创建文件，第二个返回 EEXIST

    K-->>A: fd ✅ 文件创建成功
    K-->>B: FileExistsError ❌

    A->>F: 写入 pid=12345
    A->>A: 执行 submit 流水线...
    A->>F: os.unlink() 释放锁

    B->>B: raise LockError<br/>"模块 CPU/PV_ITER 正在提交"
```

无论两个进程间隔多近（同一纳秒），内核 VFS 层的 inode 创建是串行化的，**不存在"两个进程同时创建同一个锁文件"的情况**。

### 5.3 不同模块之间不互斥

```mermaid
sequenceDiagram
    participant A as 进程 A: submit -m CPU -t PV_ITER
    participant B as 进程 B: submit -m DDR -t PV_ITER

    A->>K: os.open("raw/.lock_CPU_PV_ITER", O_CREAT|O_EXCL) → ✅
    B->>K: os.open("raw/.lock_DDR_PV_ITER", O_CREAT|O_EXCL) → ✅

    Note over A,B: 不同锁文件，互不影响，并发执行

    A->>A: CPU submit 流水线...
    B->>B: DDR submit 流水线...
```

### 5.4 两层锁交互

```mermaid
flowchart TD
    subgraph RELEASE_PROC["Release 进程"]
        R1["检查 raw/.lock_*_{TAG}<br/>有 → 拒绝（有模块正在提交）"]
        R2["获取 ready/.lock_global_release<br/>O_CREAT|O_EXCL 原子锁"]
        R3["执行发布..."]
        R4["释放 .lock_global_release"]
        R1 --> R2 --> R3 --> R4
    end

    subgraph SUBMIT_PROC["Submit 进程（被阻断）"]
        S1["检查 ready/.lock_global_release<br/>存在 → ✗ 拒绝"]
        S2["获取 raw/.lock_{MODULE}_{TAG}<br/>O_CREAT|O_EXCL"]
    end

    R2 -.->|"阻塞"| S1

    style R2 fill:#26a,stroke:#333,color:#fff
    style S1 fill:#e44,stroke:#333,color:#fff
```

### 5.5 锁互斥矩阵

| | Submit CPU/PV | Submit DDR/PV | Release PV |
|---|---|---|---|
| 无锁 | ✓ | ✓ | ✓ |
| `.lock_global_release` 被持有 | ✗ | ✗ | ✗ |
| `.lock_CPU_PV_ITER` 被持有 | ✗ | ✓ | ✗ |
| `.lock_DDR_PV_ITER` 被持有 | ✓ | ✗ | ✗ |

### 5.6 锁文件位置

```
repository/
├── raw/
│   ├── .lock_CPU_PV_ITER          ← 模块级锁（submit 持有）
│   ├── .lock_DDR_PV_ITER
│   └── .lock_CPU_BASE_CLEAN
└── ready/
    └── .lock_global_release       ← 全局锁（release 持有）
```

**关键设计决策：**
- 锁文件与数据在同一文件系统 → `O_CREAT|O_EXCL` 语义正确（NFSv3 有风险，建议 NFSv4 或本地盘）
- 锁文件不是 0 字节 → 内含 pid + 时间戳，方便人工排查残留锁
- `finally` 块保证异常路径也释放锁 → 不会死锁

---

## 6. 用户决策树

```mermaid
flowchart TD
    Q1["需要发布什么?"]

    Q1 -->|"只发布一个模块"| M1["ddm release -t TAG -m MODULE<br/>其他模块自动继承 latest"]
    Q1 -->|"发布所有模块"| Q2{"每个模块都有<br/>新的 SUBMITTED 数据?"}

    Q2 -->|"✓ 都有"| M2["ddm release -t TAG -A"]
    Q2 -->|"✗ 部分没有"| M3["ddm release -t TAG -A --inherit<br/>缺失模块从 latest 继承"]

    style M1 fill:#26a,stroke:#333,color:#fff
    style M2 fill:#4a9,stroke:#333,color:#fff
    style M3 fill:#a4a,stroke:#333,color:#fff
```

---

## 7. 完整生命周期示例

```mermaid
flowchart TD
    subgraph STEP0["初始状态"]
        E0["release/PV_ITER/ (空)"]
    end

    subgraph STEP1["第 1 次：单模块 CPU"]
        E1["ddm submit -m CPU -t PV_ITER ✓<br/>ddm submit -m DDR -t PV_ITER ✓<br/>ddm release -t PV_ITER -m CPU -v V1"]
        R1["V1_20260717/<br/>verilog/CPU.v.gz, gds/CPU.hier.gds, pg/CPU.v.pg<br/>（无 DDR，latest 尚不存在）"]
    end

    subgraph STEP2["第 2 次：单模块 DDR"]
        E2["ddm release -t PV_ITER -m DDR -v V2"]
        R2["V2_20260717/<br/>verilog/CPU.v.gz (继承) gds/CPU.hier.gds (继承)<br/>verilog/DDR.v.gz ★ gds/DDR.hier.gds ★"]
    end

    subgraph STEP3["第 3 次：全量发布"]
        E3["ddm submit -m CPU -t PV_ITER ✓<br/>ddm submit -m DDR -t PV_ITER ✓<br/>ddm release -t PV_ITER -A -v V3"]
        R3["V3_20260717/<br/>verilog/CPU.v.gz ★, DDR.v.gz ★<br/>gds/CPU.hier.gds ★, DDR.hier.gds ★"]
    end

    STEP0 --> STEP1 --> STEP2 --> STEP3

    subgraph FINAL["最终目录结构"]
        TREE["release/PV_ITER/<br/>├── latest → V3_20260717<br/>├── V1_20260717/verilog,gds,pg/<br/>├── V2_20260717/verilog,gds,pg/<br/>└── V3_20260717/verilog,gds,pg/"]
    end

    STEP3 --> FINAL

    style R1 fill:#26a,stroke:#333,color:#fff
    style R2 fill:#a4a,stroke:#333,color:#fff
    style R3 fill:#4a9,stroke:#333,color:#fff
```
