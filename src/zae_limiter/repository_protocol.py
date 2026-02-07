"""Repository protocol for rate limiter data backends.

This module defines the RepositoryProtocol that all storage backends must implement.
The protocol uses Python's typing.Protocol with @runtime_checkable decorator,
enabling duck typing and isinstance() checks at runtime.

See ADR-108 for design rationale and ADR-109 for capability matrix.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .models import (
        AuditEvent,
        BackendCapabilities,
        BucketState,
        Entity,
        Limit,
        OnUnavailableAction,
        UsageSnapshot,
        UsageSummary,
    )


@dataclass
class SpeculativeResult:
    """Result of a speculative UpdateItem attempt.

    Attributes:
        success: True if the speculative write succeeded.
        buckets: On success, deserialized BucketStates from ALL_NEW response.
        cascade: On success, whether the entity has cascade enabled.
        parent_id: On success, the entity's parent_id (if any).
        old_buckets: On failure, deserialized BucketStates from ALL_OLD response.
            None if the bucket doesn't exist (first acquire).
    """

    success: bool
    buckets: "list[BucketState]" = field(default_factory=list)
    cascade: bool = False
    parent_id: str | None = None
    old_buckets: "list[BucketState] | None" = None


@runtime_checkable
class RepositoryProtocol(Protocol):
    """
    Protocol for rate limiter data backends.

    All storage backends (DynamoDB, Redis, SQLite, In-Memory) must implement
    this protocol to work with RateLimiter. The protocol is divided into:

    - **Properties**: Backend identification and configuration
    - **Lifecycle**: Connection management
    - **Entity operations**: CRUD for rate-limited entities
    - **Bucket operations**: Token bucket state management
    - **Limit config**: Stored limit configurations at entity/resource/system level
    - **Version management**: Schema and Lambda version tracking
    - **Audit logging**: Security audit trail
    - **Usage snapshots**: Historical consumption tracking

    Example:
        # Custom backend implementation
        class MyBackend:
            @property
            def region(self) -> str | None:
                return "us-east-1"

            async def get_entity(self, entity_id: str) -> Entity | None:
                ...

        # Duck typing - no inheritance needed
        repo = MyBackend()
        assert isinstance(repo, RepositoryProtocol)  # True at runtime
    """

    # -------------------------------------------------------------------------
    # Properties
    # -------------------------------------------------------------------------

    @property
    def region(self) -> str | None:
        """AWS region (or None for local/in-memory backends)."""
        ...

    @property
    def endpoint_url(self) -> str | None:
        """Custom endpoint URL (e.g., LocalStack, local DynamoDB)."""
        ...

    @property
    def stack_name(self) -> str:
        """CloudFormation stack name."""
        ...

    @property
    def table_name(self) -> str:
        """DynamoDB table name (same as stack_name)."""
        ...

    @property
    def capabilities(self) -> "BackendCapabilities":
        """
        Declare which extended features this backend supports.

        Returns:
            BackendCapabilities instance with feature flags.

        Example:
            if repo.capabilities.supports_audit_logging:
                events = await repo.get_audit_events(entity_id)
        """
        ...

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    async def close(self) -> None:
        """
        Close the backend connection and release resources.

        Must be called when the repository is no longer needed.
        Safe to call multiple times.
        """
        ...

    async def ping(self) -> bool:
        """
        Check if the backend is reachable.

        Returns:
            True if the backend is accessible, False otherwise.
        """
        ...

    # -------------------------------------------------------------------------
    # Infrastructure management
    # -------------------------------------------------------------------------

    async def ensure_infrastructure(self) -> None:
        """
        Ensure backend infrastructure exists.

        For DynamoDB: Creates CloudFormation stack if stack_options was provided
        to the constructor.
        For other backends: May create required resources or be a no-op.

        Uses the options passed to the constructor. No-op if not provided.
        """
        ...

    # -------------------------------------------------------------------------
    # Entity operations
    # -------------------------------------------------------------------------

    async def create_entity(
        self,
        entity_id: str,
        name: str | None = None,
        parent_id: str | None = None,
        cascade: bool = False,
        metadata: dict[str, str] | None = None,
        principal: str | None = None,
    ) -> "Entity":
        """
        Create a new entity.

        Args:
            entity_id: Unique identifier for the entity
            name: Human-readable name (defaults to entity_id)
            parent_id: Parent entity ID for hierarchical limits
            cascade: If True, acquire() will also consume from parent entity
            metadata: Additional key-value metadata
            principal: Caller identity for audit logging

        Returns:
            The created Entity

        Raises:
            EntityExistsError: If entity already exists
        """
        ...

    async def get_entity(self, entity_id: str) -> "Entity | None":
        """
        Get an entity by ID.

        Args:
            entity_id: Entity identifier

        Returns:
            Entity if found, None otherwise
        """
        ...

    async def delete_entity(
        self,
        entity_id: str,
        principal: str | None = None,
    ) -> None:
        """
        Delete an entity and all related records.

        Args:
            entity_id: Entity to delete
            principal: Caller identity for audit logging
        """
        ...

    async def get_children(self, parent_id: str) -> "list[Entity]":
        """
        Get all child entities of a parent.

        Args:
            parent_id: Parent entity ID

        Returns:
            List of child entities
        """
        ...

    # -------------------------------------------------------------------------
    # Bucket operations
    # -------------------------------------------------------------------------

    async def get_bucket(
        self,
        entity_id: str,
        resource: str,
        limit_name: str,
    ) -> "BucketState | None":
        """
        Get a token bucket by entity/resource/limit.

        Args:
            entity_id: Entity owning the bucket
            resource: Resource name (e.g., "gpt-4")
            limit_name: Limit name (e.g., "tpm", "rpm")

        Returns:
            BucketState if found, None otherwise
        """
        ...

    async def get_buckets(
        self,
        entity_id: str,
        resource: str,
    ) -> "list[BucketState]":
        """
        Get all token buckets for an entity/resource pair.

        Args:
            entity_id: Entity owning the buckets
            resource: Resource name (e.g., "gpt-4")

        Returns:
            List of bucket states for all limits on this resource
        """
        ...

    async def get_or_create_bucket(
        self,
        entity_id: str,
        resource: str,
        limit: "Limit",
    ) -> "BucketState":
        """
        Get an existing bucket or create a new one with the given limit.

        This is the primary method for initializing token buckets. If a bucket
        exists, it is returned. If not, a new bucket is created with capacity
        set to the limit's capacity.

        Args:
            entity_id: Entity owning the bucket
            resource: Resource name (e.g., "gpt-4")
            limit: Limit configuration for the bucket

        Returns:
            Existing or newly created BucketState
        """
        ...

    # TODO(#260): Move batch_get methods to a capability-gated protocol.
    # Review all batch_get call sites for proper capability checks.

    async def batch_get_configs(
        self,
        keys: list[tuple[str, str]],
    ) -> "dict[tuple[str, str], tuple[list[Limit], OnUnavailableAction | None]]":
        """
        Batch get config items in a single DynamoDB call.

        Fetches config records (entity, resource, system level) in a single
        BatchGetItem request and returns deserialized limits.

        Args:
            keys: List of (PK, SK) tuples identifying config items

        Returns:
            Dict mapping (PK, SK) to (limits, on_unavailable) tuples.
            on_unavailable is extracted from system config items (None for others).
            Missing items are not included in the result.
        """
        ...

    async def batch_get_buckets(
        self,
        keys: list[tuple[str, str]],
    ) -> dict[tuple[str, str, str], "BucketState"]:
        """
        Batch get composite buckets in a single call.

        Args:
            keys: List of (entity_id, resource) tuples

        Returns:
            Dict mapping (entity_id, resource, limit_name) to BucketState.
        """
        ...

    async def batch_get_entity_and_buckets(
        self,
        entity_id: str,
        bucket_keys: list[tuple[str, str]],
    ) -> tuple["Entity | None", dict[tuple[str, str, str], "BucketState"]]:
        """
        Fetch entity metadata and composite buckets in a single call.

        Args:
            entity_id: Entity whose metadata to include
            bucket_keys: List of (entity_id, resource) for composite buckets

        Returns:
            Tuple of (entity_or_none, bucket_dict).
        """
        ...

    async def get_resource_buckets(
        self,
        resource: str,
        limit_name: str | None = None,
    ) -> "list[BucketState]":
        """
        Get all buckets for a resource across all entities.

        Used for capacity reporting and aggregation.

        Args:
            resource: Resource name
            limit_name: Optional filter by limit name

        Returns:
            List of bucket states
        """
        ...

    def build_bucket_put_item(
        self,
        state: "BucketState",
        ttl_seconds: int = 86400,
    ) -> dict[str, Any]:
        """
        Build a transaction item for upserting a bucket.

        This is a synchronous method used to build transaction payloads.

        Args:
            state: Bucket state to persist
            ttl_seconds: Time-to-live for the record

        Returns:
            Transaction item dict for use with transact_write
        """
        ...

    def build_composite_create(
        self,
        entity_id: str,
        resource: str,
        states: "list[BucketState]",
        now_ms: int,
        ttl_seconds: int | None = 86400,
        cascade: bool = False,
        parent_id: str | None = None,
    ) -> dict[str, Any]:
        """Build a PutItem for creating a new composite bucket.

        Args:
            entity_id: Entity owning the bucket
            resource: Resource name
            states: BucketState objects for each limit
            now_ms: Current timestamp in milliseconds
            ttl_seconds: TTL in seconds from now, or None to omit TTL
            cascade: Whether the entity has cascade enabled
            parent_id: The entity's parent_id (if any)
        """
        ...

    def build_composite_normal(
        self,
        entity_id: str,
        resource: str,
        consumed: dict[str, int],
        refill_amounts: dict[str, int],
        now_ms: int,
        expected_rf: int,
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Build an UpdateItem for the normal write path (ADR-115 path 2).

        Args:
            entity_id: Entity owning the bucket
            resource: Resource name
            consumed: Amount consumed per limit (millitokens)
            refill_amounts: Refill amount per limit (millitokens)
            now_ms: Current timestamp in milliseconds
            expected_rf: Expected refill timestamp for optimistic lock
            ttl_seconds: TTL behavior (None=no change, 0=remove, >0=set)
        """
        ...

    def build_composite_retry(
        self,
        entity_id: str,
        resource: str,
        consumed: dict[str, int],
    ) -> dict[str, Any]:
        """Build an UpdateItem for the retry write path (ADR-115 path 3).

        Args:
            entity_id: Entity owning the bucket
            resource: Resource name
            consumed: Amount consumed per limit (millitokens)
        """
        ...

    def build_composite_adjust(
        self,
        entity_id: str,
        resource: str,
        deltas: dict[str, int],
    ) -> dict[str, Any]:
        """Build an UpdateItem for the adjust write path (ADR-115 path 4).

        Unconditional ADD for post-hoc correction. Can go negative by design.
        Positive delta = consumed more (subtract tokens, add to counter).
        Negative delta = consumed less (add tokens, subtract from counter).

        Args:
            entity_id: Entity owning the bucket
            resource: Resource name
            deltas: Delta per limit (millitokens, positive=consume, negative=release)
        """
        ...

    async def transact_write(self, items: list[dict[str, Any]]) -> None:
        """
        Execute a write of one or more items.

        Single-item batches use the corresponding single-item API (PutItem,
        UpdateItem, or DeleteItem) to halve WCU cost. Multi-item batches use
        TransactWriteItems for atomicity.

        Args:
            items: List of transaction items from build_bucket_put_item

        Raises:
            TransactionCanceledException: If multi-item transaction fails
            ConditionalCheckFailedException: If single-item condition fails
        """
        ...

    async def write_each(self, items: list[dict[str, Any]]) -> None:
        """
        Write items independently without cross-item atomicity.

        Each item is dispatched as a single PutItem, UpdateItem, or DeleteItem
        call (1 WCU each). Use for unconditional writes (e.g., ADD adjustments)
        where partial success is acceptable.

        Args:
            items: List of items to write independently
        """
        ...

    async def speculative_consume(
        self,
        entity_id: str,
        resource: str,
        consume: dict[str, int],
        ttl_seconds: int | None = None,
    ) -> SpeculativeResult:
        """Attempt speculative UpdateItem with condition check.

        Issues an UpdateItem with ADD -consumed and condition
        ``attribute_exists(PK) AND tk >= consumed`` for each limit.
        Uses ``ReturnValuesOnConditionCheckFailure=ALL_OLD`` to return
        the current item state on failure.

        Args:
            entity_id: Entity owning the bucket
            resource: Resource name
            consume: Amount per limit (tokens, not milli)
            ttl_seconds: TTL in seconds from now, or None for no TTL change

        Returns:
            SpeculativeResult with success flag and either:
            - On success: buckets, cascade, parent_id from ALL_NEW
            - On failure with ALL_OLD: old_buckets from ALL_OLD
            - On failure without ALL_OLD: old_buckets is None (bucket missing)
        """
        ...

    # -------------------------------------------------------------------------
    # Limit config operations
    # -------------------------------------------------------------------------

    async def set_limits(
        self,
        entity_id: str,
        limits: "list[Limit]",
        resource: str = "_default_",
        principal: str | None = None,
    ) -> None:
        """
        Store limit configs for an entity.

        Args:
            entity_id: Entity to configure
            limits: Limit configurations
            resource: Resource these limits apply to
            principal: Caller identity for audit logging
        """
        ...

    async def get_limits(
        self,
        entity_id: str,
        resource: str = "_default_",
    ) -> "list[Limit]":
        """
        Get stored limit configs for an entity.

        Args:
            entity_id: Entity to query
            resource: Resource to get limits for

        Returns:
            List of limits (empty if none configured)
        """
        ...

    async def delete_limits(
        self,
        entity_id: str,
        resource: str = "_default_",
        principal: str | None = None,
    ) -> None:
        """
        Delete stored limit configs for an entity.

        Args:
            entity_id: Entity to delete limits for
            resource: Resource to delete limits for
            principal: Caller identity for audit logging
        """
        ...

    async def list_entities_with_custom_limits(
        self,
        resource: str,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> tuple[list[str], str | None]:
        """
        List all entities that have custom limit configurations for a resource.

        Uses GSI3 sparse index for efficient queries.

        Args:
            resource: Resource to filter by (required).
            limit: Maximum number of entities to return. None for all.
            cursor: Pagination cursor from previous call. None for first page.

        Returns:
            Tuple of (entity_ids, next_cursor). next_cursor is None if no more results.
        """
        ...

    async def list_resources_with_entity_configs(self) -> list[str]:
        """
        List all resources that have entity-level custom limit configurations.

        Uses the entity config resources registry for efficient O(1) lookup.

        Returns:
            Sorted list of resource names with at least one entity having custom limits
        """
        ...

    # -------------------------------------------------------------------------
    # Resource-level defaults
    # -------------------------------------------------------------------------

    async def set_resource_defaults(
        self,
        resource: str,
        limits: "list[Limit]",
        principal: str | None = None,
    ) -> None:
        """
        Store default limits for a resource.

        Resource defaults apply to all entities accessing this resource
        unless overridden at the entity level.

        Args:
            resource: Resource name
            limits: Default limits
            principal: Caller identity for audit logging
        """
        ...

    async def get_resource_defaults(self, resource: str) -> "list[Limit]":
        """
        Get default limits for a resource.

        Args:
            resource: Resource name

        Returns:
            List of limits (empty if none configured)
        """
        ...

    async def delete_resource_defaults(
        self,
        resource: str,
        principal: str | None = None,
    ) -> None:
        """
        Delete default limits for a resource.

        Args:
            resource: Resource name
            principal: Caller identity for audit logging
        """
        ...

    async def list_resources_with_defaults(self) -> list[str]:
        """
        List all resources that have default limits configured.

        Returns:
            List of resource names
        """
        ...

    # -------------------------------------------------------------------------
    # System-level defaults
    # -------------------------------------------------------------------------

    async def set_system_defaults(
        self,
        limits: "list[Limit]",
        on_unavailable: "OnUnavailableAction | None" = None,
        principal: str | None = None,
    ) -> None:
        """
        Store system-wide default limits and config.

        System defaults apply to ALL resources unless overridden.

        Args:
            limits: Global default limits
            on_unavailable: Behavior when backend unavailable ("allow" or "block")
            principal: Caller identity for audit logging
        """
        ...

    async def get_system_defaults(self) -> "tuple[list[Limit], OnUnavailableAction | None]":
        """
        Get system-wide default limits and config.

        Returns:
            Tuple of (limits, on_unavailable). on_unavailable may be None.
        """
        ...

    async def delete_system_defaults(
        self,
        principal: str | None = None,
    ) -> None:
        """
        Delete all system-wide defaults.

        Args:
            principal: Caller identity for audit logging
        """
        ...

    # -------------------------------------------------------------------------
    # Version management
    # -------------------------------------------------------------------------

    async def get_version_record(self) -> dict[str, Any] | None:
        """
        Get the infrastructure version record.

        Returns:
            Version record with schema_version, lambda_version, etc.
            None if no version record exists.
        """
        ...

    async def set_version_record(
        self,
        schema_version: str,
        lambda_version: str | None = None,
        client_min_version: str = "0.0.0",
        updated_by: str | None = None,
    ) -> None:
        """
        Set the infrastructure version record.

        Args:
            schema_version: Current schema version (e.g., "1.0.0")
            lambda_version: Currently deployed Lambda version
            client_min_version: Minimum compatible client version
            updated_by: Identifier of what performed the update
        """
        ...

    # -------------------------------------------------------------------------
    # Audit logging
    # -------------------------------------------------------------------------

    async def get_audit_events(
        self,
        entity_id: str,
        limit: int = 100,
        start_event_id: str | None = None,
    ) -> "list[AuditEvent]":
        """
        Get audit events for an entity.

        Args:
            entity_id: Entity to query
            limit: Maximum events to return
            start_event_id: Pagination cursor (event ID to start after)

        Returns:
            List of audit events, most recent first
        """
        ...

    # -------------------------------------------------------------------------
    # Usage snapshots
    # -------------------------------------------------------------------------

    async def get_usage_snapshots(
        self,
        entity_id: str | None = None,
        resource: str | None = None,
        window_type: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        limit: int = 100,
        next_key: dict[str, Any] | None = None,
    ) -> "tuple[list[UsageSnapshot], dict[str, Any] | None]":
        """
        Query usage snapshots with filtering and pagination.

        Args:
            entity_id: Entity to query (uses primary key)
            resource: Resource filter (required if entity_id is None)
            window_type: Filter by "hourly" or "daily"
            start_time: Filter snapshots >= this timestamp (ISO format)
            end_time: Filter snapshots <= this timestamp (ISO format)
            limit: Maximum items per page
            next_key: Pagination cursor from previous call

        Returns:
            Tuple of (snapshots, next_key). next_key is None if no more results.

        Raises:
            ValueError: If neither entity_id nor resource is provided
        """
        ...

    async def get_usage_summary(
        self,
        entity_id: str | None = None,
        resource: str | None = None,
        window_type: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> "UsageSummary":
        """
        Aggregate usage across snapshots into a summary.

        Args:
            entity_id: Entity to query
            resource: Resource filter
            window_type: Filter by "hourly" or "daily"
            start_time: Filter snapshots >= this timestamp
            end_time: Filter snapshots <= this timestamp

        Returns:
            UsageSummary with total, average, and time range

        Raises:
            ValueError: If neither entity_id nor resource is provided
        """
        ...
