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
9. [多用户共享部署](#9-多用户共享部署)
10. [故障排查](#10-故障排查)
11. [附录：从开发机到 CentOS 7.9 部署的完整流程](#11-附录从开发机到-centos-79-部署的完整流程)

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

> **注意**: 以下操作均以普通用户身份执行，无需 root 权限。

```csh
# 1. 克隆仓库到用户主目录
git clone <repo_url> ~/ddm
cd ~/ddm

# 2. 安装依赖（--user 安装到 ~/.local/ 下）
pip3 install --user -r requirements.txt

# 3. 编辑配置
vi config/config.yaml

# 4. 验证
python3 -m ddm check
```

---

## 3. 离线部署方案

适用于无法连接公网的服务器。

### 3.1 在有网络的机器上下载离线包

```bash
# 创建工作目录
mkdir ddm_offline && cd ddm_offline

# 下载所有依赖的 wheel 包（包含传递依赖）
pip3 download \
  --platform manylinux2014_x86_64 \
  --python-version 39 \
  --only-binary=:all: \
  -r /path/to/ddm_new/requirements.txt \
  -d ./packages/

# 如果某些包没有预编译 wheel（如 blake3），同时下载源码包
pip3 download \
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
  user@server:~/ddm/

rsync -avz --progress \
  ./packages/ \
  user@server:~/ddm_offline_packages/
```

### 3.3 在离线服务器上安装（非 root 用户）

> **关键**: 离线服务器上你是普通用户，没有 root 权限。所有安装使用 `--user` 标志安装到 `~/.local/` 下。

```csh
# 1. 进入 DDM 目录（假设已 rsync 到 ~/ddm）
cd ~/ddm

# 2. pip 用 --user 安装到用户目录（不写系统路径，不需要 root）
pip3 install --user \
  --no-index \
  --find-links ~/ddm_offline_packages/ \
  -r requirements.txt

# 3. 确认 ~/.local/bin 在 PATH 中（csh 语法）
echo 'set path = ($HOME/.local/bin $path)' >> ~/.cshrc
source ~/.cshrc

# 4. 确认安装成功
python3 -c "import click; import rich; import blake3; print('OK')"

# 5. 如果 blake3 编译失败（缺少 gcc），忽略即可
#    系统会自动使用 hashlib.blake2b 替代
```

### 3.4 处理无编译工具的情况

如果离线服务器没有 `gcc`，`blake3` 的源码包无法编译安装：

```bash
# 在有网络的机器上，下载 blake3 的预编译 wheel
pip3 download blake3==0.3.2 --platform manylinux2014_x86_64 --only-binary=:all: -d ./packages/

# 或在离线服务器上跳过 blake3，让系统使用 BLAKE2b 降级
grep -v "blake3" requirements.txt > requirements_no_blake3.txt
pip3 install --user --no-index --find-links ~/ddm_offline_packages/ -r requirements_no_blake3.txt
```

---

## 4. 二进制打包（CentOS 7.9）

DDM 支持通过 PyInstaller 打包为独立二进制文件，目标服务器无需安装 Python。

> **重要**：PyInstaller 不支持交叉编译。二进制必须在与目标系统相同（或更旧）的 Linux 上构建。以下提供两种方案。

### 方案 A：Docker 构建（任意平台 → CentOS 7.9）

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

```csh
# 在目标服务器上（普通用户）
cd ~
tar -xzf ddm.tar.gz
cd ddm_package
cp config.yaml.example config.yaml
vi config.yaml   # 修改 outgoing_root / repository_root / admins 等
./ddm check
```

### 二进制更新

修改代码后重新构建即可。二进制内已包含 Python 解释器和所有依赖，替换 `ddm` 文件即完成更新：

```csh
# 替换二进制
cp /path/to/new/ddm ~/ddm_package/ddm

# 配置文件和数据不受影响（它们在二进制外部）
```

---

## 5. 配置初始化

### 4.1 最小配置

编辑 `config/config.yaml`：

```yaml
modules:
  CPU:
    owners: [wangshuai, zhangsan]               # module 为主键，谁可以提交
  DDR:
    owners: [lisi, wangshuai]

admins:
  - wangshuai                                    # admin 免检 owner 限制
  - w00949819

outgoing_root: ./a0.outgoing/{user}/{module}   # 源数据目录，{user}{module}自动展开
repository_root: ~/ddm_repo                     # 运行时仓库（建议放在用户主目录下）
log_dir: ~/ddm_repo/logs                        # 日志目录

defaults:
  tag:
    PV_ITER:
      description: 物理验证迭代版
      modules: [CPU, DDR]                       # 该 tag 管理的模块列表
      file_patterns:
        - "{module}.v.gz"
        - "{module}.hier.gds"
        - "{module}.v.pg"
      gates:
        - name: verilog_syntax_check
          command: python3 -m ddm.gates.verilog_check
      release_users:
        - your_release_admin
    # ... 其他 tag 配置
```

### 4.2 关键配置项

| 配置项 | 说明 | 示例 |
|--------|------|------|
| `modules.<NAME>.owners` | 可提交该模块的用户列表（module 为主键），admin 免检 | `[wangshuai, zhangsan]` |
| `admins` | 全局管理员列表，可提交所有模块、发布所有 tag | `[wangshuai, admin2]` |
| `outgoing_root` | a0.outgoing 源数据根目录，支持 `{user}` `{module}` 占位符 | `./a0.outgoing/{user}/{module}` |
| `repository_root` | 运行时仓库根目录 | `~/ddm_repo` |
| `log_dir` | 日志输出目录 | `~/ddm_repo/logs` |
| `defaults.tag.<NAME>.modules` | 该 tag 管理的模块列表（`-A` 发布时检查完整性） | `[CPU, DDR]` |
| `defaults.tag.<NAME>.file_patterns` | 文件匹配模式，`{user}` `{module}` 占位符 | `["{module}.v.gz"]` |
| `defaults.tag.<NAME>.gates` | 门禁脚本列表 | 见上文 |
| `defaults.tag.<NAME>.release_users` | 有权限发布该 tag 的用户列表 | `[user1, user2]` |

### 4.3 多用户共享存储配置

```yaml
# 所有工程师的 a0.outgoing 在共享 NFS 上，{user}/{module} 自动展开为每个人独立目录
outgoing_root: /nfs/eda/a0.outgoing/{user}/{module}

# 仓库目录也在共享存储上，所有用户使用同一份
repository_root: /nfs/eda/ddm_repo
```

### 5.4 csh/tcsh 命令补全

```csh
# 写入 ~/.cshrc
echo 'alias ddm "python3 -m ddm"' >> ~/.cshrc
echo 'source ~/ddm/ddm.complete.csh' >> ~/.cshrc

# 当前终端生效
alias ddm "python3 -m ddm"
source ~/ddm/ddm.complete.csh
```

配置后支持 Tab 补全：
- `ddm <TAB>` → 列出所有子命令
- `ddm submit -t <TAB>` → 自动补全 tag（从 config.yaml 实时读取）

### 5.5 全局软链接部署

在 `/usr/local/bin/` 创建 wrapper 脚本，使 `ddm` 全局可用：

```csh
cat > /opt/ddm/ddm << 'EOF'
#!/bin/csh
cd /opt/ddm && exec python3 -m ddm $argv:q
EOF
chmod +x /opt/ddm/ddm
ln -s /opt/ddm/ddm /usr/local/bin/ddm
```

---

## 6. 验证部署

### 5.1 环境检查

```csh
cd ~/ddm
python3 -m ddm check
```

期望输出全部绿色 ✓。

### 5.2 功能测试

```bash
# 创建测试数据（按 {user}/{module} 目录结构）
mkdir -p a0.outgoing/${USER}/TEST
echo "test data" > a0.outgoing/${USER}/TEST/${USER}_TEST.v.gz
echo "test data" > a0.outgoing/${USER}/TEST/${USER}_TEST.hier.gds

# 临时修改 config.yaml，添加测试 tag
# （或使用现有 tag 和现有文件）

# 提交测试
python3 -m ddm submit -m TEST -t PV_ITER -s "deploy test"

# 查看状态
python3 -m ddm status -m TEST

# 发布
python3 -m ddm release -t PV_ITER -m TEST -v DEPLOY_TEST

# 清理
rm -rf a0.outgoing/${USER}/TEST
bash clean.sh
```

### 5.3 并发锁测试

```bash
# 终端 1：获取模块锁
echo "$$" > repository/raw/.lock_TEST_PV_ITER

# 终端 2：尝试提交（应被拒绝）
python3 -m ddm submit -m TEST -t PV_ITER
# ✗ Submit failed: 模块 TEST/PV_ITER 正在提交

# 终端 1：释放锁
rm repository/raw/.lock_TEST_PV_ITER
```

---

## 7. Git 更新与热更新

### 6.1 更新流程

```csh
cd ~/ddm
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

## 8. 权限与目录规划（非 root 用户）

### 7.1 推荐目录布局

```
~/                                  # 用户主目录
├── ddm/                            # DDM 代码仓库（普通用户可写）
│   ├── config/config.yaml
│   ├── ddm/
│   │   ├── cli.py
│   │   ├── services.py
│   │   └── ...
│   └── a0.outgoing/{user}/{module}/ # 源数据
├── ddm_repo/                       # 运行时仓库
│   ├── raw/<TAG>/<MODULE>/
│   ├── ready/<TAG>/<MODULE>/
│   ├── release/<TAG>/@latest -> VERSION
│   ├── ddm.db
│   └── logs/
└── .local/                         # pip --user 安装路径
    ├── lib/python3.x/site-packages/
    └── bin/
```

### 7.2 目录权限

所有目录由用户自己创建和管理，无需 root：

```csh
# 创建运行时目录
mkdir -p ~/ddm_repo/{raw,ready,release}
mkdir -p ~/ddm_repo/logs

# DDM 代码中的 chmod 664 确保同组用户可读（若共享 NFS 且同组）
# 如果只是个人使用，默认 umask 权限即可
```

### 7.3 多用户共享场景

如果团队需要共享 DDM 仓库和运行时数据（NFS 挂载点）：

- 联系管理员创建共享目录并添加同组成员
- `ready/` 和 `release/` 目录启用 SGID（`chmod g+s`），确保新建文件继承组权限
- DDM 代码在 submit/release 中自动调用 `os.chmod(dest, 0o664)` 设置组可读权限

---

## 9. 多用户共享部署

> **核心问题**: 管理员在共享目录安装了 DDM，其他用户如何直接使用 `python3 -m ddm` 或 `ddm` 命令？

### 前置条件

所有方案均假设：

```
/nfs/eda/shared/                     # 共享存储（NFS 挂载点）
├── ddm/                             # DDM 代码（所有人只读）
│   ├── config/config.yaml           # 唯一一份配置
│   ├── ddm/                         # Python 包
│   │   ├── cli.py
│   │   ├── services.py
│   │   └── ...
│   └── ddm.complete.csh             # csh 补全脚本
├── a0.outgoing/                     # 各用户源数据目录
│   ├── wangshuai/CPU/...
│   ├── lisi/DDR/...
│   └── zhangsan/...
└── ddm_repo/                        # 共享运行时仓库
    ├── raw/
    ├── ready/
    ├── release/
    └── ddm.db
```

配置文件已设为共享路径：

```yaml
# /nfs/eda/shared/ddm/config/config.yaml
outgoing_root: /nfs/eda/shared/a0.outgoing/{user}/{module}
repository_root: /nfs/eda/shared/ddm_repo
log_dir: /nfs/eda/shared/ddm_repo/logs
```

### 方案 A：PYTHONPATH（推荐，零安装）

**核心思路**: 不执行 pip install，直接用环境变量 `PYTHONPATH` 让 Python 找到 `ddm/` 包。所有依赖（`click`、`rich`、`pydantic` 等）已在离线服务器的系统 Python 中预装，不需要额外安装。

管理员执行一次：

```csh
# 将代码 clone 到共享目录
git clone <repo_url> /nfs/eda/shared/ddm
vi /nfs/eda/shared/ddm/config/config.yaml  # 设置共享路径
```

每个用户只需在 `~/.cshrc` 中添加：

```csh
# --- DDM 多用户共享配置 ---
setenv PYTHONPATH /nfs/eda/shared/ddm
alias ddm "python3 -m ddm -c /nfs/eda/shared/ddm/config/config.yaml"
source /nfs/eda/shared/ddm/ddm.complete.csh
```

验证：

```csh
source ~/.cshrc
ddm check
ddm submit -m CPU -t PV_ITER
```

| 优点 | 缺点 |
|------|------|
| 零安装成本 | 升级代码需通知用户 source |
| 不需 pip install | 系统已装依赖的版本要匹配 |
| 适合团队快速推广 | 不适用系统无 Python 依赖的场景 |

### 方案 B：共享虚拟环境 (venv)

**核心思路**: 管理员在共享目录创建一个 Python venv，所有用户 source 同一个 activate 脚本。

管理员执行一次：

```csh
# 创建共享 venv
cd /nfs/eda/shared
python3 -m venv ddm_venv

# 激活后安装 DDM
source /nfs/eda/shared/ddm_venv/bin/activate.csh
pip3 install -e /nfs/eda/shared/ddm
```

每个用户在 `~/.cshrc` 中添加：

```csh
# --- DDM 多用户共享配置 ---
source /nfs/eda/shared/ddm_venv/bin/activate.csh
alias ddm "python3 -m ddm -c /nfs/eda/shared/ddm/config/config.yaml"
source /nfs/eda/shared/ddm/ddm.complete.csh
```

| 优点 | 缺点 |
|------|------|
| 依赖完全隔离于系统 Python | 首次需要管理员创建 |
| pip install -e 后可 import ddm | 二进制依赖需匹配 CPU 架构 |
| 升级只需 git pull | venv 路径变更需更新 .cshrc |

### 方案 C：管理员统一 pip install（全局或用户级）

**核心思路**: pip install 到所有用户可读的位置。不需要每个用户 pip install。

#### C1：管理员有 root 权限

```csh
# root 执行
pip3 install -e /nfs/eda/shared/ddm
# 装在系统 site-packages 下，所有用户可直接用
```

每个用户只需：

```csh
alias ddm "python3 -m ddm -c /nfs/eda/shared/ddm/config/config.yaml"
source /nfs/eda/shared/ddm/ddm.complete.csh
```

#### C2：管理员无 root 权限

```csh
# 管理员执行
pip3 install --user -e /nfs/eda/shared/ddm

# 其他用户需要读 ~/.local/lib/python3.x/site-packages/ddm*.egg-link
# 加上 PYTHONPATH
```

每个用户 `~/.cshrc`：

```csh
setenv PYTHONPATH ${HOME}/.local/lib/python3.9/site-packages:/nfs/eda/shared/ddm
alias ddm "python3 -m ddm -c /nfs/eda/shared/ddm/config/config.yaml"
source /nfs/eda/shared/ddm/ddm.complete.csh
```

| 优点 | 缺点 |
|------|------|
| 最接近标准 pip 流程 | 跨用户 site-packages 权限复杂 |
| `pip list` 可见 | 升级需手动操作 |

### 方案对比

| | A: PYTHONPATH | B: 共享 venv | C: pip install |
|---|---|---|---|
| 安装成本 | 无 | 管理员 2 条命令 | 管理员 1 条命令 |
| 用户配置 | 3 行 .cshrc | 3 行 .cshrc | 2-3 行 .cshrc |
| 依赖隔离 | 依赖系统 Python | 完全隔离 | 部分隔离 |
| 升级方式 | git pull | git pull | pip install -e |
| 推荐场景 | 离线服务器、依赖已装 | 需要独立环境 | 有 root 或单用户 |

> **你们的场景推荐方案 A**：依赖已在离线服务器上预装，只需让所有人找到 `ddm/` 代码即可，零安装成本。

---

## 10. 故障排查

### pip install 失败

```csh
# 检查 pip 版本
pip3 --version

# 非 root 用 --user（见第 3.3 节）
pip3 install --user --no-index --find-links ~/ddm_offline_packages/ -r requirements.txt

# 跳过有问题的包
grep -v "blake3" requirements.txt | xargs pip3 install --user
```

### ImportError: No module named 'blake3'

```bash
# 确认 BLAKE3 降级已生效
python3 -m ddm check
# 应显示: ! BLAKE3 not available — using BLAKE2b-256 fallback
```

不影响功能，仅性能略有差异。

### SQLite 报错 "database is locked"

```csh
# 检查是否有残留锁文件
ls ~/ddm/repository/ready/.lock_*
ls ~/ddm/repository/raw/.lock_*

# 如果确认没有进程在运行，手动清理
rm -f repository/ready/.lock_global_release
rm -f repository/raw/.lock_*
```

### 磁盘空间不足

```csh
# 检查仓库目录使用量
du -sh ~/ddm/repository/*

# 清理运行时数据
bash clean.sh        # 注意：会清空所有运行时数据！
```

### 日志查看

```csh
# 实时查看
tail -f ~/ddm/logs/ddm_*.log

# 搜索特定批次
grep "a1b2c3d4" ~/ddm/logs/ddm_*.log

# 搜索错误
grep "ERROR\|WARNING" ~/ddm/logs/ddm_*.log
```

---

## 11. 附录：从开发机到 CentOS 7.9 部署的完整流程

```
┌─────────────────────────────────────────────────────────┐
│  Linux 开发机 (或 Docker)                                 │
│  1. 编写 / 修改代码                                       │
│  2. git commit                                           │
│  3. docker build -t ddm-builder -f Dockerfile.build .    │
│  4. docker run --rm -v $(pwd):/build ddm-builder         │
│     → 生成 dist/ddm.tar.gz                               │
│  5. scp dist/ddm.tar.gz user@centos7-server:~/             │
└─────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────┐
│  CentOS 7.9 离线服务器（普通用户）                           │
│  1. cd ~ && tar -xzf ddm.tar.gz                          │
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
