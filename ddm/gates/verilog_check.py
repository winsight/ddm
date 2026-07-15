"""Stub: Verilog syntax check gate."""
import sys
from pathlib import Path


def main():
    raw_dir = Path(sys.argv[1])
    module = sys.argv[2]
    tag = sys.argv[3]
    print(f"Verilog check: raw_dir={raw_dir}, module={module}, tag={tag}")
    print("PASS (stub)")
    sys.exit(0)


if __name__ == "__main__":
    main()
