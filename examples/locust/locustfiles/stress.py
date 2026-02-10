"""Stress test: whale/spike/power-law traffic distribution.

Usage:
    locust -f locustfiles/stress.py --host <stack-name>

Uses the TrafficDistributor pattern with whale/spiker/baseline entities.
For custom load patterns, subclass RateLimiterUser directly.
"""

from __future__ import annotations

import os
import random

from common.config import LoadConfig
from common.distribution import TrafficDistributor
from locust import between, task

from zae_limiter.locust import RateLimiterUser


class LoadTestUser(RateLimiterUser):
    """Load test user with whale/spiker/baseline traffic distribution."""

    # stack_name from --host flag or TARGET_STACK_NAME env var
    stack_name = os.environ.get("TARGET_STACK_NAME", "")
    region = os.environ.get("TARGET_REGION", "us-east-1")

    wait_time = between(0.01, 0.1)  # type: ignore[no-untyped-call]

    # Class-level shared state
    _config: LoadConfig | None = None
    _distributor: TrafficDistributor | None = None

    def on_start(self) -> None:
        """Initialize config and distributor."""
        if LoadTestUser._config is None:
            LoadTestUser._config = LoadConfig.from_environment()
            LoadTestUser._distributor = TrafficDistributor(LoadTestUser._config.distribution)
            print(f"Config loaded: {LoadTestUser._config.stack_name}", flush=True)

        self.config = LoadTestUser._config
        self.distributor = LoadTestUser._distributor

    @task(weight=100)
    def acquire_tokens(self) -> None:
        """Primary task: acquire rate limit tokens."""
        assert self.distributor is not None
        assert self.config is not None

        entity_id, traffic_type = self.distributor.pick_entity()
        resource = f"api-{random.randint(0, self.config.num_apis - 1)}"
        tpm = random.randint(100, 1000)

        with self.client.acquire(
            entity_id=entity_id,
            resource=resource,
            consume={"rpm": 1, "tpm": tpm},
            name=f"{resource}/{traffic_type}",
        ):
            pass  # Work would happen here

    @task(weight=5)
    def check_available(self) -> None:
        """Secondary task: read-only availability check."""
        assert self.distributor is not None
        assert self.config is not None

        entity_id, traffic_type = self.distributor.pick_entity()
        resource = f"api-{random.randint(0, self.config.num_apis - 1)}"

        self.client.available(
            entity_id=entity_id,
            resource=resource,
            name=f"{resource}/{traffic_type}",
        )
