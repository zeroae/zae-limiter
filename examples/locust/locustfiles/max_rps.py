"""Max throughput scenario: no wait between requests.

Usage:
    locust -f locustfiles/max_rps.py --host <stack-name>

Same as simple.py but with zero wait time. Each user fires acquire()
back-to-back as fast as possible to find the maximum RPS and lowest
latency the system can sustain.

Two user classes are available (select via --class-picker or Locust UI):
- MaxRpsUser: standalone entities, no cascade
- MaxRpsCascadeUser: child entities with cascade=True to a shared parent
"""

from __future__ import annotations

import os
import uuid

from locust import constant, events, task
from locust.runners import WorkerRunner

from zae_limiter import Limit, SyncRateLimiter
from zae_limiter.locust import RateLimiterUser

_created_entities: set[str] = set()
_cascade_parent: str | None = None


def _make_limiter(environment: object) -> SyncRateLimiter:
    host = getattr(environment, "host", None)
    stack_name = host or os.environ.get("TARGET_STACK_NAME", "")
    region = os.environ.get("TARGET_REGION", "us-east-1")
    return SyncRateLimiter(name=stack_name, region=region)


@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    """Set system defaults and cascade parent (master or standalone only)."""
    global _cascade_parent  # noqa: PLW0603
    if isinstance(environment.runner, WorkerRunner):
        return
    limiter = _make_limiter(environment)
    limiter.set_system_defaults(limits=[Limit.per_minute("rpm", 1_000_000_000)])
    # Create a shared parent for cascade users
    _cascade_parent = f"org-{uuid.uuid4().hex[:8]}"
    limiter.create_entity(_cascade_parent, name="Cascade Parent")
    _created_entities.add(_cascade_parent)


@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    """Clean up entities created by this process."""
    if not _created_entities:
        return
    limiter = _make_limiter(environment)
    for entity_id in _created_entities:
        limiter.delete_entity(entity_id)
    _created_entities.clear()


class MaxRpsUser(RateLimiterUser):
    """Fire acquires as fast as possible with no inter-request delay."""

    wait_time = constant(0)  # type: ignore[no-untyped-call]

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
            pass


class MaxRpsCascadeUser(RateLimiterUser):
    """Fire acquires with cascade=True to a shared parent entity.

    Each user is a child of a single parent org. Every acquire() writes
    to both the child and parent buckets, measuring the overhead of
    hierarchical rate limiting.
    """

    wait_time = constant(0)  # type: ignore[no-untyped-call]

    def on_start(self) -> None:
        """Create a child entity under the shared cascade parent."""
        limiter = _make_limiter(self.environment)
        self.entity_id = f"user-{uuid.uuid4().hex[:8]}"
        limiter.create_entity(
            self.entity_id,
            parent_id=_cascade_parent,
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
            pass
