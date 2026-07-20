# DDM tcsh 部署与用户上手指南

## 环境假设

| 项目 | 说明 |
|------|------|
| 共享存储 | `/nfs/eda/shared/ddm/` (NFS, 所有服务器挂载) |
| Shell | tcsh (用户登录 shell) |
| Python | 3.7+ (系统已安装 `click`, `rich`, `pydantic` 等依赖) |
| 共享组 | `staff` 或 `wheel` (所有 DDM 用户属于此组) |

---

## 1. 管理员一次性部署

### 1.1 放置代码

```csh
# SSH 到 NFS 可写节点
mkdir -p /nfs/eda/shared/ddm
cd /nfs/eda/shared/ddm

# 从开发机 scp 过来，或用 FileCodeBox 下载
tar -xzf ddm.tar.gz -C /nfs/eda/shared/ddm
```

### 1.2 确认目录结构

```
/nfs/eda/shared/ddm/
├── config/config.yaml       ← 唯一配置，管理员维护
├── ddm/                     ← Python 包
├── ddm.complete.csh          ← tcsh 补全脚本
├── ddm_update.csh            ← 更新脚本
├── a0.outgoing/              ← 各用户源数据
│   ├── wangshuai/CPU/
│   ├── lisi/DDR/
│   └── ...
├── repository/               ← 运行时仓库
└── logs/                     ← 日志
```

### 1.3 编辑 config.yaml（关键）

```yaml
# 源数据目录 {user} 和 {module} 会自动展开
outgoing_root: /nfs/eda/shared/ddm/a0.outgoing/{user}/{module}
repository_root: /nfs/eda/shared/ddm/repository
log_dir: /nfs/eda/shared/ddm/logs

shared_group: staff           # 或 wheel, 按实际设置
stale_lock_minutes: 10        # 过期锁自动清理

modules:
  CPU:
    owners: [wangshuai, zhangsan]
  DDR:
    owners: [lisi, wangshuai]

admins:
  - wangshuai
  - w00949819

file_groups:
  verilog: [".v.gz", ".v"]
  gds:     [".hier.gds", ".lvs.gds", ".gds.gz", ".final.hier.gds"]
  pg:      [".v.pg", ".pg"]
```

> **关键：所有路径必须是绝对路径**。`outgoing_root` 保留 `{user}` 和 `{module}` 占位符。

### 1.4 验证环境

```csh
cd /nfs/eda/shared/ddm
python3 -m ddm check
```

期望输出全部绿色 ✓，尤其是 `✓ Member of shared group: staff`。

### 1.5 创建 a0.outgoing 目录

```csh
mkdir -p /nfs/eda/shared/ddm/a0.outgoing/wangshuai/CPU
mkdir -p /nfs/eda/shared/ddm/a0.outgoing/wangshuai/DDR
# ... 为每个用户创建对应的模块目录
chmod -R 2775 /nfs/eda/shared/ddm/a0.outgoing/
```

---

## 2. 用户配置（每人操作一次）

### 2.1 配置 `~/.cshrc`

```csh
# ===== DDM 配置 =====
setenv PYTHONPATH /nfs/eda/shared/ddm       # Python 找到 ddm 包
alias ddm 'python3 -m ddm -c /nfs/eda/shared/ddm/config/config.yaml'   # 全局配置
source /nfs/eda/shared/ddm/ddm.complete.csh                            # Tab 补全
```

### 2.2 立即生效

```csh
source ~/.cshrc
ddm check
```

看到 `✓ Member of shared group: staff` 即可。

---

## 3. 日常使用

### 3.1 提交数据

```csh
# 把文件放到自己的 a0.outgoing 目录
cp my_results/CPU.v.gz /nfs/eda/shared/ddm/a0.outgoing/wangshuai/CPU/
cp my_results/CPU.hier.gds /nfs/eda/shared/ddm/a0.outgoing/wangshuai/CPU/

# 提交（任何目录都可以）
ddm submit -m CPU -t PV_ITER -s "修正了 LVS 问题"
```

**任意目录执行** —— `-c` 参数指定了全局配置路径，所有相对路径都相对于配置所在的项目根目录解析。

### 3.2 查看状态

```csh
ddm status -m CPU          # 查看所有 tag 的状态
ddm status -m CPU -d 24h   # 最近 24 小时
ddm list -t PV_ITER        # 查看已提交/已发布的数据
ddm list -t PV_ITER -A     # 查看所有历史版本
ddm list -t PV_ITER -v     # 详细信息（含 BLAKE3）
```

### 3.3 发布数据（专项管理员）

```csh
# 单模块发布（其余模块自动继承上一版本）
ddm release -t PV_ITER -m DDR -v V3

# 全量发布（所有模块必须都有新数据）
ddm release -t PV_ITER -A -v V3

# 全量发布 + 继承（允许部分模块无新数据）
ddm release -t PV_ITER -A --inherit -v V3
```

**发布成功后会显示数据路径：**

```
✓ Release successful: Released PV_ITER/V3 (6 files, 1 batches)
  Path: /nfs/eda/shared/ddm/repository/release/PV_ITER/V3
```

---

## 4. Tab 补全

```csh
ddm <TAB>        → submit status release list check version
ddm submit -<TAB> → -t -m -s
ddm submit -t<TAB> → PV_ITER  LVS_PASS  BASE_CLEAN  ...
ddm submit -m<TAB> → CPU  DDR
ddm release -v<TAB> → V1  V2  V3  ...
```

配置变更后刷新：`refresh_ddm_complete`

---

## 5. 命令速查

| 命令 | 说明 |
|------|------|
| `ddm check` | 环境检查（权限/组/过期锁） |
| `ddm submit -m CPU -t PV_ITER` | 提交 CPU @ PV_ITER |
| `ddm status -m CPU` | 查看 CPU 所有 tag 状态 |
| `ddm list -t PV_ITER` | 查看 PV_ITER 最新版本 |
| `ddm list -t PV_ITER -A` | 查看 PV_ITER 所有历史版本 |
| `ddm list -t PV_ITER -v` | 详细信息（BLAKE3 + 时间戳） |
| `ddm release -t PV_ITER -m CPU -v V1` | 发布 CPU @ PV_ITER v1 |
| `ddm release -t PV_ITER -A -v V1` | 全量发布 PV_ITER v1 |
| `ddm release -t PV_ITER -A --inherit -v V1` | 全量发布+继承 |
| `ddm version` | 版本信息 |
| `ddm update --rollback` | 回退到上一个版本 |

---

## 6. 常见问题

### Q: `ddm: Command not found`

```csh
source ~/.cshrc
which ddm
# 应显示: ddm: aliased to python3 -m ddm -c /nfs/eda/.../config.yaml
```

### Q: `ModuleNotFoundError: No module named 'ddm'`

PYTHONPATH 未设置或路径错误：
```csh
echo $PYTHONPATH
ls $PYTHONPATH/ddm/__init__.py  # 应存在
```

### Q: `✗ NOT in shared group`

用户不在 staff/wheel 组，联系管理员：
```bash
usermod -a -G staff username
```

### Q: `[CPU] 正在提交` 锁残留

上次提交被 kill -9 中断了：
```csh
ddm check  # 会显示过期锁
# 超过 stale_lock_minutes (默认10min) 会自动清除
# 或者手动: rm /nfs/eda/shared/ddm/repository/raw/.lock_CPU_*
```

### Q: Tab 补全不生效

```csh
source /nfs/eda/shared/ddm/ddm.complete.csh
refresh_ddm_complete
```

### Q: 能否在任意目录执行？

可以。`-c` 指定了绝对路径的配置文件，所有相对路径都基于配置所在的项目根目录解析。

---

## 7. 更新系统

```csh
# 拉取最新版本
ddm_update.csh user@dev-server:/tmp/ddm_v2.2.tar.gz

# 或回退
ddm_update.csh --rollback
```

更新脚本自动：备份旧版本 → 恢复配置 → 原子切换 → ddm check 验证。
