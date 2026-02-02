"""Locust load test for zae-limiter.

Usage:
    Local:  locust -f locustfile.py
    Lambda: Imported by worker handler

Uses asyncio-gevent for proper asyncio/gevent integration. This makes
asyncio use gevent's event loop, avoiding the "Timeout should be used
inside a task" errors from aiohttp.
"""

from __future__ import annotations

import asyncio
import random
import time

import asyncio_gevent

# Set asyncio to use gevent's event loop - must be done before any asyncio usage
asyncio.set_event_loop_policy(asyncio_gevent.EventLoopPolicy())

from gevent.local import local as greenlet_local  # noqa: E402
from locust import User, between, task  # noqa: E402

from zae_limiter import RateLimitExceeded, SyncRateLimiter  # noqa: E402
from zae_limiter.stress.config import StressConfig  # noqa: E402
from zae_limiter.stress.distribution import TrafficDistributor  # noqa: E402

# Greenlet-local storage for limiters (avoids event loop conflicts)
_greenlet_state = greenlet_local()


def _get_limiter(config: StressConfig) -> SyncRateLimiter:
    """Get or create a greenlet-local SyncRateLimiter instance."""
    if not hasattr(_greenlet_state, "limiter"):
        _greenlet_state.limiter = SyncRateLimiter(
            name=config.stack_name,
            region=config.region,
        )
    return _greenlet_state.limiter  # type: ignore[no-any-return]


class RateLimiterUser(User):  # type: ignore[misc]
    """Simulates a client making rate-limited API calls."""

    # Adaptive wait time based on target RPM
    wait_time = between(0.01, 0.1)

    # Class-level shared config (immutable, safe to share)
    _config: StressConfig | None = None
    _distributor: TrafficDistributor | None = None

    def on_start(self) -> None:
        """Initialize per-user state."""
        # Initialize shared config (only once, thread-safe for reads)
        if RateLimiterUser._config is None:
            RateLimiterUser._config = StressConfig.from_environment()
            RateLimiterUser._distributor = TrafficDistributor(RateLimiterUser._config.distribution)
            print(f"Config loaded: {RateLimiterUser._config.stack_name}", flush=True)

        self.config = RateLimiterUser._config
        self.distributor = RateLimiterUser._distributor
        # Set host for Locust UI display
        self.host = f"zael://{self.config.stack_name}"
        # Limiter is fetched per-greenlet in tasks (via _get_limiter)

    def _do_acquire(self, entity_id: str, api: str, tpm: int) -> None:
        """Execute sync acquire operation."""
        assert self.config is not None
        limiter = _get_limiter(self.config)
        with limiter.acquire(
            entity_id=entity_id,
            resource=api,
            consume={"rpm": 1, "tpm": tpm},
        ):
            pass  # Work would happen here

    @task(weight=100)  # type: ignore[misc]
    def acquire_tokens(self) -> None:
        """Primary task: acquire rate limit tokens."""
        assert self.distributor is not None
        assert self.config is not None

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

        entity_id, traffic_type = self.distributor.pick_entity()
        api = f"api-{random.randint(0, self.config.num_apis - 1)}"

        start_time = time.perf_counter()

        try:
            limiter = _get_limiter(self.config)
            limiter.available(
                entity_id=entity_id,
                resource=api,
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
