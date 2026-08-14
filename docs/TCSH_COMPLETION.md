# tcsh Tab 补全方案：芯片后端 submit.py

## 环境约束

| 项目 | 说明 |
|------|------|
| Shell | tcsh 6.20（不支持运行时命令补全 `{cmd}` / `$(cmd)`） |
| Python | 3.7+，`argparse` stdlib |
| 场景 | SSH 远程 CentOS 7.9 服务器 |

---

## 两套方案对比

| 维度 | 方案 A（纯 tcsh） | 方案 B（argcomplete） |
|------|-------------------|----------------------|
| 依赖 | 无 | `pip install argcomplete` |
| 补全规则维护 | 手动写 csh 脚本 | argparse 自动生成 |
| 动态目录扫描 | 写死一个目录 | Python 函数动态返回 |
| 跨 shell 移植 | 只支持 csh/tcsh | 同时支持 bash/zsh/fish |
| tcsh 局限影响 | 需手动规避 | argcomplete 内部处理 |
| 新增子命令 | 改 2 处（Python + csh） | 只改 argparse |

---

## 方案 A：纯原生 tcsh complete 补全脚本

### submit.py（核心 Python 脚本）

```python
#!/usr/bin/env python3
"""EDA chip backend data delivery tool — submit.py"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _list_release_versions() -> list[str]:
    """Scan ./release/ for version directories. Used by tcsh at source time."""
    release_dir = Path("./release")
    if not release_dir.is_dir():
        return []
    return sorted(
        d.name for d in release_dir.iterdir()
        if d.is_dir() and not d.name.startswith(".") and d.name != "latest"
    )


def cmd_release(args):
    print(f"release version={args.version} type={args.type}")


def cmd_upload(args):
    print(f"upload file={args.file} type={args.type}")


def cmd_list(args):
    print("list ...")


def cmd_delete(args):
    print(f"delete version={args.version}")


def main():
    parser = argparse.ArgumentParser(prog="submit.py")
    sub = parser.add_subparsers(dest="command")

    # ---- release ----
    p_rel = sub.add_parser("release", help="发布版本")
    p_rel.add_argument("version", help="版本名 (./release/ 下目录)")
    p_rel.add_argument("--type", choices=["gds", "spef", "def", "v", "all"],
                       default="all", help="文件类型")

    # ---- upload ----
    p_up = sub.add_parser("upload", help="上传文件")
    p_up.add_argument("file", help="文件路径")
    p_up.add_argument("--type", choices=["gds", "spef", "def", "v", "all"],
                      default="all", help="文件类型")

    # ---- list ----
    p_ls = sub.add_parser("list", help="列出数据")

    # ---- delete ----
    p_del = sub.add_parser("delete", help="删除版本")
    p_del.add_argument("version", help="版本名 (./release/ 下目录)")

    # ---- hidden: completion helpers (供 tcsh complete 脚本调用) ----
    p_comp_cmds = sub.add_parser("__complete_commands",
                                  help=argparse.SUPPRESS)
    p_comp_vers = sub.add_parser("__complete_versions",
                                  help=argparse.SUPPRESS)

    args = parser.parse_args()

    # Handle hidden completion commands
    if args.command == "__complete_commands":
        print("release upload list delete")
        return
    if args.command == "__complete_versions":
        versions = _list_release_versions()
        print(" ".join(versions) if versions else "")
        return

    # Route to subcommand
    handlers = {
        "release": cmd_release,
        "upload": cmd_upload,
        "list": cmd_list,
        "delete": cmd_delete,
    }
    if args.command in handlers:
        handlers[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
```

### submit.complete.csh（tcsh 补全脚本）

```csh
#=========================================================================
# submit.py tcsh tab completion (方案 A — 纯原生)
#
# 设计原理（tcsh 6.20 限制）：
#   - 双引号 "n/-t/(...)/" → source 时变量展开，值嵌入 complete 规则
#   - 单引号 'n/-*/.../' → 字面存储，Tab 时使用
#   - n/-*  catch-all：任何以 - 开头的词 → 显示所有 flag
#   - n/pattern/ 前缀匹配：更具体的 pattern 优先于通配符
#   - p/1/ 位置匹配：优先级最高，匹配位置 1 的词
#
# 已知限制（tcsh 6.20）：
#   从变量列表补全后，再输入 - Tab 可能不弹 flag（tcsh 边界行为）
#
# 用法：
#   alias submit 'python3 /path/to/submit.py'
#   source /path/to/submit.complete.csh
#=========================================================================

# ---- 启动时运行一次（双引号确保变量展开） ----
set _submit_cmds = `python3 /path/to/submit.py __complete_commands`
set _submit_vers = `python3 /path/to/submit.py __complete_versions`

complete submit \
  'p/1/($_submit_cmds)/' \
  \
  'n/--type/(gds spef def v all)/' \
  \
  'n/*/f:*.gds.gz/' \
  'n/*/f:*.hier.gds/' \
  'n/*/f:*.v.gz/' \
  'n/*/f:*.v.pg/' \
  'n/*/f:*.spef.gz/' \
  'n/*/f:*.def.gz/' \
  \
  "n/-v/($_submit_vers)/" \
  \
  'n/-*/(-h --help --type)/'

# ---- 手动刷新版本列表 ----
alias refresh_submit_complete 'set _submit_vers = `python3 /path/to/submit.py __complete_versions`'
```

### ~/.cshrc 配置

```csh
# ---- submit.py tab 补全 ----
alias submit 'python3 /home/$USER/scripts/submit.py'
set path_to_submit = /home/$USER/scripts
source $path_to_submit/submit.complete.csh

# 每次登录时刷新版本列表
set _submit_vers = `submit __complete_versions`
```

### tcsh 补全规则详解

```
complete submit \
  'p/1/(release upload list delete)/'    ← 位置1：子命令
  'n/--type/(gds spef def v all)/'       ← 前缀 --type：枚举值
  'n/*/f:*.gds.gz/'                      ← 任意位置：*.gds.gz 文件
  'n/*/f:*.v.gz/'                        ← 任意位置：*.v.gz 文件
  "n/-v/($_submit_vers)/"                ← 动态版本列表（source 时展开）
  'n/-*/(-h --help --type)/'             ← 任意 - 开头 → 显示 flag 列表
```

| 规则 | 格式 | 说明 |
|------|------|------|
| `p/1/` | 位置匹配 | 第 1 个参数补全子命令，优先级最高 |
| `n/--type/` | 前缀匹配 | 输入 `--type ` 后补全枚举值 |
| `n/*/f:` | 文件匹配 | `*` 通配任意位置，`.gds.gz` 等多点后缀正常匹配 |
| `"n/-v/..."` | 双引号变量展开 | source 时 `$_submit_vers` 被替换为实际版本号列表 |
| `'n/-*/'` | catch-all | 输入 `-` 后补全所有 flag，单引号防止 $ 展开 |

---

## 方案 B：argcomplete 生成 tcsh 补全配置

### 安装

```bash
pip3 install --user argcomplete
```

### submit_argcomplete.py

```python
#!/usr/bin/env python3
"""EDA chip backend data delivery tool — submit.py (argcomplete 版本)"""
# PYTHON_ARGCOMPLETE_OK
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# ---- argcomplete 初始化 ----
try:
    import argcomplete
    HAS_ARGCOMPLETE = True
except ImportError:
    HAS_ARGCOMPLETE = False


# ======================================================================
# 动态补全器（argcomplete 会在每次 Tab 时调用这些函数）
# ======================================================================

def _complete_release_versions(**kwargs):
    """Tab 时实时扫描 ./release/ 目录"""
    release_dir = Path("./release")
    if not release_dir.is_dir():
        return []
    return sorted(
        d.name for d in release_dir.iterdir()
        if d.is_dir() and not d.name.startswith(".") and d.name != "latest"
    )


def _complete_file_paths(prefix, **kwargs):
    """本地文件路径补全，兼容 .hier.gds.gz 等多点后缀"""
    import glob

    pattern = prefix + "*" if prefix else "*"
    matches = glob.glob(pattern)
    # 同时匹配多后缀变体
    if prefix:
        # 如果用户已输入部分路径，用 ** 递归搜索
        matches += glob.glob(prefix + "**/*", recursive=True)

    results = []
    for m in matches:
        if os.path.isfile(m):
            results.append(m)
        elif os.path.isdir(m):
            results.append(m + "/")
    return results


def _complete_type_enum(**kwargs):
    """--type 枚举值"""
    return ["gds", "spef", "def", "v", "all"]


# ======================================================================
# 子命令处理函数
# ======================================================================

def cmd_release(args):
    print(f"release version={args.version} type={args.type}")


def cmd_upload(args):
    print(f"upload file={args.file} type={args.type}")


def cmd_list(args):
    print("list ...")


def cmd_delete(args):
    print(f"delete version={args.version}")


# ======================================================================
# argparse 定义（所有补全规则在此集中管理）
# ======================================================================

def build_parser():
    parser = argparse.ArgumentParser(prog="submit.py")

    sub = parser.add_subparsers(dest="command")
    sub.required = True

    # ---- release ----
    p_rel = sub.add_parser("release", help="发布版本")
    p_rel.add_argument("version", help="版本名").completer = _complete_release_versions
    p_rel.add_argument("--type", choices=["gds", "spef", "def", "v", "all"],
                       default="all", help="文件类型").completer = _complete_type_enum

    # ---- upload ----
    p_up = sub.add_parser("upload", help="上传文件")
    p_up.add_argument("file", help="文件路径").completer = _complete_file_paths
    p_up.add_argument("--type", choices=["gds", "spef", "def", "v", "all"],
                      default="all", help="文件类型").completer = _complete_type_enum

    # ---- list ----
    sub.add_parser("list", help="列出数据")

    # ---- delete ----
    p_del = sub.add_parser("delete", help="删除版本")
    p_del.add_argument("version", help="版本名").completer = _complete_release_versions

    return parser


def main():
    parser = build_parser()

    if HAS_ARGCOMPLETE:
        argcomplete.autocomplete(parser)

    args = parser.parse_args()

    handlers = {
        "release": cmd_release,
        "upload": cmd_upload,
        "list": cmd_list,
        "delete": cmd_delete,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
```

### 生成 tcsh 补全脚本

```bash
# 方法 1: 直接注册到 ~/.cshrc（推荐）
echo 'eval "`register-python-argcomplete --shell tcsh submit_argcomplete.py`"' >> ~/.cshrc

# 方法 2: 生成独立脚本文件
register-python-argcomplete --shell tcsh submit_argcomplete.py > ~/submit.argcomplete.csh
echo 'source ~/submit.argcomplete.csh' >> ~/.cshrc
```

### argcomplete 生成的 tcsh 补全脚本（示例）

`register-python-argcomplete --shell tcsh submit_argcomplete.py` 输出类似：

```csh
complete submit_argcomplete.py \
  'p@1@release upload list delete@' \
  'p@2@`python3 -c "
import argcomplete
argcomplete.autocomplete(argparse.ArgumentParser())
" 2>/dev/null`@'
```

> **注意**：argcomplete 在 tcsh 下会尝试用反引号做运行时补全，这在 tcsh 6.20 中**不可用**（返回 "Invalid completion"）。argcomplete 3.x 对 tcsh 的支持实验性且有此限制。

---

## SSH 远程环境测试

### 1. 验证 Shell 版本

```bash
ssh user@server 'echo $SHELL; tcsh --version'
```

### 2. 测试方案 A（纯 tcsh）

```bash
# 登录后
source ~/submit.complete.csh

# 测试
tcsh -c 'source ~/submit.complete.csh; complete submit'
# 应显示所有 complete 规则

tcsh -c 'source ~/submit.complete.csh; complete submit | grep p/1'
# 应显示子命令列表
```

### 3. 交互式测试

```csh
# 登录到 tcsh，逐项测试
submit <TAB>              # 应显示: release upload list delete
submit release <TAB>      # 应显示: 文件列表或版本列表
submit upload ./<TAB>     # 应显示: *.gds.gz *.v.gz 等
submit delete -v <TAB>    # 应显示: release/ 下所有版本
submit release --<TAB>    # 应显示: --type --help
```

---

## 常见补全失效排错

| 症状 | 原因 | 解决 |
|------|------|------|
| `complete: command not found` | 未 source 补全脚本 | `source ~/submit.complete.csh` |
| Tab 后无声无息，无任何输出 | 变量未展开 | 检查 `_submit_vers` 是否为空，手动运行 `submit __complete_versions` |
| `-v` 后补全列表为空 | `./release/` 目录不存在或无子目录 | `mkdir -p ./release/V1_test` 创建测试数据 |
| Tab 显示文件列表而非 flag | `n/-*` 规则未生效 | 确认是 `-` 开头（不是空字符），检查其他 `n/` 规则是否覆盖 |
| source 时报 "Variable name must contain..." | 变量名含非法字符 | 变量名用 `[a-zA-Z0-9_]` |
| alias 不生效 | csh 需要 `alias cmd "command"` 双引号包裹 | `alias submit 'python3 /path/to/submit.py'` |
| argcomplete 方案 Tab 显示 `{cmd}` 原始字符 | tcsh 6.20 不支持运行时命令补全 | 换方案 A |
| `.gds.gz` 文件 Tab 不出来 | tcsh 的 `f:` 匹配以 suffix 为准 | 确保 `n/*/f:*.gds.gz/` 规则存在 |

### 快速诊断脚本

```csh
#!/bin/csh -f
# 保存为 diag_complete.csh，source 后即可诊断

echo "=== tcsh version ==="
echo $version

echo ""
echo "=== complete rules ==="
complete submit

echo ""
echo "=== _submit_vers ==="
echo $_submit_vers

echo ""
echo "=== release/ dir ==="
ls -d ./release/*/  2>/dev/null || echo "(not found)"

echo ""
echo "=== which submit ==="
which submit
```

---

## 方案 B 对 tcsh 的影响分析

**方案 B 的 argcomplete 代码加进 Python 脚本后，完全不影响现有功能：**

```
submit.py release V1 --type gds    ← 正常运行，与 argcomplete 无关
submit.py __complete_versions       ← 正常运行，供 tcsh 补全脚本调用
```

原因：

| 层面 | 说明 |
|------|------|
| Python 运行时 | `try/except ImportError` 包裹，没装 argcomplete 就跳过，零副作用 |
| `.completer` 属性 | 只是挂在 argparse Action 对象上的普通 Python 属性，argparse 自身不读它 |
| tcsh 补全 | argcomplete 输出的 tcsh 规则在当前版本不可用，但**不影响方案 A 的补全脚本同时生效** |
| 多 shell 场景 | 同一份 `submit.py`，bash/zsh 用户 source argcomplete 配置立即可用 |

**结论：方案 A + 方案 B 可以共存于同一份 `submit.py`，互不干扰。**

---

## 完整 submit.py（方案 A + B 合一）

> 单文件同时支持 tcsh 原生补全（方案 A）和 argcomplete 补全（方案 B，供 bash/zsh 用户使用）。

```python
#!/usr/bin/env python3
"""EDA chip backend data delivery tool — submit.py

Tab completion:
  tcsh  → source submit.complete.csh （方案 A，纯原生，无依赖）
  bash  → eval "$(register-python-argcomplete submit.py)" （方案 B）
  zsh   → 同上（需在 ~/.zshrc 中注册）

所有补全规则在 argparse 定义处集中管理，方案 A 的 csh 脚本通过
隐藏子命令 __complete_* 获取动态数据。
"""
# PYTHON_ARGCOMPLETE_OK  ← 标记：argcomplete 可安全激活
from __future__ import annotations

import argparse
import glob as glob_mod
import os
import sys
from pathlib import Path

# ---- argcomplete（可选依赖） ----
try:
    import argcomplete
    HAS_ARGCOMPLETE = True
except ImportError:
    HAS_ARGCOMPLETE = False


# ======================================================================
# 动态补全数据源（方案 A 和 B 共用）
# ======================================================================

def list_release_versions() -> list[str]:
    """扫描 ./release/ 下所有版本目录（忽略隐藏文件和 latest 软链接）"""
    release_dir = Path("./release")
    if not release_dir.is_dir():
        return []
    return sorted(
        d.name for d in release_dir.iterdir()
        if d.is_dir() and not d.name.startswith(".") and d.name != "latest"
    )


TYPE_CHOICES = ["gds", "spef", "def", "v", "all"]


# ======================================================================
# argcomplete Completer 函数（仅方案 B 调用，方案 A 不受影响）
# ======================================================================

def _completer_versions(**kwargs):
    return list_release_versions()


def _completer_files(prefix, **kwargs):
    """文件路径补全，兼容 .hier.gds.gz 等多点后缀"""
    pattern = prefix + "*" if prefix else "*"
    results = []
    for m in glob_mod.glob(pattern):
        if os.path.isfile(m):
            results.append(m)
        elif os.path.isdir(m):
            results.append(m + "/")
    # 递归匹配（用户已输入目录前缀时）
    if prefix and os.path.isdir(prefix):
        for m in glob_mod.glob(prefix.rstrip("/") + "/*"):
            if os.path.isfile(m):
                results.append(m)
    return results


def _completer_type_enum(**kwargs):
    return list(TYPE_CHOICES)


# ======================================================================
# 子命令实现
# ======================================================================

def cmd_release(args):
    print(f"[release] version={args.version} type={args.type}")


def cmd_upload(args):
    print(f"[upload] file={args.file} type={args.type}")


def cmd_list(args):
    print("[list] 列出所有版本...")


def cmd_delete(args):
    print(f"[delete] version={args.version}")


# ======================================================================
# argparse 构建（方案 A 和 B 的补全规则统一在此定义）
# ======================================================================

def build_parser():
    parser = argparse.ArgumentParser(prog="submit.py")

    sub = parser.add_subparsers(dest="command")
    sub.required = True

    # ---- release ----
    p_rel = sub.add_parser("release", help="发布版本")
    p_rel.add_argument("version", help="版本名 (./release/ 下目录)") \
        .completer = _completer_versions   # argcomplete 专用属性
    p_rel.add_argument("--type", choices=TYPE_CHOICES, default="all",
                       help=f"文件类型 ({', '.join(TYPE_CHOICES)})") \
        .completer = _completer_type_enum

    # ---- upload ----
    p_up = sub.add_parser("upload", help="上传文件")
    p_up.add_argument("file", help="文件路径") \
        .completer = _completer_files
    p_up.add_argument("--type", choices=TYPE_CHOICES, default="all",
                      help=f"文件类型 ({', '.join(TYPE_CHOICES)})") \
        .completer = _completer_type_enum

    # ---- list ----
    sub.add_parser("list", help="列出数据")

    # ---- delete ----
    p_del = sub.add_parser("delete", help="删除版本")
    p_del.add_argument("version", help="版本名 (./release/ 下目录)") \
        .completer = _completer_versions

    # ---- 隐藏子命令：供方案 A 的 tcsh complete 脚本调用 ----
    sub.add_parser("__complete_commands", help=argparse.SUPPRESS)
    sub.add_parser("__complete_versions", help=argparse.SUPPRESS)

    return parser


# ======================================================================
# 入口
# ======================================================================

def main():
    parser = build_parser()

    # 方案 B：argcomplete 激活（仅在 Shell 补全上下文中触发）
    if HAS_ARGCOMPLETE:
        argcomplete.autocomplete(parser)

    args = parser.parse_args()

    # 方案 A：tcsh 隐藏补全命令
    if args.command == "__complete_commands":
        print("release upload list delete")
        return
    if args.command == "__complete_versions":
        versions = list_release_versions()
        print(" ".join(versions) if versions else "")
        return

    # 路由
    handlers = {
        "release": cmd_release,
        "upload": cmd_upload,
        "list": cmd_list,
        "delete": cmd_delete,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
```

### submit.complete.csh（方案 A 补全脚本）

```csh
#=========================================================================
# submit.py tcsh tab completion — 方案 A（纯原生，零依赖）
#
# 设计：
#   - p/1  = 子命令列表
#   - n/-v = 版本值补全（source 时双引号展开 $_submit_vers）
#   - n/-*  = catch-all: 任何 - 开头 → flag 列表
#   - n/*/f = 文件补全，支持 .gds.gz .v.gz 等多点后缀
#
# 已知限制（tcsh 6.20）:
#   从 n/-v 选值后再输入 - Tab 可能不弹 flag（tcsh 边界行为）
#
# 用法:
#   alias submit 'python3 /path/to/submit.py'
#   source /path/to/submit.complete.csh
#
# 配置变更后刷新:
#   source ~/.cshrc  或  refresh_submit_complete
#=========================================================================

set _submit_cmds = `python3 /path/to/submit.py __complete_commands`
set _submit_vers = `python3 /path/to/submit.py __complete_versions`

complete submit \
  'p/1/($_submit_cmds)/' \
  'n/--type/(gds spef def v all)/' \
  "n/-v/($_submit_vers)/" \
  'n/*/f:*.gds.gz/' \
  'n/*/f:*.hier.gds/' \
  'n/*/f:*.v.gz/' \
  'n/*/f:*.v.pg/' \
  'n/*/f:*.spef.gz/' \
  'n/*/f:*.def.gz/' \
  'n/*/f:*.def/' \
  'n/-*/(-h --help --type)/'

alias refresh_submit_complete \
  'set _submit_vers = `python3 /path/to/submit.py __complete_versions`'
```

### ~/.cshrc 配置（方案 A + B 同时生效）

```csh
# ---- submit.py tab 补全 ----
alias submit 'python3 /home/$USER/scripts/submit.py'

# 方案 A：tcsh 原生补全（主要，立即可用）
if (-f /home/$USER/scripts/submit.complete.csh) then
    source /home/$USER/scripts/submit.complete.csh
endif

# 方案 B：argcomplete tcsh 配置（实验性，tcsh 6.20 暂不可用）
# 安装 argcomplete 后取消下面注释即可在支持的 shell 中使用：
#   pip3 install --user argcomplete
#   eval "`register-python-argcomplete --shell tcsh submit.py`"

# 每次登录刷新动态数据
alias refresh_submit_complete \
  'set _submit_vers = `submit __complete_versions`'
```

---

## 推荐策略

**两份方案共存，按环境自动切换：**

```
同一份 submit.py
    │
    ├── tcsh 6.20 (SSH 服务器)
    │   └── source submit.complete.csh  → 方案 A 生效
    │       complete 规则使用 source 时展开的变量
    │
    ├── bash / zsh (开发机)
    │   └── eval "$(register-python-argcomplete submit.py)" → 方案 B 生效
    │
    └── tcsh 6.22+ (未来升级后)
        └── 方案 B 的 argcomplete tcsh 输出也可用
```

| | 方案 A | 方案 B |
|---|---|---|
| tcsh 6.20 | **✓ 稳定** | ✗ 不可用 |
| bash/zsh | ✗ | ✓ |
| 依赖 | 无 | `pip install argcomplete` |
| 维护成本 | csh 脚本需手动同步 | argparse 改完自动生效 |
| 新增子命令 | 改 2 处（Python + csh） | 只改 argparse |

日常用方案 A，argcomplete hooks 留在代码里等未来环境升级后自动切换。
