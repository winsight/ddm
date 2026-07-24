# Changelog

## v0.2.1 (2026-07-24)
- 新增 `ddm submit -u --user` 管理员替用户提交
- 新增 `sync_owners.py` 从 PDSSetup.tcl 同步模块 owner
- 新增 `ddm_web.py` 数据库查看 Web 界面
- 修复多用户权限：共享组 SGID、过期锁自动清理、PID 活体检测
- 新增 `ddm_update.csh` 离线部署更新脚本

## v0.2.0 (2026-07-21)
- 新增角色权限：模块视角 vs 专项视角命令隔离
- 新增 tag 级 release 锁，不同 tag 可同时发布
- 新增 `file_groups` 全局文件类型分类（verilog/gds/pg）
- 新增 `-A --force` 强制覆盖已有版本
- 新增 release 输出发布路径
- 修复 BLAKE3 冗余计算（7次→4次）
- 新增 post_check 链条验证（raw→ready→staging）

## v0.1.0 (2026-07-16)
- 首版发布：submit / release / status / list / check
- SQLite 状态机: PENDING → SUBMITTED → RELEASED
- 门禁系统: subprocess 黑盒调用
- BLAKE3 文件完整性校验
- 模块锁 + release 锁并发控制
- csh/tcsh 动态 Tab 补全
