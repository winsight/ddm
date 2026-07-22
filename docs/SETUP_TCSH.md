# DDM tcsh 部署与用户上手指南

## 环境假设

| 项目 | 说明 |
|------|------|
| 共享路径 | `/nfs/eda/shared/ddm/` (NFS 或 /tmp，所有用户可读) |
| Shell | tcsh |
| Python | 3.7+ |
| 共享组 | `wheel` (所有 DDM 用户属于此组) |

---

## 1. 管理员一次性部署

### 1.1 部署项目代码

```csh
mkdir -p /nfs/eda/shared
cd /nfs/eda/shared

# 从开发机 scp，或解压 tar.gz
tar -xzf ddm.tar.gz -C ddm
```

### 1.2 创建共享 Python 虚拟环境

```csh
python3 -m venv /nfs/eda/shared/ddm_venv
source /nfs/eda/shared/ddm_venv/bin/activate.csh
pip install -r /nfs/eda/shared/ddm/requirements.txt
echo "/nfs/eda/shared/ddm" > /nfs/eda/shared/ddm_venv/lib/python3.9/site-packages/ddm.pth
```

### 1.3 编辑 config.yaml

```yaml
outgoing_root: /nfs/eda/shared/ddm/a0.outgoing/{user}/{module}
repository_root: /nfs/eda/shared/ddm/repository
log_dir: /nfs/eda/shared/ddm/logs
shared_group: wheel
stale_lock_minutes: 10
```

### 1.4 创建 a0.outgoing 目录 + 验证

```csh
mkdir -p /nfs/eda/shared/ddm/a0.outgoing/wangshuai/CPU
mkdir -p /nfs/eda/shared/ddm/a0.outgoing/wangshuai/DDR
# ... 为每个用户创建模块目录
chmod -R 2775 /nfs/eda/shared/ddm/a0.outgoing/

source /nfs/eda/shared/ddm_venv/bin/activate.csh
python3 -m ddm check
```

### 1.5 部署完成后的目录结构

```
/nfs/eda/shared/
├── ddm/                       ← 项目代码（所有人只读）
│   ├── config/config.yaml
│   ├── ddm/                   ← Python 包
│   ├── ddm.complete.csh
│   ├── ddm_update.csh
│   ├── a0.outgoing/
│   ├── repository/
│   └── logs/
└── ddm_venv/                  ← 共享 Python 环境
    └── lib/.../ddm.pth        ← 自动发现 ddm 包
```

---

## 2. 用户配置（每人操作一次）

在 `~/.tcshrc` 中添加：

```csh
# ===== DDM =====
source /nfs/eda/shared/ddm_venv/bin/activate.csh
alias ddm 'python3 -m ddm'
source /nfs/eda/shared/ddm/ddm.complete.csh
```

立即生效：

```csh
source ~/.tcshrc
ddm check
```

> **只需这两行（+补全一行）**，不需要 PYTHONPATH，不需要 pip install。

---

## 3. 日常使用

### 3.1 提交数据

```csh
cp my_results/CPU.v.gz /nfs/eda/shared/ddm/a0.outgoing/wangshuai/CPU/
ddm submit -m CPU -t PV_ITER -s "修正了 LVS 问题"
```

### 3.2 查看状态

```csh
ddm status -m CPU
ddm list -t PV_ITER
ddm list -t PV_ITER -v
```

### 3.3 发布数据（专项管理员）

```csh
ddm release -t PV_ITER -m DDR -v V3
ddm release -t PV_ITER -A -v V3
ddm release -t PV_ITER -A --inherit -v V3
```

发布成功显示路径：

```
✓ Release successful: Released PV_ITER/V3 (6 files, 1 batches)
  Path: /nfs/eda/shared/ddm/repository/release/PV_ITER/V3
```

---

## 4. Tab 补全

```csh
ddm <TAB>        → submit status release list check version
ddm submit -<TAB> → -t -m -s
ddm submit -t<TAB> → PV_ITER LVS_PASS BASE_CLEAN ...
source /nfs/eda/shared/ddm/ddm.complete.csh
```

配置变更后刷新：`refresh_ddm_complete`

---

## 5. 命令速查

| 命令 | 说明 |
|------|------|
| `ddm check` | 环境检查 |
| `ddm submit -m CPU -t PV_ITER` | 提交 |
| `ddm status -m CPU` | 查看状态 |
| `ddm list -t PV_ITER` | 查看最新版本 |
| `ddm list -t PV_ITER -A` | 查看历史 |
| `ddm release -t PV_ITER -m CPU -v V1` | 单模块发布 |
| `ddm release -t PV_ITER -A -v V1` | 全量发布 |
| `ddm release -t PV_ITER -A --inherit -v V1` | 全量+继承 |
| `ddm version` | 版本信息 |

---

## 6. 常见问题

### Q: `ModuleNotFoundError: No module named 'click'`

未 source venv：
```csh
source /nfs/eda/shared/ddm_venv/bin/activate.csh
```

### Q: `✗ NOT in shared group`

联系管理员将用户加入 wheel 组。

### Q: 锁残留

```csh
ddm check  # 会显示过期锁，超时自动清除
```

### Q: Tab 不补全

```csh
source /nfs/eda/shared/ddm/ddm.complete.csh
```

---

## 7. 更新系统

```csh
ddm_update.csh /nfs/eda/packages/ddm_v2.2.tar.gz
ddm_update.csh --rollback
```
