"""Gevent-specific tests for SyncRepository parallel_mode.

These tests require gevent to be importable and are skipped under xdist
(gevent monkey-patching is incompatible with xdist workers).
Run with: pytest tests/unit/ -m gevent -n 0
"""

import pytest

pytest.importorskip("gevent")

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
        repo = SyncRepository(
            name="test", region="us-east-1", _skip_deprecation_warning=True, parallel_mode="gevent"
        )
        result = repo._run_in_executor(lambda: 42, lambda: 99)
        assert result == (42, 99)


class TestGeventSpawn:
    """`_spawn` with real gevent monkey-patching (run with GEVENT=1, -m gevent -n 0)."""

    def test_spawn_runs_in_a_greenlet_and_holds_exceptions(self):
        from gevent import monkey

        if not monkey.is_module_patched("socket"):
            pytest.skip("gevent not monkey-patched (run with GEVENT=1)")
        repo = SyncRepository(
            name="test", region="us-east-1", _skip_deprecation_warning=True, parallel_mode="auto"
        )
        assert repo._spawn(lambda: 42).result() == 42

        def fail():
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            repo._spawn(fail).result()

    def test_cancelling_a_running_spawn_kills_it_and_result_raises_cancelled(self):
        """A running spawn's `.cancel()` returns False; `.result()` is CancelledError."""
        from concurrent.futures import CancelledError

        import gevent
        from gevent import monkey

        if not monkey.is_module_patched("socket"):
            pytest.skip("gevent not monkey-patched (run with GEVENT=1)")
        repo = SyncRepository(
            name="test", region="us-east-1", _skip_deprecation_warning=True, parallel_mode="gevent"
        )
        future = repo._spawn(lambda: gevent.sleep(1))
        gevent.sleep(0.01)  # let it start
        assert future.cancel() is False
        with pytest.raises(CancelledError):
            future.result(timeout=2)


class TestGeventOwnerCheckJoin:
    """The sync lease's join of a killed owner-check greenlet (ADR-150, #716 review)."""

    def test_a_killed_check_forces_the_slow_pass(self):
        from types import SimpleNamespace

        import gevent
        from gevent import monkey

        from zae_limiter.sync_lease import SyncLease

        if not monkey.is_module_patched("socket"):
            pytest.skip("gevent not monkey-patched (run with GEVENT=1)")
        repo = SyncRepository(
            name="test", region="us-east-1", _skip_deprecation_warning=True, parallel_mode="gevent"
        )
        ran: list[str] = []
        writes: list[tuple] = []

        def slow_check(*args):
            ran.append("started")
            gevent.sleep(5)
            ran.append("finished")
            return False

        backend = SimpleNamespace(
            start_background=repo.start_background,
            repair_created_owner_stamp=slow_check,
            build_vu_reset=lambda *a: ("vu=0", *a),
            write_each=writes.extend,
        )
        lease = SyncLease(repository=backend)
        lease._start_owner_checks([("user", "gpt-4", 0, 1, "inc")])
        gevent.sleep(0.01)  # the check is running
        lease._owner_task.greenlet.kill()  # killed by someone other than the joiner

        lease._await_owner_checks()  # nobody cancelled the caller: it returns

        assert ran == ["started"]  # the check never finished...
        assert writes == [("vu=0", "user", "gpt-4", 0)]  # ...so the bucket is forced
        assert lease._owner_task is None

    def test_a_check_killed_before_it_starts_does_not_hang_the_lease_exit(self):
        """A greenlet killed before it runs must not leave its future pending forever."""
        from types import SimpleNamespace

        import gevent
        from gevent import monkey

        from zae_limiter.sync_lease import SyncLease

        if not monkey.is_module_patched("socket"):
            pytest.skip("gevent not monkey-patched (run with GEVENT=1)")
        repo = SyncRepository(
            name="test", region="us-east-1", _skip_deprecation_warning=True, parallel_mode="gevent"
        )
        ran: list[str] = []
        writes: list[tuple] = []
        backend = SimpleNamespace(
            start_background=repo.start_background,
            repair_created_owner_stamp=lambda *a: ran.append("ran"),
            build_vu_reset=lambda *a: ("vu=0", *a),
            write_each=writes.extend,
        )
        lease = SyncLease(repository=backend)
        lease._start_owner_checks([("user", "gpt-4", 0, 1, "inc")])
        lease._owner_task.greenlet.kill()  # before it ever ran

        with gevent.Timeout(5):  # a hang fails here instead of stalling the suite
            lease._await_owner_checks()

        assert ran == []  # the check never ran...
        assert writes == [("vu=0", "user", "gpt-4", 0)]  # ...so the bucket is forced
        assert lease._owner_task is None
