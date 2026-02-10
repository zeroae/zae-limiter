"""Gevent-specific tests for SyncRepository parallel_mode.

These tests require gevent to be importable and are skipped under xdist
(gevent monkey-patching is incompatible with xdist workers).
Run with: pytest tests/unit/ -m gevent -n 0
"""

import pytest

from zae_limiter.sync_repository import SyncRepository

pytestmark = pytest.mark.gevent


class TestGeventParallelMode:
    """Tests for gevent parallel_mode behavior."""

    def test_auto_with_gevent_patched_returns_gevent_executor(self):
        from gevent import monkey

        if not monkey.is_module_patched("socket"):
            pytest.skip("gevent not monkey-patched (run with GEVENT=1)")
        executor = SyncRepository._resolve_parallel_mode("auto")
        assert executor is not None
        result = executor([lambda: 1, lambda: 2])
        assert result == (1, 2)

    def test_gevent_mode_returns_executor(self):
        executor = SyncRepository._resolve_parallel_mode("gevent")
        assert executor is not None
        result = executor([lambda: "x", lambda: "y"])
        assert result == ("x", "y")

    def test_gevent_executor_propagates_exceptions(self):
        executor = SyncRepository._resolve_parallel_mode("gevent")

        def fail():
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            executor([fail])

    def test_run_in_executor_with_gevent_mode(self):
        repo = SyncRepository(name="test", region="us-east-1", parallel_mode="gevent")
        result = repo._run_in_executor(lambda: 42, lambda: 99)
        assert result == (42, 99)
