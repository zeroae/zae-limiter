"""The cascade user classes of the example locustfiles cascade on every runner (#780).

``simple.py`` and ``max_rps.py`` each define a cascade class whose children
must debit one shared parent. The parent used to be created only by a
``test_start`` listener that returns early on a ``WorkerRunner``, so in a
distributed run every worker created its children with ``parent_id=None``
and the cascade benchmark measured standalone against standalone.

Each test loads a locustfile from ``examples/locust/locustfiles`` once per
simulated process (a fresh module, so module state is not shared) against
moto. The real ``locust`` package is never imported: importing it
monkey-patches the process with gevent (see ``test_worker.py``), so a small
stand-in provides the names the locustfiles and ``zae_limiter.locust`` use.
"""

from __future__ import annotations

import importlib.util
import itertools
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import zae_limiter
from zae_limiter.models import AuditAction

LOCUSTFILES = Path(__file__).resolve().parents[3] / "examples" / "locust" / "locustfiles"
STACK = "test-rate-limits"

CASCADE_CLASSES = [
    pytest.param("simple", "SimpleCascadeUser", id="simple"),
    pytest.param("max_rps", "MaxRpsCascadeUser", id="max_rps"),
]


class _Hook:
    """Stand-in for a Locust event hook: ``add_listener`` returns the function."""

    def add_listener(self, fn: Any) -> Any:
        return fn


class _FakeUser:
    """Stand-in for ``locust.User``: keeps the environment and host."""

    abstract = True
    host: str | None = None

    def __init__(self, environment: Any) -> None:
        self.environment = environment
        self.host = environment.host


class FakeLocalRunner:
    pass


class FakeMasterRunner:
    pass


class FakeWorkerRunner:
    pass


@pytest.fixture
def fake_locust(monkeypatch: pytest.MonkeyPatch, sync_limiter: Any) -> Any:
    """Install a stand-in ``locust`` and a fresh ``zae_limiter.locust`` bound to moto."""
    locust_mod = types.ModuleType("locust")
    locust_mod.__dict__.update(
        User=_FakeUser,
        between=lambda lo, hi: lambda user: lo,
        constant=lambda value: lambda user: value,
        task=lambda fn: fn,
        events=types.SimpleNamespace(test_start=_Hook(), test_stop=_Hook()),
    )
    runners_mod = types.ModuleType("locust.runners")
    runners_mod.__dict__.update(
        LocalRunner=FakeLocalRunner,
        MasterRunner=FakeMasterRunner,
        WorkerRunner=FakeWorkerRunner,
    )
    exception_mod = types.ModuleType("locust.exception")
    exception_mod.__dict__.update(StopTest=type("StopTest", (Exception,), {}))

    monkeypatch.setitem(sys.modules, "locust", locust_mod)
    monkeypatch.setitem(sys.modules, "locust.runners", runners_mod)
    monkeypatch.setitem(sys.modules, "locust.exception", exception_mod)
    # Re-import zae_limiter.locust against the stand-in, and put the real one
    # (if any) back afterwards.
    monkeypatch.delitem(sys.modules, "zae_limiter.locust", raising=False)
    monkeypatch.delattr(zae_limiter, "locust", raising=False)
    import zae_limiter.locust as zl_locust

    # Every simulated process shares the moto table through one limiter.
    zl_locust.RateLimiterUser._limiter = sync_limiter
    return zl_locust


_loads = itertools.count(1)


def _load_process(name: str, limiter: Any) -> types.ModuleType:
    """Load a locustfile as a fresh module: one simulated Locust process."""
    module_name = f"_locustfile_{name}_{next(_loads)}"
    spec = importlib.util.spec_from_file_location(module_name, LOCUSTFILES / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # The listeners build a limiter from the stack name; point them at moto.
    module.__dict__["_make_limiter"] = lambda environment: limiter
    return module


def _env(runner: object) -> Any:
    return types.SimpleNamespace(
        host=STACK, runner=runner, events=types.SimpleNamespace(request=MagicMock())
    )


def _spawn(module: types.ModuleType, cls_name: str, env: Any) -> Any:
    """Start one user of the cascade class and run its task once."""
    user = getattr(module, cls_name)(env)
    user.on_start()
    user.acquire()
    return user


def _parent_consumed_milli(limiter: Any, parent_id: str) -> int:
    buckets = limiter._repository.get_buckets(parent_id, "api")
    return sum(b.total_consumed_milli or 0 for b in buckets if b.limit_name == "rpm")


@pytest.mark.parametrize(("name", "cls_name"), CASCADE_CLASSES)
class TestCascadeLocustfiles:
    def test_worker_child_cascades_to_the_parent(self, fake_locust, sync_limiter, name, cls_name):
        """On a worker, the child gets a parent and the parent is debited."""
        master = _load_process(name, sync_limiter)
        worker = _load_process(name, sync_limiter)
        master.on_test_start(_env(FakeMasterRunner()))
        worker_env = _env(FakeWorkerRunner())
        worker.on_test_start(worker_env)

        user = _spawn(worker, cls_name, worker_env)

        child = sync_limiter.get_entity(user.entity_id)
        assert child.parent_id is not None
        assert child.cascade is True
        assert _parent_consumed_milli(sync_limiter, child.parent_id) > 0

    def test_two_workers_share_one_parent(self, fake_locust, sync_limiter, name, cls_name):
        """Every process of a run debits the same parent, created exactly once."""
        master = _load_process(name, sync_limiter)
        worker_a = _load_process(name, sync_limiter)
        worker_b = _load_process(name, sync_limiter)
        master.on_test_start(_env(FakeMasterRunner()))
        env_a, env_b = _env(FakeWorkerRunner()), _env(FakeWorkerRunner())
        worker_a.on_test_start(env_a)
        worker_b.on_test_start(env_b)

        users = [
            _spawn(worker_a, cls_name, env_a),
            _spawn(worker_a, cls_name, env_a),
            _spawn(worker_b, cls_name, env_b),
        ]

        parents = {sync_limiter.get_entity(u.entity_id).parent_id for u in users}
        assert len(parents) == 1
        (parent_id,) = parents
        assert parent_id is not None
        created = [
            e
            for e in sync_limiter.get_audit_events(parent_id)
            if e.action == AuditAction.ENTITY_CREATED
        ]
        assert len(created) == 1
        assert _parent_consumed_milli(sync_limiter, parent_id) == 3 * 1000

    def test_only_the_master_deletes_the_parent(self, fake_locust, sync_limiter, name, cls_name):
        """A worker's test_stop leaves the parent; the master's deletes it."""
        master = _load_process(name, sync_limiter)
        worker = _load_process(name, sync_limiter)
        master_env, worker_env = _env(FakeMasterRunner()), _env(FakeWorkerRunner())
        master.on_test_start(master_env)
        worker.on_test_start(worker_env)
        user = _spawn(worker, cls_name, worker_env)
        parent_id = sync_limiter.get_entity(user.entity_id).parent_id
        assert parent_id is not None

        worker.on_test_stop(worker_env)
        assert sync_limiter.get_entity(user.entity_id) is None
        assert sync_limiter.get_entity(parent_id) is not None

        master.on_test_stop(master_env)
        assert sync_limiter.get_entity(parent_id) is None

    def test_headless_run_creates_and_deletes_the_parent(
        self, fake_locust, sync_limiter, name, cls_name
    ):
        """A local runner is master and worker at once."""
        local = _load_process(name, sync_limiter)
        env = _env(FakeLocalRunner())
        local.on_test_start(env)
        user = _spawn(local, cls_name, env)
        parent_id = sync_limiter.get_entity(user.entity_id).parent_id
        assert parent_id is not None
        assert _parent_consumed_milli(sync_limiter, parent_id) > 0

        local.on_test_stop(env)
        assert sync_limiter.get_entity(user.entity_id) is None
        assert sync_limiter.get_entity(parent_id) is None

    def test_a_reused_process_recreates_the_parent_next_run(
        self, fake_locust, sync_limiter, name, cls_name
    ):
        """A warm Lambda keeps module state; the next run must not trust it."""
        master = _load_process(name, sync_limiter)
        worker = _load_process(name, sync_limiter)
        master_env, worker_env = _env(FakeMasterRunner()), _env(FakeWorkerRunner())
        for _run in range(2):
            master.on_test_start(master_env)
            worker.on_test_start(worker_env)
            user = _spawn(worker, cls_name, worker_env)
            parent_id = sync_limiter.get_entity(user.entity_id).parent_id
            assert parent_id is not None
            assert sync_limiter.get_entity(parent_id) is not None
            worker.on_test_stop(worker_env)
            master.on_test_stop(master_env)

    def test_master_stop_without_a_parent_is_a_no_op(
        self, fake_locust, sync_limiter, name, cls_name
    ):
        """A run of only the standalone class never created the parent."""
        master = _load_process(name, sync_limiter)
        master_env = _env(FakeMasterRunner())
        master.on_test_start(master_env)
        master.on_test_stop(master_env)
        assert sync_limiter.get_entity(master.CASCADE_PARENT_ID) is None
