"""Main RateLimiter implementation."""

import asyncio
import time
from collections.abc import AsyncIterator, Coroutine, Iterator
from contextlib import asynccontextmanager, contextmanager
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
    LimitStatus,
    ResourceCapacity,
    StackOptions,
    Status,
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
    # Rate limiting
    # -------------------------------------------------------------------------

    @asynccontextmanager
    async def acquire(
        self,
        entity_id: str,
        resource: str,
        limits: list[Limit],
        consume: dict[str, int],
        cascade: bool = False,
        use_stored_limits: bool = False,
        on_unavailable: OnUnavailable | None = None,
    ) -> AsyncIterator[Lease]:
        """
        Acquire rate limit capacity.

        Args:
            entity_id: Entity to acquire capacity for
            resource: Resource being accessed (e.g., "gpt-4")
            limits: Default limits to apply
            consume: Amounts to consume by limit name
            cascade: If True, also consume from parent entity
            use_stored_limits: If True, use stored limits if available
            on_unavailable: Override default on_unavailable behavior

        Yields:
            Lease for managing additional consumption

        Raises:
            RateLimitExceeded: If any limit would be exceeded
            RateLimiterUnavailable: If DynamoDB unavailable and BLOCK
        """
        await self._ensure_initialized()
        mode = on_unavailable or self.on_unavailable

        # Acquire the lease (this may fail due to rate limit or infrastructure)
        try:
            lease = await self._do_acquire(
                entity_id=entity_id,
                resource=resource,
                limits=limits,
                consume=consume,
                cascade=cascade,
                use_stored_limits=use_stored_limits,
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
        limits: list[Limit],
        consume: dict[str, int],
        cascade: bool,
        use_stored_limits: bool,
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

        # Resolve limits for each entity
        entity_limits: dict[str, list[Limit]] = {}
        for eid in entity_ids:
            if use_stored_limits:
                stored = await self._repository.get_limits(eid, resource)
                if not stored:
                    stored = await self._repository.get_limits(eid, DEFAULT_RESOURCE)
                entity_limits[eid] = stored if stored else limits
            else:
                entity_limits[eid] = limits

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

    async def available(
        self,
        entity_id: str,
        resource: str,
        limits: list[Limit],
        use_stored_limits: bool = False,
    ) -> dict[str, int]:
        """
        Check available capacity without consuming.

        Returns minimum available across entity (and parent if cascade).
        Can return negative values if bucket is in debt.

        Args:
            entity_id: Entity to check
            resource: Resource to check
            limits: Default limits
            use_stored_limits: Use stored limits if available

        Returns:
            Dict mapping limit_name -> available tokens
        """
        await self._ensure_initialized()
        now_ms = int(time.time() * 1000)

        # Resolve limits
        if use_stored_limits:
            stored = await self._repository.get_limits(entity_id, resource)
            if not stored:
                stored = await self._repository.get_limits(entity_id, DEFAULT_RESOURCE)
            limits = stored if stored else limits

        result: dict[str, int] = {}
        for limit in limits:
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
        limits: list[Limit],
        needed: dict[str, int],
        use_stored_limits: bool = False,
    ) -> float:
        """
        Calculate seconds until requested capacity is available.

        Args:
            entity_id: Entity to check
            resource: Resource to check
            limits: Default limits
            needed: Required amounts by limit name
            use_stored_limits: Use stored limits if available

        Returns:
            Seconds until available (0.0 if already available)
        """
        await self._ensure_initialized()
        now_ms = int(time.time() * 1000)

        # Resolve limits
        if use_stored_limits:
            stored = await self._repository.get_limits(entity_id, resource)
            if not stored:
                stored = await self._repository.get_limits(entity_id, DEFAULT_RESOURCE)
            limits = stored if stored else limits

        max_wait = 0.0
        for limit in limits:
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

        # Get CloudFormation stack status (does not require table to exist)
        try:
            async with StackManager(
                self.stack_name, self._repository.region, self._repository.endpoint_url
            ) as manager:
                cfn_status = await manager.get_stack_status(self.stack_name)
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
    # Rate limiting
    # -------------------------------------------------------------------------

    @contextmanager
    def acquire(
        self,
        entity_id: str,
        resource: str,
        limits: list[Limit],
        consume: dict[str, int],
        cascade: bool = False,
        use_stored_limits: bool = False,
        on_unavailable: OnUnavailable | None = None,
    ) -> Iterator[SyncLease]:
        """Acquire rate limit capacity (synchronous)."""
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
        limits: list[Limit],
        use_stored_limits: bool = False,
    ) -> dict[str, int]:
        """Check available capacity without consuming."""
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
        limits: list[Limit],
        needed: dict[str, int],
        use_stored_limits: bool = False,
    ) -> float:
        """Calculate seconds until requested capacity is available."""
        return self._run(
            self._limiter.time_until_available(
                entity_id=entity_id,
                resource=resource,
                limits=limits,
                needed=needed,
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
