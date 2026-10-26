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

Both classes do the same work against the same setup, so running each alone
compares the latency and throughput of the two shapes (expected fast-path
cost for both: 0 RCU + 3 WCU)::

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
import uuid
from typing import Any

from locust import constant, events, task
from locust.env import Environment
from locust.runners import WorkerRunner

from zae_limiter import EntityExistsError, Limit, SyncRateLimiter
from zae_limiter.locust import RateLimiterUser

# Children created by this process; the shared parent is not one of them.
_created_entities: set[str] = set()
# One parent for the whole run, whichever process (master, worker, Lambda)
# a user runs in, so the parent's hot-partition load is not split per process.
CASCADE_PARENT = "loadtest-cascade-parent-multi-resource"
# Holds CASCADE_PARENT once this process has made sure it exists.
_parent_ensured: set[str] = set()
_parent_lock = threading.Lock()

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
    is_worker = isinstance(environment.runner, WorkerRunner)
    if not _created_entities and is_worker:
        return
    limiter = _make_limiter(environment)
    for entity_id in _created_entities:
        limiter.delete_entity(entity_id)
    _created_entities.clear()
    if not is_worker:
        limiter.delete_entity(CASCADE_PARENT)
        _parent_ensured.clear()


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
            if CASCADE_PARENT not in _parent_ensured:
                try:
                    self.client._limiter.create_entity(CASCADE_PARENT, name="Cascade Parent")
                except EntityExistsError:
                    pass
                _parent_ensured.add(CASCADE_PARENT)
            return CASCADE_PARENT

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
        with self.client.acquire(
            entity_id=self.entity_id,
            resource="search",
            consume=_SEARCH_CONSUME,
            also={"budget": _BUDGET_CONSUME},
        ):
            pass


class MultiResourceNestedUser(_MultiResourceUser):
    """Debit search and budget with two nested acquires."""

    @task
    def acquire(self) -> None:
        """Two leases: search (cascades to the parent), then budget."""
        with self.client.acquire(
            entity_id=self.entity_id,
            resource="search",
            consume=_SEARCH_CONSUME,
        ):
            with self.client.acquire(
                entity_id=self.entity_id,
                resource="budget",
                consume=_BUDGET_CONSUME,
            ):
                pass
