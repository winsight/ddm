"""Verilog syntax check gate — simulates a long-running check."""
import sys
import time
from pathlib import Path


def main():
    raw_dir = Path(sys.argv[1])
    module = sys.argv[2]
    tag = sys.argv[3]

    # Simulate: scanning Verilog files
    v_files = list(raw_dir.rglob("*.v.gz"))
    print(f"Scanning {len(v_files)} Verilog file(s) for {module}/{tag}...")

    for f in v_files:
        print(f"  Checking syntax: {f.name}")
        time.sleep(1.5)  # simulate parsing

    print("Verilog syntax check PASSED")
    sys.exit(0)


if __name__ == "__main__":
    main()
