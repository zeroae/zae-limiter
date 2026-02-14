"""Main RateLimiter implementation."""

import asyncio
import logging
import time
import warnings
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from .repository_protocol import RepositoryProtocol, SpeculativeResult

from .bucket import (
    build_limit_status,
    calculate_available,
    calculate_time_until_available,
    try_consume,
    would_refill_satisfy,
)
from .config_cache import ConfigSource
from .exceptions import (
    RateLimiterUnavailable,
    RateLimitExceeded,
    ValidationError,
)
from .lease import Lease, LeaseEntry
from .models import (
    AuditEvent,
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
from .schema import DEFAULT_RESOURCE

_UNSET: Any = object()  # sentinel for detecting explicitly-passed deprecated params

logger = logging.getLogger(__name__)


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
        # Old API (deprecated in v0.5.0, removed in v2.0.0)
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
                "This will be removed in v2.0.0.",
                DeprecationWarning,
                stacklevel=2,
            )
        if auto_update is not _UNSET:
            warnings.warn(
                "auto_update constructor parameter is deprecated. "
                "Use Repository.builder(...).auto_update(True).build() instead. "
                "This will be removed in v2.0.0.",
                DeprecationWarning,
                stacklevel=2,
            )
        if bucket_ttl_refill_multiplier is not _UNSET:
            warnings.warn(
                "bucket_ttl_refill_multiplier constructor parameter is deprecated. "
                "Use Repository.builder(...).bucket_ttl_multiplier(7).build() instead. "
                "This will be removed in v2.0.0.",
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
                "This will be removed in v2.0.0.",
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
                "This will be removed in v2.0.0.",
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
            "This will be removed in v2.0.0.",
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
            "This will be removed in v2.0.0.",
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
            "This will be removed in v2.0.0.",
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

            # Try speculative fast path first (issue #315)
            if self._speculative_writes:
                lease = await self._try_speculative_acquire(
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
                )
        except (RateLimitExceeded, ValidationError):
            raise
        except Exception as e:
            if mode == OnUnavailable.ALLOW:
                # Return a no-op lease
                yield Lease(repository=self._repository)
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
    ) -> Lease | None:
        """Try the speculative fast path for acquire (issue #315).

        Repository checks its own entity cache (issue #318) and issues
        parallel child+parent UpdateItems when cache hit + cascade.

        Returns:
            Lease if speculative write succeeded (already committed).
            None if slow path is needed (refill would help, bucket missing,
            or config changed).

        Raises:
            RateLimitExceeded: If the bucket is truly exhausted (refill
                wouldn't help). Saves 1 RCU vs the slow path.
        """
        now_ms = int(time.time() * 1000)

        # Repository handles cache check and parallel writes (issue #318)
        result = await self._repository.speculative_consume(
            entity_id=entity_id,
            resource=resource,
            consume=consume,
        )

        if not result.success:
            # Child failed — check if parent was also tried (parallel path)
            if result.parent_result is not None and result.parent_result.success:
                assert result.parent_id is not None  # set by repository cache path
                await self._compensate_speculative(result.parent_id, resource, consume)
            self._check_speculative_failure(result, consume, now_ms)
            return None

        # Child succeeded — build entries from ALL_NEW
        entries: list[LeaseEntry] = []
        for state in result.buckets:
            amount = consume.get(state.limit_name, 0)
            if amount == 0:
                continue
            limit = Limit.from_bucket_state(state)
            entries.append(
                LeaseEntry(
                    entity_id=state.entity_id,
                    resource=state.resource,
                    limit=limit,
                    state=state,
                    consumed=amount,
                    _cascade=result.cascade,
                    _parent_id=result.parent_id,
                )
            )

        # Handle parent result from parallel path (issue #318)
        if result.parent_result is not None:
            if result.parent_result.success:
                for state in result.parent_result.buckets:
                    amount = consume.get(state.limit_name, 0)
                    if amount == 0:
                        continue
                    limit = Limit.from_bucket_state(state)
                    entries.append(
                        LeaseEntry(
                            entity_id=state.entity_id,
                            resource=state.resource,
                            limit=limit,
                            state=state,
                            consumed=amount,
                        )
                    )
            else:
                return await self._handle_nested_parent_failure(
                    entity_id, resource, consume, result, now_ms
                )
        elif result.cascade and result.parent_id:
            # Cache miss cascade — sequential parent speculative
            parent_id = result.parent_id
            parent_result = await self._repository.speculative_consume(
                entity_id=parent_id,
                resource=resource,
                consume=consume,
            )

            if parent_result.success:
                for state in parent_result.buckets:
                    amount = consume.get(state.limit_name, 0)
                    if amount == 0:
                        continue
                    limit = Limit.from_bucket_state(state)
                    entries.append(
                        LeaseEntry(
                            entity_id=state.entity_id,
                            resource=state.resource,
                            limit=limit,
                            state=state,
                            consumed=amount,
                        )
                    )
            else:
                if parent_result.old_buckets is None:
                    await self._compensate_child(entity_id, resource, consume)
                    return None

                parent_names = {b.limit_name for b in parent_result.old_buckets}
                if not all(name in parent_names for name in consume):
                    await self._compensate_child(entity_id, resource, consume)
                    return None

                would_help, parent_statuses = would_refill_satisfy(
                    parent_result.old_buckets, consume, now_ms
                )
                if not would_help:
                    await self._compensate_child(entity_id, resource, consume)
                    child_statuses = [
                        build_limit_status(
                            entity_id=s.entity_id,
                            resource=s.resource,
                            limit=Limit.from_bucket_state(s),
                            state=s,
                            requested=consume.get(s.limit_name, 0),
                            now_ms=now_ms,
                        )
                        for s in result.buckets
                    ]
                    raise RateLimitExceeded(child_statuses + parent_statuses)

                try:
                    parent_lease = await self._try_parent_only_acquire(
                        parent_id, resource, consume, entries
                    )
                except Exception:
                    await self._compensate_child(entity_id, resource, consume)
                    raise

                if parent_lease is not None:
                    return parent_lease

                await self._compensate_child(entity_id, resource, consume)
                return None

        # Build pre-committed lease
        lease = Lease(
            repository=self._repository,
            entries=entries,
        )
        lease._initial_committed = True
        for entry in entries:
            entry._initial_consumed = entry.consumed
        return lease

    async def _handle_nested_parent_failure(
        self,
        entity_id: str,
        resource: str,
        consume: dict[str, int],
        result: "SpeculativeResult",
        now_ms: int,
    ) -> Lease | None:
        """Handle parent failure from nested SpeculativeResult (issue #318).

        Child succeeded speculatively (result.success=True).
        Parent failed (result.parent_result.success=False).
        Decides whether to compensate child and fall back, try parent-only
        slow path, or fast-reject.

        Returns:
            Lease if parent-only slow path succeeded.
            None if full slow path is needed.

        Raises:
            RateLimitExceeded: If parent is truly exhausted.
        """
        assert result.parent_result is not None  # caller checks this
        assert result.parent_id is not None  # set by repository cache path
        parent_result = result.parent_result
        parent_id = result.parent_id

        if parent_result.old_buckets is None:
            await self._compensate_child(entity_id, resource, consume)
            return None

        parent_names = {b.limit_name for b in parent_result.old_buckets}
        if not all(name in parent_names for name in consume):
            await self._compensate_child(entity_id, resource, consume)
            return None

        would_help, parent_statuses = would_refill_satisfy(
            parent_result.old_buckets, consume, now_ms
        )
        if not would_help:
            await self._compensate_child(entity_id, resource, consume)
            child_statuses = [
                build_limit_status(
                    entity_id=s.entity_id,
                    resource=s.resource,
                    limit=Limit.from_bucket_state(s),
                    state=s,
                    requested=consume.get(s.limit_name, 0),
                    now_ms=now_ms,
                )
                for s in result.buckets
            ]
            raise RateLimitExceeded(child_statuses + parent_statuses)

        # Refill would help — build child entries for parent-only slow path
        entries: list[LeaseEntry] = []
        for state in result.buckets:
            amount = consume.get(state.limit_name, 0)
            if amount == 0:
                continue
            limit = Limit.from_bucket_state(state)
            entries.append(
                LeaseEntry(
                    entity_id=state.entity_id,
                    resource=state.resource,
                    limit=limit,
                    state=state,
                    consumed=amount,
                    _cascade=result.cascade,
                    _parent_id=result.parent_id,
                )
            )

        try:
            parent_lease = await self._try_parent_only_acquire(
                parent_id, resource, consume, entries
            )
        except Exception:
            await self._compensate_child(entity_id, resource, consume)
            raise

        if parent_lease is not None:
            return parent_lease

        await self._compensate_child(entity_id, resource, consume)
        return None

    async def _compensate_child(
        self,
        entity_id: str,
        resource: str,
        consume: dict[str, int],
    ) -> None:
        """Compensate a speculatively consumed child by adding tokens back."""
        await self._compensate_speculative(entity_id, resource, consume)

    async def _compensate_speculative(
        self,
        entity_id: str,
        resource: str,
        consume: dict[str, int],
    ) -> None:
        """Compensate a speculative write by adding consumed tokens back."""
        deltas = {name: -(amount * 1000) for name, amount in consume.items()}
        compensate_item = self._repository.build_composite_adjust(
            entity_id=entity_id,
            resource=resource,
            deltas=deltas,
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

        bucket_names = {b.limit_name for b in result.old_buckets}
        if not all(name in bucket_names for name in consume):
            return

        would_help, statuses = would_refill_satisfy(result.old_buckets, consume, now_ms)
        if not would_help:
            raise RateLimitExceeded(statuses)

    async def _try_parent_only_acquire(
        self,
        parent_id: str,
        resource: str,
        consume: dict[str, int],
        child_entries: list[LeaseEntry],
    ) -> Lease | None:
        """Attempt parent-only slow path after child speculative succeeded.

        Reads parent buckets, resolves limits, does refill + try_consume,
        and writes parent via single-item UpdateItem. Returns a Lease combining
        child's speculative entries with parent's slow-path entries.

        Returns None if parent acquire fails (caller should compensate child).
        """
        now_ms = int(time.time() * 1000)

        # Resolve parent limits
        parent_limits, parent_config_source = await self._resolve_limits(parent_id, resource, None)

        # Fetch parent buckets
        parent_buckets = await self._fetch_buckets([parent_id], resource)

        # Process parent buckets: refill + try_consume
        parent_entries: list[LeaseEntry] = []
        statuses: list[LimitStatus] = []
        has_custom_config = parent_config_source == "entity"

        for limit in parent_limits:
            bucket_key = (parent_id, resource, limit.name)
            existing = parent_buckets.get(bucket_key)
            if existing is None:
                # Parent bucket missing for this limit — can't proceed
                return None

            original_tk = existing.tokens_milli
            original_rf = existing.last_refill_ms

            amount = consume.get(limit.name, 0)
            result = try_consume(existing, amount, now_ms)

            status = LimitStatus(
                entity_id=parent_id,
                resource=resource,
                limit_name=limit.name,
                limit=limit,
                available=result.available,
                requested=amount,
                exceeded=not result.success,
                retry_after_seconds=result.retry_after_seconds,
            )
            statuses.append(status)

            if result.success:
                existing.tokens_milli = result.new_tokens_milli
                existing.last_refill_ms = result.new_last_refill_ms
                if existing.total_consumed_milli is not None and amount > 0:
                    existing.total_consumed_milli += amount * 1000

            parent_entries.append(
                LeaseEntry(
                    entity_id=parent_id,
                    resource=resource,
                    limit=limit,
                    state=existing,
                    consumed=amount if result.success else 0,
                    _original_tokens_milli=original_tk,
                    _original_rf_ms=original_rf,
                    _has_custom_config=has_custom_config,
                )
            )

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
    ) -> Lease:
        """Internal acquire implementation."""
        # Validate inputs at API boundary
        validate_identifier(entity_id, "entity_id")
        validate_resource(resource)

        now_ms = int(time.time() * 1000)

        # Phase 1: Resolve child limits, then fetch child META + child buckets
        # in a single BatchGetItem call (no separate get_entity round trip).
        child_limits, child_config_source = await self._resolve_limits(
            entity_id, resource, limits_override
        )

        entity, child_buckets = await self._fetch_entity_and_buckets(entity_id, resource)

        # Determine cascade
        entity_ids = [entity_id]
        existing_buckets: dict[tuple[str, str, str], BucketState] = dict(child_buckets)
        entity_limits: dict[str, list[Limit]] = {entity_id: child_limits}
        # Track config source per entity (for TTL calculation, issue #271)
        entity_config_sources: dict[str, str] = {entity_id: child_config_source}

        if entity and entity.cascade and entity.parent_id:
            parent_id = entity.parent_id
            entity_ids.append(parent_id)

            # Phase 2: Resolve parent limits + fetch parent buckets
            parent_limits, parent_config_source = await self._resolve_limits(
                parent_id, resource, limits_override
            )
            entity_limits[parent_id] = parent_limits
            entity_config_sources[parent_id] = parent_config_source
            parent_buckets = await self._fetch_buckets([parent_id], resource)
            existing_buckets.update(parent_buckets)

        # Process buckets and build lease entries
        entries: list[LeaseEntry] = []
        statuses: list[LimitStatus] = []

        for eid in entity_ids:
            # Track whether any bucket existed for this entity+resource
            any_existing = any(
                (eid, resource, limit.name) in existing_buckets for limit in entity_limits[eid]
            )

            for limit in entity_limits[eid]:
                # Get existing bucket from batch result or create new one
                bucket_key = (eid, resource, limit.name)
                existing = existing_buckets.get(bucket_key)
                if existing is None:
                    is_new = True
                    state = BucketState.from_limit(eid, resource, limit, now_ms)
                else:
                    is_new = False
                    state = existing

                # Capture original values before try_consume modifies them (ADR-115)
                original_tk = state.tokens_milli
                original_rf = state.last_refill_ms

                # Try to consume
                amount = consume.get(limit.name, 0)
                result = try_consume(state, amount, now_ms)

                status = LimitStatus(
                    entity_id=eid,
                    resource=resource,
                    limit_name=limit.name,
                    limit=limit,
                    available=result.available,
                    requested=amount,
                    exceeded=not result.success,
                    retry_after_seconds=result.retry_after_seconds,
                )
                statuses.append(status)

                if result.success:
                    # Update local state
                    state.tokens_milli = result.new_tokens_milli
                    state.last_refill_ms = result.new_last_refill_ms
                    # Update consumption counter if initialized (issue #179)
                    if state.total_consumed_milli is not None and amount > 0:
                        state.total_consumed_milli += amount * 1000

                # Determine if entity has custom config for TTL (Issue #271)
                has_custom_config = entity_config_sources.get(eid) == "entity"

                entries.append(
                    LeaseEntry(
                        entity_id=eid,
                        resource=resource,
                        limit=limit,
                        state=state,
                        consumed=amount if result.success else 0,
                        _original_tokens_milli=original_tk,
                        _original_rf_ms=original_rf,
                        _is_new=is_new and not any_existing,
                        _has_custom_config=has_custom_config,
                        _cascade=entity.cascade if entity and eid == entity_id else False,
                        _parent_id=entity.parent_id if entity and eid == entity_id else None,
                    )
                )

        # Check for any violations
        violations = [s for s in statuses if s.exceeded]
        if violations:
            raise RateLimitExceeded(statuses)

        return Lease(
            repository=self._repository,
            entries=entries,
        )

    async def _fetch_entity_and_buckets(
        self,
        entity_id: str,
        resource: str,
    ) -> tuple[Entity | None, dict[tuple[str, str, str], BucketState]]:
        """
        Fetch entity metadata and its composite bucket in a single call.

        With composite items (ADR-114), one item per (entity_id, resource)
        contains all limits. Uses batch_get_entity_and_buckets if the backend
        supports batch operations, otherwise falls back to separate calls.
        """
        if self._repository.capabilities.supports_batch_operations:
            # Composite key: one item per (entity_id, resource)
            bucket_keys = [(entity_id, resource)]
            result: tuple[
                Entity | None, dict[tuple[str, str, str], BucketState]
            ] = await self._repository.batch_get_entity_and_buckets(entity_id, bucket_keys)
            return result

        # Fallback: sequential calls
        entity = await self._repository.get_entity(entity_id)
        buckets = await self._repository.get_buckets(entity_id, resource)
        bucket_dict: dict[tuple[str, str, str], BucketState] = {
            (b.entity_id, b.resource, b.limit_name): b for b in buckets
        }
        return entity, bucket_dict

    async def _fetch_buckets(
        self,
        entity_ids: list[str],
        resource: str,
    ) -> dict[tuple[str, str, str], BucketState]:
        """
        Fetch composite buckets for entity/resource pairs.

        With composite items (ADR-114), each (entity_id, resource) is one
        DynamoDB item containing all limits. Uses batch_get_buckets if the
        backend supports it, otherwise falls back to sequential calls.

        Args:
            entity_ids: List of entity IDs to fetch buckets for
            resource: Resource name

        Returns:
            Dict mapping (entity_id, resource, limit_name) to BucketState.
            Missing buckets are not included in the result.
        """
        # Use batch operation if backend supports it (issue #133)
        if self._repository.capabilities.supports_batch_operations:
            # Composite key: one item per (entity_id, resource)
            bucket_keys: list[tuple[str, str]] = [(eid, resource) for eid in entity_ids]
            batch_result: dict[
                tuple[str, str, str], BucketState
            ] = await self._repository.batch_get_buckets(bucket_keys)
            return batch_result

        # Fallback: sequential get_buckets calls
        result: dict[tuple[str, str, str], BucketState] = {}
        for eid in entity_ids:
            buckets = await self._repository.get_buckets(eid, resource)
            for bucket in buckets:
                key = (bucket.entity_id, bucket.resource, bucket.limit_name)
                result[key] = bucket
        return result

    async def _resolve_limits(
        self,
        entity_id: str,
        resource: str,
        limits_override: list[Limit] | None,
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

        Returns minimum available across entity (and parent if cascade).
        Can return negative values if bucket is in debt.

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
        await self._ensure_initialized()
        now_ms = int(time.time() * 1000)

        # Deprecation warning for use_stored_limits
        if use_stored_limits:
            warnings.warn(
                "use_stored_limits is deprecated and will be removed in v1.0. "
                "Limits are now always resolved from stored config (Entity > Resource > System). "
                "Pass limits parameter as override if needed.",
                DeprecationWarning,
                stacklevel=2,
            )

        # Resolve limits using four-tier hierarchy
        resolved_limits, _ = await self._resolve_limits(entity_id, resource, limits)

        result: dict[str, int] = {}
        for limit in resolved_limits:
            state = await self._repository.get_bucket(entity_id, resource, limit.name)
            if state is None:
                result[limit.name] = limit.burst
            else:
                result[limit.name] = calculate_available(state, now_ms)

        return result

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
        await self._ensure_initialized()
        now_ms = int(time.time() * 1000)

        # Deprecation warning for use_stored_limits
        if use_stored_limits:
            warnings.warn(
                "use_stored_limits is deprecated and will be removed in v1.0. "
                "Limits are now always resolved from stored config (Entity > Resource > System). "
                "Pass limits parameter as override if needed.",
                DeprecationWarning,
                stacklevel=2,
            )

        # Resolve limits using four-tier hierarchy
        resolved_limits, _ = await self._resolve_limits(entity_id, resource, limits)

        max_wait = 0.0
        for limit in resolved_limits:
            amount = needed.get(limit.name, 0)
            if amount <= 0:
                continue

            state = await self._repository.get_bucket(entity_id, resource, limit.name)
            if state is None:
                continue  # New bucket, will have full capacity

            wait = calculate_time_until_available(state, amount, now_ms)
            max_wait = max(max_wait, wait)

        return max_wait

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
        now_ms = int(time.time() * 1000)

        buckets = await self._repository.get_resource_buckets(resource, limit_name)

        # Filter to parents only if requested
        if parents_only:
            parent_ids = set()
            for bucket in buckets:
                entity = await self._repository.get_entity(bucket.entity_id)
                if entity and entity.is_parent:
                    parent_ids.add(bucket.entity_id)
            buckets = [b for b in buckets if b.entity_id in parent_ids]

        entities: list[EntityCapacity] = []
        total_capacity = 0
        total_available = 0

        for bucket in buckets:
            available = calculate_available(bucket, now_ms)
            capacity = bucket.capacity

            total_capacity += capacity
            total_available += available

            entities.append(
                EntityCapacity(
                    entity_id=bucket.entity_id,
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
