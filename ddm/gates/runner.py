"""Black-box subprocess gate runner.

Each gate is defined in config.yaml as:
  gates:
    - name: my_check
      command: python -m ddm.gates.my_check

The runner invokes each command as a subprocess, passing the raw directory
and module name as arguments. Exit code 0 = pass; non-zero = fail.

Supports a progress callback for Rich progress bar integration.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional

from loguru import logger

from ddm.config import GateDef


class GateResult:
    def __init__(self, name: str, passed: bool, stdout: str, stderr: str, returncode: int,
                 elapsed: float = 0.0):
        self.name = name
        self.passed = passed
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.elapsed = elapsed

    def __repr__(self):
        status = "PASS" if self.passed else "FAIL"
        return f"GateResult({self.name}, {status}, {self.elapsed:.1f}s)"


def run_gates(
    gate_defs: List[GateDef],
    raw_dir: str,
    module: str,
    tag: str,
    timeout: int = 300,
    progress_callback: Optional[Callable] = None,
) -> List[GateResult]:
    """Execute each gate as a subprocess.

    Args:
        gate_defs: Gate definitions from config.
        raw_dir: Path to raw data directory.
        module: Module name.
        tag: Tag name.
        timeout: Per-gate timeout in seconds.
        progress_callback: Called as (gate_index, gate_name, status) where
                          status is 'start', 'pass', 'fail'.
    """
    results: List[GateResult] = []
    raw_path = Path(raw_dir).resolve()
    total = len(gate_defs)

    for i, gate in enumerate(gate_defs):
        if progress_callback:
            progress_callback(i, gate.name, "start", total)

        logger.info(f"Running gate '{gate.name}': {gate.command}")
        t0 = time.time()
        try:
            # Use sys.executable so gates always run with the same Python
            # that is running DDM — critical when .cshrc uses the minimal
            # alias approach (no venv activation, no PATH modification).
            cmd_parts = gate.command.split()
            if cmd_parts and cmd_parts[0] in ("python", "python3"):
                cmd_parts[0] = sys.executable
            proc = subprocess.run(
                cmd_parts + [str(raw_path), module, tag],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            elapsed = time.time() - t0
            passed = proc.returncode == 0
            result = GateResult(
                name=gate.name,
                passed=passed,
                stdout=proc.stdout,
                stderr=proc.stderr,
                returncode=proc.returncode,
                elapsed=elapsed,
            )
            if passed:
                logger.info(f"Gate '{gate.name}' PASSED ({elapsed:.1f}s)")
                if progress_callback:
                    progress_callback(i, gate.name, "pass", total)
            else:
                logger.error(f"Gate '{gate.name}' FAILED (rc={proc.returncode}): {proc.stderr[:200]}")
                if progress_callback:
                    progress_callback(i, gate.name, "fail", total)
        except subprocess.TimeoutExpired:
            elapsed = time.time() - t0
            result = GateResult(
                name=gate.name, passed=False, stdout="",
                stderr=f"Timeout after {timeout}s", returncode=-1, elapsed=elapsed,
            )
            logger.error(f"Gate '{gate.name}' TIMED OUT")
            if progress_callback:
                progress_callback(i, gate.name, "fail", total)
        except Exception as exc:
            elapsed = time.time() - t0
            result = GateResult(
                name=gate.name, passed=False, stdout="",
                stderr=str(exc), returncode=-1, elapsed=elapsed,
            )
            logger.error(f"Gate '{gate.name}' ERROR: {exc}")
            if progress_callback:
                progress_callback(i, gate.name, "fail", total)

        results.append(result)

    return results


def all_gates_passed(results: List[GateResult]) -> bool:
    return all(r.passed for r in results)
