#==================================================================
# DDM tab completion for csh / tcsh
#------------------------------------------------------------------
# Add to your ~/.cshrc or ~/.tcshrc:
#
#   source /path/to/ddm/ddm.complete.csh
#
# Once sourced:
#   ddm <TAB>               → subcommands (submit status release ...)
#   ddm submit -t <TAB>     → tag names from config.yaml
#   ddm release -t <TAB>    → same
#==================================================================
#
# NOTE: tcsh complete rules MUST be in a single call — multiple
#       `complete ddm` calls overwrite each other.
#
complete ddm \
  'p/1/(submit status release list check version)/' \
  'n/-t/x:(`ddm __complete_tags 2>/dev/null`)/' \
  'n/-m/x:<module-name>/' \
  'n/-s/x:<summary-text>/' \
  'n/-c/f:*.yaml/' \
  'n/-c/f:*.yml/' \
  'n/-d/x:<time-filter like 5m 24h 3d>/' \
  'n/-v/x:<version-label like V1 V2>/' \
  'n/check/( )/' \
  'n/version/( )/'
