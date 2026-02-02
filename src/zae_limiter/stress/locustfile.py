"""Locust load test for zae-limiter.

Usage:
    Local:  locust -f locustfile.py
    Lambda: Imported by worker handler

Uses asyncio-gevent for proper asyncio/gevent integration. Each greenlet
maintains its own event loop to avoid conflicts when running async code
from within gevent greenlets.
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

from zae_limiter import RateLimiter, RateLimitExceeded  # noqa: E402
from zae_limiter.stress.config import StressConfig  # noqa: E402
from zae_limiter.stress.distribution import TrafficDistributor  # noqa: E402

# Greenlet-local storage for limiter and event loop
_greenlet_state = greenlet_local()


def _get_loop() -> asyncio.AbstractEventLoop:
    """Get or create a greenlet-local event loop."""
    if not hasattr(_greenlet_state, "loop") or _greenlet_state.loop.is_closed():
        _greenlet_state.loop = asyncio.new_event_loop()
    return _greenlet_state.loop  # type: ignore[no-any-return]


def _get_limiter(config: StressConfig) -> RateLimiter:
    """Get or create a greenlet-local RateLimiter instance."""
    if not hasattr(_greenlet_state, "limiter"):
        _greenlet_state.limiter = RateLimiter(
            name=config.stack_name,
            region=config.region,
        )
    return _greenlet_state.limiter  # type: ignore[no-any-return]


def _run_async(coro: object) -> object:
    """Run an async coroutine in the greenlet-local event loop."""
    loop = _get_loop()
    return loop.run_until_complete(coro)  # type: ignore[arg-type]


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
        """Execute acquire operation using async RateLimiter."""
        assert self.config is not None
        limiter = _get_limiter(self.config)

        async def acquire_async() -> None:
            async with limiter.acquire(
                entity_id=entity_id,
                resource=api,
                consume={"rpm": 1, "tpm": tpm},
            ):
                pass  # Work would happen here

        _run_async(acquire_async())

    def _do_available(self, entity_id: str, api: str) -> dict[str, int] | None:
        """Execute available check using async RateLimiter."""
        assert self.config is not None
        limiter = _get_limiter(self.config)

        async def available_async() -> dict[str, int] | None:
            return await limiter.available(
                entity_id=entity_id,
                resource=api,
            )

        return _run_async(available_async())  # type: ignore[return-value]

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
            self._do_available(entity_id, api)
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
