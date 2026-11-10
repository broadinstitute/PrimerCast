"""Tests for primercast.utils.cpu."""

from unittest.mock import patch

from primercast.utils import cpu


class TestAvailableCores:
    def _cores(self, affinity, limit):
        with patch.object(cpu.os, "sched_getaffinity", lambda pid: set(range(affinity)),
                          create=True), \
                patch.object(cpu, "_cgroup_limit", return_value=limit):
            return cpu.available_cores()

    def test_capped_by_container_limit(self):
        # Cloud Run: host reports many CPUs, the instance is limited to 8.
        assert self._cores(affinity=64, limit=8.0) == 8

    def test_fractional_limit_rounds_up(self):
        assert self._cores(affinity=64, limit=1.5) == 2

    def test_no_limit_uses_affinity(self):
        assert self._cores(affinity=6, limit=None) == 6

    def test_at_least_one(self):
        assert self._cores(affinity=4, limit=0.1) == 1


class TestCgroupLimit:
    def test_v2_quota(self, tmp_path):
        (tmp_path / "cpu.max").write_text("800000 100000\n")
        real = cpu.Path
        with patch.object(cpu, "Path", lambda p: real(str(p).replace("/sys/fs/cgroup", str(tmp_path)))):
            assert cpu._cgroup_limit() == 8.0

    def test_v2_unlimited(self, tmp_path):
        (tmp_path / "cpu.max").write_text("max 100000\n")
        real = cpu.Path
        with patch.object(cpu, "Path", lambda p: real(str(p).replace("/sys/fs/cgroup", str(tmp_path)))):
            assert cpu._cgroup_limit() is None

    def test_missing_files(self, tmp_path):
        real = cpu.Path
        with patch.object(cpu, "Path", lambda p: real(str(p).replace("/sys/fs/cgroup", str(tmp_path)))):
            assert cpu._cgroup_limit() is None
