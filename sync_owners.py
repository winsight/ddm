#!/usr/bin/env python3
"""Sync module owners from PDSSetup.tcl to DDM config.yaml.

Usage:
  python3 sync_owners.py <PDSSetup.tcl> [config.yaml]

Parses Tcl array assignments of the form:
  set <prefix>(<MODULE>,OWNER) "<username>"
  e.g. set chip_owner(DMA_CTRL,OWNER) "w00949819"

Updates config.yaml modules.<NAME>.owners with the extracted mappings.
Existing owners not in the Tcl file are preserved (module handover support).
"""

import re
import sys
from pathlib import Path

import yaml


def parse_tcl_owners(tcl_path):
    """Parse PDSSetup.tcl and return {module: [owner1, owner2, ...]}."""
    owners = {}

    # Pattern: set VAR_NAME(MODULE, OWNER) VALUE
    # The array variable name can be anything; key indicator is ',OWNER)'
    pattern = re.compile(
        r"""set\s+\S+\(           # set var_name(
            \s*(\S+)\s*,\s*       # MODULE,
            OWNER\s*\)\s*         # OWNER)
            "?([^"]*)"?           # "username" or username
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    with open(tcl_path) as f:
        for line in f:
            line = line.strip()
            # Skip comments
            if line.startswith("#"):
                continue
            m = pattern.search(line)
            if m:
                module = m.group(1)
                user = m.group(2).strip()
                if user:
                    owners.setdefault(module, []).append(user)

    return owners


def update_config(config_path, new_owners, dry_run=False):
    """Update only the modules: section in config.yaml, preserving all else."""
    path = Path(config_path)
    if not path.exists():
        print(f"✗ Config not found: {config_path}")
        return

    with open(path) as f:
        config = yaml.safe_load(f)
    with open(path) as f:
        original = f.read()

    if "modules" not in config:
        config["modules"] = {}

    updated = []
    added = []
    kept_old = []
    modules_config = {}

    # Build merged module config
    for module, users in new_owners.items():
        if module in config.get("modules", {}):
            existing = set(config["modules"][module].get("owners", []))
            new_set = set(users)
            merged = sorted(new_set | existing)
            if set(config["modules"][module].get("owners", [])) != set(merged):
                updated.append(module)
            modules_config[module] = merged
        else:
            modules_config[module] = sorted(users)
            added.append(module)

    # Keep existing modules not in Tcl
    for module in config.get("modules", {}):
        if module not in modules_config:
            modules_config[module] = config["modules"][module].get("owners", [])
            kept_old.append(module)

    # Generate new modules: YAML block
    # Order: new modules in Tcl encounter order, then existing modules alphabetically
    tcl_order = list(new_owners.keys())
    ordered = [m for m in tcl_order if m in modules_config]
    # Append existing modules not in Tcl
    for m in sorted(modules_config):
        if m not in ordered:
            ordered.append(m)

    lines = ["modules:"]
    for module in ordered:
        lines.append(f"  {module}:")
        lines.append(f"    owners: [{', '.join(modules_config[module])}]")
    new_block = "\n".join(lines) + "\n"

    # Replace old modules: block with new one in the original text
    import re as _re
    pattern = _re.compile(r"^modules:.*?(?=^\S|\Z)", _re.DOTALL | _re.MULTILINE)
    new_text = pattern.sub(lambda m: new_block, original, count=1)

    if dry_run:
        print("=== DRY RUN (no changes written) ===")
        print("--- New modules: block ---")
        print(new_block)
    else:
        with open(path, "w") as f:
            f.write(new_text)
        print(f"✓ Config updated: {config_path}")

    if added:
        print(f"  新增模块: {', '.join(added)}")
    if updated:
        print(f"  更新模块: {', '.join(updated)}")
    if kept_old:
        print(f"  保留旧模块 (不在Tcl中): {', '.join(kept_old)}")
    if not added and not updated and not kept_old:
        print("  (无变化)")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        print("选项:")
        print("  --dry-run   预览变更，不实际写入")
        sys.exit(1)

    dry_run = "--dry-run" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    tcl_path = args[0]
    config_path = args[1] if len(args) > 1 else "config/config.yaml"

    if not Path(tcl_path).exists():
        print(f"✗ Tcl file not found: {tcl_path}")
        sys.exit(1)

    print(f"解析: {tcl_path}")
    owners = parse_tcl_owners(tcl_path)
    print(f"  找到 {len(owners)} 个模块")
    for mod, users in sorted(owners.items()):
        print(f"    {mod}: {', '.join(users)}")

    print()
    update_config(config_path, owners, dry_run)


if __name__ == "__main__":
    main()
