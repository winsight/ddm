# DDM 用户指南

## 目录

1. [快速开始](#1-快速开始)
2. [核心概念](#2-核心概念)
3. [命令参考](#3-命令参考)
4. [典型工作流](#4-典型工作流)
5. [Tag 与文件匹配规则](#5-tag-与文件匹配规则)
6. [门禁系统](#6-门禁系统)
7. [常见问题](#7-常见问题)

---

## 1. 快速开始

### 启用 csh/tcsh Tab 补全

```csh
# 写入 ~/.cshrc（下次登录自动生效）
alias ddm "python3 -m ddm"
source ~/ddm/ddm.complete.csh

# 当前终端立即生效
alias ddm "python3 -m ddm"
source ~/ddm/ddm.complete.csh
```

补全效果：
- `ddm <TAB>` → 列出 submit / status / release / list / check / version
- `ddm submit -t <TAB>` → 列出所有配置的 tag（实时读取 config.yaml）
- `ddm submit -m <TAB>` → 提示输入模块名
- `ddm release -t <TAB>` → 同上

> 已按上述方式配置后，下面所有 `python3 -m ddm` 命令都可以直接用 `ddm` 替代。

### 环境检查

```bash
cd /path/to/ddm_new
python3 -m ddm check
```

输出示例：
```
DDM Environment Check

  ✓ Config loaded: config/config.yaml
    Tags: PV_ITER, LVS_PASS, BASE_CLEAN, PV_FINAL, PI_ITER, PI_FINAL
  ✓ BLAKE3 available
  ✓ SQLite ready: repository/ddm.db
  ✓ Outgoing root: ./a0.outgoing/{user}/{module}
  ✓ psutil available
```

### 基本流程（3 步）

```bash
# 第 1 步：提交模块数据（自动通过门禁校验）
python3 -m ddm submit -m CPU -t PV_ITER -s "首次 PV 迭代提交"

# 第 2 步：查看提交状态
python3 -m ddm status -m CPU

# 第 3 步：发布到共享归档
python3 -m ddm release -t PV_ITER -A -v V1
```

---

## 2. 核心概念

### 数据流转路径

```
你的 a0.outgoing/{user}/{module}/ ──submit──▶ raw/ ──门禁──▶ ready/ ──release──▶ release/
   (私有源数据)               (临时校验)   (自动检查)  (就绪暂存)    (共享归档)
```

### Tag（数据标签）

Tag 是对数据类型的分组标签，由管理员在 `config/config.yaml` 中定义：

| Tag | 说明 | 典型文件 |
|-----|------|----------|
| `PV_ITER` | 物理验证迭代版 | `.v.gz`, `.hier.gds`, `.v.pg` |
| `LVS_PASS` | LVS 验证通过版 | `.lvs.v.gz`, `.lvs.gds` |
| `BASE_CLEAN` | Base DRC Clean 版 | `.base.v.gz`, `.base.gds` |
| `PV_FINAL` | 物理验证最终版 | `.final.v.gz`, `.final.hier.gds` |
| `PI_ITER` | 工艺改进迭代版 | `.pi.v.pg`, `.pi.hier.gds.gz` |
| `PI_FINAL` | PI 最终版 | `.pi.final.v.pg`, `.pi.final.gds.gz` |

### Module（模块）

芯片设计中的功能模块，如 `CPU`、`DDR`、`GPU` 等。module 名称由用户自定义，系统不限制。

### Version（版本号）

发布版本号格式：`<label>_<YYYYMMDD>`（例如 `V1_20260716`）。

- 指定 `-v V1` → 生成 `V1_20260716`
- 不指定 `-v` → 生成 `20260716`（纯日期）

---

## 3. 命令参考

### 3.1 submit — 提交数据

```bash
python3 -m ddm submit -m <MODULE> -t <TAG> [-s "备注"]
```

| 参数 | 缩写 | 必填 | 说明 |
|------|------|------|------|
| `--module` | `-m` | 是 | 模块名，如 CPU、DDR |
| `--tag` | `-t` | 是 | 数据标签（单选），支持 Tab 补全 |
| `--summary` | `-s` | 否 | 提交备注，用引号包裹 |
| `--help` | `-h` | 否 | 显示帮助信息 |

**执行流程：**

1. 检查全局锁（release 进行中则拒绝）
2. 获取模块锁（同 module+tag 并发提交则拒绝）
3. 探测磁盘空间（不足 1.2 倍源文件总大小则拒绝）
4. 在 `a0.outgoing/{user}/{module}/` 中按 `file_patterns` 匹配文件
5. 流式拷贝到 `raw/<TAG>/<MODULE>/`，边拷贝边计算 BLAKE3，拷贝后通过 `utime` 保留源文件 mtime
6. pre_check：比对源文件与 raw 文件的 size + BLAKE3
7. 运行门禁检查（subprocess 调用外部脚本）
8. 原子移动 `raw/` → `ready/<TAG>/<MODULE>/`，设置权限 664
9. post_check：再次比对源文件与 ready 文件的 size + BLAKE3
10. 更新状态为 SUBMITTED，释放模块锁
11. 写入操作日志到 `a0.outgoing/{user}/{module}/.ddm_submit.log`

**示例：**

```bash
# 提交 CPU 模块的 PV_ITER 数据
python3 -m ddm submit -m CPU -t PV_ITER -s "RTL 综合完成，首次 PV 检查"

# 提交 DDR 模块的 LVS 数据
python3 -m ddm submit -m DDR -t LVS_PASS -s "LVS clean，无 short/open"

# 提交 CPU 模块的 PI 数据
python3 -m ddm submit -m CPU -t PI_ITER -s "工艺窗口优化 v3"
```

**进度条说明：**

submit 期间终端显示一根进度条，依次经过 4 个阶段：

```
  Copying wangshuai_CPU.v.gz ──── 14%
  Copying ...                   ── 43%
  Pre-check OK                  ── 57%
  Gates verilog_check ✓ (1.5s)  ── 71%
  Gates drc_check ✓ (4.0s)      ── 86%
  Delivering Done               ─ 100%
```

### 3.2 status — 查看状态

```bash
python3 -m ddm status -m <MODULE> [-d <TIME>]
```

| 参数 | 缩写 | 必填 | 说明 |
|------|------|------|------|
| `--module` | `-m` | 是 | 模块名 |
| `--date` | `-d` | 否 | 时间过滤，如 `24h`、`3d`、`5m` |
| `--help` | `-h` | 否 | 显示帮助信息 |

**输出示例：**

```
                        Status for module: CPU
┏━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━┓
┃ UUID     ┃ Tag     ┃ User      ┃ Status    ┃ Summary      ┃ Time     ┃
┡━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━┩
│ a1b2c3d4 │ PV_ITER │ wangshuai │ SUBMITTED │ 首次 PV 检查 │ 07-16 09:30 │
│ e5f6g7h8 │ PV_ITER │ wangshuai │ RELEASED  │ V1 发布      │ 07-15 14:20 │
│ i9j0k1l2 │ PI_ITER │ wangshuai │ FAILED     │ 门禁未通过   │ 07-14 11:00 │
└──────────┴─────────┴───────────┴───────────┴──────────────┴────────────┘
```

**状态说明：**

| 状态 | 颜色 | 含义 |
|------|------|------|
| PENDING | 黄色 | 提交正在处理中 |
| SUBMITTED | 青色 | 已过门禁，等待发布 |
| RELEASED | 绿色 | 已发布到共享归档 |
| FAILED | 红色 | 校验或门禁失败 |

### 3.3 release — 发布数据

```bash
python3 -m ddm release -t <TAG> (-A | -m <MODULE>) [-v <LABEL>] [--inherit]
```

| 参数 | 缩写 | 必填 | 说明 |
|------|------|------|------|
| `--tag` | `-t` | 是 | 数据标签（单选），支持 Tab 补全 |
| `--all` | `-A` | 二选一 | 发布 config.yaml `modules` 列表中所有已配置的模块。缺失模块时报错，结合 `--inherit` 可从上一版本继承 |
| `--module` | `-m` | 二选一 | 发布指定模块（其他模块从 @latest 继承） |
| `--version` | `-v` | 否 | 版本标签（如 `V1`），生成 `V1_YYYYMMDD`；不指定则纯日期戳 |
| `--inherit` | | 否 | 配合 `-A` 使用，允许未提交的模块从上一版本继承 |
| `--help` | `-h` | 否 | 显示帮助信息 |

**执行流程：**

1. **授权检查**：当前用户必须在 `release_users` 或 `admins` 中
2. **版本检查**：目标版本目录已存在则追加模块（不会拒绝），不存在则创建新版本
3. **完整性扫描**：检查所有历史 RELEASED 版本物理文件是否被删除
4. **模块锁检查**：有模块正在提交则拒绝
5. **获取全局锁**：阻止新的 submit
6. **三阶段提交**：
   - Pass 1：复制 ready → `.staging_xxx/` 临时目录，使用 `copy2` 保留 mtime
   - Pass 1b：从 @latest 继承未变更的模块（累积版本，保留历史数据）
   - Pass 2：与上一版本对比文件大小，±30% 告警，±50% 异常警告
   - Pass 3：post_check（size + mtime + BLAKE3 比对 ready vs staging）
   - 全通过则 `os.rename`（新版本）或 `_merge_dirs`（追加）原子提交
7. **更新 @latest**：软链接指向新版本
8. **清理 ready/**：删除已发布的数据
9. **写入操作日志**到 `release/<TAG>/.ddm_release.log`

**示例：**

```bash
# 发布所有 PV_ITER 模块，版本 V1
python3 -m ddm release -t PV_ITER -A -v V1

# 只发布 CPU 模块，版本 V2（其他模块从 V1 继承）
python3 -m ddm release -t PV_ITER -m CPU -v V2

# 发布所有模块，缺失的模块从上一版本继承
python3 -m ddm release -t PV_ITER -A -v V3 --inherit

# 不指定版本，使用当日日期
python3 -m ddm release -t PI_ITER -m CPU
```

**输出示例（有完整性告警）：**

```
✓ Release successful: Released PV_ITER/V2_20260716 (3 files, 1 batches)

⚠ Integrity Warnings:
  Integrity warning: 1 previously-released version(s) have missing files: ['V1_20260716']
    repository/release/PV_ITER/V1_20260716/CPU/wangshuai_CPU.v.gz
  Size anomaly [increase 60%]: wangshuai_CPU.v.pg: 109 → 174 bytes
```

### 3.4 list — 列表查看

```bash
python3 -m ddm list -t <TAG> [-A | -m <MODULE>] [-v]
```

| 参数 | 缩写 | 必填 | 说明 |
|------|------|------|------|
| `--tag` | `-t` | 是 | 数据标签（单选），支持 Tab 补全 |
| `--all` | `-A` | 否 | 显示所有历史版本（默认只显示每个模块的最新版本） |
| `--module` | `-m` | 否 | 按模块过滤，显示该模块的所有版本 |
| `--verbose` | `-v` | 否 | 逐文件显示 BLAKE3 哈希、时间戳和文件大小变化 |
| `--help` | `-h` | 否 | 显示帮助信息 |

**默认视图**（不带 `-A` 或 `-m`）：每个模块显示一行（最新版本），顶部显示该 tag 的状态摘要（已发布版本、待发布、失败、未提交的模块列表）。

**-v 详细视图**：逐文件列出 BLAKE3 哈希（前 12 位）、文件大小变化（与上一版本相比）和源文件修改时间戳。适用于审计和数据完整性验证。

### 3.5 check — 环境检查

```bash
python3 -m ddm check
```

验证配置加载、BLAKE3 可用性、SQLite 就绪、目录存在性、psutil 可用性。

---

## 4. 典型工作流

### 场景 A：日常 PV 迭代提交与发布

```bash
# 工程师提交数据
python3 -m ddm submit -m CPU -t PV_ITER -s "RTL v2.1 综合"
python3 -m ddm submit -m DDR -t PV_ITER -s "DDR PHY 更新"

# 查看状态
python3 -m ddm status -m CPU
python3 -m ddm list -t PV_ITER -A

# 专项管理员发布
python3 -m ddm release -t PV_ITER -A -v V3
```

### 场景 B：多 Tag 并行提交

```bash
# 同一模块可以不同 tag 并行提交
python3 -m ddm submit -m CPU -t PV_ITER &
python3 -m ddm submit -m CPU -t LVS_PASS &
python3 -m ddm submit -m CPU -t PI_ITER &
wait
```

### 场景 C：门禁失败排查

```bash
# 提交后看到 FAILED 状态
python3 -m ddm status -m GPU

# 查看详细日志
cat logs/ddm_2026-07-16.log | grep "GPU"

# 修复问题后重新提交（新批次，新 UUID）
python3 -m ddm submit -m GPU -t PV_ITER -s "修复门禁问题 v2"
```

### 场景 D：增量发布

```bash
# 第一轮：只发布 CPU
python3 -m ddm release -t PV_ITER -m CPU -v V1

# 第二轮：DDR 也准备好了，追加发布（V2 会继承 V1 中的 CPU）
python3 -m ddm release -t PV_ITER -m DDR -v V2
# @latest 现在指向 V2，包含 CPU（继承） + DDR（新增）
# V1 的 CPU 数据仍在 release/PV_ITER/V1/ 中独立保留
```

---

## 5. Tag 与文件匹配规则

### a0.outgoing 目录结构

`a0.outgoing/` 按用户和模块分目录组织，每个工程师在自己的子目录中平铺文件。`outgoing_root` 路径中的 `{user}` 和 `{module}` 占位符在运行时自动替换（例如 `./a0.outgoing/{user}/{module}` 展开为 `./a0.outgoing/wangshuai/CPU/`）：

```
a0.outgoing/
├── wangshuai/
│   ├── CPU/
│   │   ├── wangshuai_CPU.v.gz
│   │   ├── wangshuai_CPU.hier.gds
│   │   └── wangshuai_CPU.v.pg
│   └── DDR/
│       ├── wangshuai_DDR.v.gz
│       └── wangshuai_DDR.hier.gds
├── w00949819/
│   └── CPU/
│       ├── w00949819_CPU.v.gz
│       └── w00949819_CPU.hier.gds
└── ...
```

每个模块目录下的文件是**平铺**的（无进一步子目录），由 `file_patterns` 模式匹配选择文件。

### file_patterns 匹配

每个 tag 在 `config.yaml` 中定义了 `file_patterns`，用 `{user}` 和 `{module}` 占位符：

```yaml
PV_ITER:
  file_patterns:
    - "{user}_{module}.v.gz"
    - "{user}_{module}.hier.gds"
    - "{user}_{module}.v.pg"
```

当执行 `submit -m CPU -t PV_ITER`（用户 `wangshuai`）时：
- `{user}` → `wangshuai`
- `{module}` → `CPU`
- 匹配结果：`wangshuai_CPU.v.gz`、`wangshuai_CPU.hier.gds`、`wangshuai_CPU.v.pg`

一个文件可以同时匹配多个 tag（如 `.v.pg` 同时被 PV_ITER 和 PI_ITER 匹配），这是正常的——不同 tag 代表该文件在不同流程中的用途。

---

## 6. 门禁系统

### 什么是门禁

门禁是独立的外部检查脚本，在数据从 `raw/` 进入 `ready/` 之前执行。每个 tag 可配置零个或多个门禁。

### 门禁的执行方式

门禁以 subprocess 方式调用，接收三个参数：

```bash
python3 -m ddm.gates.verilog_check <raw_dir> <module> <tag>
```

- **退出码 0**：通过
- **退出码非 0**：失败，submit 中止，状态标记为 FAILED

### 当前门禁列表

| 门禁名称 | 命令 | 说明 |
|----------|------|------|
| verilog_syntax_check | `python3 -m ddm.gates.verilog_check` | Verilog 语法检查 |
| drc_baseline_check | `python3 -m ddm.gates.drc_check` | DRC 基线检查 |
| lvs_integrity_check | `python3 -m ddm.gates.lvs_check` | LVS 完整性检查 |
| final_integrity_check | `python3 -m ddm.gates.final_check` | 最终签核检查 |
| pi_iter_check | `python3 -m ddm.gates.pi_check` | PI 迭代验证 |
| pi_final_check | `python3 -m ddm.gates.pi_final_check` | PI 最终签核 |

> 注：当前为 stub 实现（含模拟延时），生产环境需替换为实际检查脚本。

### 新增门禁

1. 在 `ddm/gates/` 下创建新脚本，接收 `raw_dir module tag` 参数，退出码表示结果
2. 在 `config/config.yaml` 对应 tag 的 `gates` 列表中添加条目：

```yaml
PV_ITER:
  gates:
    - name: my_new_check
      command: python3 -m ddm.gates.my_new_check
```

---

## 7. 常见问题

### Q: submit 被拒绝 "系统正在 Release"

```
✗ Submit failed: 系统正在 Release，提交被阻断
```

**原因**：专项管理员正在执行 `release`，全局锁被持有。**解决**：等待 release 完成（通常几秒到几十秒），然后重试。

### Q: submit 被拒绝 "无权提交模块"

```
✗ Submit failed: 无权提交模块 'CPU'。该模块的 owner 为: wangshuai, zhangsan。如需提交请联系管理员。
```

**原因**：你不在该模块的 `owners` 列表中，也不是 `admins`。**解决**：联系管理员在 `config/config.yaml` 的 `modules.<NAME>.owners` 中添加你的用户名。

### Q: submit 被拒绝 "未配置 owners"

```
✗ Submit failed: 模块 'GPU' 未在 config.yaml 中配置 owners 列表。
```

**原因**：该模块尚未在 config 中注册。**解决**：在 `config/config.yaml` 中添加：

```yaml
modules:
  GPU:
    owners: [yourname, otheruser]
```

### Q: submit 被拒绝 "模块正在提交"

```
✗ Submit failed: 模块 CPU/PV_ITER 正在提交
```

**原因**：同一模块 + 同一 tag 已有另一个 submit 在执行中。**解决**：等待前一个 submit 完成，或对不同 module/tag 提交。

### Q: release 被拒绝 "not authorized"

```
✗ Release failed: User 'wangshuai' not authorized to release tag 'PV_ITER'
```

**原因**：你不在该 tag 的 `release_users` 列表或 `admins` 列表中。**解决**：联系管理员将你加入 `config/config.yaml` 对应 tag 的 `release_users`。

### Q: release 被拒绝 "already exists"

```
✗ Release failed: Version 'V1_20260716' already exists
```

**原因**：该版本号已被使用。**解决**：使用不同的 `-v` 标签（如 `-v V2`）。

### Q: 如何查看某个提交的详细信息

```bash
# 查看 SQLite 中的文件记录
python3 -c "
import sqlite3
conn = sqlite3.connect('repository/ddm.db')
conn.row_factory = sqlite3.Row
for r in conn.execute('SELECT * FROM files WHERE batch_uuid LIKE \"a1b2c3d4%\"').fetchall():
    print(dict(zip(r.keys(), r)))
"

# 查看审计事件
python3 -c "
import sqlite3
conn = sqlite3.connect('repository/ddm.db')
conn.row_factory = sqlite3.Row
for r in conn.execute('SELECT * FROM events WHERE batch_uuid LIKE \"a1b2c3d4%\" ORDER BY created_at').fetchall():
    print(dict(zip(r.keys(), r)))
"
```

### Q: 如何清理运行数据

```bash
bash clean.sh
```

这会清除 `raw/`、`ready/`、`release/`、`ddm.db` 和 `logs/`，**不可恢复**。`a0.outgoing/` 不受影响。

### Q: BLAKE3 不可用时会发生什么

如果 `pip install blake3` 失败（如离线环境缺少编译工具），系统会自动降级使用 Python 标准库的 BLAKE2b-256。`ddm check` 会显示：

```
  ! BLAKE3 not available — using BLAKE2b-256 fallback
```

功能不受影响，仅哈希算法不同。生产环境建议安装 BLAKE3 以获得最佳性能。
