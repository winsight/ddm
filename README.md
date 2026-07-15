# Role: 资深后端架构师 / EDA 系统开发专家

## Task Objective

你的任务是设计并开发一套 **EDA（电子设计自动化）后端 PV/PI/...数据交付流程管理系统**。该系统用于管理芯片制造中的 PV（工艺验证）和 PI（工艺改进）数据流。
请根据以下详细的系统架构、目录规范、CLI 接口需求和状态机逻辑，提供整体技术实现方案及核心代码框架。

请严格遵循以下指定的技术栈、系统架构、目录规范和状态机逻辑，为我输出整体技术方案及核心级 Python 代码实现。

## 1. Technology Stack (离线环境限定)

由于部署在离线服务器，你必须且只能使用以下指定版本的依赖包和内置库来构建系统：

* **命令行路由**: `click==8.1.3` `prompt-toolkit==3.0.36`    `prettytable==3.6.0`   `tabulate==0.9.0`

  (用于构建嵌套 CLI 命令及参数解析)
* **终端 UI 与进度反馈**: `rich==12.6.0` `alive-progress==2.4.1`    `tqdm==4.64.0` `progressbar2==4.2.0` (核心要求：必须使用 `rich.progress` 模块，在 `submit` 阶段大文件拷贝和 MD5 计算时，提供实时的流式进度条和终端表格渲染)
* **门禁参数校验**: `pydantic==1.10.2` (用于 Gates 阶段针对 tag 枚举、必填项的强类型校验)
* **全局配置**: `PyYAML==6.0` (解析 YAML 配置，解耦路径规则)
* **状态追踪日志**: `loguru==0.6.0` /`PyMySQL==1.0.2` (记录 PENDING -> SUBMITTED 状态流转追踪及报错溯源)
* **EDA 底层解析**: `gdstk==0.9.51` / `pyverilog==1.3.0` (按需在 pre_check 阶段提取层级或验证语法)
* **元数据持久化**: `sqlite3` (Python 标准库，用于记录任务流转状态机、元数据比对结果)

## 2. System Architecture & Data Flow (系统架构与数据流)

系统需实现以下严格的数据流转逻辑：

1. **配置驱动 (Config)**：所有的模块操作和主发布操作都需读取/更新 `Config` 配置中心（可设计为 YAML）。
2. **门禁机制 (Gates)**：数据通过 `submit` 提交后，必须自动触发 `Gates` 进行合规与完整性检查。
3. **交付流转 (Delivery)**：`release` 指令会触发 `Delivery` 模块，提取 `ready/` 目录中的数据进行打包并输出。
4. **审计日志 (Database/Log)**：所有的 CLI 调用、状态变更必须被记录到后端的 `log/ database` 中（需设计对应的日志模块和数据库表）。

所有数据流转必须在以下结构内发生。由于是多用户共享存储，**必须严格处理 Linux 属主权限 (Ownership)**：

* `a0.outgoing/`：源数据区。
* `raw/`：临时校验缓冲目录。
* **`ready/` (就绪数据暂存区，核心临界区)**：

  * `PV_ITER/` | `LVS_PASS/` | `BASE_CLEAN/` | `PV_FINAL/` | `PI_ITER/` | `PI_FINAL/`
  * *注：推入此目录的文件必须通过代码强制执行 `os.chmod` (如 `0o664`)，或依赖目录的 SGID 权限，确保后续打包人员有读写权限。*
* **`release/` (最终发布归档)**

## 3. Directory Structure (目录结构规范)

程序在执行检查、打包或状态追踪时，需严格基于以下结构进行文件路由和存放：

* **`ready/` (就绪数据暂存区)**

  * `PV_ITER/`（物理验证迭代版）
    * `CPU/`
      * `CPU.v.gz` (压缩的 Verilog 源文件)
      * `CPU.hier.gds` (层级版图数据)
    * `DDR/` (DDR 内存模块数据)
  * `LVS_PASS/`（LVS 验证通过版）
  * `BASE_CLEAN/`（base drc clean版）
  * `PV_FINAL/`（物理验证最终版）
  * `PI_ITER/` (工艺改进迭代版)
    * `...` (存放 PI 相关的就绪数据)
  * `PI_FINAL/`（PI 最终版）

  > 注：文件提交类型通过 tag 绑定，Tag 仅限上述提供的几种，系统需支持后续扩展新增 Tag。
  >
* **`release/` (最终发布目录)**：Delivery 模块的输出终点。

  * `PV_ITER/` (以 tag 视角分类)
    * `@latest/`软连接最新的版本
      * `verilog/` (汇总的 PV Verilog 产物)
      * `hiergds/` (汇总的 PV GDS 产物)
      * `...`
    * `version1/`
    * `version2/`
  * `PI_ITER/`
    * `@latest/`
      * `vpg/` (PI 特定的 VPG 产物)
      * `...`
    * **`<version>_<datestamp>`** (实体发布归档命名规范，示例：`V1_20260714`)

## 4. CLI Tools Requirements (CLI 接口需求)

系统需提供以下两组命令行接口，建议使用 Python 的 `click` 或 `argparse` 库实现参数解析与路由：

### 4.1 主控制命令 (PV/PI/... CMD)

用于宏观的版本控制、发布和查询。

* `release` (发布操作，触发 Delivery 流向 release 目录)
  * `-A, --all`：[选填] 全量发布。
  * `-m, --module <name>`：[选填] 指定模块。
  * `-v, --version <ver>`：[选填] 指定版本号。默认为当日时间戳（如 `20260714`），否则拼接为 `<ver>_20260714`。
  * `-t, --tag`：[必填] `PV_ITER | LVS_PASS | BASE_CLEAN | PV_FINAL | PI_ITER | PI_FINAL`，支持选其一或多个。
* `list` (查看列表)
  * `-A, --all` [选填] | `-m, --module <name>` [选填] | `-t, --tag` [必填至少一个]。

### 4.2 模块级命令 (Module <status></status> CMD)

用于特定模块的数据提交和状态追踪。

* `submit` (提交模块数据，触发流向 Gates)
  * `-m, --module <name>`：[必填]。
  * `-t, --tag`：[必填] 选项同上，支持选其一或多个。
  * `-s, --summary "xxx"`：[选填] 用引号包裹的一段提交总结/备注。
  * `-h, --help`：打印帮助。
* `status` (查询模块状态)
  * `-m, --module <name>`：[必填] 查询模块提交状态，默认列出所有提交记录。
  * `-d, --date <time>`：[选填] 查询近期提交记录，支持时间单位如 `5m`, `24h`, `3d` 等。

## 5. File Lifecycle & State Machine (文件流转与校验状态机)

Lifecycle, State Machine & Concurrency (文件流转与防碰撞状态机) 系统必须实施三级并发隔离（全局发布锁、模块级互斥锁），并在每次目录迁移后比对 `date/blake3/size`：

1. **PENDING (处理中阶段)**
   * **触发**：用户针对 `a0.outgoing/` 中的源数据执行 `submit` 命令。
   * **拦截与隔离 (Fast-Fail)**：
     1. 检测全局锁 `ready/.lock_global_release`。若存在，立即返回 `[Warning] 系统正在 Release` 并退出。
     2. 检测模块锁 `raw/.lock_<MODULE>_<TAG>`。若存在，返回 `[Warning] 模块正在提交` 并退出。
     3. **防写爆探测**：使用 `psutil` 检查 `raw/` 所在挂载点剩余空间是否 > 源文件总大小的 1.2 倍。空间不足立即退出。
   * **流转 (提交)**：创建 `raw/`，结合 `rich` 进度条流式拷贝数据，**边拷贝边计算 BLAKE3**。
   * **前置校验 (`pre_check`)**：
     * **pre_check**：对比 `a0.outgoing/` 与 `raw/` 的元数据。
     * 对比两侧元数据是否完全一致。
     * *If OK*: 触发门禁，通过**原子重命名 (atomic move)** 推入 `ready/` 对应目录，强制修正文件权限 (chmod)。更新数据库微观状态为 `delivered`，释放模块锁。
     * *If FAIL*: 打入 `FAIL` 状态池，释放锁。
2. **SUBMITTED (已提交/待发布阶段)**
   * **就绪校验 (`post_check` - 第1次)**：
     * 提取 `ready/`（过门禁后，状态为 delivered）内文件的 `date/md5/size`。
     * 对比元数据，确保 `gates` 检查与传输过程未损坏文件。
     * *If OK*: 向上触发宏观状态变更，数据库中该批次状态正式变为 **`SUBMITTED`**，等待最终的发布指令。
     * *If FAIL*: 打入 `FAIL` 状态池。
3. **RELEASED (已发布阶段)**
   * **触发前置校验 (防脑裂)**：查询 SQLite 待发布的清单，逐一 `os.path.exists()` 校验物理文件是否存在。若物理文件被人为误删导致与数据库脱节，直接报错中止。
   * **全局锁定**：生成 `ready/.lock_global_release`，阻断外部一切 `submit`。
   * **流转 (发布)**： **流转与 post_check (第2次)**：打包至 `release/`，对比源与目的端 BLAKE3。
   * **终态校验 (`post_check` - 第2次)**：
     * 分别提取 `ready/`（源端）和 `release/`（目的端）内打包归档文件对比源与目的端 BLAKE3。
     * *If OK*: 宏观状态变更为 **`RELEASED`**。释放全局锁。
     * *If FAIL*: 归档失败，打入 `FAIL` 状态池并告警。

## 6. 目录结构

```
data_manage/
├── config/                        # YAML 配置
├── ddm/
│   ├── ×.py                     # python脚本
│   ├── gates/
│   │   ├── ×.py                  # 门禁脚本
├── tests/                         # pytest 
├── requirements.txt
├── clean.sh
└── repository/                    # 运行时生成
    ├── raw/
    	├── PV_ITER/
    		├──CPU
            ├──DDR
        └──PI_XXX
    ├── ready/ 结构同raw
    └── release/
    	├── PV_ITER
    		├── `@lates/`
                ├── `verilog/` (汇总的 PV Verilog 产物)
                ├── `hiergds/` (汇总的 PV GDS 产物)
            ├── `version1`
```

## 7. 配置模板

```
admins:
  - wangshuai
  - w00949819
  - zhangsan
  - lisi

# ── 默认视图: 所有模块自动继承 ──
defaults:
  tag:
    PV_ITER:
      description: 逻辑综合数据
      file_patterns:
        - /Users/{user}/xxx/{module}/*.v.gz
        - /Users/{user}/xxx/{module}/*.v.pg
      gates:
        - name: gate1
        - name: gate2
      release_users:
        - w00949819
    PI_ITER:
      description: 物理验证数据
      file_patterns:
        - /Users/{user}/xxx/{module}/*.v.pg
        - /Users/{user}/xxx/{module}/*.hier.gds.gz
      gates:
        - name: gate1
        - name: gate2
      release_users:
        - wangshuai

PV:

PI:
```

`{module}` 加载时展开, `{user}` 提交时用 `-u` 替换。

## Output format

请作为架构师，为我输出以下内容：

1. **SQLite 数据库设计**：提供表结构 DDL (需包含 md5, size, tag, 状态机枚举, 文件路径 等)。
2. **核心代码实现 (CLI 与 路由)**：使用 `click` 构建 `submit` 和 `release` 的入口框架，使用 `pydantic` 校验 tag。
3. **并发与锁控代码**：提供全局锁、模块锁及 Fast-Fail 拦截逻辑的 Python 实现。
4. **核心代码实现 (流转、校验与进度条)**：重点实现 `submit` 逻辑，**必须展示如何结合 `rich.progress`，以 chunk 流式读取大文件的方式，拷贝文件，计算 MD5，门禁检查等 更新终端进度条**，并完成 pre_check 状态机流转逻辑。

## 实现说明

本仓库已将设计实现为可直接运行的 Python CLI，入口为 `python -m ddm`。系统分为：

- `ddm/cli.py`：Click 交互层与 Rich 表格输出；
- `ddm/services.py`：锁控、流式复制、MD5/BLAKE3 校验、状态机与发布；
- `ddm/storage.py`：SQLite 持久层，包含 `batches`、`files`、`events` 三张表及状态约束；
- `ddm/gates/runner.py`：由 YAML 配置驱动的黑盒 subprocess gate 接口。

运行示例：

```sh
python -m pip install -r requirements.txt
python -m ddm submit -m CPU -t PV_ITER -s "initial PV"
python -m ddm status -m CPU
python -m ddm release -t PV_ITER -v V1
python -m ddm list -A -t PV_ITER
```

源文件位于平铺的 `a0.outgoing/` 文件池，Tag 不作为源目录层级；每个 Tag 的 `file_patterns` 配置决定它选择哪些 `{module}` 文件。提交后严格使用 `raw/<TAG>/<MODULE>`、`ready/<TAG>/<MODULE>` 与 `release/<TAG>/<VERSION>/<MODULE>`；模块排它锁与全局发布锁防止并发碰撞；成功后以 `os.replace` 原子移入 ready，并执行 `chmod 664`。发布前会验证 SQLite 与物理目录一致，发布完成后清理 ready 暂存区，并将 `@latest` 原子切换到新版本。运行时目录、SQLite 数据库及日志不纳入 Git，以保证 Git 更新不会影响正在运行的任务。

### 外部 owner 源目录

`a0.outgoing` 可以在 DDM 仓库以外、由设计用户拥有的共享挂载点。通过 `outgoing_root` 指定其绝对路径；在每个 Tag 的 `file_patterns` 中使用 `{user}`、`{module}` 选择文件。例如 `outgoing_root: /nfs/eda/a0.outgoing` 与 `file_patterns: ["{user}_{module}.v.gz"]` 会让 `-u alice -m CPU` 从 `/nfs/eda/a0.outgoing/alice_CPU.v.gz` 只读提交。DDM 服务账号只需要此目录的读/执行权限；它只写入自身的 `repository/raw`、`ready`、`release`。

`blake3` 是正式部署的必需依赖。若离线主机尚未完整安装依赖，程序会记录告警并暂时使用 BLAKE2b-256；生产交付前应通过 `pip install -r requirements.txt` 确认已启用 BLAKE3。

## Core Design Principles (核心架构与设计原则)

在进行系统设计和代码编写时，你必须严格贯彻以下核心原则，确保系统的高内聚低耦合、高可扩展性以及底层环境的兼容性：

1. **架构分层与绝对解耦 (Separation of Concerns)**：

   * 代码必须进行严格的三层解耦：**交互层** (负责 Click 参数解析与 Rich 进度条 UI渲染)、**业务逻辑层** (处理锁控制、状态机推进与元数据校验)、**数据持久层** (封装 SQLite 的 CRUD 操作)。
   * 不同的 Feature（如 `submit` 与 `release`）必须解耦，严禁业务逻辑层的代码越权操作或相互硬编码依赖。
2. **Git 驱动与无缝热更新 (Git-Ops & Hot-Update)**：

   * 系统代码架构必须完全基于 Git 进行版本管理，并且充分考虑传统 Linux 环境的兼容性（需确保底层逻辑兼容基础环境，如 Git 1.8.3.1）。
   * 入口脚本需支持全局软链接（Symlink）部署模式。系统需具备**平滑更新**能力：当通过 Git 更新并拉取新的核心脚本时，系统的设计必须保证不会中断、阻塞或干扰当前正在后台执行 `PENDING` 拷贝任务的长耗时进程。
3. **配置驱动与插件化扩展 (Pluggable Extensibility)**：

   * 系统必须为后期拓展更多功能（如未来可能接入的 STA 或 EM/IR 流程）留出接口。严禁将 Tag 规则、目录路由逻辑硬编码在主程序中。
   * 所有验证流程必须由全局 `config.yaml` 驱动。主程序需设计标准化的**黑盒调用接口 (Subprocess API)**，以便未来可以无缝调度外部开发的自动化工具（如利用 Python、Go、Tcl 等编写的独立规则检查或数据提取脚本），实现“新增流程只需改配置，主程序零改动”。
4. **防御性编程与快速失败 (Defensive & Fast-Fail)**：

   * 在执行任何实质性的 I/O 动作之前，必须强制完成所有防碰撞检查（如获取全局/模块并发锁、执行 `psutil` 磁盘容量探针、核对目录读写权限）。
   * 一旦任一前置条件不满足，必须立即触发 Fast-Fail（快速失败），阻断操作，绝不在系统中残留脏数据或无效的数据库死锁记录。
   * 状态机的推进必须具备原子性，状态变更必须与文件的原子级物理移动同进同退。
5. **全链路可观测性 (Full Observability)**：

   * 系统的任何一次状态流转（成功或失败）、锁竞争拦截、权限拒绝，都必须通过 `loguru` 记录标准化的日志溯源信息。
   * 通过 SQLite 记录与 BLAKE3 哈希，确保每次生成的 `release` 数据包都具备 100% 的数据血缘 (Data Lineage) 追溯能力。

## 附录

其他可用技术栈：其他可用：about-time==3.1.1 absl-py==1.4.0 aiohttp==3.8.1 aiosignal==1.2.0 alive-progress==2.4.1 ansys-mapdl-reader==0.51.15 anyio==3.6.2 aplus==0.11.0 appdirs==1.4.4 asgiref==3.5.2 astropy==5.2 asttokens==2.2.1 async-timeout==4.0.2 attrs==24.3.0 autograd==1.5 backcall==0.2.0 bayesian-optimization==1.2.0 bcrypt==4.0.1 bitstring==3.1.9 blake3==0.3.2 bqplot==0.12.36 branca==0.6.0 Brotli==1.0.9 cachetools==5.2.0 certifi==2022.5.18.1 cffi==1.15.1 charset-normalizer==2.0.12 click==8.1.3 cloudpickle==2.2.0 cma==3.2.2 cocotb==1.6.2 colorama==0.4.6 comm==0.1.2 commonmark==0.9.1 configobj==5.0.6 cryptography==38.0.2 cvxopt==1.3.0 cyclere==0.10.0 dash==2.5.0 dash-bootstrap-components==1.1.0 dash-colorscales==0.0.4 dash-core-components==2.0.0 dash-daq==0.5.0 dash-html-components==2.0.0 dash-table==5.0.0 dask==2022.12.1 debugpy==1.6.4 decorator==5.1.1 Deprecated==1.2.13 dill==0.3.6 Django==4.1.4 docutils==0.17.1 dtale==2.5.1 elasticsearch==7.6.0 entrypoints==0.4 et-xmlfile==1.1.0 executing==1.2.0 fastapi==0.88.0 filelock==3.8.2 Flask==2.1.2 Flask-Compress==1.12 flask-ngrok==0.0.25 frozendict==2.3.4 frozenlist==1.3.1 fsspec==2022.11.0 future==0.18.2 gdspy==1.6.9 gdstk==0.9.51 grapheme==0.6.0 h11==0.14.0 h5py==3.7.0 httptools==0.5.0 hyperopt==0.2.7 idna==3.3 imageio==2.21.1 importlib-metadata==4.11.4 inflection==0.5.1 iniconfig==1.1.1 ipydatawidgets==4.3.2 ipykernel==6.19.3 ipyleaflet==0.17.2 ipymp==0.9.2 ipython==8.7.0 ipython-genutils==0.2.0 ipyvolume==0.5.2 ipyvue==1.8.0 ipyvuetify==1.8.4 ipywebrtc==0.6.0 ipywidgets==8.0.3 itsdangerous==2.1.2 jedi==0.18.2 Jinja2==3.1.2 joblib==1.1.0 jsonschema==4.23.0 jsonschema-specifications==2024.10.1 jupyter_client==7.4.8 jupyter_core==5.1.0 jupyterlab-widgets==3.0.4 kaleido==0.2.1 kiwisolver==1.3.2 klayout==0.27.10 llvmlite==0.39.1 locket==1.0.0 loguru==0.6.0 lxml==5.3.0 lz4==4.0.1 Markdown==3.7 MarkupSafe==2.1.1 matlab==0.1 matplotlib==3.4.3 matplotlib-inline==0.1.6 memory-profiler==0.61.0 missingno==0.4.2 mpmath==1.2.1 multidict==6.0.2 nest-asyncio==1.5.6 networkx==2.8.3 numba==0.56.4 numpy==1.21.2 opencv-python==4.5.5.64 openpyxl==3.0.9 ortools==9.7.2996 packaging==21.3 pandas==1.3.3 paramiko==2.11.0 parsso==0.8.3 partd==1.3.0 patsy==0.5.2 pexpect==4.8.0 pickleshare==0.7.5 Pillow==8.3.2 platformdirs==2.6.0 plotly==5.8.2 pluggy==1.0.0 ply==3.11 prettytable==3.6.0 progressbar2==4.2.0 prompt-toolkit==3.0.36 protobuf==4.24.3 psutil==5.9.4 ptyprocess==0.7.0 pure-eval==0.2.2 py==1.11.0 py4j==0.10.9.7 pyarrow==10.0.1 PyBoolector==3.2.4.20240823.1 pycparser==2.21 pydantic==1.10.2 pyerfa==2.0.0.1 Pygments==2.13.0 pymoo==0.6.0 PyMySQL==1.0.2 PyNaCl==1.5.0 pyparsing==2.4.7 PyQt5==5.13.1 PyQt5-sip==12.9.0 pyqtgraph==0.12.0 PySide2==5.15.2.1 pytest==7.1.3 pytest-repeat==0.9.1 pytest-timeout==2.1.0 python-constraint==1.4.0 python-dateutil==2.8.2 python-docx==1.1.2 python-dotenv==0.21.0 python-jsonschema-objects==0.5.7 python-pptx==1.0.2 python-utils==3.4.5 pythreejs==2.4.1 pytz==2021.3 pyucis==0.1.4.11087239919 pyverilog==1.3.0 pyvista==0.36.1 pyvsc==0.9.3.10985030023 PyYAML==6.0 pyzmq==24.0.1 referencing==0.35.1 requests==2.28.0 rich==12.6.0 rpds-py==0.22.3 scikit-learn==1.0 scikit-rf==0.22.1 scipy=1.7.1   scooby==0.6.0   seaborn==0.11.2   shiboken2==5.15.2.1   six=1.16.0   skillmodels==1.2.18   sko==0.5.7   sniffio==1.3.0   sqparse==0.4.3   squarify==0.4.3   stack-data==0.6.2   starlette==0.22.0   statistics==1.0.3.5   statsmodels==0.13.2   strsimpy==0.2.1   sympy=1.11.1   tabulate==0.9.0   tenacity==8.0.1   threadpoolctl==3.0.0   tomli=2.0.1   toolz==0.12.0   toposort==1.10   tornado==6.2   tqdm==4.64.0   traitlets==5.8.0   traittypes==0.2.1   typing_extensions==4.12.2   urllib3==1.26.9   uvicorn==0.20.0   uvloop==0.17.0   vaex==4.16.0   vaex-astro==0.9.3   vaex-core==4.16.1   vaex-hdf5==0.14.1   vaex-jupyter==0.8.1   vaex-ml==0.18.1   vaex-server==0.8.1   vaex-viz==0.5.4   vtk==9.1.0   watchfiles==0.18.1   wcwidth==0.2.5   websockets==10.4   Werkzeug==2.1.2   widgetsnextextension==4.0.4   wrapt==1.14.1   wslink==1.8.2   xarray==2022.3.0   xlrd==2.0.1   xlrd3==1.1.0   xlsxwriter==3.2.5   xlwt==1.3.0   xyzservices==2022.9.0   yaml==1.8.1



请给出详细的USER_GUIDE文档，另外给出部署文档，虽然我的服务器上已经有了要使用的包，以防万一，请给出下载离线包并迁移到服务器
  的方案，另外给出详细的架构说明，已经文件流转状态说明
