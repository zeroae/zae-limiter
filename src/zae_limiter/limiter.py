"""Main RateLimiter implementation."""

import asyncio
import logging
import random
import warnings
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from .repository_protocol import RepositoryProtocol, SpeculativeResult

from .bucket import (
    calculate_available,
    declared_statuses,
    force_consume,
    try_consume,
    would_refill_satisfy,
)
from .config_cache import ConfigSource
from .exceptions import (
    RateLimiterUnavailable,
    RateLimitExceeded,
    ResourceDisabled,
    ValidationError,
)
from .lease import Lease, LeaseEntry
from .models import (
    AuditEvent,
    Availability,
    BucketState,
    Entity,
    EntityCapacity,
    Limit,
    LimiterInfo,
    LimitStatus,
    OnUnavailableAction,
    ResourceCapacity,
    StackOptions,
    UsageSnapshot,
    UsageSummary,
    validate_identifier,
    validate_resource,
)
from .repository import Repository
from .repository_protocol import SpeculativeFailureReason
from .schedule import (
    effective_params,
    next_boundary,
    prev_reset_edge,
    retry_after_with_schedule,
)
from .schema import DEFAULT_RESOURCE, WCU_LIMIT_NAME

_UNSET: Any = object()  # sentinel for detecting explicitly-passed deprecated params

logger = logging.getLogger(__name__)

#: The two :data:`~.config_cache.ConfigSource` members that mean "the entity's
#: own configuration": the per-resource level and the entity-wide ``_default_``
#: level (ADR-136).
_ENTITY_CONFIG_SOURCES: frozenset[str] = frozenset({"entity", "entity_default"})


def _is_custom_config(config_source: str | None) -> bool:
    """Whether limits from ``config_source`` are the entity's own (ADR-136).

    Decides bucket TTL: a bucket whose limits resolve from **either** entity
    level is custom and must persist indefinitely; only the resource and system
    levels (and an explicit ``limits`` override) leave it ephemeral, which is
    also how those levels propagate parameter changes, since they do not fan
    out. Kept in one place so the call sites cannot drift apart again (#489).
    """
    return config_source in _ENTITY_CONFIG_SOURCES


def _window_end_in_force(limit: Limit, state: BucketState) -> int | None:
    """The end of the duration window in force on ``state``, or ``None`` (ADR-139).

    Read **after** ``_open_window_if_elapsed`` has run, so it is the end of the
    window this pass admitted against. The lease carries it to the commit,
    which takes a second clock reading a round trip later and must detect a
    window that elapsed in between. Only the resolved ``limit`` decides whether
    a window is in force, the same rule ``_materialisation_stamps`` applies.
    """
    return state.window_end_ms if limit.reset_after is not None else None


class OnUnavailable(Enum):
    """Behavior when DynamoDB is unavailable."""

    ALLOW = "allow"  # Allow requests
    BLOCK = "block"  # Block requests


class RateLimiter:
    """
    Async rate limiter backed by DynamoDB.

    Implements token bucket algorithm with support for:
    - Multiple limits per entity/resource
    - Two-level hierarchy (parent/child entities)
    - Cascade mode (consume from entity + parent)
    - Stored limit configs
    - Usage analytics

    Example (new API - preferred):
        from zae_limiter import RateLimiter, Repository, StackOptions

        repo = Repository(
            name="my-app",
            region="us-east-1",
            stack_options=StackOptions(),
        )
        limiter = RateLimiter(repository=repo)

    Example (old API - deprecated):
        limiter = RateLimiter(
            name="my-app",
            region="us-east-1",
            stack_options=StackOptions(),
        )
    """

    def __init__(
        self,
        # New API (preferred)
        repository: "RepositoryProtocol | None" = None,
        # Old API (deprecated in v0.5.0, removed in v1.0.0)
        name: str | None = None,
        region: str | None = None,
        endpoint_url: str | None = None,
        stack_options: StackOptions | None = None,
        # Deprecated business logic config (now on Repository)
        on_unavailable: "OnUnavailable | Any" = _UNSET,
        auto_update: "bool | Any" = _UNSET,
        bucket_ttl_refill_multiplier: "int | Any" = _UNSET,
        # Business logic config (not deprecated)
        speculative_writes: bool = True,
    ) -> None:
        """
        Initialize the rate limiter.

        Args:
            repository: Repository instance (new API, preferred).
                Pass a Repository or any RepositoryProtocol implementation.
            name: DEPRECATED. Use ``Repository(name=...)`` instead.
            region: DEPRECATED. Use ``Repository(region=...)`` instead.
            endpoint_url: DEPRECATED. Use ``Repository(endpoint_url=...)`` instead.
            stack_options: DEPRECATED. Use ``Repository(stack_options=...)`` instead.
            on_unavailable: DEPRECATED. Use ``set_system_defaults(on_unavailable=...)``
                or pass ``on_unavailable=`` to ``acquire()`` instead.
            auto_update: DEPRECATED. Use ``Repository.builder(...).auto_update().build()``
                instead.
            bucket_ttl_refill_multiplier: DEPRECATED. Use
                ``Repository.builder(...).bucket_ttl_multiplier().build()`` instead.
            speculative_writes: Enable speculative UpdateItem fast path.
                When True, acquire() tries a speculative write first, falling
                back to the full read-write path only when needed.

        Raises:
            ValueError: If both repository and name/region/endpoint_url/stack_options
                are provided.
        """
        from .naming import normalize_name

        # Emit deprecation warnings for deprecated params
        if on_unavailable is not _UNSET:
            warnings.warn(
                "on_unavailable constructor parameter is deprecated. "
                "Use set_system_defaults(on_unavailable=...) or "
                "acquire(on_unavailable=...) instead. "
                "This will be removed in v1.0.0.",
                DeprecationWarning,
                stacklevel=2,
            )
        if auto_update is not _UNSET:
            warnings.warn(
                "auto_update constructor parameter is deprecated. "
                "Use Repository.builder(...).auto_update(True).build() instead. "
                "This will be removed in v1.0.0.",
                DeprecationWarning,
                stacklevel=2,
            )
        if bucket_ttl_refill_multiplier is not _UNSET:
            warnings.warn(
                "bucket_ttl_refill_multiplier constructor parameter is deprecated. "
                "Use Repository.builder(...).bucket_ttl_multiplier(7).build() instead. "
                "This will be removed in v1.0.0.",
                DeprecationWarning,
                stacklevel=2,
            )

        # Check for conflicting parameters
        old_params_provided = any(
            p is not None for p in (name, region, endpoint_url, stack_options)
        )

        if repository is not None and old_params_provided:
            raise ValueError(
                "Cannot specify both 'repository' and 'name'/'region'/'endpoint_url'/"
                "'stack_options'. Use Repository(...) to configure data access."
            )

        if repository is not None:
            # New API: use provided repository
            self._repository = repository
        elif old_params_provided:
            # Old API: emit deprecation warning
            warnings.warn(
                "Passing name/region/endpoint_url/stack_options directly to "
                "RateLimiter is deprecated. Use Repository(...) instead. "
                "This will be removed in v1.0.0.",
                DeprecationWarning,
                stacklevel=2,
            )
            effective_name = name if name is not None else "limiter"
            self._repository = Repository(
                name=normalize_name(effective_name),
                region=region,
                endpoint_url=endpoint_url,
                stack_options=stack_options,
            )
        else:
            # No-args constructor: backward compatible but deprecated
            warnings.warn(
                "RateLimiter() without a repository argument is deprecated. "
                "Use RateLimiter(repository=Repository(...)) instead. "
                "This will be removed in v1.0.0.",
                DeprecationWarning,
                stacklevel=2,
            )
            self._repository = Repository(name=normalize_name("limiter"))

        # Forward deprecated business-logic params to the internally-created repo
        if repository is None:
            assert isinstance(self._repository, Repository)
            repo = self._repository
            if bucket_ttl_refill_multiplier is not _UNSET:
                repo._bucket_ttl_refill_multiplier = bucket_ttl_refill_multiplier
            if on_unavailable is not _UNSET:
                repo._on_unavailable_cache = on_unavailable.value

        self._initialized = False

        # Speculative writes fast path (issue #315)
        self._speculative_writes = speculative_writes

    @property
    def name(self) -> str:
        """DEPRECATED. Use ``repository.stack_name`` instead."""
        warnings.warn(
            "RateLimiter.name is deprecated. "
            "Use repository.stack_name instead. "
            "This will be removed in v1.0.0.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self._repository.stack_name

    @property
    def stack_name(self) -> str:
        """DEPRECATED. Use ``repository.stack_name`` instead."""
        warnings.warn(
            "RateLimiter.stack_name is deprecated. "
            "Use repository.stack_name instead. "
            "This will be removed in v1.0.0.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self._repository.stack_name

    @property
    def table_name(self) -> str:
        """DEPRECATED. Use ``repository.stack_name`` instead."""
        warnings.warn(
            "RateLimiter.table_name is deprecated. "
            "Use repository.stack_name instead. "
            "This will be removed in v1.0.0.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self._repository.stack_name

    @staticmethod
    def _datetime_to_iso(dt: datetime) -> str:
        """Convert datetime to ISO 8601 UTC string.

        Handles both timezone-aware and naive datetimes:
        - Timezone-aware: Converted to UTC, formatted as ISO 8601
        - Naive: Assumed to be UTC, formatted with 'Z' suffix

        Args:
            dt: Datetime to convert

        Returns:
            ISO 8601 formatted UTC timestamp (e.g., "2024-01-01T14:00:00Z")
        """
        from datetime import UTC

        if dt.tzinfo is not None:
            # Convert to UTC if timezone-aware
            utc_dt = dt.astimezone(UTC)
            return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            # Assume naive datetime is UTC
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    @classmethod
    async def list_deployed(
        cls,
        region: str | None = None,
        endpoint_url: str | None = None,
    ) -> list[LimiterInfo]:
        """
        List all deployed rate limiter instances in a region.

        This is a class method that discovers existing deployments without
        requiring an initialized RateLimiter instance. It queries CloudFormation
        for stacks tagged with ``ManagedBy=zae-limiter``.

        Args:
            region: AWS region (default: use boto3 defaults)
            endpoint_url: CloudFormation endpoint (for LocalStack)

        Returns:
            List of LimiterInfo objects describing deployed instances.
            Sorted by user-friendly name. Excludes deleted stacks.

        Example:
            # Discover all limiters in us-east-1
            limiters = await RateLimiter.list_deployed(region="us-east-1")
            for limiter in limiters:
                if limiter.is_healthy:
                    print(f"✓ {limiter.user_name}: {limiter.version}")
                elif limiter.is_failed:
                    print(f"✗ {limiter.user_name}: {limiter.stack_status}")

        Raises:
            ClientError: If CloudFormation API call fails
        """
        from .infra.discovery import InfrastructureDiscovery

        async with InfrastructureDiscovery(region=region, endpoint_url=endpoint_url) as discovery:
            return await discovery.list_limiters()

    async def _ensure_initialized(self) -> None:
        """Ensure infrastructure exists."""
        if self._initialized:
            return

        # If repository was built via builder, infra already handled
        if getattr(self._repository, "_builder_initialized", False):
            self._initialized = True
            return

        # Repository owns infrastructure config - it will no-op if not configured
        await self._repository.ensure_infrastructure()

        self._initialized = True

    async def close(self) -> None:
        """Close the underlying connections."""
        await self._repository.close()

    async def __aenter__(self) -> "RateLimiter":
        await self._ensure_initialized()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    async def is_available(self, timeout: float = 1.0) -> bool:
        """
        Check if the rate limiter backend (DynamoDB) is reachable.

        Performs a lightweight health check without requiring initialization.
        This method never raises exceptions - it returns False on any error.

        Args:
            timeout: Maximum time in seconds to wait for response (default: 1.0)

        Returns:
            True if DynamoDB table is reachable, False otherwise.

        Example:
            limiter = RateLimiter(name="my-app", region="us-east-1")
            if await limiter.is_available():
                async with limiter.acquire(...) as lease:
                    ...
            else:
                # Handle degraded mode
                pass
        """
        try:
            return await asyncio.wait_for(self._repository.ping(), timeout=timeout)
        except (TimeoutError, Exception):
            return False

    # -------------------------------------------------------------------------
    # Entity management
    # -------------------------------------------------------------------------

    async def create_entity(
        self,
        entity_id: str,
        name: str | None = None,
        parent_id: str | None = None,
        cascade: bool = False,
        metadata: dict[str, str] | None = None,
        principal: str | None = None,
    ) -> Entity:
        """
        Create a new entity.

        Args:
            entity_id: Unique identifier for the entity
            name: Human-readable name (defaults to entity_id)
            parent_id: Parent entity ID (None for root/project entities)
            cascade: If True, acquire() will also consume from parent entity
            metadata: Additional metadata to store
            principal: Caller identity for audit logging (optional)

        Returns:
            The created Entity

        Raises:
            EntityExistsError: If entity already exists
        """
        await self._ensure_initialized()
        return await self._repository.create_entity(
            entity_id=entity_id,
            name=name,
            parent_id=parent_id,
            cascade=cascade,
            metadata=metadata,
            principal=principal,
        )

    async def get_entity(self, entity_id: str) -> Entity | None:
        """Get an entity by ID."""
        await self._ensure_initialized()
        return await self._repository.get_entity(entity_id)

    async def delete_entity(
        self,
        entity_id: str,
        principal: str | None = None,
    ) -> None:
        """
        Delete an entity and all its related data.

        Args:
            entity_id: ID of the entity to delete
            principal: Caller identity for audit logging (optional)
        """
        await self._ensure_initialized()
        await self._repository.delete_entity(entity_id, principal=principal)

    async def get_children(self, parent_id: str) -> list[Entity]:
        """Get all children of a parent entity."""
        await self._ensure_initialized()
        return await self._repository.get_children(parent_id)

    async def get_audit_events(
        self,
        entity_id: str,
        limit: int = 100,
        start_event_id: str | None = None,
    ) -> list[AuditEvent]:
        """
        Get audit events for an entity.

        Retrieves security audit events logged for administrative operations
        on the specified entity, ordered by most recent first.

        Args:
            entity_id: ID of the entity to query
            limit: Maximum number of events to return (default: 100)
            start_event_id: Event ID to start after (for pagination)

        Returns:
            List of AuditEvent objects, ordered by most recent first

        Example:
            events = await limiter.get_audit_events("proj-1")
            for event in events:
                print(f"{event.timestamp}: {event.action} by {event.principal}")
        """
        await self._ensure_initialized()
        return await self._repository.get_audit_events(
            entity_id=entity_id,
            limit=limit,
            start_event_id=start_event_id,
        )

    # -------------------------------------------------------------------------
    # Usage snapshots
    # -------------------------------------------------------------------------

    async def get_usage_snapshots(
        self,
        entity_id: str | None = None,
        resource: str | None = None,
        window_type: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
        next_key: dict[str, Any] | None = None,
    ) -> tuple[list[UsageSnapshot], dict[str, Any] | None]:
        """
        Query usage snapshots for historical consumption data.

        Usage snapshots are created by the aggregator Lambda from DynamoDB
        stream events. They track token consumption per entity/resource
        within time windows (hourly, daily).

        Supports two query modes:
        1. Entity-scoped: Provide entity_id (optionally with resource filter)
        2. Resource-scoped: Provide resource to query across all entities

        Args:
            entity_id: Entity to query (uses primary key)
            resource: Resource name filter (required if entity_id is None)
            window_type: Filter by window type ("hourly", "daily")
            start_time: Filter snapshots >= this timestamp
            end_time: Filter snapshots <= this timestamp
            limit: Maximum items to fetch from DynamoDB per page (default: 100)
            next_key: Pagination cursor from previous call

        Returns:
            Tuple of (snapshots, next_key). next_key is None if no more results.

        Raises:
            ValueError: If neither entity_id nor resource is provided

        Note:
            The ``limit`` parameter controls the DynamoDB query batch size.
            Client-side filters (window_type, start_time, end_time) are applied
            after fetching, so the returned count may be less than ``limit``.
            Use ``next_key`` to paginate through all matching results.

        Example:
            # Get hourly snapshots for an entity
            snapshots, cursor = await limiter.get_usage_snapshots(
                entity_id="user-123",
                resource="gpt-4",
                window_type="hourly",
                start_time=datetime(2024, 1, 1),
                end_time=datetime(2024, 1, 31),
            )
            for snap in snapshots:
                print(f"{snap.window_start}: {snap.counters}")

            # Paginate through results
            while cursor:
                more, cursor = await limiter.get_usage_snapshots(
                    entity_id="user-123",
                    next_key=cursor,
                )
        """
        await self._ensure_initialized()

        # Convert datetime to ISO strings for repository
        # Note: Naive datetimes are assumed to be UTC
        start_str = self._datetime_to_iso(start_time) if start_time else None
        end_str = self._datetime_to_iso(end_time) if end_time else None

        return await self._repository.get_usage_snapshots(
            entity_id=entity_id,
            resource=resource,
            window_type=window_type,
            start_time=start_str,
            end_time=end_str,
            limit=limit,
            next_key=next_key,
        )

    async def get_usage_summary(
        self,
        entity_id: str | None = None,
        resource: str | None = None,
        window_type: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> UsageSummary:
        """
        Get aggregated usage summary across multiple snapshots.

        Fetches all matching snapshots and computes total and average
        consumption statistics. Useful for billing, reporting, and
        capacity planning.

        Args:
            entity_id: Entity to query
            resource: Resource name filter (required if entity_id is None)
            window_type: Filter by window type ("hourly", "daily")
            start_time: Filter snapshots >= this timestamp
            end_time: Filter snapshots <= this timestamp

        Returns:
            UsageSummary with total and average consumption per limit type

        Raises:
            ValueError: If neither entity_id nor resource is provided

        Example:
            summary = await limiter.get_usage_summary(
                entity_id="user-123",
                resource="gpt-4",
                window_type="hourly",
                start_time=datetime(2024, 1, 1),
                end_time=datetime(2024, 1, 31),
            )
            print(f"Total tokens: {summary.total.get('tpm', 0)}")
            print(f"Average per hour: {summary.average.get('tpm', 0.0):.1f}")
            print(f"Snapshots: {summary.snapshot_count}")
        """
        await self._ensure_initialized()

        # Convert datetime to ISO strings for repository
        # Note: Naive datetimes are assumed to be UTC
        start_str = self._datetime_to_iso(start_time) if start_time else None
        end_str = self._datetime_to_iso(end_time) if end_time else None

        return await self._repository.get_usage_summary(
            entity_id=entity_id,
            resource=resource,
            window_type=window_type,
            start_time=start_str,
            end_time=end_str,
        )

    # -------------------------------------------------------------------------
    # Rate limiting
    # -------------------------------------------------------------------------

    @asynccontextmanager
    async def acquire(
        self,
        entity_id: str,
        resource: str,
        consume: dict[str, int],
        limits: list[Limit] | None = None,
        use_stored_limits: bool = False,
        on_unavailable: OnUnavailable | None = None,
    ) -> AsyncIterator[Lease]:
        """
        Acquire rate limit capacity.

        Limits are resolved automatically from stored config using four-tier
        hierarchy: Entity > Entity Default > Resource > System. Pass ``limits`` to override.

        Cascade behavior is controlled by the entity's ``cascade`` flag, set at
        entity creation time via ``create_entity(cascade=True)``. When enabled,
        acquire() automatically consumes from both the entity and its parent.

        Args:
            entity_id: Entity to acquire capacity for
            resource: Resource being accessed (e.g., "gpt-4")
            consume: Amounts to consume by limit name
            limits: Override stored config with explicit limits (optional)
            use_stored_limits: DEPRECATED - limits are now always resolved from
                stored config. This parameter will be removed in v1.0.
            on_unavailable: Override default on_unavailable behavior

        Yields:
            Lease for managing additional consumption

        Raises:
            RateLimitExceeded: If any limit would be exceeded
            RateLimiterUnavailable: If DynamoDB unavailable and BLOCK
            ValidationError: If no limits configured at any level
        """
        await self._ensure_initialized()

        # Deprecation warning for use_stored_limits
        if use_stored_limits:
            warnings.warn(
                "use_stored_limits is deprecated and will be removed in v1.0. "
                "Limits are now always resolved from stored config (Entity > Resource > System). "
                "Pass limits parameter as override if needed.",
                DeprecationWarning,
                stacklevel=2,
            )

        # Resolve on_unavailable mode
        mode = await self._resolve_on_unavailable(on_unavailable)

        # Acquire the lease (this may fail due to rate limit or infrastructure)
        try:
            lease: Lease | None = None
            # Shard the fast path selected, and the shard_count it observed on
            # the failure image; the slow path must read and create that same
            # shard, sized by that count, rather than draw again from a cache
            # a failed speculative write never updates (issue #439).
            slow_path_shard: int | None = None
            slow_path_shard_count: int | None = None
            slow_path_parent_shard: int | None = None

            # Try speculative fast path first (issue #315)
            if self._speculative_writes:
                (
                    lease,
                    slow_path_shard,
                    slow_path_shard_count,
                    slow_path_parent_shard,
                ) = await self._try_speculative_acquire(
                    entity_id=entity_id,
                    resource=resource,
                    consume=consume,
                )

            # Fall back to slow path if speculative didn't succeed
            if lease is None:
                lease = await self._do_acquire(
                    entity_id=entity_id,
                    resource=resource,
                    limits_override=limits,
                    consume=consume,
                    shard_id=slow_path_shard,
                    shard_count=slow_path_shard_count,
                    parent_shard_id=slow_path_parent_shard,
                )
        except (RateLimitExceeded, ValidationError, ResourceDisabled, Warning):
            # `Warning`: under warnings-as-errors (-W error, or a
            # simplefilter("error")) the FutureWarnings this module emits
            # (Issue #455) are raised as exceptions. They are the caller's
            # signal, not a backend outage, and must never be turned into
            # RateLimiterUnavailable or swallowed by a degraded lease.
            raise
        except Exception as e:
            if mode == OnUnavailable.ALLOW:
                # Return a no-op lease. `degraded` exempts it from declared-
                # scope validation (Issue #455): it has no entries by design.
                yield Lease(repository=self._repository, degraded=True)
                return
            else:
                raise RateLimiterUnavailable(
                    str(e),
                    cause=e,
                    stack_name=self._repository.stack_name,
                    entity_id=entity_id,
                    resource=resource,
                ) from e

        # Write initial consumption to DynamoDB before yielding (Issue #309)
        # No-op for speculative leases (already committed by UpdateItem)
        await lease._commit_initial()

        # Lease committed - manage the context
        try:
            yield lease
            await lease._commit_adjustments()
        except Exception:
            await lease._rollback()
            raise

    async def _try_speculative_acquire(
        self,
        entity_id: str,
        resource: str,
        consume: dict[str, int],
    ) -> tuple[Lease | None, int, int | None, int | None]:
        """Try the speculative fast path for acquire (issue #315).

        Repository checks its own entity cache (issue #318) and issues
        parallel child+parent UpdateItems when cache hit + cascade.

        Returns:
            ``(lease, shard_id, shard_count)``. ``lease`` is the pre-committed
            Lease when the speculative write succeeded, or None when the slow
            path is needed (refill would help, bucket missing, or config
            changed). ``shard_id`` is the child shard the slow path must then
            target: the shard that reported ``BUCKET_MISSING``, or a brand-new
            shard after wcu-driven doubling; ``shard_count`` is the count
            observed on the failure image (or after the bump), which the slow
            path must use to size and stamp a shard it creates — a failed
            speculative write never updates the entity cache (issue #439).
            Both are meaningless when ``lease`` is not None.

        Raises:
            RateLimitExceeded: If the bucket is truly exhausted (refill
                wouldn't help). Saves 1 RCU vs the slow path.
        """
        now_ms = self._repository._now_ms()

        # Repository handles cache check and parallel writes (issue #318)
        result = await self._repository.speculative_consume(
            entity_id=entity_id,
            resource=resource,
            consume=consume,
            now_ms=now_ms,
        )

        if not result.success:
            # Only a parent shard the fast path found MISSING pins the slow
            # path, so it is created where the fast path looked (issue #474).
            # An exhausted one must not: every shard holds its own share and
            # ADR-134 re-picks on every call, so a re-draw can admit where the
            # drawn shard could not — and pinning a wcu-exhausted shard would
            # send the slow path's writes straight back onto the hot partition.
            parent_hint = (
                result.parent_result.shard_id
                if result.parent_result is not None
                and result.parent_result.failure_reason == SpeculativeFailureReason.BUCKET_MISSING
                else None
            )

            # Child failed — check if parent was also tried (parallel path)
            if result.parent_result is not None and result.parent_result.success:
                assert result.parent_id is not None  # set by repository cache path
                await self._compensate_speculative(
                    result.parent_id, resource, consume, result.parent_result.shard_id
                )

            # Disabled: no shard retry or doubling can help (ADR-125).
            if result.failure_reason == SpeculativeFailureReason.DISABLED:
                raise ResourceDisabled(entity_id=entity_id, resource=resource, level="bucket")

            # The parallel cascade path issues both writes at once, so both can
            # fail — and a disabled parent has to outrank whatever the child
            # failed on. Falling through with the child's reason reports
            # RateLimitExceeded, whose retry_after_seconds invites a retry that
            # will hit the same disabled parent every time. Nothing was consumed
            # from the child here (its conditional write failed too), so unlike
            # the parent-succeeded branch above there is nothing to compensate.
            if (
                result.parent_result is not None
                and result.parent_result.failure_reason == SpeculativeFailureReason.DISABLED
            ):
                assert result.parent_id is not None  # set by repository cache path
                raise ResourceDisabled(
                    entity_id=result.parent_id, resource=resource, level="bucket"
                )

            # Shard doubling: if wcu exhausted, double shard_count and update
            # cache — but only once the acquire is going to proceed, exactly as
            # the cascade parent path does (issue #474, `_handle_nested_parent_
            # failure`). `BOTH_EXHAUSTED` means a declared limit is drained too,
            # so this may be on its way to a RateLimitExceeded; doubling there
            # is a pure side effect (nothing creates or reads the shard it hands
            # back) and one doubling per rejection walks an entity sitting at
            # its limit to MAX_SHARD_COUNT, shrinking every shard's share for
            # good (issue #480). A plain `WCU_EXHAUSTED` passes the gate by
            # construction — `consume` never names wcu, so the declared limits
            # are all satisfiable — which keeps the GHSA-76rv mitigation and
            # ADR-133's wcu-refill route intact: see
            # `test_wcu_exhaustion_with_room_to_admit_still_doubles` and
            # `test_exhausted_wcu_that_would_refill_does_not_double`.
            if result.failure_reason in (
                SpeculativeFailureReason.WCU_EXHAUSTED,
                SpeculativeFailureReason.BOTH_EXHAUSTED,
            ):
                self._check_speculative_failure(result, consume, now_ms)
                new_shard, new_count = await self._shard_after_wcu_exhaustion(
                    entity_id, resource, result, now_ms
                )
                return None, new_shard, new_count, parent_hint

            # Shard retry: if multi-shard and app limit exhausted, try another shard
            if (
                result.shard_count > 1
                and result.failure_reason == SpeculativeFailureReason.APP_LIMIT_EXHAUSTED
            ):
                if result.cascade:
                    # A speculative retry is a child-only write: accepting its
                    # lease would admit the child past the parent's limit (the
                    # parent's parallel debit was compensated above, or never
                    # attempted). Fast-reject from the child's image when a
                    # refill would not help (0 reads, as for any other
                    # exhausted bucket); otherwise hand an untried shard to the
                    # slow path, which commits child + parent in one transaction.
                    self._check_speculative_failure(result, consume, now_ms)
                    untried = [s for s in range(result.shard_count) if s != result.shard_id]
                    return None, random.choice(untried), result.shard_count, parent_hint
                retry_result, slow_path_shard = await self._retry_on_other_shard(
                    entity_id, resource, consume, ttl_seconds=None, result=result, now_ms=now_ms
                )
                if retry_result is not None:
                    return retry_result, result.shard_id, result.shard_count, parent_hint
                if slow_path_shard is not None:
                    # A probed shard the fast path could not settle: one the
                    # entity is entitled to but that does not exist yet (issue
                    # #439), or one past its schedule boundary (#222). Either
                    # way the slow path goes there rather than fast-rejecting
                    # on the drained shard's balance.
                    return None, slow_path_shard, result.shard_count, parent_hint

            self._check_speculative_failure(result, consume, now_ms)
            # BUCKET_MISSING has no image to read a shard_count from; let the
            # slow path fall back to the entity cache in that case.
            observed_count = (
                None
                if result.failure_reason == SpeculativeFailureReason.BUCKET_MISSING
                else result.shard_count
            )
            return None, result.shard_id, observed_count, parent_hint

        # Child succeeded — build entries from ALL_NEW
        entries: list[LeaseEntry] = []
        for state in result.buckets:
            # Key on membership, not on the amount. An estimate of 0 is
            # legitimate — "this limit is in play, I'll reconcile the cost
            # afterwards" — and it still needs a LeaseEntry, or every later
            # adjust()/consume()/release() against it is a silent no-op. A
            # limit the caller never named stays out, which also keeps the
            # reserved `wcu` infrastructure limit (carried in result.buckets
            # but never in consume) from leaking into the lease.
            if state.limit_name not in consume:
                continue
            amount = consume[state.limit_name]
            limit = Limit.from_bucket_state(state)
            entries.append(
                LeaseEntry(
                    entity_id=state.entity_id,
                    resource=state.resource,
                    limit=limit,
                    state=state,
                    consumed=amount,
                    _shard_id=result.shard_id,
                    _cascade=result.cascade,
                    _parent_id=result.parent_id,
                )
            )

        # Handle parent result from parallel path (issue #318)
        if result.parent_result is not None:
            if result.parent_result.success:
                for state in result.parent_result.buckets:
                    if state.limit_name not in consume:
                        continue
                    amount = consume[state.limit_name]
                    limit = Limit.from_bucket_state(state)
                    entries.append(
                        LeaseEntry(
                            entity_id=state.entity_id,
                            resource=state.resource,
                            limit=limit,
                            state=state,
                            consumed=amount,
                            _shard_id=result.parent_result.shard_id,
                        )
                    )
            else:
                # The parent shard the fast path judged (or the new one a wcu
                # doubling added) is where the slow path must look: a missing
                # shard is created there, and an existing one is debited there.
                nested, parent_hint = await self._handle_nested_parent_failure(
                    entity_id, resource, consume, result, now_ms
                )
                return nested, result.shard_id, result.shard_count, parent_hint
        elif result.cascade and result.parent_id:
            # Cache miss cascade — sequential parent speculative
            parent_id = result.parent_id
            parent_result = await self._repository.speculative_consume(
                entity_id=parent_id,
                resource=resource,
                consume=consume,
                now_ms=now_ms,
            )

            if parent_result.success:
                for state in parent_result.buckets:
                    if state.limit_name not in consume:
                        continue
                    amount = consume[state.limit_name]
                    limit = Limit.from_bucket_state(state)
                    entries.append(
                        LeaseEntry(
                            entity_id=state.entity_id,
                            resource=state.resource,
                            limit=limit,
                            state=state,
                            consumed=amount,
                            _shard_id=parent_result.shard_id,
                        )
                    )
            else:
                # Identical handling to the warm parallel path, which is what
                # `_handle_nested_parent_failure` is: the first acquire of an
                # entity is just as able to find the parent's shard missing or
                # hot, and it must spread, hint and compensate the same way
                # (issue #474). The nested result is the contract that handler
                # reads, so hand the sequential one over on the same field.
                result.parent_result = parent_result
                nested, parent_hint = await self._handle_nested_parent_failure(
                    entity_id, resource, consume, result, now_ms
                )
                return nested, result.shard_id, result.shard_count, parent_hint

        # Build pre-committed lease
        lease = Lease(
            repository=self._repository,
            entries=entries,
        )
        lease._initial_committed = True
        for entry in entries:
            entry._initial_consumed = entry.consumed
        return lease, result.shard_id, result.shard_count, None

    async def _handle_nested_parent_failure(
        self,
        entity_id: str,
        resource: str,
        consume: dict[str, int],
        result: "SpeculativeResult",
        now_ms: int,
    ) -> tuple[Lease | None, int | None]:
        """Handle parent failure from nested SpeculativeResult (issue #318).

        Child succeeded speculatively (result.success=True).
        Parent failed (result.parent_result.success=False).
        Decides whether to compensate child and fall back, try parent-only
        slow path, or fast-reject.

        Returns:
            ``(lease, parent_shard_id)``. ``lease`` is set when the parent-only
            slow path succeeded, None when the full slow path is needed.
            ``parent_shard_id`` is a parent shard the slow path must target —
            one the speculative write found MISSING, or the brand-new shard a
            wcu-driven doubling added — and None when the slow path is free to
            draw its own, which it must be for an exhausted shard (issue #474).

        Raises:
            RateLimitExceeded: If parent is truly exhausted.
        """
        assert result.parent_result is not None  # caller checks this
        assert result.parent_id is not None  # set by repository cache path
        parent_result = result.parent_result
        parent_id = result.parent_id
        parent_shard = parent_result.shard_id
        parent_shard_count = parent_result.shard_count
        # Same rule as the child-failure branch: only a MISSING parent shard
        # pins the slow path to a shard. A wcu bump below replaces this with
        # the shard the doubling added, which nothing else will create.
        parent_hint = (
            parent_shard
            if parent_result.failure_reason == SpeculativeFailureReason.BUCKET_MISSING
            else None
        )

        # Disabled wins over every other classification: no refill or slow-path
        # retry can help (ADR-125). The child's speculatively consumed tokens
        # must be returned before the exception propagates.
        if parent_result.failure_reason == SpeculativeFailureReason.DISABLED:
            await self._compensate_child(entity_id, resource, consume, result.shard_id)
            raise ResourceDisabled(entity_id=parent_id, resource=resource, level="bucket")

        # A closed schedule window on the parent is not a rejection either
        # (#222 §2.1), and unlike the child path there is no shared helper to
        # short-circuit: the `would_refill_satisfy` gate below would judge the
        # parent against parameters that no longer apply and raise
        # RateLimitExceeded on a window that may have just been widened. Take
        # the same route a parent image this path cannot use already takes —
        # give the child's tokens back and hand the whole acquire to the slow
        # path, which re-materialises parent and child together. The parent
        # shard is left unpinned: every shard crosses the boundary at once, so
        # pinning this one buys nothing and would concentrate the writes.
        if parent_result.failure_reason is SpeculativeFailureReason.SCHEDULE_BOUNDARY:
            await self._compensate_child(entity_id, resource, consume, result.shard_id)
            return None, parent_hint

        if parent_result.old_buckets is None:
            await self._compensate_child(entity_id, resource, consume, result.shard_id)
            return None, parent_hint

        parent_names = {b.limit_name for b in parent_result.old_buckets}
        if not all(name in parent_names for name in consume):
            await self._compensate_child(entity_id, resource, consume, result.shard_id)
            return None, parent_hint

        would_help, parent_statuses = would_refill_satisfy(
            parent_result.old_buckets, consume, now_ms
        )
        if not would_help:
            await self._compensate_child(entity_id, resource, consume, result.shard_id)
            child_statuses = declared_statuses(result.buckets, consume, now_ms)
            raise RateLimitExceeded(child_statuses + parent_statuses)

        # Only now that the acquire is going to proceed may the parent's wcu
        # exhaustion spread it, exactly as the child path spreads a hot child
        # (issue #474) — nothing bumped the parent before, so a high-fanout
        # parent never left shard 0. Doubling above the `would_help` gate would
        # be a pure side effect: the shard it hands back is never created or
        # read, and one doubling per rejection walks a parent sitting at its
        # limit to MAX_SHARD_COUNT, shrinking every shard's share for good.
        if parent_result.failure_reason in (
            SpeculativeFailureReason.WCU_EXHAUSTED,
            SpeculativeFailureReason.BOTH_EXHAUSTED,
        ):
            parent_shard, parent_shard_count = await self._shard_after_wcu_exhaustion(
                parent_id, resource, parent_result, now_ms
            )
            if parent_shard != parent_result.shard_id:
                # The doubling drew from range(old_count, new_count), so this
                # shard does not exist yet: a parent-only attempt could only
                # resolve limits, read a miss and return None. Hand the child
                # straight to the full slow path, which creates it.
                await self._compensate_child(entity_id, resource, consume, result.shard_id)
                return None, parent_shard

        # Refill would help — build child entries for parent-only slow path
        entries: list[LeaseEntry] = []
        for state in result.buckets:
            # Key on membership, not on the amount. An estimate of 0 is
            # legitimate — "this limit is in play, I'll reconcile the cost
            # afterwards" — and it still needs a LeaseEntry, or every later
            # adjust()/consume()/release() against it is a silent no-op. A
            # limit the caller never named stays out, which also keeps the
            # reserved `wcu` infrastructure limit (carried in result.buckets
            # but never in consume) from leaking into the lease.
            if state.limit_name not in consume:
                continue
            amount = consume[state.limit_name]
            limit = Limit.from_bucket_state(state)
            entries.append(
                LeaseEntry(
                    entity_id=state.entity_id,
                    resource=state.resource,
                    limit=limit,
                    state=state,
                    consumed=amount,
                    _shard_id=result.shard_id,
                    _cascade=result.cascade,
                    _parent_id=result.parent_id,
                )
            )

        try:
            parent_lease = await self._try_parent_only_acquire(
                parent_id,
                resource,
                consume,
                entries,
                parent_shard,
                parent_shard_count,
            )
        except Exception:
            await self._compensate_child(entity_id, resource, consume, result.shard_id)
            raise

        if parent_lease is not None:
            return parent_lease, parent_hint

        await self._compensate_child(entity_id, resource, consume, result.shard_id)
        return None, parent_hint

    async def _shard_after_wcu_exhaustion(
        self,
        entity_id: str,
        resource: str,
        result: "SpeculativeResult",
        now_ms: int,
    ) -> tuple[int, int]:
        """Pick the shard to fall back to after a ``wcu`` exhaustion.

        Used for the child and, since issue #474, for a cascade parent as well:
        a hot cascade parent has to spread off its shard exactly like a hot
        child, or every one of its children keeps hammering the same partition.

        Returns:
            ``(shard_id, shard_count)`` for the slow path — the same shard when
            the exhaustion is not a hot partition, otherwise one of the shards
            the doubling just added, with the new count.
        """
        # An exhausted wcu whose refill would already restore it is not a hot
        # partition — nobody refills wcu on the fast path, and without the
        # aggregator nobody refills it at all. Take the slow path on this shard
        # (it refills wcu, ADR-133) rather than doubling toward
        # MAX_SHARD_COUNT on a stale balance.
        wcu_state = next(
            (b for b in result.old_buckets or [] if b.limit_name == WCU_LIMIT_NAME),
            None,
        )
        if wcu_state is not None and try_consume(wcu_state, 1, now_ms).success:
            return result.shard_id, result.shard_count
        new_count = await self._repository.bump_shard_count(entity_id, resource, result.shard_count)
        # Send the slow path to a shard that is not the hot one: one of the
        # shards the doubling just added, which it will create (issue #439).
        # bump_shard_count returns the winner's count when another client
        # doubled first, so this range is new either way; only a vanished
        # shard 0 leaves nothing to add.
        if new_count > result.shard_count:
            return random.randrange(result.shard_count, new_count), new_count
        return result.shard_id, new_count

    async def _compensate_child(
        self,
        entity_id: str,
        resource: str,
        consume: dict[str, int],
        shard_id: int,
    ) -> None:
        """Compensate a speculatively consumed child by adding tokens back."""
        await self._compensate_speculative(entity_id, resource, consume, shard_id)

    async def _compensate_speculative(
        self,
        entity_id: str,
        resource: str,
        consume: dict[str, int],
        shard_id: int,
    ) -> None:
        """Compensate a speculative write by adding consumed tokens back.

        The credit must land on the shard the speculative debit hit
        (GHSA-76rv): crediting shard 0 leaves the debited shard short and
        mints tokens on a shard that served nothing.
        """
        deltas = {name: -(amount * 1000) for name, amount in consume.items()}
        compensate_item = self._repository.build_composite_adjust(
            entity_id=entity_id,
            resource=resource,
            deltas=deltas,
            shard_id=shard_id,
        )
        await self._repository.write_each([compensate_item])

    @staticmethod
    def _check_speculative_failure(
        result: "SpeculativeResult",
        consume: dict[str, int],
        now_ms: int,
    ) -> None:
        """Check a failed speculative result and raise if truly exhausted.

        Raises RateLimitExceeded if refill won't help (fast rejection).
        Returns normally if slow path should be attempted.
        """
        if result.old_buckets is None:
            return

        # A closed schedule window is never a rejection (#222 §2.1). The image
        # reports `tk` materialised under parameters that no longer apply, so
        # `would_refill_satisfy` would be answering the wrong question — and
        # answering it "no" raises RateLimitExceeded against a limit the new
        # window may have just raised. Only the slow path can re-materialise.
        if result.failure_reason is SpeculativeFailureReason.SCHEDULE_BOUNDARY:
            return

        bucket_names = {b.limit_name for b in result.old_buckets}
        if not all(name in bucket_names for name in consume):
            return

        would_help, statuses = would_refill_satisfy(result.old_buckets, consume, now_ms)
        if not would_help:
            raise RateLimitExceeded(statuses)

    _MAX_SHARD_RETRIES = 2

    async def _retry_on_other_shard(
        self,
        entity_id: str,
        resource: str,
        consume: dict[str, int],
        ttl_seconds: int | None,
        result: "SpeculativeResult",
        now_ms: int,
    ) -> "tuple[Lease | None, int | None]":
        """Retry speculative consume on untried shards (GHSA-76rv shard retry).

        When application limits are exhausted on one shard, other shards may
        still have available tokens (since capacity is divided across shards).
        This method picks random untried shards up to ``_MAX_SHARD_RETRIES``.

        Args:
            entity_id: Entity owning the bucket
            resource: Resource name
            consume: Amount per limit (tokens, not milli)
            ttl_seconds: TTL in seconds from now, or None for no TTL change
            result: The failed SpeculativeResult from the initial shard
            now_ms: The acquire's single clock reading (issue #430), carried
                on so a retry does not observe a different instant than the
                attempt that sent it here

        Returns:
            ``(lease, slow_path_shard)``. ``lease`` is set if a retry on
            another shard succeeded. Otherwise ``slow_path_shard`` is the
            first shard a retry found the fast path cannot settle — one that
            does not exist yet (``BUCKET_MISSING``, issue #439) or one whose
            schedule window has closed (``SCHEDULE_BOUNDARY``, #222 §2.1) —
            so the slow path creates or re-materialises it there instead of
            fast-rejecting on the drained shard that sent us here. Probing
            stops at the first such shard. None if every retried shard was
            simply exhausted or no untried shards remain. Never called for
            cascading entities.
        """
        tried_shards = {result.shard_id}
        shard_count = result.shard_count
        slow_path_shard: int | None = None

        for _ in range(self._MAX_SHARD_RETRIES):
            untried = [s for s in range(shard_count) if s not in tried_shards]
            if not untried:
                break
            new_shard = random.choice(untried)
            tried_shards.add(new_shard)

            retry = await self._repository.speculative_consume(
                entity_id, resource, consume, ttl_seconds, shard_id=new_shard, now_ms=now_ms
            )
            if retry.success:
                return (
                    self._build_lease_from_speculative(entity_id, resource, consume, retry),
                    None,
                )
            if retry.failure_reason in (
                SpeculativeFailureReason.BUCKET_MISSING,
                SpeculativeFailureReason.SCHEDULE_BOUNDARY,
            ):
                # Probing further shards costs 1 RT + 1 WCU each; a missing
                # shard is one the slow path will create with a fresh share,
                # and a boundary-expired one is a shard the slow path will
                # re-materialise. Both are "the fast path cannot settle this,
                # but the slow path can" — falling through to the caller's
                # fast rejection instead would reject on the first shard's
                # stale balance while this one was about to be refilled.
                slow_path_shard = new_shard
                break
        return None, slow_path_shard

    def _build_lease_from_speculative(
        self,
        entity_id: str,
        resource: str,
        consume: dict[str, int],
        result: "SpeculativeResult",
    ) -> "Lease":
        """Build a pre-committed Lease from a successful speculative result.

        Args:
            entity_id: Entity owning the bucket
            resource: Resource name
            consume: Amount per limit that was consumed
            result: Successful SpeculativeResult with ALL_NEW buckets
        """
        entries: list[LeaseEntry] = []
        for state in result.buckets:
            # Key on membership, not on the amount. An estimate of 0 is
            # legitimate — "this limit is in play, I'll reconcile the cost
            # afterwards" — and it still needs a LeaseEntry, or every later
            # adjust()/consume()/release() against it is a silent no-op. A
            # limit the caller never named stays out, which also keeps the
            # reserved `wcu` infrastructure limit (carried in result.buckets
            # but never in consume) from leaking into the lease.
            if state.limit_name not in consume:
                continue
            amount = consume[state.limit_name]
            limit = Limit.from_bucket_state(state)
            entries.append(
                LeaseEntry(
                    entity_id=state.entity_id,
                    resource=state.resource,
                    limit=limit,
                    state=state,
                    consumed=amount,
                    _shard_id=result.shard_id,
                    _cascade=result.cascade,
                    _parent_id=result.parent_id,
                )
            )
        # Mirror the sibling speculative path exactly: the UpdateItem has
        # already persisted the initial consumption, so mark it committed —
        # but via _initial_committed, not _committed. Both Lease.adjust() and
        # Lease._rollback() short-circuit on _committed, which would make
        # adjustments raise LeaseExpiredError and silently skip compensation
        # for tokens this path has already consumed. Seeding
        # _initial_consumed is part of the same contract: without it
        # _commit_adjustments() would re-write the initial consumption as a
        # delta and double-count it.
        lease = Lease(
            entries=entries,
            repository=self._repository,
        )
        lease._initial_committed = True
        for entry in entries:
            entry._initial_consumed = entry.consumed
        return lease

    @staticmethod
    def _warn_unknown_limits(
        consume: dict[str, int],
        limits: list[Limit],
        resource: str,
        *,
        config_source: str,
        stacklevel: int,
    ) -> frozenset[str]:
        """Report keys in ``consume`` that name no configured limit (Issue #455).

        Returns the unknown keys so the lease can skip them in its own
        declared-scope check: they were reported here with the right advice.

        Such a key is dropped at admission (nothing gates it), after which
        every ``lease.adjust()`` on it would warn "not declared in consume" —
        pointing at the wrong fix, since the caller did declare it. Close it
        at the boundary where the declaration is made. Warning only, same
        staging as the rest of #455: ``FutureWarning`` now, ``ValidationError``
        in v1.0.0. The fast path cannot see this (an unknown key makes the
        speculative write fail and fall back), so the slow-path check covers
        both paths.

        Compared against every limit the acquire can gate: the acquiring
        entity's own limits plus, when cascading, the parent's. Either side
        may track a subset of the other (per-user rpm on the child, org-level
        tpm on the parent, or a parent on rpm only); a key known to either
        side is not unknown and must not warn.

        Args:
            stacklevel: Frames from this helper to the ``acquire()`` caller;
                the ``with``/``async with`` context-manager machinery adds one.
                The single call site is ``_do_acquire`` (helper -> _do_acquire
                -> acquire -> __aenter__ -> caller), so it is always 5.
        """
        configured = sorted(limit.name for limit in limits)
        unknown = sorted(set(consume) - set(configured))
        if not unknown:
            return frozenset()
        if config_source == "override":
            where = "not in the `limits` override passed to acquire()"
            listing = "override limits"
        else:
            where = "not configured for this resource"
            listing = "configured limits"
        # The text deliberately carries no entity id or resource name: the
        # warnings registry is keyed on (text, category, lineno), so per-entity
        # text would add a registry entry per entity and defeat the default
        # once-per-location filter — a warning storm. The resource goes to the
        # log; the entity id does not, because entity ids are routinely API keys
        # and must not be written to logs in clear text.
        warnings.warn(
            f"acquire() names limit(s) {unknown} that are {where}; {listing}: {configured}. "
            "Unknown keys are ignored. This becomes a ValidationError in v1.0.0.",
            FutureWarning,
            stacklevel=stacklevel,
        )
        logger.warning(
            "acquire(): unknown limit key(s) %s for resource %r (%s: %s)",
            unknown,
            resource,
            listing,
            configured,
        )
        return frozenset(unknown)

    @staticmethod
    def _apply_reset_edge(limit: Limit, state: BucketState, now_ms: int) -> bool:
        """Restore the balance if a calendar reset edge was crossed (#222 §3.6).

        Detection is **backwards**: "was there a rising edge since this item was
        last refilled?", not "is a reset due?". That makes idle buckets correct
        for free — a bucket idle from 18:00 to 09:00 has its ``rf`` sitting at
        18:00, and the 09:00 pass sees the missed midnight and applies it then.
        Two missed midnights apply once, because setting the balance to the
        capacity is idempotent.

        The comparison is strictly ``>``: the pass that applies a reset stamps
        ``rf`` at or after the edge, so ``>=`` would re-fire on every later
        request and refund everything spent since — an unbounded quota.

        The target is the **shard's share** of the capacity *in force at*
        ``now_ms``: ``effective_capacity_milli`` applies the parameter schedule
        and then divides by ``shard_count``. Resetting every shard to the
        undivided capacity would multiply the entity's quota by ``shard_count``.

        Must be called **before** :meth:`_admit_limit`, so the restored balance
        gates the request that crossed the edge rather than the one after it.
        Mutates ``state`` in place and returns whether it did;
        ``_original_tokens_milli`` and ``_original_rf_ms`` must already have
        been captured, because they are the *stored* values the ``ADD`` delta
        and the ``rf`` lock are built from. ``tc`` is never touched: the
        counter has to stay monotonic (``.claude/rules/design-validation.md``),
        which is what makes this safer than #471's ``reset_bucket()``.
        """
        if not limit.reset_schedule:
            return False
        edge = prev_reset_edge(limit.reset_schedule, now_ms)
        if edge is None or edge <= state.last_refill_ms:
            return False
        state.tokens_milli = state.effective_capacity_milli(now_ms)
        return True

    @staticmethod
    def _apply_window_roll(
        limit: Limit, state: BucketState, now_ms: int, *, opened: bool = False
    ) -> bool:
        """Restore the balance if a duration window has been rolled (ADR-139).

        :meth:`_apply_reset_edge` with the backwards cron scan replaced by an
        attribute read (:attr:`BucketState.window_rolled`, ``ws > rf``), and
        every property that one was designed for carries over verbatim:

        - **Idempotent.** It is a set, not an add, so two shards applying the
          same ``ws``, or one shard seeing it on two successive passes,
          converge.
        - **Idle buckets are correct for free.** A shard idle across three
          window boundaries applies one roll on wake, because ``ws`` holds only
          the *current* window's start.
        - **Strictly ``>``.** The pass that applies the roll stamps ``rf`` at or
          after ``ws``, so ``>=`` would re-fire on every later request and
          refund everything spent since — an unbounded quota.
        - **Per shard, to the shard's share.** ``effective_capacity_milli``
          applies the parameter schedule and then divides by ``shard_count``.
          Resetting every shard to the undivided capacity would multiply the
          entity's quota by ``shard_count``.
        - **``tc`` untouched**, so the consumption counter stays monotonic.

        The *anchoring* of a new window is **not** here. This applies a window
        another writer (or an earlier pass) already opened. Opening one is
        :meth:`_open_window_if_elapsed`, which runs immediately before this and
        mutates the same ``state``; the caller passes ``opened=True`` when it
        did, and the reset is then **unconditional**. ``ws > rf`` is the rule
        for a shard that *sees* a window another writer opened; the opener
        applies its own reset under its own ``rf`` lock (ADR-139). Gating the
        opener on ``ws > rf`` too would fail whenever another writer's clock
        stamped ``rf`` after this client's ``now``: the window would be anchored
        over the dead window's leftovers, and because ``rf >= ws`` after the
        commit, ``ws > rf`` would never hold again — the entity held to those
        leftovers for a whole new window. Nudging ``ws`` past ``rf`` instead is
        wrong the other way: the reset would re-fire after the commit and
        refund everything spent in between.

        Must be called **before** :meth:`_admit_limit`, so the restored balance
        gates the request that crossed the boundary rather than the one after
        it. Mutates ``state`` in place and returns whether it did;
        ``_original_tokens_milli`` and ``_original_rf_ms`` must already have
        been captured, because they are the *stored* values the ``ADD`` delta
        and the ``rf`` lock are built from.
        """
        if limit.reset_after is None or not (opened or state.window_rolled):
            return False
        state.tokens_milli = state.effective_capacity_milli(now_ms)
        return True

    @staticmethod
    def _open_window_if_elapsed(limit: Limit, state: BucketState, now_ms: int) -> int | None:
        """Anchor a new duration window when the current one has elapsed (ADR-139).

        Idle-restarting, not tiling: the new window starts at ``now_ms`` — the
        first use after expiry — rather than at ``ws_old + rsa``. Anchoring to
        the old end would be a fixed grid offset by the first-ever use, which
        cannot express "go idle long enough and your window restarts", the
        thing anchoring to the entity is *for*.

        The window is half-open, ``[ws, ws + rsa)``: its end instant already
        belongs to the next window, which is also the instant ``vu`` stops
        admitting fast-path writes.

        A limit with no window, or one whose window has not elapsed, is left
        alone — which is how "exhaustion inside the current window does not
        move the anchor" is enforced: an exhausted quota is still inside its
        window, so nothing here fires and :meth:`_admit_limit` rejects against
        the balance on disk. And a pass that *does* open a window but is then
        rejected writes nothing (write-on-enter invariant 1), so the anchor
        moves only when a request is admitted and committed.

        A bucket carrying ``rsa`` but **no** ``ws`` (a shard stamped before its
        limit gained a window, e.g. by the param sync) opens its first window
        here. A client-created bucket never lacks one, since
        ``BucketState.from_limit`` stamps it at creation.

        Mutates ``state`` in place. Returns the new ``ws`` when it opened one,
        so the caller can stamp it; ``None`` otherwise.
        """
        if limit.reset_after is None or state.reset_after_seconds is None:
            return None
        end = state.window_end_ms
        if end is not None and now_ms < end:
            return None
        state.window_start_ms = now_ms
        return now_ms

    @staticmethod
    def _materialisation_stamps(
        limit: Limit, state: BucketState, now_ms: int
    ) -> tuple[int | None, int | None]:
        """``(vu, next reset edge)`` for one limit at one clock reading (#222, ADR-139).

        ``vu`` is the minimum of the futures that invalidate the materialised
        ``tk``: the next parameter change, the next reset edge, and — since
        ADR-139 — the end of the current duration window. All three are
        boundaries past which the fast path must not spend tokens minted under
        conditions no longer in force.

        The window member is what keeps the speculative condition
        byte-identical. An elapsed window makes ``vu <= now``, the pre-existing
        ``(attribute_not_exists(vu) OR vu > :now)`` term fails, the failure
        classifies as ``SCHEDULE_BOUNDARY``, and the limiter routes it to the
        slow path — the only place that re-materialises, and therefore the only
        place that anchors. No new condition term, no new expression value, no
        config read on the fast path. Read the window end off ``state``
        **after** :meth:`_open_window_if_elapsed` has run, so a pass that just
        anchored a new window stamps ``vu`` at the *new* window's end. Only the
        resolved ``limit`` decides whether a window is in force; a stale
        ``ws``/``rsa`` left on the item by a limit that no longer has one does
        not vote.

        A limit carrying a ``reset_schedule`` and **no** ``schedule`` is
        the daily-quota shape, and it must still produce a ``vu``: without one
        the speculative condition never fails, the slow path never runs, and the
        reset fires only when something unrelated forces a materialising pass.

        The reset half is returned separately because the commit needs the two
        apart. ``_commit_initial()`` takes a *second* clock reading a round trip
        later, and only a reset edge crossed in that gap can invalidate the
        reset decision taken here — a parameter boundary crossed in the same gap
        is handled by ``vu <= rf`` forcing one extra pass, which for a reset is
        not enough (the next pass would compare the edge against an ``rf`` that
        has already moved past it).

        Both halves are computed here rather than through one
        ``next_boundary(schedule, reset_schedule, ...)`` call so that neither
        cron scan runs twice.
        """
        param_ms = next_boundary(limit.schedule, now_ms=now_ms) if limit.schedule else None
        reset_ms = (
            next_boundary((), limit.reset_schedule, now_ms=now_ms) if limit.reset_schedule else None
        )
        window_ms = state.window_end_ms if limit.reset_after is not None else None
        candidates = [b for b in (param_ms, reset_ms, window_ms) if b is not None]
        return (min(candidates) if candidates else None), reset_ms

    @staticmethod
    def _admit_limit(
        entity_id: str,
        resource: str,
        limit: Limit,
        state: BucketState,
        consume: dict[str, int],
        now_ms: int,
    ) -> tuple[LimitStatus | None, int]:
        """Slow-path admission for one resolved limit (Issue #455).

        Declared (named in ``consume``): ``try_consume`` gates admission even
        at amount 0 — it fails when the bucket is in debt, so a declared
        zero-estimate limit waits for refill to clear an earlier overdraw.
        That is what declaring it means. On success the state is updated in
        place. Returns ``(status, consumed)``.

        Undeclared: refill only, so the composite write stays complete. It
        never gates admission — not even when in debt, matching the fast
        path, whose condition covers declared limits only — and never appears
        in ``RateLimitExceeded``. Returns ``(None, 0)``.
        """
        if limit.name not in consume:
            state.tokens_milli, state.last_refill_ms = force_consume(state, 0, now_ms)
            return None, 0

        amount = consume[limit.name]
        result = try_consume(state, amount, now_ms)
        status = LimitStatus(
            entity_id=entity_id,
            resource=resource,
            limit_name=limit.name,
            # The shard holds only its share, and only the window in force
            # scales it, so that is what is reported (#475, #222 §3.5);
            # identity only when the bucket is unsharded and unscheduled.
            limit=limit.per_shard(state.shard_count, now_ms),
            available=result.available,
            requested=amount,
            exceeded=not result.success,
            retry_after_seconds=result.retry_after_seconds,
        )
        if not result.success:
            return status, 0

        state.tokens_milli = result.new_tokens_milli
        state.last_refill_ms = result.new_last_refill_ms
        # Update consumption counter if initialized (issue #179)
        if state.total_consumed_milli is not None and amount > 0:
            state.total_consumed_milli += amount * 1000
        return status, amount

    @staticmethod
    def _wcu_carrier(
        entity_id: str,
        resource: str,
        buckets: dict[tuple[str, str, str], BucketState],
        now_ms: int,
        shard_id: int,
        shard_count: int,
        has_custom_config: bool,
    ) -> LeaseEntry | None:
        """Carry an existing ``wcu`` bucket through the lease, refill only.

        The fast path never refills ``wcu`` and without the aggregator nothing
        else does, so an idle shard would stay "exhausted" and drive doubling
        (ADR-133). Treated exactly like an undeclared limit: refilled from its
        stored ra/rp, written back under the shared rf lock, never gated,
        never a LimitStatus, never visible through the lease.
        """
        state = buckets.get((entity_id, resource, WCU_LIMIT_NAME))
        if state is None:
            return None
        original_tk, original_rf = state.tokens_milli, state.last_refill_ms
        state.tokens_milli, state.last_refill_ms = force_consume(state, 0, now_ms)
        return LeaseEntry(
            entity_id=entity_id,
            resource=resource,
            limit=Limit._carrier(state),
            state=state,
            consumed=0,
            _original_tokens_milli=original_tk,
            _original_rf_ms=original_rf,
            _has_custom_config=has_custom_config,
            _declared=False,
            _shard_id=shard_id,
            _shard_count=shard_count,
        )

    async def _try_parent_only_acquire(
        self,
        parent_id: str,
        resource: str,
        consume: dict[str, int],
        child_entries: list[LeaseEntry],
        parent_shard: int,
        parent_shard_count: int,
    ) -> Lease | None:
        """Attempt parent-only slow path after child speculative succeeded.

        Reads parent buckets, resolves limits, does refill + try_consume,
        and writes parent via single-item UpdateItem. Returns a Lease combining
        child's speculative entries with parent's slow-path entries.

        Args:
            parent_shard: The parent shard the speculative write hit — the
                one whose ALL_OLD image the "refill would help" decision was
                made on. Reused verbatim rather than drawn again (GHSA-76rv).
            parent_shard_count: shard_count observed on that image.

        Returns None if parent acquire fails (caller should compensate child).
        """
        now_ms = self._repository._now_ms()

        # Resolve parent limits
        parent_limits, parent_config_source = await self._resolve_limits(parent_id, resource, None)
        # No unknown-key check here: the declaration in `consume` is about the
        # child. A parent tracking a subset of the child's limits is a valid
        # configuration; keys with no parent limit are simply not applied.

        parent_buckets = await self._fetch_buckets([parent_id], resource, parent_shard)

        # Process parent buckets: refill + try_consume
        parent_entries: list[LeaseEntry] = []
        statuses: list[LimitStatus] = []
        has_custom_config = _is_custom_config(parent_config_source)

        for limit in parent_limits:
            bucket_key = (parent_id, resource, limit.name)
            existing = parent_buckets.get(bucket_key)
            if existing is None:
                # Parent bucket missing for this limit — can't proceed
                return None

            # See `_do_acquire`: the resolved config, not the item, is what
            # makes the parent's refill and ceiling schedule-aware here.
            existing.sched = limit.schedule
            # And the reset schedule alongside it, for the same reason and from
            # the same source: `try_consume` reads it to decide whether a reset
            # edge beats the drip in a rejection's `retry_after_seconds`
            # (#222 §7). Attaching one without the other would leave a quota's
            # rejection quoting a drip that ADR-137 says does not exist.
            existing.reset_sched = limit.reset_schedule
            # And the duration window's length (ADR-139), from config for the
            # same reason: a resource- or system-level `reset_after` never
            # fans out, so an item created before it was configured carries no
            # `rsa`, and without this its window would never open.
            existing.reset_after_seconds = limit.reset_after_seconds

            original_tk = existing.tokens_milli
            original_rf = existing.last_refill_ms

            # The second, easily-missed reset seam. A cascading child whose own
            # bucket is fine but whose parent crossed a reset edge would be
            # rejected on the parent without this. Every bucket here exists by
            # construction — the method returns None above when one is missing
            # — so there is no `is_new` case to guard (#222 §3.6).
            #
            # A duration window is the same seam (ADR-139). The parent anchors
            # its own window, independently of the child's: this reads the
            # parent's `ws` off the parent's item and nothing else.
            parent_new_ws = self._open_window_if_elapsed(limit, existing, now_ms)
            self._apply_reset_edge(limit, existing, now_ms)
            self._apply_window_roll(limit, existing, now_ms, opened=parent_new_ws is not None)

            status, consumed = self._admit_limit(
                parent_id, resource, limit, existing, consume, now_ms
            )
            if status is not None:
                statuses.append(status)

            # The parent schedules independently of the child: this is a
            # different item, so it gets its own `vu` from its own limits
            # (#222 §2.2).
            parent_boundary_ms, parent_reset_edge_ms = self._materialisation_stamps(
                limit, existing, now_ms
            )

            # Every resolved limit gets an entry so _commit_initial() persists
            # refill for all of them; only the declared ones are adjustable
            # through the lease (Issue #455).
            parent_entries.append(
                LeaseEntry(
                    entity_id=parent_id,
                    resource=resource,
                    limit=limit,
                    state=existing,
                    consumed=consumed,
                    _original_tokens_milli=original_tk,
                    _original_rf_ms=original_rf,
                    _has_custom_config=has_custom_config,
                    _declared=status is not None,
                    _shard_id=parent_shard,
                    _shard_count=parent_shard_count,
                    _boundary_ms=parent_boundary_ms,
                    _reset_edge_ms=parent_reset_edge_ms,
                    _window_start_ms=parent_new_ws,
                    _window_end_ms=_window_end_in_force(limit, existing),
                )
            )

        carrier = self._wcu_carrier(
            parent_id,
            resource,
            parent_buckets,
            now_ms,
            parent_shard,
            parent_shard_count,
            has_custom_config,
        )
        parent_carriers = [carrier] if carrier is not None else []

        # Check for violations
        violations = [s for s in statuses if s.exceeded]
        if violations:
            return None

        # Write parent only via _commit_initial on a parent-only lease
        all_entries = list(child_entries) + parent_entries
        lease = Lease(
            repository=self._repository,
            entries=all_entries,
        )
        # Mark child entries as already committed (speculative write succeeded)
        for entry in child_entries:
            entry._initial_consumed = entry.consumed
        # Commit only parent entries
        parent_lease = Lease(
            repository=self._repository,
            entries=parent_entries,
            _carriers=parent_carriers,
        )
        try:
            await parent_lease._commit_initial()
        except RateLimitExceeded:
            return None

        # Mark the full lease as committed
        lease._initial_committed = True
        for entry in parent_entries:
            entry._initial_consumed = entry.consumed
        return lease

    async def _do_acquire(
        self,
        entity_id: str,
        resource: str,
        limits_override: list[Limit] | None,
        consume: dict[str, int],
        shard_id: int | None = None,
        shard_count: int | None = None,
        parent_shard_id: int | None = None,
    ) -> Lease:
        """Internal acquire implementation (the slow path).

        Args:
            shard_id: Child shard the speculative fast path selected, so this
                path reads and — if missing — creates that same shard
                (issue #439). None draws one from the cached shard_count.
            shard_count: The shard_count the fast path observed on its failure
                image, used to size and stamp a shard created here. None
                falls back to the entity cache.
            parent_shard_id: Parent shard the fast path found missing, so it
                is created where the fast path looked rather than re-drawn.
        """
        # Validate inputs at API boundary
        validate_identifier(entity_id, "entity_id")
        validate_resource(resource)

        now_ms = self._repository._now_ms()

        # The shard is part of a bucket's identity (GHSA-76rv). Resolve it
        # once here and carry it through the read, the LeaseEntry and the
        # write, so the slow path never silently collapses back onto shard 0.
        child_shard, child_shard_count = self._repository.select_shard(
            entity_id, resource, shard_id, shard_count
        )
        entity_shards: dict[str, tuple[int, int]] = {entity_id: (child_shard, child_shard_count)}

        # Phase 1: Resolve child limits, then fetch child META + child buckets
        # in a single BatchGetItem call (no separate get_entity round trip).
        # The disable walk's levels are a subset of the config levels, so let
        # the config fetch hand back what it actually read (ADR-125).
        fetched_disabled: dict[tuple[str, str], bool | None] = {}
        child_limits, child_config_source = await self._resolve_limits(
            entity_id, resource, limits_override, fetched_disabled
        )

        # Slow path gate (ADR-125). Covers first acquire — no bucket exists yet,
        # so the fast-path guard cannot fire — and every fallback path.
        #
        # Reuse the config fetch only when it genuinely read every level of the
        # walk; otherwise those levels came from the config cache and must not
        # answer this gate. See Repository.resolve_disabled_from_fetched.
        resolved = self._repository.resolve_disabled_from_fetched(
            entity_id, resource, fetched_disabled
        )
        if resolved is None:
            resolved = await self._repository.resolve_disabled(entity_id, resource)
        disabled, level = resolved
        if disabled:
            raise ResourceDisabled(
                entity_id=entity_id, resource=resource, level=level or "resource"
            )

        entity, child_buckets = await self._fetch_entity_and_buckets(
            entity_id, resource, child_shard
        )

        # Determine cascade
        entity_ids = [entity_id]
        existing_buckets: dict[tuple[str, str, str], BucketState] = dict(child_buckets)
        entity_limits: dict[str, list[Limit]] = {entity_id: child_limits}
        # Track config source per entity (for TTL calculation, issue #271)
        entity_config_sources: dict[str, str] = {entity_id: child_config_source}

        if entity and entity.cascade and entity.parent_id:
            parent_id = entity.parent_id
            entity_ids.append(parent_id)

            # Slow path gate for the parent (ADR-125), same reasoning as the
            # child gate above: no parent bucket should be fetched or created
            # once the parent is disabled.
            parent_disabled, parent_level = await self._repository.resolve_disabled(
                parent_id, resource
            )
            if parent_disabled:
                raise ResourceDisabled(
                    entity_id=parent_id, resource=resource, level=parent_level or "resource"
                )

            # Phase 2: Resolve parent limits + fetch parent buckets
            parent_limits, parent_config_source = await self._resolve_limits(
                parent_id, resource, limits_override
            )
            entity_limits[parent_id] = parent_limits
            entity_config_sources[parent_id] = parent_config_source
            # The parent shards independently of the child (GHSA-76rv)
            entity_shards[parent_id] = self._repository.select_shard(
                parent_id, resource, parent_shard_id
            )
            parent_buckets = await self._fetch_buckets(
                [parent_id], resource, entity_shards[parent_id][0]
            )
            existing_buckets.update(parent_buckets)

        # Unknown-key check (Issue #455) against every limit this acquire can
        # gate: the child's, plus the parent's when cascading. Entity config
        # replaces rather than merges, so a child pinned to [rpm] under a
        # parent on [rpm, tpm] still has tpm gated and consumed on the parent
        # — a key known to either side of the cascade is not unknown.
        known_limits = [limit for eid in entity_ids for limit in entity_limits[eid]]
        # helper -> here -> acquire -> __aenter__ -> caller
        unknown_keys = self._warn_unknown_limits(
            consume,
            known_limits,
            resource,
            config_source=child_config_source,
            stacklevel=5,
        )

        # Process buckets and build lease entries
        entries: list[LeaseEntry] = []
        carriers: list[LeaseEntry] = []
        statuses: list[LimitStatus] = []

        for eid in entity_ids:
            eid_shard, eid_shard_count = entity_shards[eid]
            # Track whether any bucket existed for this entity+resource
            any_existing = any(
                (eid, resource, limit.name) in existing_buckets for limit in entity_limits[eid]
            )

            # A quota has no rate for a freshly minted share to amortise
            # against (ADR-137), so a shard added to a set that already exists
            # is filled by transfer from its siblings rather than by a mint
            # (#587). Costs 1 GSI3 KEYS_ONLY query + 1 BatchGetItem, paid only
            # here — once per shard creation, for a resource that actually
            # carries a quota, and never on the speculative fast path.
            quota_transfer = await self._quota_transfer(
                eid, resource, entity_limits[eid], eid_shard_count, any_existing, now_ms
            )

            for limit in entity_limits[eid]:
                # Get existing bucket from batch result or create new one
                bucket_key = (eid, resource, limit.name)
                existing = existing_buckets.get(bucket_key)
                if existing is None:
                    is_new = True
                    # A new shard of a sharded *dripping* bucket starts at its
                    # effective per-shard share — capacity_milli //
                    # shard_count, exactly like the aggregator's
                    # propagate_shard_count Path 2. The stored ra is
                    # undivided, so the ceilings across all shards still sum
                    # to the configured capacity and the entity's long-run
                    # admission rate is unchanged by the doubling (issue
                    # #439). Stored cp/ra stay undivided.
                    #
                    # A quota is the exception: it never drips, so a fresh
                    # share would be net-new allowance nothing reclaims before
                    # the next reset edge (#587). It is handed the surplus just
                    # clamped off its siblings instead, which conserves the
                    # entity-wide spendable total exactly.
                    state = BucketState.from_limit(
                        eid,
                        resource,
                        limit,
                        now_ms,
                        shard_count=eid_shard_count,
                        reclaimed_milli=quota_transfer.get(limit.name),
                    )
                else:
                    is_new = False
                    state = existing
                    # A bucket item read back carries no schedule of its own
                    # yet (`_deserialize_composite_bucket` reads the base
                    # params only), and the config this acquire just resolved
                    # is the fresher of the two anyway — an item stamped
                    # before the last `set_limits` would still hold the old
                    # one. Attaching it here is what makes `effective_params`
                    # apply on the slow path at all; without it the refill and
                    # the ceiling below come out at the base rate while `vu`
                    # claims the window was honoured.
                    state.sched = limit.schedule
                    # The reset schedule travels with it. `try_consume` reads
                    # it for the rejection estimate (#222 §7), so a bucket that
                    # carried `sched` alone would report a quota's wait as the
                    # drip ADR-137 gives it — which is none.
                    state.reset_sched = limit.reset_schedule
                    # The duration window's length travels with them (ADR-139).
                    # Config is the fresher source, and it is the *only* source
                    # for an item created before a resource- or system-level
                    # `reset_after` was configured: those levels never fan out
                    # (#271/#296), so the item carries no `rsa` and its window
                    # would never open. `ws` stays the item's — it is state,
                    # not config.
                    state.reset_after_seconds = limit.reset_after_seconds

                # Capture original values before try_consume modifies them (ADR-115)
                original_tk = state.tokens_milli
                original_rf = state.last_refill_ms

                # A calendar reset edge crossed since this item was last
                # refilled restores the balance *before* admission, so a
                # request arriving just after midnight is gated against the
                # restored quota rather than the burnt one (#222 §3.6). A
                # brand-new bucket starts at its full share already and has no
                # stored `rf` an edge could be compared against.
                #
                # A duration window elapsed since the last use is the same seam
                # (ADR-139): anchor the new window at this reading, then restore
                # the balance under `ws > rf`. Exhaustion inside the window
                # moves nothing — `_open_window_if_elapsed` fires only past the
                # window's end — and a pass that anchors and is then rejected
                # writes nothing at all (write-on-enter invariant 1).
                new_ws: int | None = None
                if not is_new:
                    new_ws = self._open_window_if_elapsed(limit, state, now_ms)
                    self._apply_reset_edge(limit, state, now_ms)
                    self._apply_window_roll(limit, state, now_ms, opened=new_ws is not None)

                status, consumed = self._admit_limit(eid, resource, limit, state, consume, now_ms)
                if status is not None:
                    statuses.append(status)

                # Determine if entity has custom config for TTL (Issue #271)
                has_custom_config = _is_custom_config(entity_config_sources.get(eid))

                # Same `now_ms` that drove `effective_params` for the refill
                # above and the reset decision before it, deliberately not the
                # later reading `_commit_initial()` takes: if a boundary falls
                # in between, this `vu` lands at or before the item's `rf` and
                # the next acquire re-materialises, whereas a boundary computed
                # at commit time would point past the window just entered and
                # leave the fast path spending pre-boundary tokens for a whole
                # window.
                boundary_ms, reset_edge_ms = self._materialisation_stamps(limit, state, now_ms)

                # Every resolved limit gets an entry: _commit_initial() needs
                # them all to create the composite bucket and to credit refill
                # when it advances the shared `rf`. Only limits the caller
                # named in `consume` are declared, i.e. visible and adjustable
                # through the lease — the same rule the fast path applies
                # when it filters result.buckets (Issue #455).
                entries.append(
                    LeaseEntry(
                        entity_id=eid,
                        resource=resource,
                        limit=limit,
                        state=state,
                        consumed=consumed,
                        _original_tokens_milli=original_tk,
                        _original_rf_ms=original_rf,
                        _is_new=is_new and not any_existing,
                        _has_custom_config=has_custom_config,
                        _shard_id=eid_shard,
                        _shard_count=eid_shard_count,
                        _cascade=entity.cascade if entity and eid == entity_id else False,
                        _parent_id=entity.parent_id if entity and eid == entity_id else None,
                        _declared=status is not None,
                        _boundary_ms=boundary_ms,
                        _reset_edge_ms=reset_edge_ms,
                        _window_start_ms=new_ws,
                        _window_end_ms=_window_end_in_force(limit, state),
                    )
                )

            carrier = self._wcu_carrier(
                eid,
                resource,
                existing_buckets,
                now_ms,
                eid_shard,
                eid_shard_count,
                _is_custom_config(entity_config_sources.get(eid)),
            )
            if carrier is not None:
                carriers.append(carrier)

        # Check for any violations
        violations = [s for s in statuses if s.exceeded]
        if violations:
            raise RateLimitExceeded(statuses)

        return Lease(
            repository=self._repository,
            entries=entries,
            _carriers=carriers,
            _unknown_keys=unknown_keys,
        )

    async def _fetch_entity_and_buckets(
        self,
        entity_id: str,
        resource: str,
        shard_id: int,
    ) -> tuple[Entity | None, dict[tuple[str, str, str], BucketState]]:
        """
        Fetch entity metadata and its composite bucket in a single call.

        With composite items (ADR-114), one item per (entity_id, resource,
        shard) contains all limits. Uses batch_get_entity_and_buckets if the
        backend supports batch operations, otherwise falls back to separate
        calls. The shard is the one the acquire selected (issue #439).
        """
        if self._repository.capabilities.supports_batch_operations:
            # Composite key: one item per (entity_id, resource, shard)
            bucket_keys = [(entity_id, resource, shard_id)]
            result: tuple[
                Entity | None, dict[tuple[str, str, str], BucketState]
            ] = await self._repository.batch_get_entity_and_buckets(entity_id, bucket_keys)
            return result

        # Fallback: sequential calls
        entity = await self._repository.get_entity(entity_id)
        buckets = await self._repository.get_buckets(entity_id, resource, shard_id)
        bucket_dict: dict[tuple[str, str, str], BucketState] = {
            (b.entity_id, b.resource, b.limit_name): b for b in buckets
        }
        return entity, bucket_dict

    async def _quota_transfer(
        self,
        entity_id: str,
        resource: str,
        limits: list[Limit],
        shard_count: int,
        any_existing: bool,
        now_ms: int,
    ) -> dict[str, int]:
        """Reclaim the surplus a new quota shard is to be created from (#587).

        A quota has no drip for a freshly minted ``capacity // shard_count`` to
        amortise against (ADR-137), so a shard added mid-period must be filled
        by **transfer**: ``Repository.reclaim_quota_surplus`` clamps the shards
        that already exist to the ceiling the doubling just shrank them to, and
        what it takes is what this shard is created with. See
        :func:`~zae_limiter.models.new_shard_starting_tokens_milli` for why that
        conserves and why zero-filling and blind redistribution do not.

        Returns ``{}`` — costing nothing, and leaving every limit on the full
        share — in each case that cannot need it:

        * the entity already has a bucket item on the shard being acquired, so
          nothing is being created;
        * ``shard_count`` is 1, so there is no sibling to transfer from and the
          only shard rightly starts full;
        * no resolved limit is a quota, which is the whole dripping path; or
        * no shard exists for this (entity, resource) at all, so nothing has
          been spent and each shard is entitled to its full share.

        Args:
            entity_id: Entity whose shard is about to be created.
            resource: Resource the acquire is for.
            limits: Limits resolved for this entity and resource.
            shard_count: Shards this bucket is split across.
            any_existing: Whether a bucket item already exists on the shard
                being acquired.
            now_ms: The acquire's single clock reading (#430), so the ceiling
                clamped to is the one in force at the same instant the new
                shard's own share is computed from.

        Returns:
            ``{limit_name: reclaimed_milli}`` for the quota limits only. A name
            absent from the mapping keeps the full share.
        """
        if any_existing or shard_count <= 1:
            return {}
        shares_milli = {
            limit.name: effective_params(
                limit.capacity * 1000,
                limit.refill_amount * 1000,
                limit.refill_period_seconds * 1000,
                limit.schedule,
                now_ms,
            )[0]
            // shard_count
            for limit in limits
            if limit.is_quota
        }
        if not shares_milli:
            return {}
        shards_found, reclaimed = await self._repository.reclaim_quota_surplus(
            entity_id, resource, shares_milli
        )
        return reclaimed if shards_found else {}

    async def _fetch_buckets(
        self,
        entity_ids: list[str],
        resource: str,
        shard_id: int,
    ) -> dict[tuple[str, str, str], BucketState]:
        """
        Fetch composite buckets for entity/resource pairs on one shard.

        With composite items (ADR-114), each (entity_id, resource, shard) is
        one DynamoDB item containing all limits. Uses batch_get_buckets if the
        backend supports it, otherwise falls back to sequential calls.

        Args:
            entity_ids: List of entity IDs to fetch buckets for
            resource: Resource name
            shard_id: Shard to read for every entity (issue #439)

        Returns:
            Dict mapping (entity_id, resource, limit_name) to BucketState.
            Missing buckets are not included in the result.
        """
        # Use batch operation if backend supports it (issue #133)
        if self._repository.capabilities.supports_batch_operations:
            # Composite key: one item per (entity_id, resource, shard)
            bucket_keys: list[tuple[str, str, int]] = [
                (eid, resource, shard_id) for eid in entity_ids
            ]
            batch_result: dict[
                tuple[str, str, str], BucketState
            ] = await self._repository.batch_get_buckets(bucket_keys)
            return batch_result

        # Fallback: sequential get_buckets calls
        result: dict[tuple[str, str, str], BucketState] = {}
        for eid in entity_ids:
            buckets = await self._repository.get_buckets(eid, resource, shard_id)
            for bucket in buckets:
                key = (bucket.entity_id, bucket.resource, bucket.limit_name)
                result[key] = bucket
        return result

    async def _resolve_limits(
        self,
        entity_id: str,
        resource: str,
        limits_override: list[Limit] | None,
        disabled_out: dict[tuple[str, str], bool | None] | None = None,
    ) -> tuple[list[Limit], ConfigSource | Literal["override"]]:
        """
        Resolve limits using four-tier hierarchy.

        Delegates to repository.resolve_limits() for config resolution (ADR-122).

        Hierarchy: Entity > Entity Default > Resource > System > Override.

        Args:
            entity_id: Entity to resolve limits for
            resource: Resource being accessed
            limits_override: Optional override limits (from limits parameter)

        Returns:
            Tuple of (limits, config_source) where config_source is one of:
            - "entity": Entity-level config for specific resource
            - "entity_default": Entity-level _default_ config
            - "resource": Resource-level defaults
            - "system": System-level defaults
            - "override": Override parameter provided

        Raises:
            ValidationError: If no limits found at any level and no override provided
        """
        # Try override parameter first (skip repository call)
        if limits_override is not None:
            return limits_override, "override"

        # Delegate to repository (ADR-122)
        limits, _, config_source = await self._repository.resolve_limits(
            entity_id,
            resource,
            disabled_out,
        )

        if limits is not None and config_source is not None:
            return limits, config_source

        # No limits found anywhere
        raise ValidationError(
            field="limits",
            value=f"entity={entity_id}, resource={resource}",
            reason=(
                f"No limits configured for entity '{entity_id}' and resource '{resource}'. "
                "Configure limits at entity (resource-specific or _default_), resource, "
                "or system level, or provide limits parameter."
            ),
        )

    async def _resolve_on_unavailable(
        self,
        on_unavailable_param: OnUnavailable | None,
    ) -> OnUnavailable:
        """
        Resolve on_unavailable behavior: Parameter > System Config (cached).

        Delegates system config lookup to repository.resolve_on_unavailable() (#333).

        Args:
            on_unavailable_param: Optional per-call override

        Returns:
            Resolved OnUnavailable enum value
        """
        if on_unavailable_param is not None:
            return on_unavailable_param

        on_unavailable_action = await self._repository.resolve_on_unavailable()
        return OnUnavailable(on_unavailable_action)

    @staticmethod
    def _readable_balance(bucket: BucketState, limit: Limit | None, now_ms: int) -> int:
        """One shard's balance as a *reader* should see it (#222 §3.6, §7).

        `check_availability` writes nothing, so a bucket that crossed a reset
        edge and has not been touched by a request since still holds the burnt
        balance on disk — the slow path applies the edge at admission time
        (`_apply_reset_edge`), and nothing has admitted yet. Reporting the disk
        value makes the display read "0 remaining, resets at midnight
        tomorrow" while the very next `acquire()` restores the quota
        immediately.

        Computed per **shard**, not per limit name: a sharded entity can have
        some shards past the edge and some not, and collapsing that to "the
        limit is pending" would report the whole entity restored on the
        strength of one stale shard.

        The reset schedule comes from the resolved config rather than from
        `bucket.reset_sched`, because the `rf` it is compared against lives on
        the item — the same pairing `_apply_reset_edge` uses on the slow path,
        so the two agree by construction. Costs one backwards cron scan per
        shard, and only for a limit that actually carries a reset.
        """
        if limit is not None and limit.reset_schedule:
            edge = prev_reset_edge(limit.reset_schedule, now_ms)
            if edge is not None and edge > bucket.last_refill_ms:
                return bucket.effective_capacity_milli(now_ms) // 1000
        return calculate_available(bucket, now_ms)

    async def check_availability(
        self,
        entity_id: str,
        resource: str,
        needed: dict[str, int] | None = None,
        limits: list[Limit] | None = None,
    ) -> Availability:
        """
        Check available capacity and wait time in a single read (issue #472).

        Answers both "how much is left?" and "how long until I can proceed?"
        for every resolved limit, from one config resolution and one bucket
        read taken at one instant.

        **Why one call rather than two.** Calling :meth:`available` and
        :meth:`time_until_available` back to back is two reads at two instants,
        and they can disagree: tokens refill in between, and each discovers the
        entity's shards separately. A display built from the pair can render
        "0 remaining, available now" or "47 remaining, wait 0s". Both are lies
        a user acts on. Everything on the returned :class:`Availability` is
        derived from a single snapshot stamped with ``checked_at_ms``, so the
        numbers cannot contradict each other.

        Limits are resolved using four-tier hierarchy: Entity > Entity Default >
        Resource > System. If no stored limits found, falls back to the `limits`
        parameter.

        Consumes nothing and writes nothing. This is a read; it is not a
        pre-flight check for :meth:`acquire`. Deciding with it and then calling
        ``acquire()`` is TOCTOU and costs an extra read — ``acquire()`` already
        answers "may I proceed, and if not when" in 1 WCU, or 0 RCU + 0 WCU on
        a fast rejection, via ``RateLimitExceeded.retry_after_seconds``. Use
        this when the answer is *displayed* rather than acted on.

        **Sharding.** A sharded entity's balance is spread across its shards
        (GHSA-76rv), so this sums every shard, exactly as :meth:`available`
        and ``get_resource_capacity()`` do, and computes the wait against the
        summed refill rate. The reported ``limit`` is therefore the undivided
        configured limit, not one shard's share — unlike the per-shard
        statuses in ``RateLimitExceeded`` (#475), which describe the one shard
        a rejection happened on. Known limitation, inherited from #475: a
        *single* request larger than ``capacity // shard_count`` is
        unadmittable on every shard even while the entity is under its
        configured limit, so ``acquire()`` can reject an amount this call
        reports as available.

        **Schedules.** Everything reported is the value in force at
        ``checked_at_ms``, not the stored base (#222 §7): the ceiling comes
        from ``effective_params``, so a ``scale: 0.5`` window reports 500 and
        not 1000, and the wait walks forward across boundaries instead of
        dividing by the rate that happens to apply right now. A limit with a
        ``reset_schedule`` reports the wait to its next edge — for a daily
        quota, whose ``refill_amount`` is 0 by ADR-137, that is the only finite
        answer there is. A bucket that crossed a reset edge and has not been
        written to since reports the balance the next ``acquire()`` will
        restore, per shard, rather than the burnt one still on disk.

        Cost: 1 GSI3 query (KEYS_ONLY) + 1 ``BatchGetItem``, plus config
        resolution (free on a cache hit), regardless of limit count or shard
        count. A limit carrying a ``reset_schedule`` adds one backwards cron
        scan per shard and the forward walk adds up to eight per exceeded
        limit; an unscheduled limit adds neither.

        Args:
            entity_id: Entity to check
            resource: Resource to check
            needed: Required amounts by limit name. Omit to ask only about
                current availability (`retry_after_seconds` is then 0.0);
                pass e.g. ``{"rpm": 1}`` for "when may I make one more
                request". Keys naming no resolved limit are ignored.
            limits: Override limits (optional, falls back to stored config)

        Returns:
            Availability carrying one :class:`LimitStatus` per resolved limit,
            plus the derived aggregate verdict and countdown.

        Raises:
            ValidationError: If no limits found at any level and no override provided

        Example:
            ```python
            check = await limiter.check_availability(
                entity_id="key-abc",
                resource="gpt-4",
                needed={"rpm": 1, "tpm": 500},
            )
            for status in check.statuses:
                render(status.limit_name, status.available, status.retry_after_seconds)
            ```
        """
        await self._ensure_initialized()
        now_ms = self._repository._now_ms()

        needed = needed or {}

        # Resolve limits using four-tier hierarchy
        resolved_limits, _ = await self._resolve_limits(entity_id, resource, limits)

        resolved_by_name = {limit.name: limit for limit in resolved_limits}

        # One GSI3 pass discovers every shard of every resource for this
        # entity (GHSA-76rv); shard 0 alone holds at most capacity // N.
        totals: dict[str, int] = {}
        for bucket in await self._repository.get_buckets(entity_id):
            if bucket.resource != resource:
                continue
            name = bucket.limit_name
            totals[name] = totals.get(name, 0) + self._readable_balance(
                bucket, resolved_by_name.get(name), now_ms
            )

        statuses: list[LimitStatus] = []
        for limit in resolved_limits:
            # The ceiling in force *now*, not the base: inside a `scale: 0.5`
            # window `limit.capacity` is twice what any acquire would admit,
            # and both the clamp and the no-bucket branch below reported it.
            # These two sites work from the config-resolved `Limit`, so no
            # amount of `BucketState` conversion reaches them (#222 §7).
            eff_cp, _eff_ra, _eff_rp = effective_params(
                limit.capacity * 1000,
                limit.refill_amount * 1000,
                limit.refill_period_seconds * 1000,
                limit.schedule,
                now_ms,
            )
            ceiling = max(1, eff_cp // 1000)
            if limit.name in totals:
                available = min(totals[limit.name], ceiling)
            else:
                # No bucket yet: the first acquire creates it at the capacity
                # in force now, not at the base.
                available = ceiling
            requested = needed.get(limit.name, 0)
            exceeded = requested > 0 and available < requested

            wait = 0.0
            if exceeded:
                # Derive the wait from the same `available` that is reported,
                # so the two numbers on screen can never disagree.
                #
                # Shards are summed above, so the walk is handed the undivided
                # base with the default `shard_count=1` — the sum of the shares
                # *is* the undivided rate, modulo flooring, which is also why
                # #475's floored-share fallback is not needed here.
                wait = retry_after_with_schedule(
                    deficit_milli=(requested - available) * 1000,
                    cp_milli=limit.capacity * 1000,
                    ra_milli=limit.refill_amount * 1000,
                    rp_ms=limit.refill_period_seconds * 1000,
                    sched=limit.schedule,
                    reset_sched=limit.reset_schedule,
                    now_ms=now_ms,
                )

            statuses.append(
                LimitStatus(
                    entity_id=entity_id,
                    resource=resource,
                    limit_name=limit.name,
                    limit=limit,
                    available=available,
                    requested=requested,
                    exceeded=exceeded,
                    retry_after_seconds=wait,
                )
            )

        return Availability(
            entity_id=entity_id,
            resource=resource,
            checked_at_ms=now_ms,
            statuses=statuses,
        )

    async def available(
        self,
        entity_id: str,
        resource: str,
        limits: list[Limit] | None = None,
        use_stored_limits: bool = False,
    ) -> dict[str, int]:
        """
        Check available capacity without consuming.

        Limits are resolved using four-tier hierarchy: Entity > Entity Default > Resource > System.
        If no stored limits found, falls back to the `limits` parameter.

        Sums every shard of the entity (GHSA-76rv). Can return negative values
        if the bucket is in debt.

        Thin wrapper over :meth:`check_availability`, which returns this and
        the wait time together from one read at one instant. Prefer it when
        you want both — asking here and there is two snapshots that can
        disagree.

        Args:
            entity_id: Entity to check
            resource: Resource to check
            limits: Override limits (optional, falls back to stored config)
            use_stored_limits: DEPRECATED - limits are now always resolved from
                stored config. This parameter will be removed in v1.0.

        Returns:
            Dict mapping limit_name -> available tokens

        Raises:
            ValidationError: If no limits found at any level and no override provided
        """
        # Deprecation warning for use_stored_limits
        if use_stored_limits:
            warnings.warn(
                "use_stored_limits is deprecated and will be removed in v1.0. "
                "Limits are now always resolved from stored config (Entity > Resource > System). "
                "Pass limits parameter as override if needed.",
                DeprecationWarning,
                stacklevel=2,
            )

        check = await self.check_availability(entity_id, resource, None, limits)
        return check.available

    async def time_until_available(
        self,
        entity_id: str,
        resource: str,
        needed: dict[str, int],
        limits: list[Limit] | None = None,
        use_stored_limits: bool = False,
    ) -> float:
        """
        Calculate seconds until requested capacity is available.

        Limits are resolved using four-tier hierarchy: Entity > Entity Default > Resource > System.
        If no stored limits found, falls back to the `limits` parameter.

        The estimate is computed against the entity's total across every shard
        and the summed refill rate (GHSA-76rv), matching :meth:`available`.

        Thin wrapper over :meth:`check_availability`, which returns this and
        the available capacity together from one read at one instant. Prefer
        it when you want both, or when you want the wait per limit rather than
        the slowest one.

        Args:
            entity_id: Entity to check
            resource: Resource to check
            needed: Required amounts by limit name
            limits: Override limits (optional, falls back to stored config)
            use_stored_limits: DEPRECATED - limits are now always resolved from
                stored config. This parameter will be removed in v1.0.

        Returns:
            Seconds until available (0.0 if already available)

        Raises:
            ValidationError: If no limits found at any level and no override provided
        """
        # Deprecation warning for use_stored_limits
        if use_stored_limits:
            warnings.warn(
                "use_stored_limits is deprecated and will be removed in v1.0. "
                "Limits are now always resolved from stored config (Entity > Resource > System). "
                "Pass limits parameter as override if needed.",
                DeprecationWarning,
                stacklevel=2,
            )

        check = await self.check_availability(entity_id, resource, needed, limits)
        return check.retry_after_seconds

    # -------------------------------------------------------------------------
    # Stored limits management
    # -------------------------------------------------------------------------

    async def set_limits(
        self,
        entity_id: str,
        limits: list[Limit],
        resource: str = DEFAULT_RESOURCE,
        principal: str | None = None,
    ) -> None:
        """
        Store limit configs for an entity.

        Args:
            entity_id: Entity to set limits for
            limits: Limits to store
            resource: Resource these limits apply to (or _default_)
            principal: Caller identity for audit logging (optional)
        """
        await self._ensure_initialized()
        await self._repository.set_limits(entity_id, limits, resource, principal=principal)

    async def get_limits(
        self,
        entity_id: str,
        resource: str = DEFAULT_RESOURCE,
    ) -> list[Limit]:
        """
        Get stored limit configs for an entity.

        Args:
            entity_id: Entity to get limits for
            resource: Resource to get limits for

        Returns:
            List of stored limits (empty if none)
        """
        await self._ensure_initialized()
        return await self._repository.get_limits(entity_id, resource)

    async def delete_limits(
        self,
        entity_id: str,
        resource: str = DEFAULT_RESOURCE,
        principal: str | None = None,
    ) -> None:
        """
        Delete stored limit configs for an entity.

        Reconciles existing buckets to fallback config (resource/system
        defaults) by syncing limit fields, setting TTL, and removing
        stale limit attributes (issue #327).

        Args:
            entity_id: Entity to delete limits for
            resource: Resource to delete limits for
            principal: Caller identity for audit logging (optional)
        """
        await self._ensure_initialized()

        # Capture old entity limits before deletion (for stale detection)
        old_limits = await self._repository.get_limits(entity_id, resource)

        # Delete entity config (auto-evicts config cache, ADR-122)
        await self._repository.delete_limits(entity_id, resource, principal=principal)

        # Resolve effective fallback limits (entity config gone → resource/system)
        try:
            effective_limits, _ = await self._resolve_limits(
                entity_id, resource, limits_override=None
            )
        except ValidationError:
            # No fallback config — bucket left as-is (acceptance criterion #11)
            return

        # Compute stale limit names (in old entity config but not in defaults)
        stale_names = {lim.name for lim in old_limits} - {lim.name for lim in effective_limits}

        # Reconcile bucket to effective defaults
        await self._repository.reconcile_bucket_to_defaults(
            entity_id,
            resource,
            effective_limits,
            stale_limit_names=stale_names if stale_names else None,
        )

    async def list_entities_with_custom_limits(
        self,
        resource: str,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> tuple[list[str], str | None]:
        """
        List all entities that have custom limit configurations.

        Uses GSI3 sparse index for efficient queries. Only entities with
        custom limits for the specified resource are returned.

        Args:
            resource: Resource to filter by.
            limit: Maximum number of entities to return. None for all.
            cursor: Pagination cursor from previous call.

        Returns:
            Tuple of (entity_ids, next_cursor). next_cursor is None if no more results.

        Example:
            # Get all entities with custom limits for gpt-4
            entities, cursor = await limiter.list_entities_with_custom_limits("gpt-4")
            for entity_id in entities:
                print(entity_id)

            # Paginate through results
            while cursor:
                more, cursor = await limiter.list_entities_with_custom_limits(
                    "gpt-4", cursor=cursor
                )
                entities.extend(more)
        """
        await self._ensure_initialized()
        return await self._repository.list_entities_with_custom_limits(resource, limit, cursor)

    async def list_resources_with_entity_configs(self) -> list[str]:
        """
        List all resources that have entity-level custom limit configurations.

        Uses the entity config resources registry for efficient O(1) lookup.

        Returns:
            Sorted list of resource names with at least one entity having custom limits

        Example:
            resources = await limiter.list_resources_with_entity_configs()
            for resource in resources:
                entities, _ = await limiter.list_entities_with_custom_limits(resource)
                print(f"{resource}: {len(entities)} entities with custom limits")
        """
        await self._ensure_initialized()
        return await self._repository.list_resources_with_entity_configs()

    # -------------------------------------------------------------------------
    # Resource-level defaults management
    # -------------------------------------------------------------------------

    async def set_resource_defaults(
        self,
        resource: str,
        limits: list[Limit],
        principal: str | None = None,
    ) -> None:
        """
        Store default limit configs for a resource.

        Resource defaults override system defaults for the specified resource.

        Args:
            resource: Resource name
            limits: Limits to store
            principal: Caller identity for audit logging (optional)
        """
        await self._ensure_initialized()
        await self._repository.set_resource_defaults(resource, limits, principal=principal)

    async def get_resource_defaults(
        self,
        resource: str,
    ) -> list[Limit]:
        """
        Get stored default limit configs for a resource.

        Args:
            resource: Resource name

        Returns:
            List of stored limits (empty if none)
        """
        await self._ensure_initialized()
        return await self._repository.get_resource_defaults(resource)

    async def delete_resource_defaults(
        self,
        resource: str,
        principal: str | None = None,
    ) -> None:
        """
        Delete stored default limit configs for a resource.

        Args:
            resource: Resource name
            principal: Caller identity for audit logging (optional)
        """
        await self._ensure_initialized()
        await self._repository.delete_resource_defaults(resource, principal=principal)

    async def list_resources_with_defaults(self) -> list[str]:
        """List all resources that have default limit configs."""
        await self._ensure_initialized()
        return await self._repository.list_resources_with_defaults()

    # -------------------------------------------------------------------------
    # System-level defaults management
    # -------------------------------------------------------------------------

    async def set_system_defaults(
        self,
        limits: list[Limit],
        on_unavailable: OnUnavailable | None = None,
        principal: str | None = None,
    ) -> None:
        """
        Store system-wide default limits and config.

        System defaults apply to ALL resources unless overridden at resource
        or entity level.

        Args:
            limits: Limits to store (apply globally to all resources)
            on_unavailable: Behavior when DynamoDB unavailable (optional)
            principal: Caller identity for audit logging (optional)
        """
        await self._ensure_initialized()
        on_unavailable_action: OnUnavailableAction | None = (
            on_unavailable.value if on_unavailable else None
        )
        await self._repository.set_system_defaults(
            limits, on_unavailable=on_unavailable_action, principal=principal
        )

    async def get_system_defaults(self) -> tuple[list[Limit], OnUnavailable | None]:
        """
        Get system-wide default limits and config.

        Returns:
            Tuple of (limits, on_unavailable). on_unavailable may be None if not set.
        """
        await self._ensure_initialized()
        limits, on_unavailable_action = await self._repository.get_system_defaults()
        on_unavailable = OnUnavailable(on_unavailable_action) if on_unavailable_action else None
        return limits, on_unavailable

    async def delete_system_defaults(
        self,
        principal: str | None = None,
    ) -> None:
        """
        Delete all system-wide default limits and config.

        Args:
            principal: Caller identity for audit logging (optional)
        """
        await self._ensure_initialized()
        await self._repository.delete_system_defaults(principal=principal)

    # -------------------------------------------------------------------------
    # Capacity queries
    # -------------------------------------------------------------------------

    async def get_resource_capacity(
        self,
        resource: str,
        limit_name: str,
        parents_only: bool = False,
    ) -> ResourceCapacity:
        """
        Get aggregated capacity for a resource across all entities.

        Args:
            resource: Resource to query
            limit_name: Limit name to query
            parents_only: If True, only include parent entities

        Returns:
            ResourceCapacity with aggregated data
        """
        await self._ensure_initialized()
        now_ms = self._repository._now_ms()

        buckets = await self._repository.get_resource_buckets(resource, limit_name)

        # Filter to parents only if requested
        if parents_only:
            parent_ids = set()
            for bucket in buckets:
                entity = await self._repository.get_entity(bucket.entity_id)
                if entity and entity.is_parent:
                    parent_ids.add(bucket.entity_id)
            buckets = [b for b in buckets if b.entity_id in parent_ids]

        # Group buckets by entity_id to deduplicate shards (GHSA-76rv).
        # Each shard stores full undivided capacity; available tokens are
        # distributed across shards.
        entity_buckets: dict[str, list[BucketState]] = {}
        for bucket in buckets:
            entity_buckets.setdefault(bucket.entity_id, []).append(bucket)

        entities: list[EntityCapacity] = []
        total_capacity = 0
        total_available = 0

        for entity_id, entity_bucket_list in entity_buckets.items():
            capacity = entity_bucket_list[0].capacity
            available = sum(calculate_available(b, now_ms) for b in entity_bucket_list)
            available = min(available, capacity)

            total_capacity += capacity
            total_available += available

            entities.append(
                EntityCapacity(
                    entity_id=entity_id,
                    capacity=capacity,
                    available=available,
                    utilization_pct=(
                        ((capacity - available) / capacity * 100) if capacity > 0 else 0
                    ),
                )
            )

        return ResourceCapacity(
            resource=resource,
            limit_name=limit_name,
            total_capacity=total_capacity,
            total_available=total_available,
            utilization_pct=(
                ((total_capacity - total_available) / total_capacity * 100)
                if total_capacity > 0
                else 0
            ),
            entities=entities,
        )
