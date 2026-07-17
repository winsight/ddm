#==================================================================
# DDM tab completion for csh / tcsh
#------------------------------------------------------------------
# source /path/to/ddm/ddm.complete.csh
#==================================================================
# 设计:
#   p/1  = 子命令列表
#   n/-t = tag 值补全 (source 时双引号展开 $_ddm_tags)
#   n/-m = module 值补全
#   n/-v = release 版本值补全 (source 时双引号展开 $_ddm_versions)
#   n/-*  = catch-all: 任何 - 开头 → flag 列表
#
# 补全数据来源:
#   ddm __complete_tags      → 所有 tag 列表
#   ddm __complete_modules    → 所有 module 列表
#   ddm __complete_versions   → release/ 下所有已发布版本
#
# 方案 B (argcomplete):
#   pip3 install --user argcomplete
#   eval "$(register-python-argcomplete ddm)"  >> ~/.cshrc
#   注: argcomplete 在 tcsh 6.20 下不可用 (tcsh 不支持运行时命令补全),
#       仅在 bash/zsh 或 tcsh 6.22+ 下有效。
#
# 已知限制 (tcsh 6.20):
#   从 n/-t / n/-m / n/-v 选值后，再输入 - Tab 可能不弹 flag。
#   这是 tcsh 6.20 对变量列表的边界行为，非代码 bug。
#==================================================================

set _ddm_tags     = `ddm __complete_tags`
set _ddm_modules  = `ddm __complete_modules`
set _ddm_versions = `ddm __complete_versions`

complete ddm \
  'p/1/(submit status release list check version)/' \
  "n/-t/($_ddm_tags)/" \
  "n/-m/($_ddm_modules)/" \
  "n/-v/($_ddm_versions)/" \
  'n/-c/f:*.yaml/' \
  'n/-c/f:*.yml/' \
  'n/-d/(5m 24h 3d)/' \
  'n/-s/x:<summary>/' \
  'n/-*/(-t -m -s -c -d -v)/'

# ---- 手动刷新动态数据（配置变更或发布新版本后执行） ----
alias refresh_ddm_complete \
  'set _ddm_tags = `ddm __complete_tags`; set _ddm_modules = `ddm __complete_modules`; set _ddm_versions = `ddm __complete_versions`'
