"""Black-box subprocess gate runner.

Each gate is defined in config.yaml as:
  gates:
    - name: my_check
      command: python -m ddm.gates.my_check

The runner invokes each command as a subprocess, passing the raw directory
and module name as arguments. Exit code 0 = pass; non-zero = fail.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List

from loguru import logger

from ddm.config import GateDef


class GateResult:
    def __init__(self, name: str, passed: bool, stdout: str, stderr: str, returncode: int):
        self.name = name
        self.passed = passed
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode

    def __repr__(self):
        status = "PASS" if self.passed else "FAIL"
        return f"GateResult({self.name}, {status})"


def run_gates(
    gate_defs: List[GateDef],
    raw_dir: str,
    module: str,
    tag: str,
    timeout: int = 300,
) -> List[GateResult]:
    """Execute each gate as a subprocess.

    The raw directory and module are passed as positional args.
    """
    results: List[GateResult] = []
    raw_path = Path(raw_dir).resolve()

    for gate in gate_defs:
        logger.info(f"Running gate '{gate.name}': {gate.command}")
        try:
            proc = subprocess.run(
                gate.command.split() + [str(raw_path), module, tag],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            passed = proc.returncode == 0
            result = GateResult(
                name=gate.name,
                passed=passed,
                stdout=proc.stdout,
                stderr=proc.stderr,
                returncode=proc.returncode,
            )
            if passed:
                logger.info(f"Gate '{gate.name}' PASSED")
            else:
                logger.error(f"Gate '{gate.name}' FAILED (rc={proc.returncode}): {proc.stderr[:200]}")
        except subprocess.TimeoutExpired:
            result = GateResult(
                name=gate.name,
                passed=False,
                stdout="",
                stderr=f"Timeout after {timeout}s",
                returncode=-1,
            )
            logger.error(f"Gate '{gate.name}' TIMED OUT")
        except Exception as exc:
            result = GateResult(
                name=gate.name,
                passed=False,
                stdout="",
                stderr=str(exc),
                returncode=-1,
            )
            logger.error(f"Gate '{gate.name}' ERROR: {exc}")

        results.append(result)

    return results


def all_gates_passed(results: List[GateResult]) -> bool:
    return all(r.passed for r in results)
