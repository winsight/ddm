# DDM 数据完整性校验体系

## 1. 校验点全景

```
a0.outgoing/{user}/{module}/                    ← 工程师私有源目录 (读)
        │
        │  submit: streaming_copy (边拷贝边 BLAKE3, 保留 mtime)
        ▼
raw/{TAG}/{MODULE}/                             ← 临时暂存 (写)
        │
        ├─ pre_check (source vs raw)             size + BLAKE3
        │
        ├─ run_gates (subprocess 黑盒门禁)
        │
        ├─ os.replace (raw → ready), chmod 664
        ▼
ready/{TAG}/{MODULE}/                           ← 就绪暂存 (临界区)
        │
        ├─ post_check_1 (raw vs ready)            BLAKE3 only
        │                                         raw hash 在 os.replace 前缓存
        │                                         ready hash 移动后重算
        │                                         ← submit 流水线最后一步
        │
        │  release: copy2 (ready → staging)
        ▼
.staging_xxx/                                   ← 临时 staging (原子事务)
        │
        ├─ post_check_2a (ready vs staging)      size + mtime + BLAKE3
        │                                         ← release Pass 3
        │
        ├─ os.rename (staging → VERSION) 或 merge_dirs
        ▼
release/{TAG}/@latest → VERSION/                ← 发布归档 (只读)
        │
        └─ post_check_2b (后续 release 审计)     扫描历史 RELEASED batch,
                                                  检测物理文件是否被 rm -rf

检查矩阵:
                                     size    mtime    BLAKE3
  pre_check       (a0 → raw)           ✓       ✗        ✓
  post_check_1    (raw → ready)        ✗       ✗        ✓     ← 链条验证
  post_check_2a   (ready → staging)    ✓       ✓        ✓
  post_check_2b   (历史 RELEASED)     文件是否存在 + 跨版本 size diff
```

---

## 2. 各校验点时机与作用

### 2.1 pre_check — submit 核心流水线

**时机：** `streaming_copy` 完成 → gate 执行前

**比对：** `a0.outgoing` 源文件 vs `raw/` 暂存文件

**校验项：**
- size: `src_stat.st_size != dst_stat.st_size` → 失败
- BLAKE3 hash: 逐块读取两边的文件重新计算 → 失败

**能发现的：**
- 拷贝过程中磁盘 I/O 错误（静默数据损坏）
- 非正常截断（size 不匹配）
- 网络文件系统（NFS）缓存不一致导致的内容错误

**漏掉的：**
- mtime 不被校验（此阶段不需要，raw 的 mtime 由 streaming_copy 保留）
- 拷贝过程中 a0 源文件被源用户修改（读时快照，无锁保护）

```python
# services.py line ~390
for src in source_files:
    if not compare_metadata(src, str(dest_path)):
        pre_ok = False
        break
```

---

### 2.2 post_check_1 — raw → ready（链条验证）

**时机：** gate 通过 → `os.replace` raw→ready → chmod 664 **之后**

**比对：** `raw/` 的 BLAKE3（在 `os.replace` **之前**缓存） vs `ready/` 的 BLAKE3（移动后重新计算）

**不比对 a0 的原因：** pre_check 已证明 raw == a0。只需证明 ready == raw，传递性保证 ready == a0。消除了 a0 两次读取之间的 TOCTOU 窗口。

```python
# 1. os.replace 前缓存 raw hash
raw_hashes[src] = _blake3_hash(str(raw_path))

# 2. 原子移动
os.replace(str(raw_path), str(ready_path))

# 3. 移动后计算 ready hash，比对缓存
ready_hash = _blake3_hash(str(ready_path))
if ready_hash != raw_hashes[src]:
    post_ok = False  # raw→ready 数据损坏
```

**能发现的：**
- gate 执行过程中 raw/ 文件被修改（gate 不应改文件，但它是 subprocess）
- `os.replace` 原子 rename 中静默数据损坏（极罕见）
- raw/ 在 gate 执行期间被其他进程写入

**size/mtime 不需要另行校验：** `os.replace` 是 rename，不涉及数据拷贝，size 不可能变；mtime 由 `streaming_copy` 保留，不受 `os.replace` 影响。

---

### 2.3 post_check_2a — release Pass 3（提交前最后一道）

**时机：** staging 目录已建好、Pass 2 size diff 完成 → commit 前

**比对：** `ready/` 文件 vs staging/ 临时文件

**校验项：**
- size: ready vs staging
- BLAKE3: ready vs staging  
- mtime: 整数秒级比对（hard fail）

**能发现的：**
- `shutil.copy2` 拷贝 ready→staging 过程中的任何差错
- staging 目录在构造过程中被意外修改

**失败结果：** 整个 staging 目录被 `shutil.rmtree` 清理，Release 中止

```python
# services.py line ~800
if not compare_metadata(ready_path, sp, check_mtime=True):
    all_ok = False
    ...
if not all_ok:
    shutil.rmtree(str(staging_dir), ignore_errors=True)
    return ReleaseResult(False, "Release aborted: post-check failed")
```

---

### 2.4 post_check_2b — 后续 release 审计

**时机：** 每次 `ddm release` 命令启动时，**在提交新版本之前**

**比对：** SQLite 中所有 status=RELEASED 的 batch → 检查其 `release_path` 物理文件是否存在

**能发现的：**
- 有人手动 `rm -rf` 了已发布的版本目录
- 存储介质故障导致部分文件丢失
- NFS 服务端异常导致目录消失

```python
# services.py line ~750
for b in released_batches:
    for f in files:
        rp = f.get("release_path", "")
        if rp and not os.path.exists(rp):
            # 写入 event: "release_file_missing"
            # 累积到 integrity_warnings 随 ReleaseResult 返回
```

**不阻塞 Release** — 发现缺失后记录 warning 继续发布，因为缺失的是**已发布版本**，与本次发布动作无关。

---

### 2.5 跨版本 size diff — release Pass 2

**时机：** staging 文件已就绪 → post_check_2a 之前

**比对：** 本次 staging 同名文件 vs 上一版本的同名文件

| 变化 | 阈值 | 处理 |
|------|------|------|
| < 30% | 正常 | 无 |
| 30% ~ 50% | `abs(ratio) >= 0.30` | `size_change` event（记录） |
| > 50% | `abs(ratio) >= 0.50` | `size_anomaly` event（告警） |

**不阻塞 Release**，仅记录事件。

---

## 3. 能防住的攻击/损坏场景

| 场景 | 防线 | 结果 |
|------|------|------|
| 磁盘静默 bit-flip | pre_check (BLAKE3) | 检测 ✓ |
| 拷贝到一半磁盘满 | streaming_copy OSError | 上层捕获 ✓ |
| gate 子进程意外修改文件 | post_check_1 (BLAKE3) | 检测 ✓ |
| ready/ 被外部手动篡改 | post_check_2a (BLAKE3) | 检测 ✓ |
| 已发布版本被 rm -rf | post_check_2b (文件存在) | 发现 + 告警 ✓ |
| 新版本文件异常缩小 60% | Pass 2 size diff | size_anomaly 告警 ✓ |
| 并发 submit 同一模块 | O_CREAT\|O_EXCL 模块锁 | 拒绝 ✓ |
| release 和 submit 同一 tag | release_lock 锁 | 相互阻塞 ✓ |
| 锁文件残留 (kill -9) | PID 活体检测 + 过期自动清除 | 自动恢复 ✓ |
| NFS 挂载点断开 | 所有 I/O 抛 OSError | 上层捕获 ✓ |

---

## 4. 未覆盖的极端情况

### 4.1 a0.outgoing 源文件在 submit 过程中被修改

```
T1: submit 开始，streaming_copy 读 a0.outgoing/CPU/CPU.v.gz
T2: 用户 vi 修改 CPU.v.gz（在 a0 目录里）
T3: streaming_copy 完成，BLAKE3 基于 T2 后的内容
T4: pre_check 重新读 a0 → 和 raw 一致 → 通过
T5: 但 ready/ 里是 T2 之后的内容，不是 T1 用户想提交的版本
```

**风险等级：** 低（a0.outgoing 是用户自己的目录，改自己的文件是预期行为）

**可能的防护：** submit 前先对 a0 文件做快照（hardlink 到临时目录），或者对 a0 文件加读锁（不可行，共享目录）

### 4.2 同一文件在两次 BLAKE3 计算之间被替换

```
T1: compare_metadata 计算 src_hash = BLAKE3(a0/CPU.v.gz)
T2: 外部进程 cp 新版本 CPU.v.gz a0/CPU.v.gz
T3: compare_metadata 计算 dst_hash = BLAKE3(raw/CPU.v.gz)
T4: 比较通过（两遍都是各自时刻的正确值）
```

TOCTOU 问题。`compare_metadata` 分两次计算 hash，中间源文件可能被替换。

**风险等级：** 极低（时间窗口 < 1 秒，需要并发写入 a0）

**可能的防护：** 一次读取同时计算两个文件（但需要内存）：

```python
# 当前：两次读
src_hash = _blake3_hash(source_path)
dst_hash = _blake3_hash(dest_path)

# 更安全但开销大：
# 同时打开两个文件，交错读取，确保读到的是同一时刻的数据
```

### 4.3 BLAKE3 校验成功但文件属性异常

BLAKE3 只校验内容。以下情况不会被检测：

- 错误的所有权（chown 给了错的人）— chmod 664 设置正确
- 错误的权限位 — `os.chmod(ready_path, 0o664)` 确保
- 时间戳被篡改 — mtime 比对（warning 不阻塞）

SGID 目录权限已经在创建时确保。

### 4.4 release staging 和 commit 之间的时间窗口

```
T1: post_check_2a 通过，所有校验 OK
T2: 恶意进程修改 staging/ 下的文件
T3: os.rename(staging, VERSION) 提交的是 T2 后的内容
```

**风险等级：** 极低（staging 目录在服务器本地文件系统，非共享 NFS）

`os.rename` 是同文件系统原子操作，不涉及内容拷贝，所以时间窗口极短。但理论上如果 staging 在 NFS 上且被其他节点写入，可能出问题。

### 4.5 跨版本继承的文件未经当前 release 校验

```
V1/CPU/verilog/CPU.v.gz      ← 上次 release 时校验通过
V2/CPU/verilog/CPU.v.gz      ← 本次从 V1 继承（copytree），只做了 chmod 664
                              没有重新计算 BLAKE3 与 a0 比对
```

**风险等级：** 低（继承的是已发布的归档版本，逻辑上已是可信数据）

post_check_2a 只校验本次**新提交**的文件（ready vs staging），不校验继承的文件。

### 4.6 数据库和文件系统之间的状态不同步

```
ddm release 成功 → batch → RELEASED → ready/ 被清理
                 → os.rename 成功     → 文件在 release/ 里
                 → 但 release_path 记录错误路径 → SQLite 和实际不一致
```

`update_file_released` 用 staging→release 路径替换，如果 `os.rename` 成功但 DB 更新失败（事务回滚），路径就不一致。

**风险等级：** 中等。当前使用 WAL 模式 SQLite，单个连接内的事务是原子的，但 release 过程横跨多个事务。

### 4.7 文件大小校验使用 64-bit int 可能溢出

1.7GB 的文件在 Python 的 `os.stat().st_size` 中表现为 Python int（无限精度），不存在溢出问题。但如果文件超过文件系统限制（ext4 最大 16TB），`streaming_copy` 可能在拷贝过程中 OSError。

### 4.8 BLAKE3 不可用时的降级

如果 `blake3` 包未安装，系统自动降级到 `hashlib.blake2b()`：

```python
HAS_BLAKE3 = False  # 降级标记
```

**风险：** BLAKE2b 比 BLAKE3 慢约 3-10 倍。大文件（1.7GB）的 hash 计算时间显著增加。但安全性等价（两者都是密码学哈希，碰撞概率可忽略）。

---

## 5. 总体评估

```
                    ┌──────────────┐
                    │  攻击面/风险  │
                    └──────┬───────┘
                           │
        ┌──────────────────┼──────────────────┐
        │                  │                  │
   ┌────▼────┐      ┌──────▼──────┐     ┌─────▼─────┐
   │ 已覆盖   │      │ 覆盖但有窗口 │     │  未覆盖    │
   └────┬────┘      └──────┬──────┘     └─────┬─────┘
        │                  │                  │
  拷贝损坏 ✓          a0 TOCTOU          恶意 root
  gate 篡改 ✓         staging 间隙       硬件内存错误
  ready 篡改 ✓        继承未重校验        供应链攻击
  磁盘满 ✓                              （Python 解释
  rm -rf 审计 ✓                          器/OS 层面）
  并发冲突 ✓
```

**结论：** 当前校验体系覆盖了数据流转中的主要风险点，对 EDA 芯片交付场景足够。极端情况（root 权限、硬件故障、供应链）属于基础设施层面，不适合在应用层解决。
