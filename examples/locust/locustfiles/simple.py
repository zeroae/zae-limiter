"""Simple scenario: single resource, single limit, many anonymous entities.

Usage:
    locust -f locustfiles/simple.py --host <stack-name>

Each Locust user simulates a unique API consumer hitting a single
resource ("api") with a single rate limit (RPM). System-level defaults
define the limit so entities need no individual configuration.
"""

from __future__ import annotations

import uuid

from locust import between, task

from zae_limiter import Limit
from zae_limiter.locust import RateLimiterUser


class SimpleUser(RateLimiterUser):
    """Many anonymous API consumers sharing a system-level rate limit."""

    wait_time = between(0.1, 1.0)  # type: ignore[no-untyped-call]

    _defaults_configured: bool = False

    def on_start(self) -> None:
        """Assign a unique entity ID and configure system defaults once."""
        self.entity_id = f"user-{uuid.uuid4().hex[:8]}"

        if not SimpleUser._defaults_configured:
            self.client.set_system_defaults(
                limits=[Limit.per_minute("rpm", 60)],
            )
            SimpleUser._defaults_configured = True

    @task
    def acquire(self) -> None:
        """Acquire one request token."""
        with self.client.acquire(
            entity_id=self.entity_id,
            resource="api",
            consume={"rpm": 1},
        ):
            pass  # Simulated work happens here
