"""Simple scenario: single resource, single limit, many anonymous entities.

Usage:
    locust -f locustfiles/simple.py --host <stack-name>

Each Locust user simulates a unique API consumer hitting a single
resource ("api") with a single rate limit (RPM). System-level defaults
define the limit so entities need no individual configuration.

Two user classes are available (select via --class-picker or Locust UI):
- SimpleUser: standalone entities, no cascade
- SimpleCascadeUser: child entities with cascade=True to a shared parent
"""

from __future__ import annotations

import os
import uuid

from locust import between, events, task
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
            pass  # Simulated work happens here
