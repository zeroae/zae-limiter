"""Locust integration for zae-limiter load testing.

Provides RateLimiterUser, analogous to HttpUser or FastHttpUser.
It creates a ``client`` attribute that wraps SyncRateLimiter with
automatic Locust event firing and timing.

Usage::

    from zae_limiter.locust import RateLimiterUser

    class MyUser(RateLimiterUser):
        stack_name = "my-limiter"

        @task
        def do_acquire(self):
            with self.client.acquire(
                entity_id="user-123",
                resource="gpt-4",
                consume={"rpm": 1, "tpm": 500},
                name="gpt-4/baseline",
            ):
                pass  # simulate work
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from locust import User
from locust.exception import StopTest

from zae_limiter import SyncRateLimiter

if TYPE_CHECKING:
    from collections.abc import Generator

    from zae_limiter import Entity, Limit
    from zae_limiter.limiter import OnUnavailable


class RateLimiterSession:
    """Instrumented wrapper around SyncRateLimiter.

    Fires Locust request events with timing on every operation,
    analogous to how HttpSession wraps requests.Session.
    """

    def __init__(
        self,
        limiter: SyncRateLimiter,
        request_event: Any,
        user: RateLimiterUser,
    ) -> None:
        self._limiter = limiter
        self.request_event = request_event
        self.user = user

    def _fire(
        self,
        request_type: str,
        name: str,
        start_time: float,
        exception: BaseException | None = None,
        **context: Any,
    ) -> None:
        response_time = (time.perf_counter() - start_time) * 1000
        self.request_event.fire(
            request_type=request_type,
            name=name,
            response_time=response_time,
            response_length=0,
            exception=exception,
            context=context,
        )

    @contextmanager
    def acquire(
        self,
        entity_id: str,
        resource: str,
        consume: dict[str, int],
        *,
        name: str | None = None,
    ) -> Generator[Any, None, None]:
        """Acquire rate limit tokens, firing Locust events with timing.

        Fires two distinct events:

        - **ACQUIRE** — entering the rate limiter (read + consume).
        - **COMMIT** — exiting the context manager (persist adjustments).

        Args:
            entity_id: Entity to acquire for.
            resource: Resource name.
            consume: Dict of limit_name -> amount (e.g. {"rpm": 1, "tpm": 500}).
            name: Locust stats name (defaults to resource).

        Yields:
            The lease context from SyncRateLimiter.acquire().
        """
        stat_name = name or resource
        acquire_start = time.perf_counter()
        acquired = False
        commit_start: float | None = None
        try:
            with self._limiter.acquire(
                entity_id=entity_id,
                resource=resource,
                consume=consume,
            ) as lease:
                self._fire("ACQUIRE", stat_name, acquire_start)
                acquired = True
                yield lease
                commit_start = time.perf_counter()
            self._fire("COMMIT", stat_name, commit_start)
        except Exception as exc:
            if not acquired:
                self._fire("ACQUIRE", stat_name, acquire_start, exception=exc)
            elif commit_start is not None:
                self._fire("COMMIT", stat_name, commit_start, exception=exc)
            raise

    def available(
        self,
        entity_id: str,
        resource: str,
        *,
        name: str | None = None,
    ) -> dict[str, int]:
        """Check availability, firing a Locust event with timing.

        Args:
            entity_id: Entity to check.
            resource: Resource name.
            name: Locust stats name (defaults to resource).

        Returns:
            Dict of limit_name -> available capacity.
        """
        stat_name = name or resource
        start = time.perf_counter()
        try:
            result = self._limiter.available(
                entity_id=entity_id,
                resource=resource,
            )
            self._fire("AVAILABLE", stat_name, start)
            return result
        except Exception as exc:
            self._fire("AVAILABLE", stat_name, start, exception=exc)
            raise

    # -------------------------------------------------------------------------
    # System-level defaults
    # -------------------------------------------------------------------------

    def set_system_defaults(
        self,
        limits: list[Limit],
        on_unavailable: OnUnavailable | None = None,
        *,
        name: str | None = None,
    ) -> None:
        stat_name = name or "system"
        start = time.perf_counter()
        try:
            self._limiter.set_system_defaults(limits, on_unavailable=on_unavailable)
            self._fire("SET_SYSTEM_DEFAULTS", stat_name, start)
        except Exception as exc:
            self._fire("SET_SYSTEM_DEFAULTS", stat_name, start, exception=exc)
            raise

    def get_system_defaults(
        self,
        *,
        name: str | None = None,
    ) -> tuple[list[Limit], OnUnavailable | None]:
        stat_name = name or "system"
        start = time.perf_counter()
        try:
            result = self._limiter.get_system_defaults()
            self._fire("GET_SYSTEM_DEFAULTS", stat_name, start)
            return result
        except Exception as exc:
            self._fire("GET_SYSTEM_DEFAULTS", stat_name, start, exception=exc)
            raise

    def delete_system_defaults(
        self,
        *,
        name: str | None = None,
    ) -> None:
        stat_name = name or "system"
        start = time.perf_counter()
        try:
            self._limiter.delete_system_defaults()
            self._fire("DELETE_SYSTEM_DEFAULTS", stat_name, start)
        except Exception as exc:
            self._fire("DELETE_SYSTEM_DEFAULTS", stat_name, start, exception=exc)
            raise

    # -------------------------------------------------------------------------
    # Resource-level defaults
    # -------------------------------------------------------------------------

    def set_resource_defaults(
        self,
        resource: str,
        limits: list[Limit],
        *,
        name: str | None = None,
    ) -> None:
        stat_name = name or resource
        start = time.perf_counter()
        try:
            self._limiter.set_resource_defaults(resource, limits)
            self._fire("SET_RESOURCE_DEFAULTS", stat_name, start)
        except Exception as exc:
            self._fire("SET_RESOURCE_DEFAULTS", stat_name, start, exception=exc)
            raise

    def get_resource_defaults(
        self,
        resource: str,
        *,
        name: str | None = None,
    ) -> list[Limit]:
        stat_name = name or resource
        start = time.perf_counter()
        try:
            result = self._limiter.get_resource_defaults(resource)
            self._fire("GET_RESOURCE_DEFAULTS", stat_name, start)
            return result
        except Exception as exc:
            self._fire("GET_RESOURCE_DEFAULTS", stat_name, start, exception=exc)
            raise

    def delete_resource_defaults(
        self,
        resource: str,
        *,
        name: str | None = None,
    ) -> None:
        stat_name = name or resource
        start = time.perf_counter()
        try:
            self._limiter.delete_resource_defaults(resource)
            self._fire("DELETE_RESOURCE_DEFAULTS", stat_name, start)
        except Exception as exc:
            self._fire("DELETE_RESOURCE_DEFAULTS", stat_name, start, exception=exc)
            raise

    def list_resources_with_defaults(
        self,
        *,
        name: str | None = None,
    ) -> list[str]:
        stat_name = name or "system"
        start = time.perf_counter()
        try:
            result = self._limiter.list_resources_with_defaults()
            self._fire("LIST_RESOURCES_WITH_DEFAULTS", stat_name, start)
            return result
        except Exception as exc:
            self._fire("LIST_RESOURCES_WITH_DEFAULTS", stat_name, start, exception=exc)
            raise

    # -------------------------------------------------------------------------
    # Entity management
    # -------------------------------------------------------------------------

    def create_entity(
        self,
        entity_id: str,
        *,
        name: str | None = None,
        **kwargs: Any,
    ) -> Entity:
        stat_name = name or entity_id
        start = time.perf_counter()
        try:
            result = self._limiter.create_entity(entity_id=entity_id, **kwargs)
            self._fire("CREATE_ENTITY", stat_name, start)
            return result
        except Exception as exc:
            self._fire("CREATE_ENTITY", stat_name, start, exception=exc)
            raise

    def get_entity(
        self,
        entity_id: str,
        *,
        name: str | None = None,
    ) -> Entity | None:
        stat_name = name or entity_id
        start = time.perf_counter()
        try:
            result = self._limiter.get_entity(entity_id=entity_id)
            self._fire("GET_ENTITY", stat_name, start)
            return result
        except Exception as exc:
            self._fire("GET_ENTITY", stat_name, start, exception=exc)
            raise

    def delete_entity(
        self,
        entity_id: str,
        *,
        name: str | None = None,
    ) -> None:
        stat_name = name or entity_id
        start = time.perf_counter()
        try:
            self._limiter.delete_entity(entity_id=entity_id)
            self._fire("DELETE_ENTITY", stat_name, start)
        except Exception as exc:
            self._fire("DELETE_ENTITY", stat_name, start, exception=exc)
            raise

    def get_children(
        self,
        parent_id: str,
        *,
        name: str | None = None,
    ) -> list[Entity]:
        stat_name = name or parent_id
        start = time.perf_counter()
        try:
            result = self._limiter.get_children(parent_id=parent_id)
            self._fire("GET_CHILDREN", stat_name, start)
            return result
        except Exception as exc:
            self._fire("GET_CHILDREN", stat_name, start, exception=exc)
            raise

    # -------------------------------------------------------------------------
    # Entity-level limits
    # -------------------------------------------------------------------------

    def set_limits(
        self,
        entity_id: str,
        limits: list[Limit],
        *,
        resource: str = "_default_",
        name: str | None = None,
    ) -> None:
        stat_name = name or resource
        start = time.perf_counter()
        try:
            self._limiter.set_limits(entity_id, limits, resource)
            self._fire("SET_LIMITS", stat_name, start)
        except Exception as exc:
            self._fire("SET_LIMITS", stat_name, start, exception=exc)
            raise

    def get_limits(
        self,
        entity_id: str,
        *,
        resource: str = "_default_",
        name: str | None = None,
    ) -> list[Limit]:
        stat_name = name or resource
        start = time.perf_counter()
        try:
            result = self._limiter.get_limits(entity_id, resource)
            self._fire("GET_LIMITS", stat_name, start)
            return result
        except Exception as exc:
            self._fire("GET_LIMITS", stat_name, start, exception=exc)
            raise

    def delete_limits(
        self,
        entity_id: str,
        *,
        resource: str = "_default_",
        name: str | None = None,
    ) -> None:
        stat_name = name or resource
        start = time.perf_counter()
        try:
            self._limiter.delete_limits(entity_id, resource)
            self._fire("DELETE_LIMITS", stat_name, start)
        except Exception as exc:
            self._fire("DELETE_LIMITS", stat_name, start, exception=exc)
            raise

    # -------------------------------------------------------------------------
    # Queries
    # -------------------------------------------------------------------------

    def time_until_available(
        self,
        entity_id: str,
        resource: str,
        needed: dict[str, int],
        *,
        name: str | None = None,
    ) -> float:
        stat_name = name or resource
        start = time.perf_counter()
        try:
            result = self._limiter.time_until_available(
                entity_id=entity_id,
                resource=resource,
                needed=needed,
            )
            self._fire("TIME_UNTIL_AVAILABLE", stat_name, start)
            return result
        except Exception as exc:
            self._fire("TIME_UNTIL_AVAILABLE", stat_name, start, exception=exc)
            raise

    def is_available(
        self,
        *,
        timeout: float = 1.0,
        name: str | None = None,
    ) -> bool:
        stat_name = name or "system"
        start = time.perf_counter()
        try:
            result = self._limiter.is_available(timeout=timeout)
            self._fire("IS_AVAILABLE", stat_name, start)
            return result
        except Exception as exc:
            self._fire("IS_AVAILABLE", stat_name, start, exception=exc)
            raise


class RateLimiterUser(User):
    """Locust User for load testing zae-limiter.

    Creates a ``client`` attribute (a :class:`RateLimiterSession`) that wraps
    :class:`SyncRateLimiter` with automatic Locust event firing.

    Set ``stack_name`` (and optionally ``region``) on the subclass, then
    define tasks using ``self.client.acquire()`` and ``self.client.available()``.

    Example::

        class MyUser(RateLimiterUser):
            stack_name = "my-limiter"

            @task
            def acquire_tokens(self):
                with self.client.acquire(
                    entity_id="user-123",
                    resource="gpt-4",
                    consume={"rpm": 1, "tpm": 500},
                ):
                    pass
    """

    abstract: bool = True

    stack_name: str
    region: str = "us-east-1"

    # Shared SyncRateLimiter across all user instances (thread-safe with boto3)
    _limiter: SyncRateLimiter | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

        # Fall back to --host if stack_name not set as class attribute
        if not getattr(self, "stack_name", None) and self.host:
            self.stack_name = self.host

        if not getattr(self, "stack_name", None):
            raise StopTest(
                "You must specify stack_name. Either as a class attribute or via the --host option."
            )

        # Lazily share a single SyncRateLimiter across all users
        if RateLimiterUser._limiter is None:
            RateLimiterUser._limiter = SyncRateLimiter(
                name=self.stack_name,
                region=self.region,
            )

        self.client = RateLimiterSession(
            limiter=RateLimiterUser._limiter,
            request_event=self.environment.events.request,
            user=self,
        )
