"""Main RateLimiter implementation."""

import asyncio
import time
from contextlib import asynccontextmanager, contextmanager
from enum import Enum
from typing import Any, AsyncIterator, Iterator, Optional

from .bucket import (
    build_limit_status,
    calculate_available,
    calculate_time_until_available,
    try_consume,
)
from .exceptions import (
    EntityNotFoundError,
    RateLimitExceeded,
    RateLimiterUnavailable,
)
from .lease import Lease, LeaseEntry, SyncLease
from .models import (
    BucketState,
    Entity,
    EntityCapacity,
    Limit,
    LimitStatus,
    ResourceCapacity,
)
from .repository import Repository
from .schema import DEFAULT_RESOURCE


class FailureMode(Enum):
    """Behavior when DynamoDB is unavailable."""

    FAIL_OPEN = "open"  # Allow requests
    FAIL_CLOSED = "closed"  # Reject requests


class RateLimiter:
    """
    Async rate limiter backed by DynamoDB.

    Implements token bucket algorithm with support for:
    - Multiple limits per entity/resource
    - Two-level hierarchy (parent/child entities)
    - Cascade mode (consume from entity + parent)
    - Stored limit configs
    - Usage analytics
    """

    def __init__(
        self,
        table_name: str,
        region: Optional[str] = None,
        endpoint_url: Optional[str] = None,
        create_table: bool = False,
        failure_mode: FailureMode = FailureMode.FAIL_CLOSED,
    ) -> None:
        self.table_name = table_name
        self.failure_mode = failure_mode
        self._create_table = create_table
        self._repository = Repository(
            table_name=table_name,
            region=region,
            endpoint_url=endpoint_url,
        )
        self._initialized = False

    async def _ensure_initialized(self) -> None:
        """Ensure table exists if create_table=True."""
        if self._initialized:
            return
        if self._create_table:
            await self._repository.create_table()
        self._initialized = True

    async def close(self) -> None:
        """Close the underlying connections."""
        await self._repository.close()

    async def __aenter__(self) -> "RateLimiter":
        await self._ensure_initialized()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    # -------------------------------------------------------------------------
    # Entity management
    # -------------------------------------------------------------------------

    async def create_entity(
        self,
        entity_id: str,
        name: Optional[str] = None,
        parent_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> Entity:
        """
        Create a new entity.

        Args:
            entity_id: Unique identifier for the entity
            name: Human-readable name (defaults to entity_id)
            parent_id: Parent entity ID (None for root/project entities)
            metadata: Additional metadata to store

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
        )

    async def get_entity(self, entity_id: str) -> Optional[Entity]:
        """Get an entity by ID."""
        await self._ensure_initialized()
        return await self._repository.get_entity(entity_id)

    async def delete_entity(self, entity_id: str) -> None:
        """Delete an entity and all its related data."""
        await self._ensure_initialized()
        await self._repository.delete_entity(entity_id)

    async def get_children(self, parent_id: str) -> list[Entity]:
        """Get all children of a parent entity."""
        await self._ensure_initialized()
        return await self._repository.get_children(parent_id)

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
        failure_mode: Optional[FailureMode] = None,
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
            failure_mode: Override default failure mode

        Yields:
            Lease for managing additional consumption

        Raises:
            RateLimitExceeded: If any limit would be exceeded
            RateLimiterUnavailable: If DynamoDB unavailable and FAIL_CLOSED
        """
        await self._ensure_initialized()
        mode = failure_mode or self.failure_mode

        try:
            lease = await self._do_acquire(
                entity_id=entity_id,
                resource=resource,
                limits=limits,
                consume=consume,
                cascade=cascade,
                use_stored_limits=use_stored_limits,
            )

            try:
                yield lease
                await lease._commit()
            except Exception:
                await lease._rollback()
                raise

        except RateLimitExceeded:
            raise
        except Exception as e:
            if mode == FailureMode.FAIL_OPEN:
                # Return a no-op lease
                yield Lease(repository=self._repository)
            else:
                raise RateLimiterUnavailable(str(e), cause=e) from e

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
    ) -> None:
        """
        Store limit configs for an entity.

        Args:
            entity_id: Entity to set limits for
            limits: Limits to store
            resource: Resource these limits apply to (or _default_)
        """
        await self._ensure_initialized()
        await self._repository.set_limits(entity_id, limits, resource)

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
    ) -> None:
        """
        Delete stored limit configs for an entity.

        Args:
            entity_id: Entity to delete limits for
            resource: Resource to delete limits for
        """
        await self._ensure_initialized()
        await self._repository.delete_limits(entity_id, resource)

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
    # Table management
    # -------------------------------------------------------------------------

    async def create_table(self) -> None:
        """Create the DynamoDB table if it doesn't exist."""
        await self._repository.create_table()
        self._initialized = True

    async def delete_table(self) -> None:
        """Delete the DynamoDB table."""
        await self._repository.delete_table()
        self._initialized = False


class SyncRateLimiter:
    """
    Synchronous rate limiter backed by DynamoDB.

    Wraps RateLimiter, running async operations in an event loop.
    """

    def __init__(
        self,
        table_name: str,
        region: Optional[str] = None,
        endpoint_url: Optional[str] = None,
        create_table: bool = False,
        failure_mode: FailureMode = FailureMode.FAIL_CLOSED,
    ) -> None:
        self._limiter = RateLimiter(
            table_name=table_name,
            region=region,
            endpoint_url=endpoint_url,
            create_table=create_table,
            failure_mode=failure_mode,
        )
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        """Get or create an event loop."""
        if self._loop is None or self._loop.is_closed():
            try:
                self._loop = asyncio.get_event_loop()
            except RuntimeError:
                self._loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self._loop)
        return self._loop

    def _run(self, coro: Any) -> Any:
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

    # -------------------------------------------------------------------------
    # Entity management
    # -------------------------------------------------------------------------

    def create_entity(
        self,
        entity_id: str,
        name: Optional[str] = None,
        parent_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> Entity:
        """Create a new entity."""
        return self._run(
            self._limiter.create_entity(
                entity_id=entity_id,
                name=name,
                parent_id=parent_id,
                metadata=metadata,
            )
        )

    def get_entity(self, entity_id: str) -> Optional[Entity]:
        """Get an entity by ID."""
        return self._run(self._limiter.get_entity(entity_id))

    def delete_entity(self, entity_id: str) -> None:
        """Delete an entity and all its related data."""
        self._run(self._limiter.delete_entity(entity_id))

    def get_children(self, parent_id: str) -> list[Entity]:
        """Get all children of a parent entity."""
        return self._run(self._limiter.get_children(parent_id))

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
        failure_mode: Optional[FailureMode] = None,
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
                failure_mode=failure_mode,
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
    ) -> None:
        """Store limit configs for an entity."""
        self._run(self._limiter.set_limits(entity_id, limits, resource))

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
    ) -> None:
        """Delete stored limit configs for an entity."""
        self._run(self._limiter.delete_limits(entity_id, resource))

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
    # Table management
    # -------------------------------------------------------------------------

    def create_table(self) -> None:
        """Create the DynamoDB table if it doesn't exist."""
        self._run(self._limiter.create_table())

    def delete_table(self) -> None:
        """Delete the DynamoDB table."""
        self._run(self._limiter.delete_table())
