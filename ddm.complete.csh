#==================================================================
# DDM tab completion for csh / tcsh
#------------------------------------------------------------------
# Add to ~/.cshrc or ~/.tcshrc:
#   source /path/to/ddm/ddm.complete.csh
#==================================================================
# Press Tab after each position for hints:
#   ddm <TAB>                 → subcommands
#   ddm submit <TAB>          → -t -m -s -c
#   ddm submit -t <TAB>       → tag names
#   ddm submit -m <TAB>       → module names
#   ddm release <TAB>         → -t -A -m -v --inherit -c
#   ...
#==================================================================

# --- p/1 = position 1 after 'ddm': subcommand list ---
# --- C/<cmd>/((...)) = after typing this subcommand, show its options ---
# --- n/-<flag>/... = after typing this flag, show its value completions ---

complete ddm \
  'p/1/(submit status release list check version)/' \
  \
  'C/submit/((-t -m -s -c))/' \
  'C/status/((-m -d -c))/' \
  'C/release/((-t -A -m -v --inherit -c))/' \
  'C/list/((-t -A -m -v -c))/' \
  \
  'n/-t/x:(`ddm __complete_tags 2>/dev/null`)/' \
  'n/-m/x:(`ddm __complete_modules 2>/dev/null`)/' \
  'n/-s/x:<summary-text>/' \
  'n/-d/x:<time-filter like 5m 24h 3d>/' \
  'n/-v/x:<version-label like V1 V2>/' \
  'n/-c/f:*.yaml/' \
  'n/--inherit/( )/'
