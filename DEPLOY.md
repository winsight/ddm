# DDM 部署指南

## 目录

1. [环境要求](#1-环境要求)
2. [快速部署（在线环境）](#2-快速部署在线环境)
3. [离线部署方案](#3-离线部署方案)
4. [二进制打包（CentOS 7.9）](#4-二进制打包centos-79)
5. [配置初始化](#5-配置初始化)
6. [验证部署](#6-验证部署)
7. [Git 更新与热更新](#7-git-更新与热更新)
8. [权限与目录规划](#8-权限与目录规划)
9. [故障排查](#9-故障排查)
10. [附录：从 macOS 开发到 CentOS 部署的完整流程](#10-附录从-macos-开发到-centos-部署的完整流程)

---

## 1. 环境要求

| 项目 | 最低要求 |
|------|----------|
| 操作系统 | Linux (RHEL/CentOS 7+, Ubuntu 18.04+) |
| Python | 3.7+（推荐 3.9+） |
| Git | 1.8.3.1+ |
| 磁盘空间 | ≥ 10 GiB（代码 + 依赖 + 运行数据） |
| 文件系统 | 支持 POSIX 文件锁和原子 rename（ext4 / xfs / NFSv4） |

### Python 依赖清单

```
click==8.1.3
prompt-toolkit==3.0.36
prettytable==3.6.0
tabulate==0.9.0
rich==12.6.0
alive-progress==2.4.1
tqdm==4.64.0
progressbar2==4.2.0
pydantic==1.10.2
PyYAML==6.0
loguru==0.6.0
PyMySQL==1.0.2
psutil==5.9.4
blake3==0.3.2
```

> `gdstk==0.9.51` 和 `pyverilog==1.3.0` 按需安装，系统核心功能不依赖。

---

## 2. 快速部署（在线环境）

```bash
# 1. 克隆仓库
git clone <repo_url> /opt/ddm
cd /opt/ddm

# 2. 安装依赖
pip install -r requirements.txt

# 3. 编辑配置
vim config/config.yaml

# 4. 验证
python -m ddm check
```

---

## 3. 离线部署方案

适用于无法连接公网的服务器。

### 3.1 在有网络的机器上下载离线包

```bash
# 创建工作目录
mkdir ddm_offline && cd ddm_offline

# 下载所有依赖的 wheel 包（包含传递依赖）
pip download \
  --platform manylinux2014_x86_64 \
  --python-version 39 \
  --only-binary=:all: \
  -r /path/to/ddm_new/requirements.txt \
  -d ./packages/

# 如果某些包没有预编译 wheel（如 blake3），同时下载源码包
pip download \
  -r /path/to/ddm_new/requirements.txt \
  -d ./packages/
```

**关键包说明：**

| 包 | 类型 | 说明 |
|----|------|------|
| `blake3` | 含 C 扩展 | 需要 `gcc` + `libc-dev` 编译。如果没有编译工具，系统会自动降级到 BLAKE2b |
| `psutil` | 含 C 扩展 | 通常有预编译 wheel |
| 其余 | 纯 Python | 无需编译，直接安装 |

### 3.2 打包传输

```bash
# 方案 A：直接打包 DDM 仓库 + 离线包
tar -czf ddm_deploy.tar.gz \
  /path/to/ddm_new/ \
  ./packages/

# 方案 B：使用 rsync（推荐，支持断点续传）
rsync -avz --progress \
  /path/to/ddm_new/ \
  user@server:/opt/ddm/

rsync -avz --progress \
  ./packages/ \
  user@server:/opt/ddm_offline_packages/
```

### 3.3 在离线服务器上安装

```bash
# 1. 进入 DDM 目录
cd /opt/ddm

# 2. 从本地 wheel 包安装（不访问 PyPI）
pip install \
  --no-index \
  --find-links /opt/ddm_offline_packages/ \
  -r requirements.txt

# 3. 如果 blake3 编译失败（缺少 gcc），忽略即可
#    系统会自动使用 hashlib.blake2b 替代
```

### 3.4 处理无编译工具的情况

如果离线服务器没有 `gcc`，`blake3` 的源码包无法编译安装：

```bash
# 在有网络的机器上，下载 blake3 的预编译 wheel
pip download blake3==0.3.2 --platform manylinux2014_x86_64 --only-binary=:all: -d ./packages/

# 或在离线服务器上跳过 blake3，让系统使用 BLAKE2b 降级
grep -v "blake3" requirements.txt > requirements_no_blake3.txt
pip install --no-index --find-links /opt/ddm_offline_packages/ -r requirements_no_blake3.txt
```

---

## 4. 二进制打包（CentOS 7.9）

DDM 支持通过 PyInstaller 打包为独立二进制文件，目标服务器无需安装 Python。

> **重要**：PyInstaller 不支持交叉编译。二进制必须在与目标系统相同（或更旧）的 Linux 上构建。以下提供两种方案。

### 方案 A：Docker 构建（macOS / 任意平台 → CentOS 7.9）

在开发机上使用 Docker 模拟 CentOS 7.9 环境构建：

```bash
# 1. 构建 Docker 镜像（首次约 5 分钟，含编译 Python 3.9）
docker build -t ddm-builder -f Dockerfile.build .

# 2. 挂载源码目录，自动构建二进制
docker run --rm -v $(pwd):/build ddm-builder
```

构建产物在 `dist/ddm`（Linux ELF 二进制）和 `dist/ddm.tar.gz`（部署包）。

### 方案 B：在 CentOS 7.9 服务器上直接构建

如果服务器上已有 Python 3.7+，可以直接构建：

```bash
# 1. 安装构建工具
pip3 install pyinstaller
pip3 install -r requirements.txt

# 2. 执行构建
chmod +x build_binary.sh
./build_binary.sh
```

### 方案 C：从有网络的 CentOS 7.9 虚拟机构建

1. 在 CentOS 7.9 VM 中安装 Python 3.9 和依赖
2. 拉取 DDM 代码
3. 执行 `./build_binary.sh`
4. 将 `dist/ddm.tar.gz` 拷贝到离线服务器

### 部署二进制

构建完成后，将 `ddm.tar.gz` 传输到目标服务器：

```bash
# 在目标服务器上
cd /opt
tar -xzf ddm.tar.gz
cd ddm_package
cp config.yaml.example config.yaml
vi config.yaml   # 修改 outgoing_root / repository_root / admins 等
./ddm check
```

### 二进制更新

修改代码后重新构建即可。二进制内已包含 Python 解释器和所有依赖，替换 `ddm` 文件即完成更新：

```bash
# 替换二进制
cp /path/to/new/ddm /opt/ddm_package/ddm

# 配置文件和数据不受影响（它们在二进制外部）
```

---

## 5. 配置初始化

### 4.1 最小配置

编辑 `config/config.yaml`：

```yaml
admins:
  - your_username

outgoing_root: /nfs/eda/a0.outgoing   # 指向实际的 a0.outgoing 目录
repository_root: /nfs/eda/ddm_repo    # 仓库运行时目录（建议放在共享存储）
log_dir: /nfs/eda/ddm_repo/logs       # 日志目录

defaults:
  tag:
    PV_ITER:
      description: 物理验证迭代版
      file_patterns:
        - "{user}_{module}.v.gz"
        - "{user}_{module}.hier.gds"
        - "{user}_{module}.v.pg"
      gates:
        - name: verilog_syntax_check
          command: python -m ddm.gates.verilog_check
      release_users:
        - your_release_admin
    # ... 其他 tag 配置
```

### 4.2 关键配置项

| 配置项 | 说明 | 示例 |
|--------|------|------|
| `admins` | 全局管理员列表，可发布所有 tag | `[wangshuai, admin2]` |
| `outgoing_root` | a0.outgoing 源数据根目录（平铺） | `/nfs/eda/a0.outgoing` |
| `repository_root` | 运行时仓库根目录 | `/nfs/eda/ddm_repo` |
| `log_dir` | 日志输出目录 | `/nfs/eda/ddm_repo/logs` |
| `defaults.tag.<NAME>.file_patterns` | 文件匹配模式，`{user}` `{module}` 占位符 | `["{user}_{module}.v.gz"]` |
| `defaults.tag.<NAME>.gates` | 门禁脚本列表 | 见上文 |
| `defaults.tag.<NAME>.release_users` | 有权限发布该 tag 的用户列表 | `[user1, user2]` |

### 4.3 多用户共享存储配置

```yaml
# 所有工程师的 a0.outgoing 在共享 NFS 上
outgoing_root: /nfs/eda/a0.outgoing

# 仓库目录也在共享存储上，所有用户使用同一份
repository_root: /nfs/eda/ddm_repo
```

### 4.4 全局软链接部署

在 `/usr/local/bin/` 创建软链接，使 `ddm` 命令全局可用：

```bash
ln -s /opt/ddm/ddm /usr/local/bin/ddm
```

然后需要确保 `ddm` 脚本可执行：

```bash
cat > /opt/ddm/ddm << 'EOF'
#!/bin/bash
cd /opt/ddm && exec python -m ddm "$@"
EOF
chmod +x /opt/ddm/ddm
```

---

## 6. 验证部署

### 5.1 环境检查

```bash
cd /opt/ddm
python -m ddm check
```

期望输出全部绿色 ✓。

### 5.2 功能测试

```bash
# 创建测试数据
mkdir -p a0.outgoing
echo "test data" > a0.outgoing/${USER}_TEST.v.gz
echo "test data" > a0.outgoing/${USER}_TEST.hier.gds

# 临时修改 config.yaml，添加测试 tag
# （或使用现有 tag 和现有文件）

# 提交测试
python -m ddm submit -m TEST -t PV_ITER -s "deploy test"

# 查看状态
python -m ddm status -m TEST

# 发布
python -m ddm release -t PV_ITER -m TEST -v DEPLOY_TEST

# 清理
rm a0.outgoing/${USER}_TEST.*
bash clean.sh
```

### 5.3 并发锁测试

```bash
# 终端 1：获取模块锁
echo "$$" > repository/raw/.lock_TEST_PV_ITER

# 终端 2：尝试提交（应被拒绝）
python -m ddm submit -m TEST -t PV_ITER
# ✗ Submit failed: 模块 TEST/PV_ITER 正在提交

# 终端 1：释放锁
rm repository/raw/.lock_TEST_PV_ITER
```

---

## 7. Git 更新与热更新

### 6.1 更新流程

```bash
cd /opt/ddm
git pull origin master
```

### 6.2 热更新保证

DDM 设计保证了 Git 更新不会中断正在运行的任务：

| 保证 | 原理 |
|------|------|
| 锁文件独立 | `.lock_*` 在 `repository/` 下，不在 Git 仓库内 |
| 运行时目录不纳入 Git | `repository/` 和 `logs/` 在 `.gitignore` 中 |
| SQLite 不纳入 Git | `ddm.db` 在 `.gitignore` 中 |
| Python 模块懒加载 | 已运行的进程持有旧代码的引用，继续执行完毕 |
| 新进程使用新代码 | 更新后启动的新 submit/release 自动使用最新代码 |

### 6.3 回滚

```bash
# 查看历史
git log --oneline -10

# 回滚到指定版本
git checkout <commit_hash>

# 或回滚到上一个版本
git checkout HEAD~1
```

---

## 8. 权限与目录规划

### 7.1 推荐目录布局

```
/nfs/eda/
├── a0.outgoing/          # 工程师源数据（各用户有写权限）
├── ddm/                  # DDM 代码仓库（管理员有写权限，其他只读）
│   ├── config/
│   ├── ddm/
│   └── ...
└── ddm_repo/             # 运行时仓库（DDM 服务账号有写权限）
    ├── raw/
    ├── ready/
    ├── release/
    ├── ddm.db
    └── logs/
```

### 7.2 权限设置

```bash
# DDM 代码目录：管理员可写，其他只读
chown -R admin:eda_group /nfs/eda/ddm
chmod -R 755 /nfs/eda/ddm

# 运行时仓库：DDM 服务账号可写
chown -R ddm_svc:eda_group /nfs/eda/ddm_repo
chmod -R 775 /nfs/eda/ddm_repo

# a0.outgoing 源数据：各用户有读权限（DDM 只读）
# 实际权限取决于你们的 NFS 导出配置
```

### 7.3 SGID 位设置

让 `ready/` 和 `release/` 下新建的文件自动继承目录组：

```bash
chmod g+s /nfs/eda/ddm_repo/ready
chmod g+s /nfs/eda/ddm_repo/release
```

这样即使代码中 `chmod 664`，SGID 位也会确保组权限正确。

---

## 9. 故障排查

### pip install 失败

```bash
# 检查 pip 版本
pip --version

# 使用离线安装方式（见第 3 节）
pip install --no-index --find-links ./packages/ -r requirements.txt

# 跳过有问题的包
grep -v "blake3" requirements.txt | xargs pip install
```

### ImportError: No module named 'blake3'

```bash
# 确认 BLAKE3 降级已生效
python -m ddm check
# 应显示: ! BLAKE3 not available — using BLAKE2b-256 fallback
```

不影响功能，仅性能略有差异。

### SQLite 报错 "database is locked"

```bash
# 检查是否有残留锁文件
ls repository/ready/.lock_*
ls repository/raw/.lock_*

# 如果确认没有进程在运行，手动清理
rm -f repository/ready/.lock_global_release
rm -f repository/raw/.lock_*
```

### 磁盘空间不足

```bash
# 检查仓库目录使用量
du -sh repository/*

# 清理运行时数据
bash clean.sh        # 注意：会清空所有运行时数据！
```

### 日志查看

```bash
# 实时查看
tail -f logs/ddm_$(date +%Y-%m-%d).log

# 搜索特定批次
grep "a1b2c3d4" logs/ddm_*.log

# 搜索错误
grep "ERROR\|WARNING" logs/ddm_*.log
```

---

## 10. 附录：从 macOS 开发到 CentOS 7.9 部署的完整流程

```
┌─────────────────────────────────────────────────────────┐
│  macOS 开发机                                            │
│  1. 编写 / 修改代码                                       │
│  2. git commit                                           │
│  3. docker build -t ddm-builder -f Dockerfile.build .    │
│  4. docker run --rm -v $(pwd):/build ddm-builder         │
│     → 生成 dist/ddm.tar.gz                               │
│  5. scp dist/ddm.tar.gz root@centos7-server:/opt/        │
└─────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────┐
│  CentOS 7.9 离线服务器                                    │
│  1. cd /opt && tar -xzf ddm.tar.gz                       │
│  2. cd ddm_package && cp config.yaml.example config.yaml │
│  3. vi config.yaml  # 设置 outgoing_root 等路径            │
│  4. ./ddm check                                         │
│  5. ./ddm submit -m CPU -t PV_ITER                       │
│  6. ./ddm release -t PV_ITER -A -v V1                    │
└─────────────────────────────────────────────────────────┘
```

### 首次构建 Docker 镜像时间估算

| 步骤 | 耗时 |
|------|------|
| 拉取 centos:7.9.2009 镜像 | ~30s |
| yum 安装编译工具链 | ~2min |
| 编译 Python 3.9.18 | ~5min |
| pip 安装依赖 + PyInstaller | ~2min |
| PyInstaller 打包 DDM | ~1min |
| **总计** | **~10min** |

后续构建利用 Docker 缓存，仅 PyInstaller 打包步骤需重新执行，约 **1 分钟**。

### 构建产物

```
dist/
├── ddm              # 独立二进制（~15-25 MB，含 Python 3.9 + 所有依赖）
└── ddm.tar.gz       # 部署包（二进制 + 配置模板 + 文档）
```
