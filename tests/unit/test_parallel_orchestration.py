"""`zae_limiter._parallel.run_parallel`: the sync limiter's and lease's executor (ADR-148)."""

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from zae_limiter import _parallel
from zae_limiter._parallel import run_parallel


def _thread_name() -> str:
    return threading.current_thread().name


class TestRunParallel:
    def test_one_function_runs_in_place(self):
        repo = SimpleNamespace(_parallel_mode="threadpool", _executor_fn=None)
        assert run_parallel(repo, [_thread_name]) == (threading.current_thread().name,)

    def test_the_repository_executor_is_used_when_it_has_one(self):
        calls = []

        def executor(funcs):
            calls.append(len(funcs))
            return [fn() for fn in funcs]

        repo = SimpleNamespace(_parallel_mode="serial", _executor_fn=executor)
        assert run_parallel(repo, [lambda: 1, lambda: 2]) == (1, 2)
        assert calls == [2]

    def test_a_repository_without_parallel_mode_runs_serially(self):
        # A test double answers every attribute; only instance attributes count.
        assert run_parallel(MagicMock(), [lambda: 1, lambda: 2]) == (1, 2)

    def test_an_object_without_instance_attributes_runs_serially(self):
        assert run_parallel(object(), [lambda: 1, lambda: 2]) == (1, 2)

    def test_threadpool_runs_on_the_orchestration_pool_not_the_caller(self):
        repo = SimpleNamespace(_parallel_mode="threadpool", _executor_fn=None)
        names = run_parallel(repo, [_thread_name, _thread_name])
        assert all(name.startswith("zae-limiter") for name in names)
        assert run_parallel(repo, [lambda: 1, lambda: 2]) == (1, 2)

    def test_a_gather_inside_orchestration_work_runs_in_place(self):
        """A nested gather never waits on the pool its caller holds a worker of."""
        repo = SimpleNamespace(_parallel_mode="threadpool", _executor_fn=None)

        def nested():
            outer = _thread_name()
            inner = run_parallel(repo, [_thread_name, _thread_name])
            return outer, inner

        (outer, inner), _ = run_parallel(repo, [nested, nested])
        assert inner == (outer, outer)

    def test_the_pool_is_created_once(self):
        assert _parallel._orchestration_pool() is _parallel._orchestration_pool()
