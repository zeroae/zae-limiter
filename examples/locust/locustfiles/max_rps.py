"""Max throughput scenario: no wait between requests.

Usage:
    locust -f locustfiles/max_rps.py --host <stack-name>

Same as simple.py but with zero wait time. Each user fires acquire()
back-to-back as fast as possible to find the maximum RPS and lowest
latency the system can sustain.
"""

from __future__ import annotations

import uuid

from locust import constant, task

from zae_limiter import Limit
from zae_limiter.locust import RateLimiterUser


class MaxRpsUser(RateLimiterUser):
    """Fire acquires as fast as possible with no inter-request delay."""

    wait_time = constant(0)  # type: ignore[no-untyped-call]

    _defaults_configured: bool = False

    def on_start(self) -> None:
        """Assign a unique entity ID and configure system defaults once."""
        self.entity_id = f"user-{uuid.uuid4().hex[:8]}"

        if not MaxRpsUser._defaults_configured:
            self.client.set_system_defaults(
                limits=[Limit.per_minute("rpm", 1_000_000)],
            )
            MaxRpsUser._defaults_configured = True

    @task
    def acquire(self) -> None:
        """Acquire one request token."""
        with self.client.acquire(
            entity_id=self.entity_id,
            resource="api",
            consume={"rpm": 1},
        ):
            pass
