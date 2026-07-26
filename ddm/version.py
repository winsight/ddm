"""Single source of truth for DDM version and changelog.

To bump the version, edit only this file:
  __version__   → version string
  __changelog__ → list of (version, description) tuples, newest first
"""

__version__ = "0.3.1"

__changelog__ = [
    ("v0.3.0", "2026-07-24 — 管理员-u替用户提交、SUPERSEDED覆盖状态、Web分页持久化、多用户权限完善"),
    ("v0.2.1", "2026-07-24 — 新增 -u 替用户提交、sync_owners、ddm_web、多用户权限修复"),
    ("v0.2.0", "2026-07-21 — 角色权限隔离、tag级release锁、file_groups分类、-A --force"),
    ("v0.1.0", "2026-07-16 — 首版: submit/release/status/list/check，门禁+BLAKE3+并发锁"),
]
