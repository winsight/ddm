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
        assert "{user}_{module}.v.gz" in patterns
        assert "{user}_{module}.hier.gds" in patterns

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
            ["{user}_{module}.v.gz", "{user}_{module}.hier.gds"],
            "wangshuai",
            "CPU",
        )
        assert any("wangshuai_CPU.v.gz" in f for f in files)
        assert any("wangshuai_CPU.hier.gds" in f for f in files)

    def test_find_different_user(self):
        from ddm.services import find_source_files
        files = find_source_files(
            "a0.outgoing/{user}/{module}",
            ["{user}_{module}.v.gz"],
            "w00949819",
            "CPU",
        )
        assert any("w00949819_CPU.v.gz" in f for f in files)

    def test_no_match(self):
        from ddm.services import find_source_files
        files = find_source_files(
            "a0.outgoing/{user}/{module}",
            ["{user}_{module}.v.gz"],
            "nonexistent",
            "CPU",
        )
        assert len(files) == 0
