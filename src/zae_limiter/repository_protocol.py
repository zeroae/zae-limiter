"""Repository protocol for rate limiter data backends.

This module defines the RepositoryProtocol that all storage backends must implement.
The protocol uses Python's typing.Protocol with @runtime_checkable decorator,
enabling duck typing and isinstance() checks at runtime.

See ADR-108 for design rationale and ADR-109 for capability matrix.
"""

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .models import (
        AuditEvent,
        BackendCapabilities,
        BucketState,
        Entity,
        Limit,
        StackOptions,
        UsageSnapshot,
        UsageSummary,
    )


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
        """CloudFormation stack name (with ZAEL- prefix)."""
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
    # Infrastructure management (DynamoDB-specific)
    # -------------------------------------------------------------------------

    async def create_stack(
        self,
        stack_options: "StackOptions | None" = None,
    ) -> None:
        """
        Create backend infrastructure (DynamoDB-specific).

        For DynamoDB: Creates CloudFormation stack with table, Lambda, etc.
        For other backends: May be a no-op or create required resources.

        Args:
            stack_options: Configuration for infrastructure creation
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
        metadata: dict[str, str] | None = None,
        principal: str | None = None,
    ) -> "Entity":
        """
        Create a new entity.

        Args:
            entity_id: Unique identifier for the entity
            name: Human-readable name (defaults to entity_id)
            parent_id: Parent entity ID for hierarchical limits
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

    # NOTE: batch_get_buckets() is NOT part of the protocol.
    # It is an optional optimization method available when
    # capabilities.supports_batch_operations is True.
    # See ADR-108 section 3 for rationale.

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

    async def transact_write(self, items: list[dict[str, Any]]) -> None:
        """
        Execute a transactional write of multiple items.

        All items succeed or fail together (atomic).

        Args:
            items: List of transaction items from build_bucket_put_item

        Raises:
            TransactionCanceledException: If transaction fails
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
        on_unavailable: str | None = None,
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

    async def get_system_defaults(self) -> "tuple[list[Limit], str | None]":
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
