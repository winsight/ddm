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
    """Update config.yaml modules section with parsed owners."""
    path = Path(config_path)
    if not path.exists():
        print(f"✗ Config not found: {config_path}")
        return

    with open(path) as f:
        config = yaml.safe_load(f)

    if "modules" not in config:
        config["modules"] = {}

    updated = []
    added = []
    kept_old = []

    for module, users in new_owners.items():
        if module in config.get("modules", {}):
            # Merge: keep existing owners that aren't in the new list
            existing = set(config["modules"][module].get("owners", []))
            new_set = set(users)
            merged = list(new_set | existing)  # union: new + existing
            if set(config["modules"][module].get("owners", [])) != set(merged):
                config["modules"][module]["owners"] = merged
                updated.append(module)
            else:
                # No change
                pass
        else:
            config["modules"][module] = {"owners": users}
            added.append(module)

    # Modules in config but NOT in Tcl — keep as-is (handover)
    for module in config.get("modules", {}):
        if module not in new_owners:
            kept_old.append(module)

    if dry_run:
        print("=== DRY RUN (no changes written) ===")
    else:
        with open(path, "w") as f:
            yaml.safe_dump(
                config, f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )
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
