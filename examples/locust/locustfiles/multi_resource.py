"""Multi-resource scenario: one acquire over two resources vs two acquires.

Usage:
    locust -f locustfiles/multi_resource.py --host <stack-name>

Each Locust user is a child entity cascading to one shared parent org on the
``search`` resource, plus a per-user ``budget`` resource that does not
cascade (resource-level cascade policy, ADR-146). Every request debits
``search`` (child + parent) and ``budget`` (child only): three bucket items.

Two user classes are available (select via --class-picker, Locust UI, or
positional class names):
- MultiResourceAlsoUser: one ``acquire(..., also={"budget": ...})`` (ADR-148).
  One lease, one round trip on the fast path, one ACQUIRE row named ``search``.
- MultiResourceNestedUser: two nested acquires, ``search`` then ``budget``.
  Two leases, two round trips, ACQUIRE rows named ``search`` and ``budget``.

Both classes do the same work against the same setup (expected fast-path
cost for both: 0 RCU + 3 WCU). **Compare the ``ITERATION multi-resource`` row**:
each class fires it once per task, timing the whole iteration, so its latency
and RPS are like for like. The per-resource ACQUIRE rows are informational
only: the nested class has two per iteration, so its RPS reads doubled, and
its ``search`` row times only the outer acquire, so its p50 reads halved.
Run each class alone::

    zae-limiter loadtest run -n <stack> -f locustfiles/multi_resource.py \\
        MultiResourceAlsoUser
    zae-limiter loadtest run -n <stack> -f locustfiles/multi_resource.py \\
        MultiResourceNestedUser

The parent has a fixed id (``CASCADE_PARENT``), so a distributed or
multi-Lambda run has one parent, not one per process. Each process creates it
if it is missing (an existing one counts as success); only the master or a
local runner deletes it at test stop, never a worker.

Limits are sized so nothing is rejected: the scenario measures the admitted
path, not rate limiting. Setting the ``budget`` cascade policy needs the
stack's Lambdas at v0.16.0 or later.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from locust import constant, events, task
from locust.runners import WorkerRunner

from zae_limiter import EntityExistsError, Limit, RateLimitExceeded, SyncRateLimiter
from zae_limiter.locust import RateLimiterUser

if TYPE_CHECKING:
    from locust.env import Environment

# Children created by this process; the shared parent is not one of them.
_created_entities: set[str] = set()

# The run's one cascade parent: the same id in every process (see above).
CASCADE_PARENT_ID = "loadtest-cascade-parent-multi-resource"
_parent_lock = threading.Lock()
# Set once this process has made sure the parent exists. Cleared at every
# test start and stop: a warm Lambda worker keeps module state across runs,
# and the master deletes the parent at the end of each one.
_parent_ready = threading.Event()

# The like-for-like row both classes fire once per iteration.
ITERATION_EVENT = ("ITERATION", "multi-resource")

_SEARCH_CONSUME = {"rpm": 1}
_BUDGET_CONSUME = {"budget_rpm": 1}


def _make_limiter(environment: object) -> SyncRateLimiter:
    host = getattr(environment, "host", None)
    stack_name = host or os.environ.get("TARGET_STACK_NAME", "")
    region = os.environ.get("TARGET_REGION", "us-east-1")
    return SyncRateLimiter(name=stack_name, region=region)


@events.test_start.add_listener
def on_test_start(environment: Environment, **kwargs: Any) -> None:
    """Store resource defaults and the budget cascade policy (master or standalone only)."""
    _parent_ready.clear()
    if isinstance(environment.runner, WorkerRunner):
        return
    repo = _make_limiter(environment)._repository
    repo.set_resource_defaults("search", [Limit.per_minute("rpm", 1_000_000_000)])
    # Children cascade on search only: budget is per user. One write, so a
    # concurrent process's full-replace defaults write cannot drop the policy.
    repo.set_resource_defaults(
        "budget", [Limit.per_minute("budget_rpm", 1_000_000_000)], cascade=False
    )


@events.test_stop.add_listener
def on_test_stop(environment: Environment, **kwargs: Any) -> None:
    """Delete this process's children; the master or local runner deletes the parent."""
    _parent_ready.clear()
    is_worker = isinstance(environment.runner, WorkerRunner)
    if not _created_entities and is_worker:
        return
    limiter = _make_limiter(environment)
    for entity_id in _created_entities:
        limiter.delete_entity(entity_id)
    _created_entities.clear()
    if not is_worker:
        # On a normal stop the master fires this once its workers report no
        # users; on a quit or a timeout it may not (see the module docstring).
        limiter.delete_entity(CASCADE_PARENT_ID)


class _MultiResourceUser(RateLimiterUser):
    """Shared setup: a child entity cascading to the run's shared parent org."""

    abstract = True
    wait_time = constant(0)

    def _parent(self) -> str:
        """Make sure the shared parent exists, once per process (workers included).

        Every process tries to create it; one that finds it already created
        by another process carries on, so the call is idempotent.
        """
        with _parent_lock:
            if not _parent_ready.is_set():
                try:
                    self.client._limiter.create_entity(CASCADE_PARENT_ID, name="Cascade Parent")
                except EntityExistsError:
                    pass  # Another process of this run, or a killed earlier run, made it
                _parent_ready.set()
        return CASCADE_PARENT_ID

    @contextmanager
    def _iteration(self) -> Iterator[None]:
        """Fire one ITERATION event timing the whole task body.

        A rejection is not timed as an iteration: its RATE_LIMITED row
        already records it, and the limits are sized so none happens.
        """
        start = time.perf_counter()
        try:
            yield
        except RateLimitExceeded:
            raise
        except Exception as exc:
            self._fire_iteration(start, exc)
            raise
        self._fire_iteration(start, None)

    def _fire_iteration(self, start: float, exception: BaseException | None) -> None:
        request_type, name = ITERATION_EVENT
        self.environment.events.request.fire(
            request_type=request_type,
            name=name,
            response_time=(time.perf_counter() - start) * 1000,
            response_length=0,
            exception=exception,
            context={},
        )

    def on_start(self) -> None:
        """Create a child entity under the shared cascade parent."""
        self.entity_id = f"user-{uuid.uuid4().hex[:8]}"
        self.client._limiter.create_entity(
            self.entity_id,
            parent_id=self._parent(),
            cascade=True,
        )
        _created_entities.add(self.entity_id)


class MultiResourceAlsoUser(_MultiResourceUser):
    """Debit search and budget in one multi-resource acquire."""

    @task
    def acquire(self) -> None:
        """One lease over search (cascades to the parent) and budget."""
        with (
            self._iteration(),
            self.client.acquire(
                entity_id=self.entity_id,
                resource="search",
                consume=_SEARCH_CONSUME,
                also={"budget": _BUDGET_CONSUME},
            ),
        ):
            pass


class MultiResourceNestedUser(_MultiResourceUser):
    """Debit search and budget with two nested acquires."""

    @task
    def acquire(self) -> None:
        """Two leases: search (cascades to the parent), then budget."""
        with (
            self._iteration(),
            self.client.acquire(
                entity_id=self.entity_id,
                resource="search",
                consume=_SEARCH_CONSUME,
            ),
            self.client.acquire(
                entity_id=self.entity_id,
                resource="budget",
                consume=_BUDGET_CONSUME,
            ),
        ):
            pass
