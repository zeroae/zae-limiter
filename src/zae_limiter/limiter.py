"""Main RateLimiter implementation."""

import asyncio
import time
import warnings
from collections.abc import AsyncIterator, Coroutine, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from enum import Enum
from typing import Any, TypeVar

from .bucket import (
    calculate_available,
    calculate_time_until_available,
    try_consume,
)
from .exceptions import (
    IncompatibleSchemaError,
    RateLimiterUnavailable,
    RateLimitExceeded,
    ValidationError,
    VersionMismatchError,
)
from .lease import Lease, LeaseEntry, SyncLease
from .models import (
    AuditEvent,
    BucketState,
    Entity,
    EntityCapacity,
    Limit,
    LimiterInfo,
    LimitStatus,
    ResourceCapacity,
    StackOptions,
    Status,
    UsageSnapshot,
    UsageSummary,
    validate_identifier,
    validate_name,
)
from .repository import Repository
from .schema import DEFAULT_RESOURCE

_T = TypeVar("_T")


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

    Example:
        limiter = RateLimiter(
            name="my-app",  # Creates ZAEL-my-app resources
            region="us-east-1",
            stack_options=StackOptions(),
        )
    """

    def __init__(
        self,
        name: str = "limiter",
        region: str | None = None,
        endpoint_url: str | None = None,
        stack_options: StackOptions | None = None,
        on_unavailable: OnUnavailable = OnUnavailable.BLOCK,
        auto_update: bool = True,
        strict_version: bool = True,
        skip_version_check: bool = False,
    ) -> None:
        """
        Initialize the rate limiter.

        Args:
            name: Resource identifier (e.g., 'my-app').
                Will be prefixed with 'ZAEL-' automatically.
                Default: 'limiter' (creates 'ZAEL-limiter' resources)
            region: AWS region
            endpoint_url: DynamoDB endpoint URL (for local development)
            stack_options: Desired infrastructure state (None = connect only, don't manage)
            on_unavailable: Behavior when DynamoDB is unavailable
            auto_update: Auto-update Lambda when version mismatch detected
            strict_version: Fail if version mismatch (when auto_update is False)
            skip_version_check: Skip all version checks (dangerous)
        """
        from .naming import normalize_name

        # Validate and normalize name (adds ZAEL- prefix)
        self._name = normalize_name(name)
        # Internal: stack_name and table_name for AWS resources
        self.stack_name = self._name
        self.table_name = self._name
        self.on_unavailable = on_unavailable
        self._auto_update = auto_update
        self._strict_version = strict_version
        self._skip_version_check = skip_version_check

        self._stack_options = stack_options
        self._repository = Repository(
            stack_name=self._name,
            region=region,
            endpoint_url=endpoint_url,
        )
        self._initialized = False

    @property
    def name(self) -> str:
        """The resource identifier (with ZAEL- prefix)."""
        return self._name

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
        for stacks with the ZAEL- prefix.

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
        """Ensure infrastructure exists and version is compatible."""
        if self._initialized:
            return

        if self._stack_options is not None:
            await self._repository.create_stack(
                stack_options=self._stack_options,
            )

        # Version check (skip for local DynamoDB without CloudFormation)
        if not self._skip_version_check:
            await self._check_and_update_version()

        self._initialized = True

    async def _check_and_update_version(self) -> None:
        """Check version compatibility and update Lambda if needed."""
        from . import __version__
        from .version import (
            InfrastructureVersion,
            check_compatibility,
        )

        # Get current infrastructure version
        version_record = await self._repository.get_version_record()

        if version_record is None:
            # First time setup or legacy infrastructure - initialize version record
            await self._initialize_version_record()
            return

        infra_version = InfrastructureVersion.from_record(version_record)
        compatibility = check_compatibility(__version__, infra_version)

        if compatibility.is_compatible and not compatibility.requires_lambda_update:
            return

        if compatibility.requires_schema_migration:
            raise IncompatibleSchemaError(
                client_version=__version__,
                schema_version=infra_version.schema_version,
                message=compatibility.message,
            )

        if compatibility.requires_lambda_update:
            if self._auto_update and not self._repository.endpoint_url:
                # Auto-update Lambda (skip for local DynamoDB)
                await self._perform_lambda_update()
            elif self._strict_version:
                raise VersionMismatchError(
                    client_version=__version__,
                    schema_version=infra_version.schema_version,
                    lambda_version=infra_version.lambda_version,
                    message=compatibility.message,
                    can_auto_update=not self._repository.endpoint_url,
                )
            # else: continue with version mismatch (not strict)

    async def _initialize_version_record(self) -> None:
        """Initialize the version record for first-time setup."""
        from . import __version__
        from .version import get_schema_version

        lambda_version = __version__ if not self._repository.endpoint_url else None

        await self._repository.set_version_record(
            schema_version=get_schema_version(),
            lambda_version=lambda_version,
            client_min_version="0.0.0",
            updated_by=f"client:{__version__}",
        )

    async def _perform_lambda_update(self) -> None:
        """Update Lambda code to match client version."""
        from . import __version__
        from .infra.stack_manager import StackManager
        from .version import get_schema_version

        async with StackManager(
            self.stack_name,
            self._repository.region,
            self._repository.endpoint_url,
        ) as manager:
            # Deploy updated Lambda code
            await manager.deploy_lambda_code()

            # Update version record in DynamoDB
            await self._repository.set_version_record(
                schema_version=get_schema_version(),
                lambda_version=__version__,
                client_min_version="0.0.0",
                updated_by=f"client:{__version__}",
            )

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
        metadata: dict[str, str] | None = None,
        principal: str | None = None,
    ) -> Entity:
        """
        Create a new entity.

        Args:
            entity_id: Unique identifier for the entity
            name: Human-readable name (defaults to entity_id)
            parent_id: Parent entity ID (None for root/project entities)
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
        limits: list[Limit] | None,
        consume: dict[str, int],
        cascade: bool = False,
        use_stored_limits: bool = False,
        on_unavailable: OnUnavailable | None = None,
    ) -> AsyncIterator[Lease]:
        """
        Acquire rate limit capacity.

        Limits are resolved using three-tier hierarchy: Entity > Resource > System.
        If no stored limits found, falls back to the `limits` parameter.

        Args:
            entity_id: Entity to acquire capacity for
            resource: Resource being accessed (e.g., "gpt-4")
            consume: Amounts to consume by limit name
            limits: Override limits (optional, falls back to stored config)
            cascade: If True, also consume from parent entity
            use_stored_limits: DEPRECATED - limits are now always resolved from
                stored config. This parameter will be removed in v1.0.
            on_unavailable: Override default on_unavailable behavior

        Yields:
            Lease for managing additional consumption

        Raises:
            RateLimitExceeded: If any limit would be exceeded
            RateLimiterUnavailable: If DynamoDB unavailable and BLOCK
            ValidationError: If no limits found at any level and no override provided
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
            lease = await self._do_acquire(
                entity_id=entity_id,
                resource=resource,
                limits_override=limits,
                consume=consume,
                cascade=cascade,
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
                    stack_name=self.stack_name,
                    entity_id=entity_id,
                    resource=resource,
                ) from e

        # Lease acquired successfully - manage the context
        try:
            yield lease
            await lease._commit()
        except Exception:
            await lease._rollback()
            raise

    async def _do_acquire(
        self,
        entity_id: str,
        resource: str,
        limits_override: list[Limit] | None,
        consume: dict[str, int],
        cascade: bool,
    ) -> Lease:
        """Internal acquire implementation."""
        # Validate inputs at API boundary
        validate_identifier(entity_id, "entity_id")
        validate_name(resource, "resource")

        now_ms = int(time.time() * 1000)

        # Determine which entities to check
        entity_ids = [entity_id]
        if cascade:
            entity = await self._repository.get_entity(entity_id)
            if entity and entity.parent_id:
                entity_ids.append(entity.parent_id)

        # Resolve limits for each entity using three-tier hierarchy
        entity_limits: dict[str, list[Limit]] = {}
        for eid in entity_ids:
            entity_limits[eid] = await self._resolve_limits(eid, resource, limits_override)

        # Get or create buckets for each entity/limit
        entries: list[LeaseEntry] = []
        statuses: list[LimitStatus] = []

        for eid in entity_ids:
            for limit in entity_limits[eid]:
                # Get existing bucket or create new one
                state = await self._repository.get_bucket(eid, resource, limit.name)
                if state is None:
                    state = BucketState.from_limit(eid, resource, limit, now_ms)

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

                entries.append(
                    LeaseEntry(
                        entity_id=eid,
                        resource=resource,
                        limit=limit,
                        state=state,
                        consumed=amount if result.success else 0,
                    )
                )

        # Check for any violations
        violations = [s for s in statuses if s.exceeded]
        if violations:
            raise RateLimitExceeded(statuses)

        return Lease(repository=self._repository, entries=entries)

    async def _resolve_limits(
        self,
        entity_id: str,
        resource: str,
        limits_override: list[Limit] | None,
    ) -> list[Limit]:
        """
        Resolve limits using three-tier hierarchy: Entity > Resource > System > Override.

        Resolution order:
        1. Entity-level config for this resource
        2. Resource-level defaults (if entity config is empty)
        3. System-level defaults (if resource config is empty)
        4. Override parameter (if all configs are empty)
        5. ValidationError (if no limits found anywhere)

        Args:
            entity_id: Entity to resolve limits for
            resource: Resource being accessed
            limits_override: Optional override limits (from limits parameter)

        Returns:
            Resolved list of Limit objects

        Raises:
            ValidationError: If no limits found at any level and no override provided
        """
        # Try Entity level
        entity_limits = await self._repository.get_limits(entity_id, resource)
        if entity_limits:
            return entity_limits

        # Try Resource level
        resource_limits = await self._repository.get_resource_defaults(resource)
        if resource_limits:
            return resource_limits

        # Try System level
        system_limits, _ = await self._repository.get_system_defaults()
        if system_limits:
            return system_limits

        # Try override parameter
        if limits_override is not None:
            return limits_override

        # No limits found anywhere
        raise ValidationError(
            field="limits",
            value=f"entity={entity_id}, resource={resource}",
            reason=(
                f"No limits configured for entity '{entity_id}' and resource '{resource}'. "
                "Configure limits at entity, resource, or system level, "
                "or provide limits parameter."
            ),
        )

    async def _resolve_on_unavailable(
        self,
        on_unavailable_param: OnUnavailable | None,
    ) -> OnUnavailable:
        """
        Resolve on_unavailable behavior: Parameter > System Config > Constructor default.

        Args:
            on_unavailable_param: Optional parameter override

        Returns:
            Resolved OnUnavailable enum value
        """
        # If parameter is provided, use it
        if on_unavailable_param is not None:
            return on_unavailable_param

        # Try System config
        _, on_unavailable_str = await self._repository.get_system_defaults()
        if on_unavailable_str is not None:
            return OnUnavailable(on_unavailable_str)

        # Fall back to constructor default
        return self.on_unavailable

    async def available(
        self,
        entity_id: str,
        resource: str,
        limits: list[Limit] | None = None,
        use_stored_limits: bool = False,
    ) -> dict[str, int]:
        """
        Check available capacity without consuming.

        Limits are resolved using three-tier hierarchy: Entity > Resource > System.
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

        # Resolve limits using three-tier hierarchy
        resolved_limits = await self._resolve_limits(entity_id, resource, limits)

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

        Limits are resolved using three-tier hierarchy: Entity > Resource > System.
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

        # Resolve limits using three-tier hierarchy
        resolved_limits = await self._resolve_limits(entity_id, resource, limits)

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

        Args:
            entity_id: Entity to delete limits for
            resource: Resource to delete limits for
            principal: Caller identity for audit logging (optional)
        """
        await self._ensure_initialized()
        await self._repository.delete_limits(entity_id, resource, principal=principal)

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
        on_unavailable_str = on_unavailable.value if on_unavailable else None
        await self._repository.set_system_defaults(
            limits, on_unavailable=on_unavailable_str, principal=principal
        )

    async def get_system_defaults(self) -> tuple[list[Limit], OnUnavailable | None]:
        """
        Get system-wide default limits and config.

        Returns:
            Tuple of (limits, on_unavailable). on_unavailable may be None if not set.
        """
        await self._ensure_initialized()
        limits, on_unavailable_str = await self._repository.get_system_defaults()
        on_unavailable = None
        if on_unavailable_str:
            on_unavailable = OnUnavailable(on_unavailable_str)
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

    # -------------------------------------------------------------------------
    # Stack management
    # -------------------------------------------------------------------------

    async def create_stack(
        self,
        stack_options: StackOptions | None = None,
    ) -> dict[str, Any]:
        """
        Create CloudFormation stack for infrastructure.

        Args:
            stack_options: Stack configuration

        Returns:
            Dict with stack_id, stack_name, and status

        Raises:
            StackCreationError: If stack creation fails
        """
        from .infra.stack_manager import StackManager

        async with StackManager(
            self.stack_name, self._repository.region, self._repository.endpoint_url
        ) as manager:
            return await manager.create_stack(stack_options)

    async def delete_stack(self) -> None:
        """
        Delete the CloudFormation stack and all associated resources.

        This method permanently removes the CloudFormation stack, including:

        - DynamoDB table and all stored data
        - Lambda aggregator function (if deployed)
        - IAM roles and CloudWatch log groups
        - All other stack resources

        The method waits for deletion to complete before returning.
        If the stack doesn't exist, no error is raised.

        Raises:
            StackCreationError: If deletion fails (e.g., permission denied,
                resources in use, or CloudFormation service error)

        Example:
            Cleanup after integration testing::

                limiter = RateLimiter(
                    name="test-limits",
                    region="us-east-1",
                    stack_options=StackOptions(),
                )

                async with limiter:
                    # Run tests...
                    pass

                # Clean up infrastructure
                await limiter.delete_stack()

        Warning:
            This operation is irreversible. All rate limit state, entity data,
            and usage history will be permanently deleted.
        """
        from .infra.stack_manager import StackManager

        async with StackManager(
            self.stack_name, self._repository.region, self._repository.endpoint_url
        ) as manager:
            await manager.delete_stack(self.stack_name)

    async def get_status(self) -> Status:
        """
        Get comprehensive status of the RateLimiter infrastructure.

        Consolidates connectivity, infrastructure state, version information,
        and table metrics into a single status object. This method does not
        raise exceptions for missing infrastructure - it gracefully handles
        all error cases and returns status information accordingly.

        Returns:
            Status object containing:
            - Connectivity: available, latency_ms
            - Infrastructure: stack_status, table_status, aggregator_enabled
            - Identity: name, region
            - Versions: schema_version, lambda_version, client_version
            - Table metrics: table_item_count, table_size_bytes
            - IAM Roles: app_role_arn, admin_role_arn, readonly_role_arn

        Example:
            Check infrastructure health::

                status = await limiter.get_status()
                if status.available:
                    print(f"Ready! Latency: {status.latency_ms}ms")
                    print(f"Stack: {status.stack_status}")
                    print(f"Schema: {status.schema_version}")
                else:
                    print("DynamoDB is not reachable")

        Note:
            This method measures actual DynamoDB latency by performing a
            lightweight operation. The latency_ms value reflects real
            round-trip time to the DynamoDB endpoint.
        """
        from . import __version__
        from .infra.stack_manager import StackManager

        # Initialize defaults
        available = False
        latency_ms: float | None = None
        cfn_status: str | None = None
        table_status: str | None = None
        aggregator_enabled = False
        schema_version: str | None = None
        lambda_version: str | None = None
        table_item_count: int | None = None
        table_size_bytes: int | None = None
        app_role_arn: str | None = None
        admin_role_arn: str | None = None
        readonly_role_arn: str | None = None

        # Get CloudFormation stack status and outputs (does not require table to exist)
        try:
            async with StackManager(
                self.stack_name, self._repository.region, self._repository.endpoint_url
            ) as manager:
                cfn_status = await manager.get_stack_status(self.stack_name)
                # Get stack outputs for role ARNs if stack exists and is complete
                if cfn_status and "COMPLETE" in cfn_status:
                    try:
                        client = await manager._get_client()
                        response = await client.describe_stacks(StackName=self.stack_name)
                        if response.get("Stacks"):
                            outputs = response["Stacks"][0].get("Outputs", [])
                            for output in outputs:
                                key = output.get("OutputKey", "")
                                value = output.get("OutputValue", "")
                                if key == "AppRoleArn":
                                    app_role_arn = value
                                elif key == "AdminRoleArn":
                                    admin_role_arn = value
                                elif key == "ReadOnlyRoleArn":
                                    readonly_role_arn = value
                    except Exception:
                        pass  # Stack outputs unavailable
        except Exception:
            pass  # Stack status unavailable

        # Ping DynamoDB and measure latency
        try:
            start_time = time.time()
            client = await self._repository._get_client()

            # Use DescribeTable to check connectivity and get table info
            response = await client.describe_table(TableName=self.table_name)
            latency_ms = (time.time() - start_time) * 1000
            available = True

            # Extract table information
            table = response.get("Table", {})
            table_status = table.get("TableStatus")
            table_item_count = table.get("ItemCount")
            table_size_bytes = table.get("TableSizeInBytes")

            # Check if aggregator is enabled by looking for stream specification
            stream_spec = table.get("StreamSpecification", {})
            aggregator_enabled = stream_spec.get("StreamEnabled", False)

        except Exception:
            pass  # DynamoDB unavailable

        # Get version information from DynamoDB
        if available:
            try:
                version_record = await self._repository.get_version_record()
                if version_record:
                    schema_version = version_record.get("schema_version")
                    lambda_version = version_record.get("lambda_version")
            except Exception:
                pass  # Version record unavailable

        return Status(
            available=available,
            latency_ms=latency_ms,
            stack_status=cfn_status,
            table_status=table_status,
            aggregator_enabled=aggregator_enabled,
            name=self._name,
            region=self._repository.region,
            schema_version=schema_version,
            lambda_version=lambda_version,
            client_version=__version__,
            table_item_count=table_item_count,
            table_size_bytes=table_size_bytes,
            app_role_arn=app_role_arn,
            admin_role_arn=admin_role_arn,
            readonly_role_arn=readonly_role_arn,
        )


class SyncRateLimiter:
    """
    Synchronous rate limiter backed by DynamoDB.

    Wraps RateLimiter, running async operations in an event loop.

    Example:
        limiter = SyncRateLimiter(
            name="my-app",  # Creates ZAEL-my-app resources
            region="us-east-1",
            stack_options=StackOptions(),
        )
    """

    def __init__(
        self,
        name: str = "limiter",
        region: str | None = None,
        endpoint_url: str | None = None,
        stack_options: StackOptions | None = None,
        on_unavailable: OnUnavailable = OnUnavailable.BLOCK,
        auto_update: bool = True,
        strict_version: bool = True,
        skip_version_check: bool = False,
    ) -> None:
        self._limiter = RateLimiter(
            name=name,
            region=region,
            endpoint_url=endpoint_url,
            stack_options=stack_options,
            on_unavailable=on_unavailable,
            auto_update=auto_update,
            strict_version=strict_version,
            skip_version_check=skip_version_check,
        )
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def name(self) -> str:
        """The resource identifier (with ZAEL- prefix)."""
        return self._limiter.name

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        """Get or create an event loop."""
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
        return self._loop

    def _run(self, coro: Coroutine[Any, Any, _T]) -> _T:
        """Run a coroutine in the event loop."""
        return self._get_loop().run_until_complete(coro)

    def close(self) -> None:
        """Close the underlying connections."""
        self._run(self._limiter.close())

    def __enter__(self) -> "SyncRateLimiter":
        self._run(self._limiter._ensure_initialized())
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def is_available(self, timeout: float = 1.0) -> bool:
        """
        Check if the rate limiter backend (DynamoDB) is reachable.

        Performs a lightweight health check without requiring initialization.
        This method never raises exceptions - it returns False on any error.

        Args:
            timeout: Maximum time in seconds to wait for response (default: 1.0)

        Returns:
            True if DynamoDB table is reachable, False otherwise.

        Example:
            limiter = SyncRateLimiter(name="my-app", region="us-east-1")
            if limiter.is_available():
                with limiter.acquire(...) as lease:
                    ...
            else:
                # Handle degraded mode
                pass
        """
        try:
            return self._run(self._limiter.is_available(timeout=timeout))
        except Exception:
            return False

    # -------------------------------------------------------------------------
    # Entity management
    # -------------------------------------------------------------------------

    def create_entity(
        self,
        entity_id: str,
        name: str | None = None,
        parent_id: str | None = None,
        metadata: dict[str, str] | None = None,
        principal: str | None = None,
    ) -> Entity:
        """Create a new entity."""
        return self._run(
            self._limiter.create_entity(
                entity_id=entity_id,
                name=name,
                parent_id=parent_id,
                metadata=metadata,
                principal=principal,
            )
        )

    def get_entity(self, entity_id: str) -> Entity | None:
        """Get an entity by ID."""
        return self._run(self._limiter.get_entity(entity_id))

    def delete_entity(
        self,
        entity_id: str,
        principal: str | None = None,
    ) -> None:
        """Delete an entity and all its related data."""
        self._run(self._limiter.delete_entity(entity_id, principal=principal))

    def get_children(self, parent_id: str) -> list[Entity]:
        """Get all children of a parent entity."""
        return self._run(self._limiter.get_children(parent_id))

    def get_audit_events(
        self,
        entity_id: str,
        limit: int = 100,
        start_event_id: str | None = None,
    ) -> list[AuditEvent]:
        """Get audit events for an entity."""
        return self._run(
            self._limiter.get_audit_events(
                entity_id=entity_id,
                limit=limit,
                start_event_id=start_event_id,
            )
        )

    # -------------------------------------------------------------------------
    # Usage snapshots
    # -------------------------------------------------------------------------

    def get_usage_snapshots(
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

        Synchronous wrapper for :meth:`RateLimiter.get_usage_snapshots`.
        See the async version for full documentation.

        Returns:
            Tuple of (snapshots, next_key). next_key is None if no more results.
        """
        return self._run(
            self._limiter.get_usage_snapshots(
                entity_id=entity_id,
                resource=resource,
                window_type=window_type,
                start_time=start_time,
                end_time=end_time,
                limit=limit,
                next_key=next_key,
            )
        )

    def get_usage_summary(
        self,
        entity_id: str | None = None,
        resource: str | None = None,
        window_type: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> UsageSummary:
        """
        Get aggregated usage summary across multiple snapshots.

        Synchronous wrapper for :meth:`RateLimiter.get_usage_summary`.
        See the async version for full documentation.

        Returns:
            UsageSummary with total and average consumption per limit type.
        """
        return self._run(
            self._limiter.get_usage_summary(
                entity_id=entity_id,
                resource=resource,
                window_type=window_type,
                start_time=start_time,
                end_time=end_time,
            )
        )

    # -------------------------------------------------------------------------
    # Rate limiting
    # -------------------------------------------------------------------------

    @contextmanager
    def acquire(
        self,
        entity_id: str,
        resource: str,
        limits: list[Limit] | None,
        consume: dict[str, int],
        cascade: bool = False,
        use_stored_limits: bool = False,
        on_unavailable: OnUnavailable | None = None,
    ) -> Iterator[SyncLease]:
        """
        Acquire rate limit capacity (synchronous).

        Limits are resolved using three-tier hierarchy: Entity > Resource > System.
        If no stored limits found, falls back to the `limits` parameter.
        """
        loop = self._get_loop()

        async def do_acquire() -> tuple[Lease, bool]:
            ctx = self._limiter.acquire(
                entity_id=entity_id,
                resource=resource,
                limits=limits,
                consume=consume,
                cascade=cascade,
                use_stored_limits=use_stored_limits,
                on_unavailable=on_unavailable,
            )
            lease = await ctx.__aenter__()
            return lease, True

        async def do_commit(lease: Lease) -> None:
            await lease._commit()

        async def do_rollback(lease: Lease) -> None:
            await lease._rollback()

        lease, _ = loop.run_until_complete(do_acquire())
        sync_lease = SyncLease(lease, loop)

        try:
            yield sync_lease
            loop.run_until_complete(do_commit(lease))
        except Exception:
            loop.run_until_complete(do_rollback(lease))
            raise

    def available(
        self,
        entity_id: str,
        resource: str,
        limits: list[Limit] | None = None,
        use_stored_limits: bool = False,
    ) -> dict[str, int]:
        """
        Check available capacity without consuming.

        Limits are resolved using three-tier hierarchy: Entity > Resource > System.
        If no stored limits found, falls back to the `limits` parameter.
        """
        return self._run(
            self._limiter.available(
                entity_id=entity_id,
                resource=resource,
                limits=limits,
                use_stored_limits=use_stored_limits,
            )
        )

    def time_until_available(
        self,
        entity_id: str,
        resource: str,
        needed: dict[str, int],
        limits: list[Limit] | None = None,
        use_stored_limits: bool = False,
    ) -> float:
        """
        Calculate seconds until requested capacity is available.

        Limits are resolved using three-tier hierarchy: Entity > Resource > System.
        If no stored limits found, falls back to the `limits` parameter.
        """
        return self._run(
            self._limiter.time_until_available(
                entity_id=entity_id,
                resource=resource,
                needed=needed,
                limits=limits,
                use_stored_limits=use_stored_limits,
            )
        )

    # -------------------------------------------------------------------------
    # Stored limits management
    # -------------------------------------------------------------------------

    def set_limits(
        self,
        entity_id: str,
        limits: list[Limit],
        resource: str = DEFAULT_RESOURCE,
        principal: str | None = None,
    ) -> None:
        """Store limit configs for an entity."""
        self._run(self._limiter.set_limits(entity_id, limits, resource, principal=principal))

    def get_limits(
        self,
        entity_id: str,
        resource: str = DEFAULT_RESOURCE,
    ) -> list[Limit]:
        """Get stored limit configs for an entity."""
        return self._run(self._limiter.get_limits(entity_id, resource))

    def delete_limits(
        self,
        entity_id: str,
        resource: str = DEFAULT_RESOURCE,
        principal: str | None = None,
    ) -> None:
        """Delete stored limit configs for an entity."""
        self._run(self._limiter.delete_limits(entity_id, resource, principal=principal))

    # -------------------------------------------------------------------------
    # Resource-level defaults management
    # -------------------------------------------------------------------------

    def set_resource_defaults(
        self,
        resource: str,
        limits: list[Limit],
        principal: str | None = None,
    ) -> None:
        """Store default limit configs for a resource."""
        self._run(self._limiter.set_resource_defaults(resource, limits, principal=principal))

    def get_resource_defaults(
        self,
        resource: str,
    ) -> list[Limit]:
        """Get stored default limit configs for a resource."""
        return self._run(self._limiter.get_resource_defaults(resource))

    def delete_resource_defaults(
        self,
        resource: str,
        principal: str | None = None,
    ) -> None:
        """Delete stored default limit configs for a resource."""
        self._run(self._limiter.delete_resource_defaults(resource, principal=principal))

    def list_resources_with_defaults(self) -> list[str]:
        """List all resources that have default limit configs."""
        return self._run(self._limiter.list_resources_with_defaults())

    # -------------------------------------------------------------------------
    # System-level defaults management
    # -------------------------------------------------------------------------

    def set_system_defaults(
        self,
        limits: list[Limit],
        on_unavailable: OnUnavailable | None = None,
        principal: str | None = None,
    ) -> None:
        """Store system-wide default limits and config."""
        self._run(
            self._limiter.set_system_defaults(
                limits, on_unavailable=on_unavailable, principal=principal
            )
        )

    def get_system_defaults(self) -> tuple[list[Limit], OnUnavailable | None]:
        """Get system-wide default limits and config."""
        return self._run(self._limiter.get_system_defaults())

    def delete_system_defaults(
        self,
        principal: str | None = None,
    ) -> None:
        """Delete all system-wide default limits and config."""
        self._run(self._limiter.delete_system_defaults(principal=principal))

    # -------------------------------------------------------------------------
    # Capacity queries
    # -------------------------------------------------------------------------

    def get_resource_capacity(
        self,
        resource: str,
        limit_name: str,
        parents_only: bool = False,
    ) -> ResourceCapacity:
        """Get aggregated capacity for a resource across all entities."""
        return self._run(
            self._limiter.get_resource_capacity(
                resource=resource,
                limit_name=limit_name,
                parents_only=parents_only,
            )
        )

    # -------------------------------------------------------------------------
    # Stack management
    # -------------------------------------------------------------------------

    def create_stack(
        self,
        stack_options: StackOptions | None = None,
    ) -> dict[str, Any]:
        """Create CloudFormation stack for infrastructure."""
        return self._run(self._limiter.create_stack(stack_options))

    def delete_stack(self) -> None:
        """
        Delete the CloudFormation stack and all associated resources.

        Synchronous wrapper for :meth:`RateLimiter.delete_stack`.
        See the async version for full documentation.

        Raises:
            StackCreationError: If deletion fails

        Example:
            Cleanup after testing::

                limiter = SyncRateLimiter(
                    name="test-limits",
                    region="us-east-1",
                    stack_options=StackOptions(),
                )

                with limiter:
                    # Run tests...
                    pass

                # Clean up infrastructure
                limiter.delete_stack()

        Warning:
            This operation is irreversible. All data will be permanently deleted.
        """
        self._run(self._limiter.delete_stack())

    def get_status(self) -> Status:
        """
        Get comprehensive status of the RateLimiter infrastructure.

        Synchronous wrapper for :meth:`RateLimiter.get_status`.
        See the async version for full documentation.

        Returns:
            Status object with connectivity, infrastructure, versions, and metrics.

        Example:
            Check infrastructure health::

                limiter = SyncRateLimiter(name="my-app", region="us-east-1")

                status = limiter.get_status()
                if status.available:
                    print(f"Ready! Latency: {status.latency_ms}ms")
                    print(f"Schema: {status.schema_version}")
                else:
                    print("DynamoDB is not reachable")
        """
        return self._run(self._limiter.get_status())

    @staticmethod
    def list_deployed(
        region: str | None = None,
        endpoint_url: str | None = None,
    ) -> list[LimiterInfo]:
        """List all deployed rate limiter instances in a region.

        Synchronous wrapper for :meth:`RateLimiter.list_deployed`.
        See the async version for full documentation.

        Args:
            region: AWS region to search. Defaults to boto3 session default.
            endpoint_url: AWS endpoint URL (e.g., LocalStack).

        Returns:
            List of LimiterInfo objects describing discovered stacks.

        Example:
            Discover all limiters in a region::

                limiters = SyncRateLimiter.list_deployed(region="us-east-1")
                for limiter in limiters:
                    print(f"{limiter.user_name}: {limiter.stack_status}")
        """
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                RateLimiter.list_deployed(region=region, endpoint_url=endpoint_url)
            )
        finally:
            loop.close()
