"""YAML configuration loader with pydantic validation."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional, Union

import yaml
from loguru import logger
from pydantic import BaseModel, ValidationError

class ModuleOwnerConfig(BaseModel):
    """Per-module submit permission: which users can submit this module."""
    owners: List[str] = []


class GateDef(BaseModel):
    name: str
    command: str
    on_fail: str = ""   # optional: custom hint shown when this gate fails


class TagConfig(BaseModel):
    description: str = ""
    modules: List[str] = []
    exclude_modules: List[str] = []
    file_patterns: List[str] = []
    gates: List[GateDef] = []
    release_users: List[str] = []


class AppConfig(BaseModel):
    shared_group: str = "staff"
    stale_lock_minutes: int = 10  # 0 = disable auto-clean
    file_groups: Dict[str, List[str]] = {}
    modules: Dict[str, ModuleOwnerConfig] = {}
    admins: List[str] = []
    outgoing_root: Union[str, List[str]] = "./a0.outgoing"
    repository_root: str = "./repository"
    log_dir: str = "./logs"
    db_path: Optional[str] = None  # None = use <repository_root>/ddm.db; NFS → set to local path
    defaults: Dict[str, Dict[str, TagConfig]] = {}


class Config:
    """Loads and exposes YAML configuration."""

    def __init__(self, config_path: str = "config/config.yaml", quiet: bool = False):
        self.config_path = config_path
        self._raw: dict = {}
        self._model: Optional[AppConfig] = None
        self.load(quiet=quiet)

    def load(self, quiet: bool = False):
        path = Path(self.config_path).resolve()
        # Config paths (./a0.outgoing, ./repository) are relative to the
        # project root. With the standard layout (config/config.yaml),
        # that is two levels up from the config file.
        if path.parent.name == "config" and not path.parent.parent.name.startswith("/"):
            self._project_root = str(path.parent.parent)
        else:
            self._project_root = str(path.parent)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {self.config_path}")

        with open(path, "r") as f:
            self._raw = yaml.safe_load(f)

        try:
            self._model = AppConfig(**self._raw)
        except ValidationError as e:
            logger.error(f"Config validation failed: {e}")
            raise

        if not quiet:
            logger.info(f"Config loaded from {self.config_path}")

    # ---- accessors ----

    @property
    def admins(self) -> List[str]:
        return self._model.admins if self._model else []

    def module_owners(self, module: str) -> List[str]:
        """Return list of users who can submit data for this module."""
        if self._model and module in self._model.modules:
            return self._model.modules[module].owners
        return []

    def is_module_owner(self, module: str, user: str) -> bool:
        """Check whether user is allowed to submit this module."""
        # Admins bypass owner check
        if user in self.admins:
            return True
        return user in self.module_owners(module)

    @property
    def outgoing_roots(self) -> List[str]:
        """Return all outgoing_root templates as a list.

        Accepts both:
          outgoing_root: /home/{user}/a0.outgoing/{module}
          outgoing_root: [/home/{user}/a0.outgoing/{module}, /nfs/shared/{module}]
        """
        raw = self._model.outgoing_root if self._model else "./a0.outgoing"
        if isinstance(raw, str):
            return [raw]
        return raw

    @property
    def outgoing_root(self) -> str:
        """First outgoing_root template (for backward compat)."""
        return self.outgoing_roots[0]

    @property
    def outgoing_roots_resolved(self) -> List[str]:
        """Absolute paths for all outgoing roots, preserving {user}/{module}."""
        result = []
        for raw in self.outgoing_roots:
            path = Path(raw)
            if path.is_absolute():
                result.append(raw)
            else:
                result.append(str(self.resolve_path(raw)))
        return result

    @property
    def outgoing_root_resolved(self) -> str:
        """First resolved outgoing root (for backward compat)."""
        return self.outgoing_roots_resolved[0]

    @property
    def repository_root(self) -> str:
        return self._model.repository_root if self._model else "./repository"

    @property
    def log_dir(self) -> str:
        return self._model.log_dir if self._model else "./logs"

    def tag_config(self, tag: str) -> Optional[TagConfig]:
        """Return TagConfig for a given tag, or None."""
        if self._model and "tag" in self._model.defaults:
            return self._model.defaults["tag"].get(tag)
        return None

    def tag_names(self) -> List[str]:
        if self._model and "tag" in self._model.defaults:
            return list(self._model.defaults["tag"].keys())
        return []

    def modules_for(self, tag: str) -> List[str]:
        tc = self.tag_config(tag)
        if tc and tc.modules:
            return tc.modules
        # Default to all configured modules, minus exclude_modules
        all_mods = sorted(self._raw.get("modules", {}).keys())
        if tc and tc.exclude_modules:
            return [m for m in all_mods if m not in tc.exclude_modules]
        return all_mods

    def file_patterns_for(self, tag: str) -> List[str]:
        tc = self.tag_config(tag)
        return tc.file_patterns if tc else []

    def gates_for(self, tag: str) -> List[GateDef]:
        tc = self.tag_config(tag)
        return tc.gates if tc else []

    def release_users_for(self, tag: str) -> List[str]:
        tc = self.tag_config(tag)
        return tc.release_users if tc else []

    # ---- helpers (relative paths resolve against config.yaml directory) ----

    def resolve_path(self, rel_path: str) -> Path:
        """Resolve a config-relative path to absolute. Supports {user} placeholders."""
        p = Path(rel_path)
        if p.is_absolute():
            return p
        return (Path(self._project_root) / p).resolve()

    def raw_dir(self) -> Path:
        return self.resolve_path(self.repository_root) / "raw"

    def ready_dir(self) -> Path:
        return self.resolve_path(self.repository_root) / "ready"

    def release_dir(self) -> Path:
        return self.resolve_path(self.repository_root) / "release"

    @property
    def stale_lock_minutes(self) -> int:
        """Return stale lock timeout in minutes (0 = disabled)."""
        return self._model.stale_lock_minutes if self._model else 10

    @property
    def shared_group(self) -> str:
        """Return the shared group name for multi-user access (default: staff)."""
        return self._model.shared_group if self._model else "staff"

    @property
    def shared_group_gid(self) -> int:
        """Return the GID of the shared group (cached after first lookup)."""
        name = self.shared_group
        if not hasattr(self, "_shared_group_gid_cache") or self._shared_group_gid_cache.get("name") != name:
            import grp
            try:
                self._shared_group_gid_cache = {"name": name, "gid": grp.getgrnam(name).gr_gid}
            except KeyError:
                self._shared_group_gid_cache = {"name": name, "gid": None}
        gid = self._shared_group_gid_cache.get("gid")
        if gid is None:
            raise KeyError(f"Shared group '{name}' not found on system")
        return gid

    @property
    def file_groups(self) -> dict:
        """Return the global file_groups mapping (group_name → suffix patterns)."""
        return self._model.file_groups if self._model else {}

    def classify_file(self, filename: str, module: str) -> str:
        """Classify a file into its group based on suffix matching.

        filename  like "CPU.v.gz" or "CPU.final.hier.gds"
        module    like "CPU"

        Returns the group name (e.g. "verilog", "gds"), or "" if unmatched.

        Matching rule:
          1. Strip the module name prefix → ".v.gz" or ".final.hier.gds"
          2. For each group (in declaration order), check if the stripped suffix
             ends with any pattern in that group.
          3. First match wins.
          4. Return "" if no group matches.
        """
        stripped = filename[len(module):]  # "CPU.v.gz" → ".v.gz"
        for group_name, patterns in self.file_groups.items():
            for pat in patterns:
                if stripped.endswith(pat):
                    return group_name
        return ""

    def db_path(self) -> str:
        """Return the SQLite database path.

        If config.yaml sets ``db_path`` (recommended when repository_root is on
        NFS), use that.  Otherwise default to ``<repository_root>/ddm.db``.
        """
        if self._model and self._model.db_path is not None:
            return self._model.db_path
        return str(self.resolve_path(self.repository_root) / "ddm.db")

    def log_path(self) -> str:
        return str(self.resolve_path(self.log_dir))

    def release_lock_path(self, tag: str) -> Path:
        return self.ready_dir() / f".lock_release_{tag}"

    def module_lock_path(self, module: str, tag: str) -> Path:
        return self.raw_dir() / f".lock_{module}_{tag}"
