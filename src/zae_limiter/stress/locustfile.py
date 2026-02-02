"""Locust load test for zae-limiter.

Usage:
    Local:  locust -f locustfile.py
    Lambda: Imported by worker handler
"""

from __future__ import annotations

import random
import time

from locust import User, between, events, task

from zae_limiter import RateLimitExceeded, SyncRateLimiter

from .config import StressConfig
from .distribution import TrafficDistributor


class RateLimiterUser(User):  # type: ignore[misc]
    """Simulates a client making rate-limited API calls."""

    # Adaptive wait time based on target RPM
    wait_time = between(0.01, 0.1)

    # Class-level shared limiter (connection pooling)
    _limiter: SyncRateLimiter | None = None
    _config: StressConfig | None = None
    _distributor: TrafficDistributor | None = None

    def on_start(self) -> None:
        """Initialize per-user state."""
        # Shared limiter across all users (thread-safe)
        if RateLimiterUser._limiter is None:
            RateLimiterUser._config = StressConfig.from_environment()
            RateLimiterUser._limiter = SyncRateLimiter(
                name=RateLimiterUser._config.stack_name,
                region=RateLimiterUser._config.region,
            )
            RateLimiterUser._distributor = TrafficDistributor(RateLimiterUser._config.distribution)

        self.config = RateLimiterUser._config
        self.limiter = RateLimiterUser._limiter
        self.distributor = RateLimiterUser._distributor

    @task(weight=100)  # type: ignore[misc]
    def acquire_tokens(self) -> None:
        """Primary task: acquire rate limit tokens."""
        assert self.distributor is not None
        assert self.config is not None
        assert self.limiter is not None

        entity_id, traffic_type = self.distributor.pick_entity()
        api = f"api-{random.randint(0, self.config.num_apis - 1)}"

        # Simulate realistic token consumption
        tpm_consumed = random.randint(100, 2000)

        start_time = time.perf_counter()

        try:
            with self.limiter.acquire(
                entity_id=entity_id,
                resource=api,
                consume={"rpm": 1, "tpm": tpm_consumed},
            ):
                # Success
                response_time = (time.perf_counter() - start_time) * 1000
                events.request.fire(
                    request_type="ACQUIRE",
                    name=f"{api}/{traffic_type}",
                    response_time=response_time,
                    response_length=0,
                    context={"traffic_type": traffic_type},
                )

        except RateLimitExceeded as e:
            # Rate limited
            response_time = (time.perf_counter() - start_time) * 1000
            events.request.fire(
                request_type="ACQUIRE",
                name=f"{api}/{traffic_type}",
                response_time=response_time,
                response_length=0,
                exception=e,
                context={"traffic_type": traffic_type, "retry_after": e.retry_after_seconds},
            )

        except Exception as e:
            # Unexpected error
            response_time = (time.perf_counter() - start_time) * 1000
            events.request.fire(
                request_type="ACQUIRE",
                name=f"{api}/{traffic_type}",
                response_time=response_time,
                response_length=0,
                exception=e,
                context={"traffic_type": traffic_type},
            )

    @task(weight=5)  # type: ignore[misc]
    def check_available(self) -> None:
        """Secondary task: read-only availability check."""
        assert self.distributor is not None
        assert self.config is not None
        assert self.limiter is not None

        entity_id, traffic_type = self.distributor.pick_entity()
        api = f"api-{random.randint(0, self.config.num_apis - 1)}"

        start_time = time.perf_counter()

        try:
            self.limiter.available(
                entity_id=entity_id,
                resource=api,
            )
            response_time = (time.perf_counter() - start_time) * 1000
            events.request.fire(
                request_type="AVAILABLE",
                name=f"{api}/{traffic_type}",
                response_time=response_time,
                response_length=0,
            )

        except Exception as e:
            response_time = (time.perf_counter() - start_time) * 1000
            events.request.fire(
                request_type="AVAILABLE",
                name=f"{api}/{traffic_type}",
                response_time=response_time,
                response_length=0,
                exception=e,
            )
