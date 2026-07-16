#==================================================================
# DDM tab completion for csh / tcsh
#------------------------------------------------------------------
# Add to your ~/.cshrc to enable:
#
#   source /path/to/ddm/ddm.complete.csh
#
# Once sourced, type `ddm <TAB>` to see subcommands,
# `ddm submit -t <TAB>` to see available tags, etc.
#==================================================================

# --- static subcommand list (top-level) ---
complete ddm 'p/1/(submit status release list check version)/'

# --- dynamic completions via ddm hidden commands ---
complete ddm 'n/-t/x:(`ddm __complete_tags 2>/dev/null`)/'   # tag names
complete ddm 'n/-m/x:<module-name>/'                            # module names (free-form)
complete ddm 'n/-s/x:<summary-text>/'                           # summary (free-form)
complete ddm 'n/-c/f:*.yaml/' 'n/-c/f:*.yml/'                   # config file paths
complete ddm 'n/-d/x:<time-filter like 5m 24h 3d>/'
complete ddm 'n/-v/x:<version-label like V1 V2>/'

# suppress extra hints for commands with no further args
complete ddm 'n/check/( )/'
complete ddm 'n/version/( )/'
