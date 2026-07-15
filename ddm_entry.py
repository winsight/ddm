#!/usr/bin/env python
"""PyInstaller entry point for DDM binary.

This is used by PyInstaller to create a standalone executable.
It locates config.yaml relative to the binary at runtime.
"""

import os
import sys
from pathlib import Path

# When frozen by PyInstaller, sys._MEIPASS is the temp extract dir
# When running normally, __file__ is this script's location
if getattr(sys, "frozen", False):
    _ROOT = Path(sys._MEIPASS)
else:
    _ROOT = Path(__file__).resolve().parent

# Add the root to sys.path so ddm package can be imported
sys.path.insert(0, str(_ROOT))

from ddm.cli import main

if __name__ == "__main__":
    main()
