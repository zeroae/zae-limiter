"""Locust load test for zae-limiter.

Usage:
    Local:  locust -f locustfile.py
    Lambda: Imported by worker handler

NOTE: Uses nest_asyncio to allow nested event loops. This is needed because
Locust uses gevent which monkey-patches asyncio, and aioboto3 needs to run
its own event loop.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

# Allow nested event loops (needed when gevent monkey-patches asyncio)
import nest_asyncio

nest_asyncio.apply()

from locust import User, between, task  # noqa: E402

from zae_limiter import RateLimiter, RateLimitExceeded  # noqa: E402
from zae_limiter.stress.config import StressConfig  # noqa: E402
from zae_limiter.stress.distribution import TrafficDistributor  # noqa: E402


def _run_async(coro: Any) -> Any:
    """Run async operation using asyncio.run().

    nest_asyncio.apply() allows this to work even when called from
    within an existing event loop (gevent's monkey-patched asyncio).
    """
    return asyncio.run(coro)


class RateLimiterUser(User):  # type: ignore[misc]
    """Simulates a client making rate-limited API calls."""

    # Adaptive wait time based on target RPM
    wait_time = between(0.01, 0.1)

    # Class-level shared limiter (connection pooling)
    _limiter: RateLimiter | None = None
    _config: StressConfig | None = None
    _distributor: TrafficDistributor | None = None

    def on_start(self) -> None:
        """Initialize per-user state."""
        print("on_start called", flush=True)

        # Initialize shared limiter (connection pooling across all users)
        if RateLimiterUser._limiter is None:
            RateLimiterUser._config = StressConfig.from_environment()
            RateLimiterUser._distributor = TrafficDistributor(RateLimiterUser._config.distribution)
            RateLimiterUser._limiter = RateLimiter(
                name=RateLimiterUser._config.stack_name,
                region=RateLimiterUser._config.region,
            )
            print(f"Limiter initialized: {RateLimiterUser._config.stack_name}", flush=True)

        self.config = RateLimiterUser._config
        self.limiter = RateLimiterUser._limiter
        self.distributor = RateLimiterUser._distributor
        print("User started", flush=True)

    def _do_acquire(self, entity_id: str, api: str, tpm: int) -> None:
        """Execute async acquire operation."""

        async def inner() -> None:
            async with self.limiter.acquire(
                entity_id=entity_id,
                resource=api,
                consume={"rpm": 1, "tpm": tpm},
            ):
                pass  # Work would happen here

        _run_async(inner())

    @task(weight=100)  # type: ignore[misc]
    def acquire_tokens(self) -> None:
        """Primary task: acquire rate limit tokens."""
        assert self.distributor is not None
        assert self.config is not None
        assert self.limiter is not None

        entity_id, traffic_type = self.distributor.pick_entity()
        api = f"api-{random.randint(0, self.config.num_apis - 1)}"
        tpm = random.randint(100, 1000)

        start_time = time.perf_counter()

        try:
            self._do_acquire(entity_id, api, tpm)
            response_time = (time.perf_counter() - start_time) * 1000
            self.environment.events.request.fire(
                request_type="ACQUIRE",
                name=f"{api}/{traffic_type}",
                response_time=response_time,
                response_length=0,
                context={"traffic_type": traffic_type},
            )

        except RateLimitExceeded as e:
            response_time = (time.perf_counter() - start_time) * 1000
            self.environment.events.request.fire(
                request_type="ACQUIRE",
                name=f"{api}/{traffic_type}",
                response_time=response_time,
                response_length=0,
                exception=e,
                context={"traffic_type": traffic_type, "rate_limited": True},
            )

        except Exception as e:
            response_time = (time.perf_counter() - start_time) * 1000
            self.environment.events.request.fire(
                request_type="ACQUIRE",
                name=f"{api}/{traffic_type}",
                response_time=response_time,
                response_length=0,
                exception=e,
                context={"traffic_type": traffic_type, "error": True},
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
            _run_async(
                self.limiter.available(
                    entity_id=entity_id,
                    resource=api,
                )
            )
            response_time = (time.perf_counter() - start_time) * 1000
            self.environment.events.request.fire(
                request_type="AVAILABLE",
                name=f"{api}/{traffic_type}",
                response_time=response_time,
                response_length=0,
            )

        except Exception as e:
            response_time = (time.perf_counter() - start_time) * 1000
            self.environment.events.request.fire(
                request_type="AVAILABLE",
                name=f"{api}/{traffic_type}",
                response_time=response_time,
                response_length=0,
                exception=e,
            )
