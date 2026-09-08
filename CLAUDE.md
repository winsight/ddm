# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

DDM(Data Delivery Manager)是 EDA 后端 PV/PI 数据交付流程管理系统:驱动芯片数据从工程师私有源目录,经门禁校验、完整性检查,最终发布到共享归档目录。多用户共享存储(常为 NFS)+ tcsh 环境。代码以中文注释/报错为主。

> 仓库里 README.md 是**最初的产品需求稿**,`docs/` 是配套说明;部分 docs(如 `-v` 自动拼日期、`ready/.lock_global_release`)已与当前代码脱节。**代码为准**,改动前先读 `ddm/services.py` + `ddm/cli.py`。

## 常用命令

入口:安装后 console script 是 `ddm`;也可 `python3 -m ddm`(包内 `ddm/__main__.py` → `cli._cli_entry()`)。仓库未被 `pip install -e` 时,直接 `python3 -m ddm` 需 `PYTHONPATH` 指向仓库根。

- 模块 owner:`ddm submit -m CPU -t PV_ITER -s "备注"` | `ddm status -m CPU [-d 24h]` | `ddm version`
- tag 专项(admin / 该 tag 的 release_users):`ddm release -t PV_ITER -m CPU -v V2` | `-A`(全量,缺模块报错)| `-A --inherit` | `-A --force`(覆盖已存在版本)| `ddm list -t PV_ITER [-A|-m CPU|-v 详细]` | `ddm check`
- CLI 入口按角色隐藏命令:普通用户只见 `submit/status/version`;`release/list/check` 仅 tag admin 可见(见 `RoleBasedGroup`)。判断依据= 全局 admins + 各 tag `release_users`(`config.py` `_is_tag_admin`)。

测试(必须在仓库根目录执行,测试读的是真实 `config/config.yaml` 与 `a0.outgoing/`):

- `python3 -m pytest tests/test_services.py -q`(config/storage/classify/release 结构等,不碰真实数据)
- `bash tests/test_locks.sh`:**非隔离**——用 config 里 `repository_root`/`outgoing_root` 做真实 submit/release 与锁竞争,会写脏真实运行时 DB 与 ready/raw。想安全跑需临时换一个草稿 config(改 `repository_root`/`outgoing_root`)。

构建/部署(见"部署模型"):`bash build_offline.sh` 在有网机打离线包;目标机 `install.sh` 一次、每人 `setup_user.sh` 一次;滚动更新/回退 `ddm_update.csh`。`python3 ddm_web.py` 是**纯 stdlib** 的只读 DB 浏览器(直接读 `ddm.db`,与 ddm 包零耦合)。`python3 sync_owners.py <PDSSetup.tcl> [config.yaml]` 从 Tcl 合并模块 owner。

## 架构分层

改任何一处都要保持三层解耦:

- **交互层** `ddm/cli.py`:Click 命令路由 + Rich 表格/进度条;不做业务。内含 argcomplete(tcsh 用)与隐藏补全子命令 `__complete_{commands,tags,modules,versions}`(tcsh 补全数据源,新开终端会各执行一次,输出必须纯净)。`_setup_signal_handling()` 对非交互进程忽略 SIGINT/SIGHUP。
- **业务层** `ddm/services.py`:submit/release 全流水线、`_acquire_lock/_release_lock`、`streaming_copy`(边拷边算 BLAKE3 + fsync + `os.utime` 保留 mtime)、`compare_metadata`、`find_source_files`、release 的 staging 三阶段与继承。
- **持久层** `ddm/storage.py`:纯 SQL,`batches/files/events` 三表,无业务逻辑。每个写事务都过 **DB 级文件锁**(`<db_path>.lock`,O_EXCL)。NFS 上 `journal_mode=DELETE` + `synchronous=NORMAL`,本地 `WAL`。`Storage()` 会按 `shared_group` 修正 DB 文件/目录属主权限。
- **配置层** `ddm/config.py`:YAML → pydantic(`AppConfig`)。`{user}`/`{module}` 占位符、多 `outgoing_root`、tag 三选一模块选择(`modules` / 缺省全模块 / `exclude_modules`)。相对路径相对配置文件所在的项目根解析(非 cwd)。
- **门禁层** `ddm/gates/runner.py` + `ddm/gates/*`:黑盒子进程调用,零业务耦合。

## 数据流与状态机(核心大图)

文件/状态在 4 个目录间迁移,`repository_root` 下的运行时目录 + SQLite + logs 均不入 Git:

```
a0.outgoing/{user}/{module}/  (只读源,平铺,文件以 {module}.ext 命名)
   │  ddm submit  → find_source_files() 按 file_patterns 匹配
   ▼
raw/{TAG}/{MODULE}/           临时暂存;streaming_copy(边拷边 BLAKE3,保留 mtime)
   │  pre_check(a0 vs raw) → run_gates() 子进程门禁 → os.replace 原子移入 ready + chmod 664
   ▼
ready/{TAG}/{MODULE}/         就绪临界区;post_check#1(a0/DB vs ready);批量状态→SUBMITTED
   │  ddm release → 反脑裂检查(ready 物理文件存在)→ 授权/版本检查
   │  staging 三阶段(见下)→ commit
   ▼
release/{TAG}/{VERSION}/{verilog,gds,pg,...}/   文件按 file_groups 分组,文件名自带 {module}. 前缀
   latest → 软链到最新 VERSION(tcsh 兼容,不用 @latest)
```

- 状态机:`batches.status` ∈ PENDING→SUBMITTED→RELEASED / SUPERSEDED / FAILED;每次流转写 `events`(事件类型常量集中在 `storage.py`)。同 module+tag 再 submit 会把旧 SUBMITTED 标记 SUPERSEDED。
- 版本号 **按 `-v` 原样** 作目录名(CLI `required=True`,不再拼日期)。
- release 语义:
  - `-m MODULE`:发布该模块,**其余模块自动从 latest 继承**(累积版本);
  - `-A`:要求 tag 配置的全部模块都已 SUBMITTED,缺则报错;
  - `-A --inherit`:允许缺失模块从 latest 继承;
  - `-A --force`:整体原子替换已存在版本(先 rename 走旧目录再换入,避免残留)。
  - staging 目录 `.staging_{version}_{rand}`:Pass1 收集新文件 → 继承 latest 中未更新模块 → Pass2 对比上一版本同名文件 size(±30% 记 size_change、±50% 记 size_anomaly)→ Pass3 post_check(ready vs staging)→ 全部通过才 rename/merge 提交,否则 rmtree staging。

## 配置驱动 + 插件(新增流程零代码)

`config/config.yaml` 是唯一控制面(部署包只带 `config.yaml.example`,绝不覆盖线上 config):

- `modules`(module→owners)+ `admins`:提交权限,admin 绕过 owner 校验;所有相关用户必须在 `shared_group`。
- `defaults.tag.*`:description / modules / exclude_modules / file_patterns(`{module}.v.gz`…)/ gates / release_users。
- `file_groups`:verilog/gds/pg → 后缀列表,决定 release 分组子目录;`classify_file()` 先剥 module 前缀再按声明顺序取 endswith 命中,未命中放版本根目录。
- 新增 tag、改 file_patterns、增 gate、换模块名单 → 只改 YAML。提交用户必须属于 `shared_group`(submit 与 `check` 都强制)。

## 并发与权限(多用户 NFS 场景的硬约束)

- 模块互斥锁 `raw/.lock_{MODULE}_{TAG}`(submit 持有);tag 发布锁 `ready/.lock_release_{TAG}`(release 持有)。两者均 `os.open(O_CREAT|O_EXCL)` 原子创建,内容含 pid/user/time。锁是 **tag 维度**:不同 tag 的 submit/release 互不阻塞。
- 过期锁自动清理:submit 抢锁时按 PID 是否存活 + 超过 `stale_lock_minutes` 判定(pid 已死立即清;pid 活着但超时也清但告警"若原任务仍在跑请 Ctrl+C")。`ddm check` 只报告不清理。
- 权限:目录 SGID `0o2775`、文件 `0o664`,归 `shared_group`;chown/chmod 用 try/except(可能非属主)。`storage.py` 的 DB 写锁有 30s 超时,超时提示手动删 `<db_path>.lock`。
- NFS 陷阱:`db_path` 强烈建议设本地路径(config 里注释)。`streaming_copy` 拷贝后 fsync,保证落盘后才记 DB。release 用**硬链接**(ready→release,NFS 上 O(1));硬链接场景**不要 chmod 目标文件**(同 inode 会改到 ready 原件);Pass3 对同 inode 文件跳过重复哈希。BLAKE3 缺失自动回退 BLAKE2b-256(会告警,正式环境应装)。

## Gate 插件协议

runner 对每个 gate 执行 `command.split() + [raw_dir, module, tag]`;**command 以 python/python3 开头时替换为 `sys.executable`**(保证用当前 venv 解释器),否则走 shebang/PATH。exit 0=过、非 0=拒;执行后可选 flag 文件覆盖判定:`raw_dir/.ddm_gate_<name>`(首行 PASS/FAIL)或 `.ddm_gate_<name>.json`(`{"status":"pass|fail","reason":...}`,用于异步/LSF 提交场景)。gates 按配置串行、任一失败即短路整批 submit,`on_fail` 字段给出给用户的提示。详见 `docs/GATE_PLUGIN.md`。

## 部署模型 / GitOps 热更新

生产是 `venv --system-site-packages` + 软链版本目录:

- `ddm_update.csh`(csh,离线服务器):把 tar.gz 解到 `<parent>/.ddm/releases/<ver>`(同级,方便回退),备份并回填 config.yaml,`ln -sfn` 原子切换 `~/ddm`→新版本,`--rollback/--list/--clean N`。**别名指向稳定的 `~/ddm`,只换链接目标**,故长任务(git 更新/部署)不打断运行中 submit。
- 用户 `.cshrc` 里 `alias ddm '<STABLE_ROOT>/venv/bin/python3 -m ddm'`,不 source activate;补全 `source <STABLE_ROOT>/ddm.complete.csh`(tcsh 6.20 起不依赖 argcomplete)。
- 迭代版本号只改 `ddm/version.py`(`__version__` + `__changelog__`,setup.py 自动读)。

## 易踩的坑

- 补全子命令会被终端频繁执行:任何改动不得让 `ddm __complete_*` 输出日志/告警(只输出纯数据)。历史上两次回归(`Config loaded`、`_setup_signal_handling` 的 DEBUG 行)都来自未在 `_cli_entry()` 早期压掉 loguru 默认 sink——补全检测在 `_cli_entry` 用 `sys.argv` 前缀 `__complete` 或 `_ARGCOMPLETE` env 判断。
- 改 Python 代码遵循仓库自带 skill `/.claude/skills/python/SKILL.md`(导入置顶、多参强制关键字等)。
- 单测依赖真实 `config/config.yaml` 内容(tag/owner 断言),改动 config 后先跑 `pytest` 确认不破坏断言。
- tcsh 环境里文件名避免 `@`(`@latest` 已改名 `latest`)。
