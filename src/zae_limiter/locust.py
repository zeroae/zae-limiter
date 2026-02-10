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

import os
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from locust import User
from locust.exception import StopTest

from zae_limiter import SyncRateLimiter
from zae_limiter.exceptions import RateLimitExceeded

if TYPE_CHECKING:
    from collections.abc import Generator

    from zae_limiter import Entity, Limit
    from zae_limiter.limiter import OnUnavailable


def _configure_boto3_pool(max_connections: int = 1000) -> None:
    """Configure boto3 to use a larger connection pool for DynamoDB.

    Locust spawns many concurrent users sharing a single SyncRateLimiter.
    The default boto3 pool (10 connections) gets saturated, causing
    "Connection pool is full, discarding connection" warnings.

    Pool size can be overridden via BOTO3_MAX_POOL env var.
    """
    if getattr(_configure_boto3_pool, "_configured", False):
        return

    import boto3
    from botocore.config import Config

    max_connections = int(os.environ.get("BOTO3_MAX_POOL", str(max_connections)))
    default_config = Config(max_pool_connections=max_connections)
    original_client = boto3.Session.client

    def patched_client(self: Any, service_name: str, **kwargs: Any) -> Any:
        if service_name == "dynamodb" and "config" not in kwargs:
            kwargs["config"] = default_config
        return original_client(self, service_name, **kwargs)  # type: ignore[call-overload]

    boto3.Session.client = patched_client  # type: ignore[assignment]
    _configure_boto3_pool._configured = True  # type: ignore[attr-defined]


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
        """Initialize the session.

        Args:
            limiter: Shared SyncRateLimiter instance.
            request_event: Locust environment request event hook.
            user: The RateLimiterUser that owns this session.
        """
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

        Fires up to two distinct events:

        - **ACQUIRE** — entering the rate limiter (read + consume).
        - **COMMIT** — exiting the context manager (only if adjustments were made).

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
            if lease._has_adjustments:
                self._fire("COMMIT", stat_name, commit_start)
        except RateLimitExceeded as exc:
            # RateLimitExceeded is expected behavior, not a failure
            # Fire as RATE_LIMITED (success) so it's tracked but not counted as error
            self._fire("RATE_LIMITED", stat_name, acquire_start, rate_limit_exceeded=exc)
            raise
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
        """Set system-level default limits, firing a Locust event with timing.

        Args:
            limits: Default limits to apply system-wide.
            on_unavailable: Behavior when the rate limiter is unavailable.
            name: Locust stats name (defaults to "system").
        """
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
        """Retrieve system-level default limits, firing a Locust event with timing.

        Args:
            name: Locust stats name (defaults to "system").

        Returns:
            Tuple of (limits, on_unavailable).
        """
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
        """Delete system-level default limits, firing a Locust event with timing.

        Args:
            name: Locust stats name (defaults to "system").
        """
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
        """Set resource-level default limits, firing a Locust event with timing.

        Args:
            resource: Resource name.
            limits: Default limits for the resource.
            name: Locust stats name (defaults to resource).
        """
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
        """Retrieve resource-level default limits, firing a Locust event with timing.

        Args:
            resource: Resource name.
            name: Locust stats name (defaults to resource).

        Returns:
            List of limits configured for the resource.
        """
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
        """Delete resource-level default limits, firing a Locust event with timing.

        Args:
            resource: Resource name.
            name: Locust stats name (defaults to resource).
        """
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
        """List resources that have default limits, firing a Locust event with timing.

        Args:
            name: Locust stats name (defaults to "system").

        Returns:
            List of resource names with configured defaults.
        """
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
        """Create an entity, firing a Locust event with timing.

        Args:
            entity_id: Entity identifier.
            name: Locust stats name (defaults to entity_id).
            **kwargs: Additional arguments passed to SyncRateLimiter.create_entity().

        Returns:
            The created Entity.
        """
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
        """Retrieve an entity, firing a Locust event with timing.

        Args:
            entity_id: Entity identifier.
            name: Locust stats name (defaults to entity_id).

        Returns:
            The Entity if found, or None.
        """
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
        """Delete an entity, firing a Locust event with timing.

        Args:
            entity_id: Entity identifier.
            name: Locust stats name (defaults to entity_id).
        """
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
        """Retrieve child entities of a parent, firing a Locust event with timing.

        Args:
            parent_id: Parent entity identifier.
            name: Locust stats name (defaults to parent_id).

        Returns:
            List of child Entity objects.
        """
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
        """Set entity-level limits, firing a Locust event with timing.

        Args:
            entity_id: Entity identifier.
            limits: Limits to set.
            resource: Resource name (defaults to "_default_").
            name: Locust stats name (defaults to resource).
        """
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
        """Retrieve entity-level limits, firing a Locust event with timing.

        Args:
            entity_id: Entity identifier.
            resource: Resource name (defaults to "_default_").
            name: Locust stats name (defaults to resource).

        Returns:
            List of limits configured for the entity.
        """
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
        """Delete entity-level limits, firing a Locust event with timing.

        Args:
            entity_id: Entity identifier.
            resource: Resource name (defaults to "_default_").
            name: Locust stats name (defaults to resource).
        """
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
        """Check time until capacity is available, firing a Locust event with timing.

        Args:
            entity_id: Entity to check.
            resource: Resource name.
            needed: Dict of limit_name -> required capacity.
            name: Locust stats name (defaults to resource).

        Returns:
            Seconds until the requested capacity is available (0.0 if already available).
        """
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
        """Check if the rate limiter backend is available, firing a Locust event with timing.

        Args:
            timeout: Timeout in seconds for the availability check.
            name: Locust stats name (defaults to "system").

        Returns:
            True if the backend is reachable within the timeout.
        """
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

        # Fall back to --host or TARGET_STACK_NAME env var if stack_name not set as class attribute
        if not getattr(self, "stack_name", None) and self.host:
            self.stack_name = self.host
        if not getattr(self, "stack_name", None):
            self.stack_name = os.environ.get("TARGET_STACK_NAME", "")

        # Fall back to TARGET_REGION env var if region not customized
        if self.region == "us-east-1":
            self.region = os.environ.get("TARGET_REGION", "us-east-1")

        if not getattr(self, "stack_name", None):
            raise StopTest(
                "You must specify stack_name. Either as a class attribute, "
                "via the --host option, or TARGET_STACK_NAME environment variable."
            )

        # Lazily share a single SyncRateLimiter across all users
        if RateLimiterUser._limiter is None:
            _configure_boto3_pool()
            RateLimiterUser._limiter = SyncRateLimiter(
                name=self.stack_name,
                region=self.region,
            )

        self.client = RateLimiterSession(
            limiter=RateLimiterUser._limiter,
            request_event=self.environment.events.request,
            user=self,
        )
