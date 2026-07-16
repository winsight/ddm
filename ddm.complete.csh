#==================================================================
# DDM tab completion for csh / tcsh
#------------------------------------------------------------------
# source /path/to/ddm/ddm.complete.csh
#==================================================================
# 设计:
#   p/1  = 子命令列表
#   n/-t = tag 值补全 (source 时双引号展开 $_ddm_tags)
#   n/-m = module 值补全
#   n/-* = catch-all: 任何 - 开头 → flag 列表
#
# 已知限制 (tcsh 6.20):
#   从 n/-t 或 n/-m 选值后，再输入 - Tab 可能不弹 flag。
#   这是 tcsh 6.20 对变量列表的边界行为，非代码 bug。
#==================================================================

set _ddm_tags    = `ddm __complete_tags`
set _ddm_modules = `ddm __complete_modules`

complete ddm \
  'p/1/(submit status release list check version)/' \
  "n/-t/($_ddm_tags)/" \
  "n/-m/($_ddm_modules)/" \
  'n/-c/f:*.yaml/' \
  'n/-c/f:*.yml/' \
  'n/-d/(5m 24h 3d)/' \
  'n/-v/(V1 V2 V3)/' \
  'n/-s/x:<summary>/' \
  'n/-*/(-t -m -s -c -d -v)/'
