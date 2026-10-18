"""Tests for SyncRepository parallel_mode parameter."""

from unittest.mock import patch

import pytest

from zae_limiter.sync_repository import SyncRepository


class TestResolveParallelMode:
    """Tests for _resolve_parallel_mode static method."""

    def test_serial_returns_callable(self):
        executor = SyncRepository._resolve_parallel_mode("serial")
        assert executor is not None
        result = executor([lambda: 1, lambda: 2])
        assert result == (1, 2)

    def test_threadpool_returns_none(self):
        with patch("os.cpu_count", return_value=4):
            executor = SyncRepository._resolve_parallel_mode("threadpool")
        assert executor is None

    def test_threadpool_single_cpu_warns(self):
        with patch("os.cpu_count", return_value=1):
            with pytest.warns(UserWarning, match="single-CPU"):
                executor = SyncRepository._resolve_parallel_mode("threadpool")
        assert executor is None

    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError, match="Invalid parallel_mode"):
            SyncRepository._resolve_parallel_mode("invalid")

    def test_auto_without_gevent_multi_cpu_returns_none(self):
        with (
            patch.dict("sys.modules", {"gevent": None}),
            patch("os.cpu_count", return_value=4),
        ):
            executor = SyncRepository._resolve_parallel_mode("auto")
        assert executor is None

    def test_auto_without_gevent_single_cpu_returns_serial(self):
        with (
            patch.dict("sys.modules", {"gevent": None}),
            patch("os.cpu_count", return_value=1),
        ):
            executor = SyncRepository._resolve_parallel_mode("auto")
        assert executor is not None
        result = executor([lambda: 10, lambda: 20])
        assert result == (10, 20)

    def test_gevent_without_patching_warns(self):
        pytest.importorskip("gevent")
        with pytest.warns(UserWarning, match="without monkey-patching"):
            executor = SyncRepository._resolve_parallel_mode("gevent")
        assert executor is not None


class TestRunInExecutor:
    """Tests for _run_in_executor method."""

    def test_serial_mode_executes_sequentially(self):
        repo = SyncRepository.__new__(SyncRepository)
        repo._executor_fn = lambda funcs: tuple(fn() for fn in funcs)
        repo._thread_pool = None

        result = repo._run_in_executor(lambda: "a", lambda: "b")
        assert result == ("a", "b")

    def test_threadpool_mode_creates_pool_lazily(self):
        repo = SyncRepository.__new__(SyncRepository)
        repo._executor_fn = None
        repo._thread_pool = None

        result = repo._run_in_executor(lambda: 1, lambda: 2)
        assert result == (1, 2)
        assert repo._thread_pool is not None
        repo._cleanup_thread_pool()

    def test_cleanup_thread_pool(self):
        repo = SyncRepository.__new__(SyncRepository)
        repo._executor_fn = None
        repo._thread_pool = None

        repo._run_in_executor(lambda: 1)
        assert repo._thread_pool is not None

        repo._cleanup_thread_pool()
        assert repo._thread_pool is None

    def test_cleanup_thread_pool_noop_when_none(self):
        repo = SyncRepository.__new__(SyncRepository)
        repo._thread_pool = None
        repo._cleanup_thread_pool()
        assert repo._thread_pool is None


class TestParallelModeConstructor:
    """Tests for parallel_mode parameter on SyncRepository constructor."""

    def test_default_is_auto(self):
        repo = SyncRepository(name="test", region="us-east-1", _skip_deprecation_warning=True)
        assert repo._parallel_mode == "auto"

    def test_serial_mode(self):
        repo = SyncRepository(
            name="test", region="us-east-1", _skip_deprecation_warning=True, parallel_mode="serial"
        )
        assert repo._parallel_mode == "serial"
        assert repo._executor_fn is not None

    def test_threadpool_mode(self):
        repo = SyncRepository(
            name="test",
            region="us-east-1",
            _skip_deprecation_warning=True,
            parallel_mode="threadpool",
        )
        assert repo._parallel_mode == "threadpool"
        assert repo._thread_pool is None

    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError, match="Invalid parallel_mode"):
            SyncRepository(
                name="test", region="us-east-1", _skip_deprecation_warning=True, parallel_mode="bad"
            )


def _repo(mode: str) -> SyncRepository:
    return SyncRepository(
        name="test", region="us-east-1", _skip_deprecation_warning=True, parallel_mode=mode
    )


def _fail() -> None:
    raise RuntimeError("boom")


class TestSpawn:
    """`_spawn`, the sync twin of `asyncio.create_task`, in every parallel_mode."""

    @pytest.mark.parametrize("mode", ["serial", "threadpool", "auto"])
    def test_result_returns_the_value(self, mode):
        repo = _repo(mode)
        assert repo._spawn(lambda: 42).result() == 42
        repo._cleanup_thread_pool()

    @pytest.mark.parametrize("mode", ["serial", "threadpool", "auto"])
    def test_an_exception_is_held_and_raised_from_result(self, mode):
        repo = _repo(mode)
        future = repo._spawn(_fail)
        with pytest.raises(RuntimeError, match="boom"):
            future.result()
        assert isinstance(future.exception(), RuntimeError)
        repo._cleanup_thread_pool()

    def test_serial_runs_inline_and_is_done(self):
        calls = []
        future = _repo("serial")._spawn(lambda: calls.append(1))
        assert calls == [1]
        assert future.done()
        assert future.cancel() is False  # already done

    def test_threadpool_creates_the_pool_lazily(self):
        repo = _repo("threadpool")
        assert repo._thread_pool is None
        repo._spawn(lambda: None).result()
        assert repo._thread_pool is not None
        repo._spawn(lambda: None).result()  # reuses it
        repo._cleanup_thread_pool()

    def test_auto_on_one_cpu_without_gevent_patching_is_serial(self):
        with patch("os.cpu_count", return_value=1):
            repo = _repo("auto")
        calls = []
        future = repo._spawn(lambda: calls.append(1))
        assert calls == [1] and future.done()

    def test_an_auto_repo_without_gevent_installed_is_serial(self):
        with patch("os.cpu_count", return_value=1):
            repo = _repo("auto")
        with patch.dict("sys.modules", {"gevent": None}):
            future = repo._spawn(lambda: 7)
        assert future.result() == 7

    def test_gevent_mode_unpatched_runs_like_serial(self):
        pytest.importorskip("gevent")
        with pytest.warns(UserWarning):
            repo = _repo("gevent")
        calls = []
        future = repo._spawn(lambda: calls.append(1))
        assert calls == [1] and future.done()

    def test_gevent_patched_spawns_a_greenlet(self):
        pytest.importorskip("gevent")
        with pytest.warns(UserWarning):
            repo = _repo("gevent")
        with patch("gevent.monkey.is_module_patched", return_value=True):
            future = repo._spawn(lambda: 5)
            failing = repo._spawn(_fail)
        assert not future.done()  # a greenlet, not run yet
        future.greenlet.join()
        failing.greenlet.join()
        assert future.result() == 5
        with pytest.raises(RuntimeError, match="boom"):
            failing.result()

    def test_gevent_cancel_before_it_runs(self):
        pytest.importorskip("gevent")
        with pytest.warns(UserWarning):
            repo = _repo("gevent")
        calls = []
        with patch("gevent.monkey.is_module_patched", return_value=True):
            future = repo._spawn(lambda: calls.append(1))
        assert future.cancel() is True
        future.greenlet.join()
        assert calls == []  # never ran
        assert future.cancelled()

    def test_gevent_cancel_while_running_kills_the_greenlet(self):
        pytest.importorskip("gevent")
        import gevent

        with pytest.warns(UserWarning):
            repo = _repo("gevent")
        with patch("gevent.monkey.is_module_patched", return_value=True):
            future = repo._spawn(lambda: gevent.sleep(10))
        gevent.sleep(0)  # the greenlet starts and blocks in its sleep
        assert future.running()
        assert future.cancel() is False  # a running future cannot be cancelled...
        future.greenlet.join()  # ...but its greenlet was killed
        assert future.greenlet.dead
        assert future.done()
