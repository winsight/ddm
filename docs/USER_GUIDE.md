# DDM 用户指南

## 角色与权限

DDM 面向两类角色，不同角色看到的命令不同：

| 角色 | 身份 | 可见命令 |
|------|------|----------|
| **模块视角** | 模块 owner（`modules.<NAME>.owners` 中的用户） | `submit` `status` `version` |
| **专项视角** | Tag 管理员（`release_users` 或 `admins` 中的用户） | 全部：`submit` `status` `release` `list` `check` `version` |

权限判定：用户是 `config.admins` 成员 **或** 任意 tag 的 `release_users` 成员 → 专项视角；否则 → 模块视角。

---

## 一、模块视角（Module Owner）

### 1.1 提交数据

```csh
ddm submit -m <MODULE> -t <TAG> [-s "备注"]
```

**前置条件：**
- 用户在模块的 `owners` 列表中（或为 admin）
- 用户属于 `shared_group`
- a0.outgoing 中有匹配 `file_patterns` 的文件
- 模块未被其他人提交中（无锁）
- 该 tag 未被 release 中

**示例：**

```csh
cp my_cpu.v.gz a0.outgoing/wangshuai/CPU/
cp my_cpu.hier.gds a0.outgoing/wangshuai/CPU/
ddm submit -m CPU -t PV_ITER -s "修复时序问题"
```

**输出：**

```
Submit module=CPU tag=PV_ITER user=wangshuai

✓ Submit successful
  Submitted: 2 files (total 1782579200 bytes)
```

**背后发生了什么：**

```
a0.outgoing/{user}/{module}/
        │ streaming_copy (边拷贝边 BLAKE3)
        ▼
raw/{TAG}/{MODULE}/           ← 临时暂存
        │ pre_check (BLAKE3 校验)
        │ run_gates (门禁检查)
        │ os.replace → ready/
        ▼
ready/{TAG}/{MODULE}/         ← 提交完成，等待 release
```

**快速失败顺序**（最轻量检查排最前面，避免无效 I/O）：

| 步骤 | 检查内容 | 失败代价 |
|------|----------|----------|
| 1 | Tag 白名单 | 零 I/O |
| 2 | 共享组成员 | 系统调用 |
| 3 | 模块 Owner 权限 | 读 config |
| 4 | Tag release 锁 | stat() |
| 5 | 模块锁获取 | O_CREAT\|O_EXCL |
| 6 | 磁盘空间 | statvfs() |
| 7 | 源文件扫描 | 写 SQLite |

### 1.2 查看状态

```csh
ddm status -m <MODULE> [-d 24h]
```

```
Status for module: CPU
┌──────────┬──────────┬────────────┬───────────┬─────────┬───────┐
│ UUID     │ Tag      │ User       │ Status    │ Summary │ Time  │
├──────────┼──────────┼────────────┼───────────┼─────────┼───────┤
│ a1b2c3d4 │ PV_ITER  │ wangshuai  │ SUBMITTED │ 时序修复│ 07-23 │
│ e5f6g7h8 │ LVS_PASS │ wangshuai  │ RELEASED  │ LVS通过 │ 07-22 │
└──────────┴──────────┴────────────┴───────────┴─────────┴───────┘
```

### 1.3 版本信息

```csh
ddm version
ddm -V
```

---

## 二、专项视角（Tag Admin）

### 2.1 发布数据

```csh
ddm release -t <TAG> -v <VERSION> (-m <MODULE> | -A) [--inherit] [--force]
```

| 参数 | 说明 |
|------|------|
| `-t` | Tag（必填） |
| `-v` | 版本号（必填，如 V1 V2） |
| `-m MODULE` | 单模块发布，其余从 @latest 继承 |
| `-A` | 全量发布，所有模块必须已提交 |
| `--inherit` | 配合 -A，缺失模块从上一版本继承 |
| `--force` | 配合 -A，允许覆盖已有版本 |

**三种模式：**

```
ddm release -t PV_ITER -m CPU -v V2       → CPU 新发布，DDR 自动继承 V1
ddm release -t PV_ITER -A -v V2            → CPU/DDR 都必须已提交
ddm release -t PV_ITER -A --inherit -v V2  → 缺失模块从上一版本继承
```

**版本覆盖规则：**

```
-A -v V1      → V1 不存在 → 创建 ✓
-A -v V1      → V1 已存在 → 拒绝 ✗
-A --force -v V1 → 强制覆盖 ✓
-m CPU -v V1  → V1 已存在 → 追加 CPU ✓
```

**输出：**

```
✓ Release successful: Released PV_ITER/V2 (6 files)
  Path: /nfs/eda/shared/ddm/repository/release/PV_ITER/V2
```

**背后发生了什么：**

```
ready/{TAG}/{MODULE}/
        │
        ├─ Pass 1: copy2 → staging
        ├─ Pass 1b: @latest → staging (继承未更新模块)
        ├─ Pass 2: size diff vs 上一版本
        ├─ Pass 3: post_check (staging hash vs DB hash)
        │
        └─ os.rename() 或 merge_dirs → release/
           ▼
release/{TAG}/@latest → VERSION/
  verilog/CPU.v.gz, DDR.v.gz
  gds/CPU.hier.gds, DDR.hier.gds
```

**版本继承链：**

```
V1: CPU(新), DDR(新)           ← -A -v V1
V2: CPU(新), DDR(继承自V1)     ← -m CPU -v V2
V3: CPU(继承自V2), DDR(新)     ← -m DDR -v V3
```

**全局文件分组（所有 tag 共用）：**

```
release/PV_ITER/V2/
├── verilog/          ← .v.gz, .v
├── gds/              ← .hier.gds, .gds.gz
└── pg/               ← .v.pg, .pg
```

**Tag 级锁隔离：** 不同 tag 的 release 互不阻塞。PV_ITER 发布中，PI_ITER 可以同时发布。

### 2.2 查看发布数据

```csh
ddm list -t <TAG>              # 最新版本
ddm list -t <TAG> -A           # 所有历史版本
ddm list -t <TAG> -v           # 详细（BLAKE3 + 文件级别时间戳）
ddm list -t <TAG> -m CPU       # 指定模块所有版本
```

**最新版本输出：**

```
PV_ITER Status
  已发布 (V2): CPU, DDR

┌────────┬──────────┬─────────┬────────────┬─────────┬───────┬──────┬─────────┐
│ Module │ Status   │ Version │ User       │ Summary │ Files │ Size │ Δ vs prev│
├────────┼──────────┼─────────┼────────────┼─────────┼───────┼──────┼─────────┤
│ CPU    │ RELEASED │ V2      │ wangshuai  │ E2E V2  │     2 │ 20B  │     +0% │
│ DDR    │ RELEASED │ V2      │ wangshuai  │ E2E     │     2 │ 20B  │     +0% │
└────────┴──────────┴─────────┴────────────┴─────────┴───────┴──────┴─────────┘
```

**详细模式 (`-v`)：**

```
CPU  RELEASED  v=V2  user=wangshuai  files=2  size=20B
 File        Size  Δ file  BLAKE3        Timestamp
 CPU.v.gz    10B           abc123def456  2026-07-23 00:50
 CPU.v.pg    10B           abc123def456  2026-07-23 00:50
```

### 2.3 环境检查

```csh
ddm check
```

检查项：配置文件、BLAKE3 可用性、SQLite、目录结构、共享组成员、模块 owner 组归属、过期锁、psutil。

---

## 三、Submit 深度解析

### 并发保护

| 锁文件 | 位置 | 作用 |
|--------|------|------|
| `.lock_{MODULE}_{TAG}` | raw/ | 同模块+tag 不能并发提交 |
| `.lock_release_{TAG}` | ready/ | release 期间阻止同 tag submit |

锁使用 `O_CREAT|O_EXCL` 内核原子操作。两进程同时创建同一锁文件，恰好一个成功。

**锁文件内容：**

```
pid=12345
user=wangshuai
time=1753234567.0
```

**过期锁自动清除：** 进程崩溃（kill -9）→ 锁残留 → 下次 submit 检测到原 PID 已死 → 自动清除并警告。若 PID 活着但锁超过 `stale_lock_minutes`（默认 10 分钟），同样清除并提示风险。

### 数据完整性链路

```
streaming_copy → hash 存 DB
     │
pre_check: BLAKE3(a0) vs DB hash    ← 用 DB 存的 hash，不复算 raw
     │
gates 执行 (在 raw/ 沙箱中)
     │
post_check: cache BLAKE3(raw) → os.replace → BLAKE3(ready) vs cache  ← 仅比对
     │
release post_check: BLAKE3(staging) vs DB hash                        ← 用 DB hash
```

DB 中 `streaming_copy` 写入的 hash 是信任锚点，后续校验都以它为准，避免反复计算大文件。

---

## 四、命令速查

| 命令 | 角色 | 说明 |
|------|------|------|
| `ddm submit -m CPU -t PV_ITER [-s 备注]` | 模块 | 提交 CPU 到 PV_ITER |
| `ddm status -m CPU [-d 24h]` | 模块 | 查看 CPU 各 tag 状态 |
| `ddm version` | 通用 | 版本信息 |
| `ddm release -t PV_ITER -m CPU -v V2` | 专项 | 发布 CPU（其余继承） |
| `ddm release -t PV_ITER -A -v V2` | 专项 | 全量发布 |
| `ddm release -t PV_ITER -A --inherit -v V2` | 专项 | 全量+继承 |
| `ddm release -t PV_ITER -A --force -v V2` | 专项 | 强制覆盖已有版本 |
| `ddm list -t PV_ITER` | 专项 | 最新版本概要 |
| `ddm list -t PV_ITER -A` | 专项 | 全部历史版本 |
| `ddm list -t PV_ITER -v` | 专项 | 详细（BLAKE3） |
| `ddm list -t PV_ITER -m CPU` | 专项 | 指定模块历史 |
| `ddm check` | 专项 | 环境检查 |
