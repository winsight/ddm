"""YAML configuration loader with pydantic validation."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional

import yaml
from loguru import logger
from pydantic import BaseModel, ValidationError, validator

# Valid tag enum
VALID_TAGS = {"PV_ITER", "LVS_PASS", "BASE_CLEAN", "PV_FINAL", "PI_ITER", "PI_FINAL"}


class ModuleOwnerConfig(BaseModel):
    """Per-module submit permission: which users can submit this module."""
    owners: List[str] = []


class GateDef(BaseModel):
    name: str
    command: str


class TagConfig(BaseModel):
    description: str = ""
    modules: List[str] = []
    file_patterns: List[str] = []
    gates: List[GateDef] = []
    release_users: List[str] = []


class AppConfig(BaseModel):
    shared_group: str = "staff"
    file_groups: Dict[str, List[str]] = {}
    modules: Dict[str, ModuleOwnerConfig] = {}
    admins: List[str] = []
    outgoing_root: str = "./a0.outgoing"
    repository_root: str = "./repository"
    log_dir: str = "./logs"
    defaults: Dict[str, Dict[str, TagConfig]] = {}

    @validator("defaults")
    def check_tags(cls, v):
        if "tag" in v:
            for tag_name in v["tag"]:
                if tag_name not in VALID_TAGS:
                    logger.warning(f"Unknown tag '{tag_name}' in config — add to VALID_TAGS if intentional")
        return v


class Config:
    """Loads and exposes YAML configuration."""

    def __init__(self, config_path: str = "config/config.yaml"):
        self.config_path = config_path
        self._raw: dict = {}
        self._model: Optional[AppConfig] = None
        self.load()

    def load(self):
        path = Path(self.config_path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {self.config_path}")

        with open(path, "r") as f:
            self._raw = yaml.safe_load(f)

        try:
            self._model = AppConfig(**self._raw)
        except ValidationError as e:
            logger.error(f"Config validation failed: {e}")
            raise

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
    def outgoing_root(self) -> str:
        return self._model.outgoing_root if self._model else "./a0.outgoing"

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
        return tc.modules if tc else []

    def file_patterns_for(self, tag: str) -> List[str]:
        tc = self.tag_config(tag)
        return tc.file_patterns if tc else []

    def gates_for(self, tag: str) -> List[GateDef]:
        tc = self.tag_config(tag)
        return tc.gates if tc else []

    def release_users_for(self, tag: str) -> List[str]:
        tc = self.tag_config(tag)
        return tc.release_users if tc else []

    # ---- helpers ----

    def resolve_outgoing_path(self) -> Path:
        return Path(self.outgoing_root).resolve()

    def raw_dir(self) -> Path:
        return Path(self.repository_root) / "raw"

    def ready_dir(self) -> Path:
        return Path(self.repository_root) / "ready"

    def release_dir(self) -> Path:
        return Path(self.repository_root) / "release"

    @property
    def shared_group(self) -> str:
        """Return the shared group name for multi-user access (default: staff)."""
        return self._model.shared_group if self._model else "staff"

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
        return str(Path(self.repository_root) / "ddm.db")

    def log_path(self) -> str:
        return str(Path(self.log_dir))

    def global_lock_path(self) -> Path:
        return self.ready_dir() / ".lock_global_release"

    def module_lock_path(self, module: str, tag: str) -> Path:
        return Path(self.repository_root) / "raw" / f".lock_{module}_{tag}"
