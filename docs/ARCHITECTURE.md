# DDM 架构设计文档

## 1. 系统概述

DDM（Data Delivery Manager）是一套 EDA 后端 PV/PI 数据交付流程管理系统，用于管理芯片制造中的工艺验证（PV）和工艺改进（PI）数据流。系统以 CLI 命令行工具形式运行，驱动数据从工程师私有区经过门禁校验、完整性检查，最终发布到共享归档目录。

### 核心设计原则

| 原则 | 说明 |
|------|------|
| 三层解耦 | 交互层（Click+Rich）、业务逻辑层（锁/状态机/校验）、持久层（SQLite）严格分离 |
| 配置驱动 | Tag 规则、目录路由、门禁编排全部由 YAML 控制，新增流程零代码改动 |
| 防御性编程 | 任何 I/O 之前先完成锁检查、磁盘探针、权限校验，不满足则 Fast-Fail |
| 全链路可观测 | loguru 日志 + SQLite events 表记录每一次状态流转和异常 |

---

## 2. 系统分层

```
┌──────────────────────────────────────────┐
│  CLI 交互层 (cli.py)                      │
│  Click 命令路由 + Rich 进度条/表格渲染      │
├──────────────────────────────────────────┤
│  业务逻辑层 (services.py)                  │
│  submit / release / 锁控制 / 状态机 / 校验  │
├──────────────────────────────────────────┤
│  持久层 (storage.py)                       │
│  SQLite CRUD: batches / files / events    │
├──────────────────────────────────────────┤
│  配置层 (config.py)                        │
│  YAML 解析 + Pydantic 校验                 │
├──────────────────────────────────────────┤
│  门禁层 (gates/runner.py + *_check.py)     │
│  Subprocess 黑盒调用，由配置驱动             │
└──────────────────────────────────────────┘
```

### 各层职责

**cli.py** — 纯交互，不包含任何业务逻辑：
- Click 参数解析与路由
- Rich `Progress` 进度条渲染（单条贯穿全流程）
- Rich `Table` 表格输出
- loguru 控制：submit 期间静默终端日志，仅写文件

**services.py** — 所有核心逻辑：
- `submit()`: 完整的 PENDING → SUBMITTED 流水线
- `release()`: 两阶段提交的发布流程
- `_acquire_lock()` / `_release_lock()`: 原子文件锁
- `streaming_copy()`: 流式拷贝 + BLAKE3 校验
- `compare_metadata()`: 元数据比对（size / mtime / BLAKE3）
- `find_source_files()`: 在 a0.outgoing/{user}/{module} 中按模式匹配文件

**storage.py** — 纯 SQL，无业务逻辑：
- 三表 DDL：`batches` / `files` / `events`
- CRUD 方法，每个方法独立事务
- `_row_to_dict()` 统一 sqlite3.Row 转换

**config.py** — 配置加载：
- `AppConfig` Pydantic 模型校验
- `TagConfig` 包含 `file_patterns` / `gates` / `release_users`
- `Config` 门面提供便捷访问方法

---

## 3. 数据库设计

### 3.1 batches 表

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增主键 |
| batch_uuid | TEXT UNIQUE | UUID v4，批次血缘标识 |
| module | TEXT | 模块名（CPU, DDR, ...） |
| tag | TEXT | 数据标签（PV_ITER, PI_ITER, ...） |
| username | TEXT | 提交用户（从进程 USER 环境变量获取） |
| version | TEXT | 发布版本号（release 时写入） |
| summary | TEXT | 提交备注 |
| status | TEXT | PENDING / SUBMITTED / RELEASED / FAILED |
| created_at | REAL | Unix 时间戳 |
| updated_at | REAL | Unix 时间戳 |

### 3.2 files 表

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增主键 |
| batch_uuid | TEXT FK | 关联 batches |
| source_path | TEXT | a0.outgoing 源文件绝对路径 |
| raw_path | TEXT | raw/ 暂存路径 |
| ready_path | TEXT | ready/ 就绪路径 |
| release_path | TEXT | release/ 发布路径 |
| file_size | INTEGER | 拷贝后实际大小 |
| source_size | INTEGER | 源文件原始大小 |
| source_mtime | REAL | 源文件修改时间戳 |
| blake3_hash | TEXT | BLAKE3 哈希 |
| status | TEXT | PENDING / SUBMITTED / RELEASED / FAILED |
| created_at | REAL | Unix 时间戳 |

### 3.3 events 表（审计日志）

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增主键 |
| batch_uuid | TEXT | 关联 batches |
| event_type | TEXT | 事件类型枚举 |
| message | TEXT | 事件详情 |
| created_at | REAL | Unix 时间戳 |

### 3.4 事件类型枚举

```
submit_start  copy_done   pre_check_ok   pre_check_fail
gate_pass     gate_fail   delivered      submitted
release_start release_done  post_check_ok  post_check_fail
release_file_missing  size_anomaly  size_change
lock_blocked  disk_full  failed
```

---

## 4. 文件流转状态机

```
                    ┌──────────────────────────┐
                    │  a0.outgoing/{user}/     │  按 {user}/{module} 分目录
                    │  {module}/               │  文件由 {module}.ext 命名
                    │  (只读)                  │
                    └──────────┬───────────────┘
                           │ submit 命令
                           │ find_source_files() 按 file_patterns 匹配
                           ▼
                    ┌─────────────┐
                    │    raw/      │  临时暂存区
                    │  <TAG>/      │  streaming_copy (边拷贝边 BLAKE3)
                    │  <MODULE>/   │  .lock_{MODULE}_{TAG} 模块锁
                    └──────┬──────┘
                           │ pre_check: size + BLAKE3 vs a0
                           │ run_gates(): subprocess 门禁
                           │ os.replace() 原子移动
                           │ chmod 664
                           ▼
                    ┌─────────────┐
                    │   ready/     │  就绪暂存区（临界区）
                    │  <TAG>/      │  .lock_global_release 全局锁
                    │  <MODULE>/   │  post_check 1: size + BLAKE3 + mtime vs a0
                    └──────┬──────┘
                           │ release 命令
                           │ 完整性扫描（检测 rm -rf 缺失）
                           │ 授权检查（release_users）
                           │ 版本检查（不存在则创建，已存在则追加）
                           │ 从 latest 继承未变更模块（累积版本）
                           │ 三阶段 staging
                           │  ┌─ Pass 1: copy2 → .staging/（保留 mtime）
                           │  ├─ Pass 1b: 继承 latest 中未更新模块
                           │  ├─ Pass 2: size diff vs 上一版本（±30% 警告）
                           │  ├─ Pass 3: post_check 2 (size + mtime + BLAKE3)
                           │  └─ commit: os.rename（新版本）或 merge（追加）
                           ▼
                    ┌─────────────┐
                    │  release/    │  最终发布归档
                    │  <TAG>/      │  VERSION/{verilog,gds,pg,...}/
                    │    latest → │  软链接指向最新版本
                    │    VERSION/  │  文件名自带 {module}. 前缀区分模块
                    └─────────────┘
```

### 状态转换

```
PENDING ──(pre_check+gate+atomic move)──▶ SUBMITTED ──(release+post_check)──▶ RELEASED
    │                                           │                                  │
    └────────── (any failure) ──────▶ FAILED ◀──┘                                  │
                                                                                    │
    RELEASED ──(rm -rf 物理删除)──▶ release_file_missing 事件（下次 release 扫描时检测）
```

---

## 5. 并发控制

### 两层锁机制

```
┌────────────────────────────────────────────────┐
│  .lock_global_release  (ready/ 目录下)           │
│  发布全局锁，阻止所有 submit                       │
│  持有者: release 进程                             │
├────────────────────────────────────────────────┤
│  .lock_{MODULE}_{TAG}  (raw/ 目录下)              │
│  模块级互斥锁，阻止同 module+tag 并发提交           │
│  持有者: submit 进程                              │
└────────────────────────────────────────────────┘
```

### 锁矩阵

| | Submit | Release |
|---|---|---|
| 无锁 | ✓ | ✓ |
| 全局锁被持有 | ✗ "系统正在 Release" | ✗ "锁已被持有" |
| 模块锁被持有（同 tag） | ✗ "模块正在提交" | ✗ "模块正在提交中" |
| 模块锁被持有（不同 tag） | ✓ | ✗ "模块正在提交中" |
| 模块锁被持有（不同 module） | ✓ | ✗ |

### 原子性保证

- **锁创建**: `os.open(O_CREAT | O_EXCL)` 内核级原子操作，杜绝 TOCTOU
- **raw → ready**: `os.replace()` 同文件系统原子 rename
- **staging → release**: `os.rename()` staging 目录整体原子提交
- **latest**: `symlink_to()` + 先 `unlink()` 防止悬空

---

## 6. 数据完整性校验矩阵

```
a0.outgoing ──copy──▶ raw ──os.replace──▶ ready ──copy2──▶ release
     │                   │                    │                │
     ▼                   ▼                    ▼                ▼
  streaming_copy    pre_check           post_check 1      post_check 2
  BLAKE3 实时计算    size ✓              size ✓            size ✓
  utime 保留 mtime   BLAKE3 ✓            BLAKE3 ✓          BLAKE3 ✓
                    mtime ✓             mtime ✓           mtime ✓
                    (a0 vs raw)         (a0 vs ready)     (ready vs release)

跨版本检查:
  每次 release 扫描所有 RELEASED batch，检测物理文件是否被 rm -rf
  每次 release 对比新旧版本同名文件大小，±30% 记录 size_change，±50% 告警 size_anomaly
```

---

## 7. 配置驱动架构

```yaml
file_groups:                                   # 全局文件类型映射（所有 tag 共用）
  verilog: [".v.gz", ".v"]                    #   release 时按后缀分类到子目录
  gds:     [".hier.gds", ".gds.gz", ...]
  pg:      [".v.pg", ".pg"]

outgoing_root: ./a0.outgoing/{user}/{module}   # 每用户每模块独立目录，{user}/{module} 自动展开

defaults:
  tag:
    PV_ITER:
      description: 物理验证迭代版
      modules: [CPU, DDR]                       # 该 tag 管理的模块列表（-A 发布时检查）
      file_patterns:                            # 在 a0.outgoing/{user}/{module} 中匹配文件
        - "{module}.v.gz"
        - "{module}.hier.gds"
      gates:                                    # 黑盒门禁，由 subprocess 调用
        - name: verilog_syntax_check
          command: python3 -m ddm.gates.verilog_check
      release_users:                            # 发布授权白名单
        - w00949819
```

新增 tag、文件类型或修改流程只需编辑 YAML，主程序零改动。

---

## 8. 目录结构

```
ddm_new/                         # Git 仓库
├── config/config.yaml           # 全局配置
├── ddm/                         # 核心代码
│   ├── cli.py                   # Click CLI
│   ├── services.py              # 业务逻辑
│   ├── storage.py               # SQLite 持久层
│   ├── config.py                # 配置加载
│   └── gates/
│       ├── runner.py            # 门禁调度器
│       └── *_check.py           # 各门禁实现
├── tests/                       # pytest
├── a0.outgoing/                 # 源数据（按 {user}/{module} 分目录）
│   ├── wangshuai/
│   │   ├── CPU/
│   │   └── DDR/
│   └── w00949819/
│       └── CPU/
├── docs/                         # 项目文档
│   ├── WORKFLOWS.md              # Submit & Release 完整流程
│   ├── USER_GUIDE.md             # 用户指南
│   ├── DEPLOY.md                 # 部署指南
│   └── ARCHITECTURE.md           # 架构设计（本文件）
├── repository/                   # 运行时生成（不纳入 Git）
│   ├── raw/<TAG>/<MODULE>/       # 临时暂存
│   ├── ready/<TAG>/<MODULE>/     # 就绪暂存
│   ├── release/<TAG>/            # 发布归档
│   │   ├── latest → VERSION
│   │   └── VERSION/{verilog,gds,pg,...}/
│   └── ddm.db                    # SQLite 数据库
└── logs/                         # 运行日志（不纳入 Git）
```

## 9. 技术栈

| 组件 | 版本 | 用途 |
|------|------|------|
| click | 8.1.3 | CLI 命令路由与参数解析 |
| rich | 12.6.0 | 终端进度条、表格渲染 |
| pydantic | 1.10.2 | YAML 配置强类型校验 |
| PyYAML | 6.0 | 配置文件解析 |
| loguru | 0.6.0 | 结构化日志 |
| blake3 | 0.3.2 | 文件完整性哈希 |
| psutil | 5.9.4 | 磁盘容量探测 |
| sqlite3 | stdlib | 状态机持久化 |
