"""Integration tests for DDM services."""

import os
import shutil
import tempfile
from pathlib import Path

import pytest

# Add project root to path
sys_path = str(Path(__file__).resolve().parent.parent)
import sys
if sys_path not in sys.path:
    sys.path.insert(0, sys_path)


class TestConfig:
    def test_load_config(self):
        from ddm.config import Config
        cfg = Config("config/config.yaml")
        assert "PV_ITER" in cfg.tag_names()
        assert "PI_ITER" in cfg.tag_names()
        assert "{user}" in cfg.outgoing_root

    def test_tag_config(self):
        from ddm.config import Config
        cfg = Config("config/config.yaml")
        tc = cfg.tag_config("PV_ITER")
        assert tc is not None
        assert len(tc.file_patterns) > 0
        assert tc.description == "物理验证迭代版"
        assert "w00949819" in tc.release_users

    def test_file_patterns(self):
        from ddm.config import Config
        cfg = Config("config/config.yaml")
        patterns = cfg.file_patterns_for("PV_ITER")
        assert "{module}.v.gz" in patterns
        assert "{module}.v.pg" in patterns

    def test_gates_release_users(self):
        from ddm.config import Config
        cfg = Config("config/config.yaml")

        pv_gates = cfg.gates_for("PV_ITER")
        assert len(pv_gates) == 2
        assert pv_gates[0].name == "verilog_syntax_check"
        assert pv_gates[1].name == "drc_baseline_check"

        pv_users = cfg.release_users_for("PV_ITER")
        assert "w00949819" in pv_users
        pi_users = cfg.release_users_for("PI_ITER")
        assert "wangshuai" in pi_users

    def test_module_owners(self):
        from ddm.config import Config
        cfg = Config("config/config.yaml")

        # CPU: configured with wangshuai + zhangsan
        cpu_owners = cfg.module_owners("CPU")
        assert "wangshuai" in cpu_owners
        assert "zhangsan" in cpu_owners

        # DDR: configured with lisi + wangshuai
        ddr_owners = cfg.module_owners("DDR")
        assert "lisi" in ddr_owners
        assert "wangshuai" in ddr_owners

        # Unknown module: empty list
        assert cfg.module_owners("NONEXISTENT") == []

    def test_is_module_owner(self):
        from ddm.config import Config
        cfg = Config("config/config.yaml")

        # Owner can submit
        assert cfg.is_module_owner("CPU", "wangshuai") is True
        assert cfg.is_module_owner("CPU", "zhangsan") is True

        # Admin bypasses owner check
        assert cfg.is_module_owner("CPU", "w00949819") is True
        assert cfg.is_module_owner("CPU", "lisi") is True

        # Non-owner, non-admin: denied
        assert cfg.is_module_owner("CPU", "randomuser") is False

        # admin can submit any module even unconfigured
        assert cfg.is_module_owner("NONEXISTENT", "w00949819") is True

        # non-admin on unconfigured module: denied
        assert cfg.is_module_owner("NONEXISTENT", "randomuser") is False


class TestStorage:
    @pytest.fixture
    def storage(self):
        from ddm.storage import Storage
        db_path = tempfile.mktemp(suffix=".db")
        s = Storage(db_path)
        yield s
        try:
            os.unlink(db_path)
        except OSError:
            pass

    def test_create_batch(self, storage):
        uuid = storage.create_batch("CPU", "PV_ITER", "wangshuai", "test summary")
        assert uuid
        batch = storage.get_batch(uuid)
        assert batch is not None
        assert batch["module"] == "CPU"
        assert batch["tag"] == "PV_ITER"
        assert batch["status"] == "PENDING"

    def test_update_status(self, storage):
        uuid = storage.create_batch("CPU", "PV_ITER", "wangshuai")
        storage.update_batch_status(uuid, "SUBMITTED")
        batch = storage.get_batch(uuid)
        assert batch["status"] == "SUBMITTED"

    def test_list_batches(self, storage):
        storage.create_batch("CPU", "PV_ITER", "wangshuai")
        storage.create_batch("DDR", "PV_ITER", "wangshuai")
        storage.create_batch("CPU", "PI_ITER", "wangshuai")

        all_batches = storage.list_batches()
        assert len(all_batches) == 3

        cpu_batches = storage.list_batches(module="CPU")
        assert len(cpu_batches) == 2

        pv_batches = storage.list_batches(tag="PV_ITER")
        assert len(pv_batches) == 2

    def test_add_file(self, storage):
        uuid = storage.create_batch("CPU", "PV_ITER", "wangshuai")
        fid = storage.add_file(uuid, "/tmp/test.v.gz", 1024, "abc123")
        assert fid
        files = storage.get_files(uuid)
        assert len(files) == 1
        assert files[0]["file_size"] == 1024
        assert files[0]["blake3_hash"] == "abc123"

    def test_events(self, storage):
        uuid = storage.create_batch("CPU", "PV_ITER", "wangshuai")
        storage.add_event(uuid, "submit_start", "test")
        storage.add_event(uuid, "copy_done", "3 files")
        events = storage.get_events(uuid)
        assert len(events) == 2
        assert events[0]["event_type"] == "submit_start"

    def test_invalid_status(self, storage):
        from ddm.storage import VALID_STATUSES
        assert "INVALID" not in VALID_STATUSES
        with pytest.raises(ValueError):
            storage.update_batch_status("some-uuid", "INVALID")


class TestFindSourceFiles:
    def test_find_flat_files(self):
        from ddm.services import find_source_files
        files = find_source_files(
            "a0.outgoing/{user}/{module}",
            ["{module}.v.gz", "{module}.hier.gds"],
            "w00949819",
            "CPU",
        )
        assert any("CPU.v.gz" in f for f in files)
        assert any("CPU.hier.gds" in f for f in files)

    def test_find_different_user(self):
        from ddm.services import find_source_files
        files = find_source_files(
            "a0.outgoing/{user}/{module}",
            ["{module}.v.gz"],
            "w00949819",
            "CPU",
        )
        assert any("CPU.v.gz" in f for f in files)

    def test_no_match(self):
        from ddm.services import find_source_files
        files = find_source_files(
            "a0.outgoing/{user}/{module}",
            ["{module}.v.gz"],
            "nonexistent",
            "CPU",
        )
        assert len(files) == 0


class TestFileGroups:
    """Tests for file_groups classification and release directory structure."""

    def test_file_groups_loaded(self):
        from ddm.config import Config
        cfg = Config("config/config.yaml")
        groups = cfg.file_groups
        assert "verilog" in groups
        assert "gds" in groups
        assert "pg" in groups
        assert ".v.gz" in groups["verilog"]
        assert ".hier.gds" in groups["gds"]
        assert ".v.pg" in groups["pg"]

    def test_classify_verilog(self):
        from ddm.config import Config
        cfg = Config("config/config.yaml")
        assert cfg.classify_file("CPU.v.gz", "CPU") == "verilog"
        assert cfg.classify_file("CPU.base.v.gz", "CPU") == "verilog"
        assert cfg.classify_file("CPU.final.v.gz", "CPU") == "verilog"
        assert cfg.classify_file("DDR.v.gz", "DDR") == "verilog"

    def test_classify_gds(self):
        from ddm.config import Config
        cfg = Config("config/config.yaml")
        assert cfg.classify_file("CPU.hier.gds", "CPU") == "gds"
        assert cfg.classify_file("CPU.final.hier.gds", "CPU") == "gds"
        assert cfg.classify_file("CPU.lvs.gds", "CPU") == "gds"
        # .gds.gz → stripped suffix ".pi.final.gds.gz" ends with ".gds.gz"
        assert cfg.classify_file("CPU.pi.final.gds.gz", "CPU") == "gds"

    def test_classify_pg(self):
        from ddm.config import Config
        cfg = Config("config/config.yaml")
        assert cfg.classify_file("CPU.v.pg", "CPU") == "pg"
        assert cfg.classify_file("CPU.pi.v.pg", "CPU") == "pg"
        assert cfg.classify_file("CPU.pi.final.v.pg", "CPU") == "pg"

    def test_classify_unmatched(self):
        from ddm.config import Config
        cfg = Config("config/config.yaml")
        assert cfg.classify_file("CPU.unknown.xyz", "CPU") == ""
        assert cfg.classify_file("CPU.raw", "CPU") == ""
        assert cfg.classify_file("CPU.tar.gz", "CPU") == ""

    def test_classify_first_match_wins(self):
        """If a suffix could match multiple groups, first declared wins."""
        from ddm.config import Config
        cfg = Config("config/config.yaml")
        # .v.gz is declared in verilog (first), so it should be verilog
        assert cfg.classify_file("CPU.v.gz", "CPU") == "verilog"

    def test_empty_file_groups(self):
        """Without file_groups config, classify_file always returns ''."""
        from ddm.config import Config, AppConfig
        # Build a Config with empty file_groups
        cfg = Config.__new__(Config)
        cfg.config_path = ""
        cfg._raw = {}
        cfg._model = AppConfig(
            file_groups={},
            outgoing_root="./a0.outgoing",
            repository_root="./repository",
            log_dir="./logs",
            defaults={"tag": {}},
        )
        assert cfg.classify_file("CPU.v.gz", "CPU") == ""
        assert cfg.classify_file("CPU.hier.gds", "CPU") == ""
        assert cfg.file_groups == {}

    def test_release_with_file_groups(self, tmp_path):
        """End-to-end: submit → release → verify grouped directory structure."""
        from ddm.config import Config, AppConfig, TagConfig, GateDef
        from ddm.services import release
        from ddm.storage import STATUS_SUBMITTED, STATUS_RELEASED, Storage

        cfg = Config.__new__(Config)
        cfg.config_path = ""
        cfg._raw = {}

        repo = tmp_path / "repo"
        repo.mkdir()
        out_dir = tmp_path / "a0.outgoing" / "testuser" / "CPU"
        out_dir.mkdir(parents=True)

        # Create test files
        (out_dir / "CPU.v.gz").write_text("verilog content")
        (out_dir / "CPU.hier.gds").write_text("gds content")
        (out_dir / "CPU.v.pg").write_text("pg content")
        (out_dir / "CPU.unknown.xyz").write_text("unmatched content")

        cfg._model = AppConfig(
            file_groups={
                "verilog": [".v.gz"],
                "gds": [".hier.gds"],
                "pg": [".v.pg"],
            },
            modules={},
            admins=["testuser"],
            outgoing_root=str(tmp_path / "a0.outgoing" / "{user}" / "{module}"),
            repository_root=str(repo),
            log_dir=str(tmp_path / "logs"),
            defaults={
                "tag": {
                    "PV_ITER": TagConfig(
                        description="test",
                        modules=["CPU"],
                        file_patterns=[
                            "{module}.v.gz",
                            "{module}.hier.gds",
                            "{module}.v.pg",
                            "{module}.unknown.xyz",
                        ],
                        gates=[],
                        release_users=["testuser"],
                    )
                }
            },
        )

        # Setup ready/ directory as if submit completed
        ready_dir = cfg.ready_dir() / "PV_ITER" / "CPU"
        ready_dir.mkdir(parents=True)
        for f in out_dir.iterdir():
            shutil.copy2(str(f), str(ready_dir / f.name))

        # Create SUBMITTED batch in storage
        db_path = str(repo / "ddm.db")
        storage = Storage(db_path)
        batch_uuid = storage.create_batch("CPU", "PV_ITER", "testuser")
        for f in sorted(out_dir.iterdir()):
            storage.add_file(batch_uuid, str(f), f.stat().st_size, "fakehash")
            storage.update_file_raw(batch_uuid, str(f),
                                    str(ready_dir / f.name), f.stat().st_size, "fakehash")
            storage.update_file_ready(batch_uuid, str(f), str(ready_dir / f.name))
        storage.update_batch_status(batch_uuid, STATUS_SUBMITTED)

        # Release
        result = release(cfg, storage, tag="PV_ITER", version="V1",
                         module="CPU", release_all=False,
                         username="testuser")

        assert result.success, f"Release failed: {result.message}"

        # Verify directory structure
        version_dir = cfg.release_dir() / "PV_ITER" / result.version
        assert version_dir.is_dir()

        # Grouped files — directly under version dir, no module subdirectory
        verilog_file = version_dir / "verilog" / "CPU.v.gz"
        gds_file = version_dir / "gds" / "CPU.hier.gds"
        pg_file = version_dir / "pg" / "CPU.v.pg"
        root_file = version_dir / "CPU.unknown.xyz"

        assert verilog_file.is_file(), f"Expected {verilog_file}"
        assert gds_file.is_file(), f"Expected {gds_file}"
        assert pg_file.is_file(), f"Expected {pg_file}"
        # Unmatched file stays at version root
        assert root_file.is_file(), f"Expected {root_file} at version root"

        # Verify @latest symlink
        latest = cfg.release_dir() / "PV_ITER" / "@latest"
        assert latest.is_symlink()
        assert latest.resolve().name == result.version

        # Verify batch status → RELEASED
        batch = storage.get_batch(batch_uuid)
        assert batch["status"] == STATUS_RELEASED
