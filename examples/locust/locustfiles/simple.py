"""Simple scenario: single resource, single limit, many anonymous entities.

Usage:
    locust -f locustfiles/simple.py --host <stack-name>

Each Locust user simulates a unique API consumer hitting a single
resource ("api") with a single rate limit (RPM). System-level defaults
define the limit so entities need no individual configuration.

Optimal per-Lambda concurrency: 40+ users (calibrated at 90% efficiency).
The wait_time=between(0.1, 1.0) means users spend most of their time
sleeping, so there is no GIL contention even at high concurrency.
p50 stays at floor latency (8-9ms) regardless of user count.

Two user classes are available (select via --class-picker or Locust UI):
- SimpleUser: standalone entities, no cascade
- SimpleCascadeUser: child entities with cascade=True to a shared parent

The cascade parent is ONE entity for the whole run,
``loadtest-cascade-parent-simple``, shared by the master, every worker and a
headless runner. Its id is fixed, so every process agrees on it without a
message from the master. Each process creates it once, in the first cascade
user's ``on_start`` (behind a lock), and treats ``EntityExistsError`` as
success. Only the master or a local runner deletes it, in ``test_stop``; a
worker never does, since other workers may still be debiting it.

On a normal stop (``MasterRunner.stop()``) the master fires ``test_stop`` only
after its workers report no running users, so no child is still debiting the
parent when it is deleted. Residuals, each of which can leave a worker debiting
a deleted parent, whose next slow path then recreates an orphan parent bucket
(system defaults, so it expires through its TTL):

- the workers do not report within ``stop_timeout`` + 60 s, and the master
  fires ``test_stop`` anyway;
- the master quits instead of stopping (Ctrl-C, or the distributed CLI
  stopping the Fargate task: SIGTERM -> ``runner.quit()``). ``quit()`` fires
  ``test_stop`` at once and only then tells the workers to quit.

Two more: a run killed before ``test_stop`` leaves the parent behind, and the
next run reuses it with its old bucket state (possibly already sharded), so
that run is not comparable with a cold start. Two runs of the same locustfile
against one stack at the same time share the parent, and the first to finish
deletes it.

One parent for the run rather than one per process (#780): every cascading
child writes to the parent's bucket, so the parent is the hot partition a
cascade benchmark exists to load. N parents for N workers would divide that
load by N (see CLAUDE.md "Hot Partition Risk Mitigation").
"""

from __future__ import annotations

import os
import threading
import uuid

from locust import between, events, task
from locust.runners import WorkerRunner

from zae_limiter import Limit, SyncRateLimiter
from zae_limiter.exceptions import EntityExistsError
from zae_limiter.locust import RateLimiterUser

_created_entities: set[str] = set()

# The run's one cascade parent: the same id in every process (see above).
CASCADE_PARENT_ID = "loadtest-cascade-parent-simple"
_parent_lock = threading.Lock()
# Set once this process has made sure the parent exists. Cleared at every
# test start and stop: a warm Lambda worker keeps module state across runs,
# and the master deletes the parent at the end of each one.
_parent_ready = threading.Event()


def _make_limiter(environment: object) -> SyncRateLimiter:
    host = getattr(environment, "host", None)
    stack_name = host or os.environ.get("TARGET_STACK_NAME", "")
    region = os.environ.get("TARGET_REGION", "us-east-1")
    return SyncRateLimiter(name=stack_name, region=region)


@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    """Set system defaults (master or standalone only)."""
    _parent_ready.clear()
    if isinstance(environment.runner, WorkerRunner):
        return
    limiter = _make_limiter(environment)
    limiter.set_system_defaults(limits=[Limit.per_minute("rpm", 1_000_000_000)])


@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    """Clean up entities created by this process, and the parent if not a worker."""
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


def _ensure_cascade_parent(limiter: SyncRateLimiter) -> str:
    """Make sure the run's shared parent exists, once per process."""
    with _parent_lock:
        if not _parent_ready.is_set():
            try:
                limiter.create_entity(CASCADE_PARENT_ID, name="Cascade Parent")
            except EntityExistsError:
                pass  # Another process of this run, or a killed earlier run, made it
            _parent_ready.set()
    return CASCADE_PARENT_ID


class SimpleUser(RateLimiterUser):
    """Many anonymous API consumers sharing a system-level rate limit."""

    wait_time = between(0.1, 1.0)  # type: ignore[no-untyped-call]

    def on_start(self) -> None:
        """Assign a unique entity ID."""
        self.entity_id = f"user-{uuid.uuid4().hex[:8]}"
        _created_entities.add(self.entity_id)

    @task
    def acquire(self) -> None:
        """Acquire one request token."""
        with self.client.acquire(
            entity_id=self.entity_id,
            resource="api",
            consume={"rpm": 1},
        ):
            pass  # Simulated work happens here


class SimpleCascadeUser(RateLimiterUser):
    """Anonymous API consumers with cascade=True to a shared parent.

    Same wait time as SimpleUser but each user is a child of a single
    parent org, measuring cascade overhead under realistic traffic.
    """

    wait_time = between(0.1, 1.0)  # type: ignore[no-untyped-call]

    def on_start(self) -> None:
        """Create a child entity under the shared cascade parent."""
        self.entity_id = f"user-{uuid.uuid4().hex[:8]}"
        self.client._limiter.create_entity(
            self.entity_id,
            parent_id=_ensure_cascade_parent(self.client._limiter),
            cascade=True,
        )
        _created_entities.add(self.entity_id)

    @task
    def acquire(self) -> None:
        """Acquire one request token (cascades to parent)."""
        with self.client.acquire(
            entity_id=self.entity_id,
            resource="api",
            consume={"rpm": 1},
        ):
            pass  # Simulated work happens here
