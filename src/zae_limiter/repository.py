"""DynamoDB repository for rate limiter data."""

import time
from typing import TYPE_CHECKING, Any, cast

import aioboto3
from botocore.exceptions import ClientError
from ulid import ULID

from . import schema
from .exceptions import EntityExistsError
from .models import (
    AuditAction,
    AuditEvent,
    BackendCapabilities,
    BucketState,
    Entity,
    Limit,
    StackOptions,
    UsageSnapshot,
    UsageSummary,
    validate_identifier,
    validate_name,
)
from .naming import normalize_stack_name


class Repository:
    """
    Async DynamoDB repository for rate limiter data.

    Handles all DynamoDB operations including entities, buckets,
    limit configs, and transactions.

    Args:
        name: Resource identifier (e.g., "my-app"). Automatically prefixed
            with 'ZAEL-' to form stack_name and table_name.
        region: AWS region (e.g., "us-east-1").
        endpoint_url: Custom endpoint URL (e.g., LocalStack).
        stack_options: Configuration for CloudFormation infrastructure.
            Pass StackOptions to enable declarative infrastructure management.

    Example:
        # Basic usage
        repo = Repository(name="my-app", region="us-east-1")

        # With infrastructure management (ADR-108)
        repo = Repository(
            name="my-app",
            region="us-east-1",
            stack_options=StackOptions(lambda_memory=512, enable_alarms=True),
        )
    """

    def __init__(
        self,
        name: str,
        region: str | None = None,
        endpoint_url: str | None = None,
        stack_options: StackOptions | None = None,
    ) -> None:
        # Validate and normalize name (adds ZAEL- prefix)
        self.stack_name = normalize_stack_name(name)
        # Table name is always identical to stack name
        self.table_name = self.stack_name
        self.region = region
        self.endpoint_url = endpoint_url
        self._stack_options = stack_options
        self._session: aioboto3.Session | None = None
        self._client: Any = None
        self._caller_identity_arn: str | None = None
        self._caller_identity_fetched = False

        # DynamoDB supports all extended features
        self._capabilities = BackendCapabilities(
            supports_audit_logging=True,
            supports_usage_snapshots=True,
            supports_infrastructure_management=True,
            supports_change_streams=True,
            supports_batch_operations=True,
        )

    @property
    def capabilities(self) -> BackendCapabilities:
        """Declare which extended features this backend supports."""
        return self._capabilities

    async def _get_client(self) -> Any:
        """Get or create the DynamoDB client."""
        if self._client is None:
            self._session = aioboto3.Session()
            self._client = await self._session.client(
                "dynamodb",
                region_name=self.region,
                endpoint_url=self.endpoint_url,
            ).__aenter__()
        return self._client

    async def close(self) -> None:
        """Close the DynamoDB client."""
        if self._client is not None:
            await self._client.__aexit__(None, None, None)
            self._client = None
            self._session = None

    async def _get_caller_identity_arn(self) -> str | None:
        """
        Get the ARN of the AWS caller identity (lazy cached).

        Returns the full ARN of the IAM user/role making API calls.
        Returns None if the identity cannot be determined (e.g., local testing).
        """
        if self._caller_identity_fetched:
            return self._caller_identity_arn

        self._caller_identity_fetched = True
        try:
            if self._session is None:
                self._session = aioboto3.Session()

            async with self._session.client(
                "sts",
                region_name=self.region,
                endpoint_url=self.endpoint_url,
            ) as sts_client:
                response = await sts_client.get_caller_identity()
                self._caller_identity_arn = response.get("Arn")
        except Exception:
            # Silently fail - caller identity is optional
            self._caller_identity_arn = None

        return self._caller_identity_arn

    def _now_ms(self) -> int:
        """Current time in milliseconds."""
        return int(time.time() * 1000)

    # -------------------------------------------------------------------------
    # Table operations
    # -------------------------------------------------------------------------

    async def create_table(self) -> None:
        """Create the DynamoDB table if it doesn't exist."""
        client = await self._get_client()
        definition = schema.get_table_definition(self.table_name)

        try:
            await client.create_table(**definition)
            # Wait for table to be active
            waiter = client.get_waiter("table_exists")
            await waiter.wait(TableName=self.table_name)
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceInUseException":
                raise

    async def delete_table(self) -> None:
        """Delete the DynamoDB table."""
        client = await self._get_client()
        try:
            await client.delete_table(TableName=self.table_name)
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceNotFoundException":
                raise

    async def ensure_infrastructure(self) -> None:
        """
        Ensure DynamoDB infrastructure exists.

        Creates CloudFormation stack using stack_options passed to the constructor.
        No-op if stack_options was not provided.

        Raises:
            StackCreationError: If CloudFormation stack creation fails
        """
        if self._stack_options is None:
            return

        from .infra.stack_manager import StackManager

        async with StackManager(self.stack_name, self.region, self.endpoint_url) as manager:
            await manager.create_stack(stack_options=self._stack_options)

    async def create_stack(
        self,
        stack_options: StackOptions | None = None,
    ) -> None:
        """
        Create DynamoDB infrastructure via CloudFormation.

        .. deprecated:: 0.6.0
            Use :meth:`ensure_infrastructure` instead. Pass stack_options
            to the Repository constructor. Will be removed in v2.0.0.

        Args:
            stack_options: Configuration for CloudFormation stack.
                If None, uses the stack_options passed to the constructor.

        Raises:
            StackCreationError: If CloudFormation stack creation fails
        """
        import warnings

        warnings.warn(
            "create_stack() is deprecated. Use ensure_infrastructure() instead. "
            "Pass stack_options to the Repository constructor. "
            "This will be removed in v2.0.0.",
            DeprecationWarning,
            stacklevel=2,
        )

        if stack_options is not None:
            # Temporarily override stack_options for this call
            saved = self._stack_options
            self._stack_options = stack_options
            try:
                await self.ensure_infrastructure()
            finally:
                self._stack_options = saved
        else:
            await self.ensure_infrastructure()

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
    ) -> Entity:
        """
        Create a new entity.

        Args:
            entity_id: Unique identifier for the entity
            name: Optional display name (defaults to entity_id)
            parent_id: Optional parent entity ID (for hierarchical limits)
            metadata: Optional key-value metadata
            principal: Caller identity for audit logging

        Returns:
            The created Entity

        Raises:
            InvalidIdentifierError: If entity_id or parent_id is invalid
            EntityExistsError: If entity already exists
        """
        # Validate inputs at API boundary
        validate_identifier(entity_id, "entity_id")
        if parent_id is not None:
            validate_identifier(parent_id, "parent_id")

        client = await self._get_client()
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        item: dict[str, Any] = {
            "PK": {"S": schema.pk_entity(entity_id)},
            "SK": {"S": schema.sk_meta()},
            "entity_id": {"S": entity_id},
            "name": {"S": name or entity_id},
            "parent_id": {"S": parent_id} if parent_id else {"NULL": True},
            "metadata": {"M": self._serialize_map(metadata or {})},
            "created_at": {"S": now},
        }

        # Add GSI1 keys for parent lookup if this is a child
        if parent_id:
            item["GSI1PK"] = {"S": schema.gsi1_pk_parent(parent_id)}
            item["GSI1SK"] = {"S": schema.gsi1_sk_child(entity_id)}

        try:
            await client.put_item(
                TableName=self.table_name,
                Item=item,
                ConditionExpression="attribute_not_exists(PK)",
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise EntityExistsError(entity_id)
            raise

        # Log audit event
        await self._log_audit_event(
            action=AuditAction.ENTITY_CREATED,
            entity_id=entity_id,
            principal=principal,
            details={
                "name": name or entity_id,
                "parent_id": parent_id,
                "metadata": metadata or {},
            },
        )

        return Entity(
            id=entity_id,
            name=name or entity_id,
            parent_id=parent_id,
            metadata=metadata or {},
            created_at=now,
        )

    async def get_entity(self, entity_id: str) -> Entity | None:
        """Get an entity by ID."""
        client = await self._get_client()

        response = await client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_entity(entity_id)},
                "SK": {"S": schema.sk_meta()},
            },
        )

        item = response.get("Item")
        if not item:
            return None

        return self._deserialize_entity(item)

    async def delete_entity(
        self,
        entity_id: str,
        principal: str | None = None,
    ) -> None:
        """
        Delete an entity and all its related records.

        Args:
            entity_id: ID of the entity to delete
            principal: Caller identity for audit logging
        """
        client = await self._get_client()

        # First, query all items for this entity
        response = await client.query(
            TableName=self.table_name,
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": {"S": schema.pk_entity(entity_id)}},
        )

        # Delete all items in batches
        items = response.get("Items", [])
        if not items:
            return

        # Build delete requests
        delete_requests = [
            {"DeleteRequest": {"Key": {"PK": item["PK"], "SK": item["SK"]}}} for item in items
        ]

        # BatchWriteItem in chunks of 25
        for i in range(0, len(delete_requests), 25):
            chunk = delete_requests[i : i + 25]
            await client.batch_write_item(RequestItems={self.table_name: chunk})

        # Log audit event
        await self._log_audit_event(
            action=AuditAction.ENTITY_DELETED,
            entity_id=entity_id,
            principal=principal,
            details={"records_deleted": len(items)},
        )

    async def get_children(self, parent_id: str) -> list[Entity]:
        """Get all children of a parent entity."""
        client = await self._get_client()

        response = await client.query(
            TableName=self.table_name,
            IndexName=schema.GSI1_NAME,
            KeyConditionExpression="GSI1PK = :pk",
            ExpressionAttributeValues={":pk": {"S": schema.gsi1_pk_parent(parent_id)}},
        )

        entities = []
        for item in response.get("Items", []):
            entity = self._deserialize_entity(item)
            if entity:
                entities.append(entity)

        return entities

    # -------------------------------------------------------------------------
    # Bucket operations
    # -------------------------------------------------------------------------

    async def get_bucket(
        self,
        entity_id: str,
        resource: str,
        limit_name: str,
    ) -> BucketState | None:
        """Get a bucket by entity/resource/limit."""
        client = await self._get_client()

        response = await client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_entity(entity_id)},
                "SK": {"S": schema.sk_bucket(resource, limit_name)},
            },
        )

        item = response.get("Item")
        if not item:
            return None

        return self._deserialize_bucket(item)

    async def get_buckets(
        self,
        entity_id: str,
        resource: str | None = None,
    ) -> list[BucketState]:
        """Get all buckets for an entity, optionally filtered by resource."""
        client = await self._get_client()

        key_condition = "PK = :pk AND begins_with(SK, :sk_prefix)"
        expression_values: dict[str, Any] = {
            ":pk": {"S": schema.pk_entity(entity_id)},
            ":sk_prefix": {"S": schema.SK_BUCKET + (f"{resource}#" if resource else "")},
        }

        response = await client.query(
            TableName=self.table_name,
            KeyConditionExpression=key_condition,
            ExpressionAttributeValues=expression_values,
        )

        return [self._deserialize_bucket(item) for item in response.get("Items", [])]

    async def batch_get_buckets(
        self,
        keys: list[tuple[str, str, str]],
    ) -> dict[tuple[str, str, str], BucketState]:
        """
        Batch get multiple buckets in a single DynamoDB call.

        Uses BatchGetItem to reduce round trips when fetching buckets for
        multiple entity/resource/limit combinations (e.g., cascade scenarios).

        Args:
            keys: List of (entity_id, resource, limit_name) tuples

        Returns:
            Dict mapping (entity_id, resource, limit_name) to BucketState.
            Missing buckets are not included in the result.

        Note:
            DynamoDB BatchGetItem supports up to 100 items per request.
            For larger batches, this method automatically chunks the requests.
        """
        if not keys:
            return {}

        client = await self._get_client()
        result: dict[tuple[str, str, str], BucketState] = {}

        # Build request keys (deduplicate)
        unique_keys = list(set(keys))

        # BatchGetItem supports max 100 items per request
        for i in range(0, len(unique_keys), 100):
            chunk = unique_keys[i : i + 100]

            request_keys = [
                {
                    "PK": {"S": schema.pk_entity(entity_id)},
                    "SK": {"S": schema.sk_bucket(resource, limit_name)},
                }
                for entity_id, resource, limit_name in chunk
            ]

            response = await client.batch_get_item(
                RequestItems={
                    self.table_name: {
                        "Keys": request_keys,
                    }
                }
            )

            # Process responses
            items = response.get("Responses", {}).get(self.table_name, [])
            for item in items:
                bucket = self._deserialize_bucket(item)
                key = (bucket.entity_id, bucket.resource, bucket.limit_name)
                result[key] = bucket

            # Handle unprocessed keys (retry with exponential backoff if needed)
            # For simplicity, we don't retry here - the caller can handle missing keys
            # In production, consider adding retry logic for UnprocessedKeys

        return result

    async def get_or_create_bucket(
        self,
        entity_id: str,
        resource: str,
        limit: Limit,
    ) -> BucketState:
        """
        Get an existing bucket or create a new one with the given limit.

        If the bucket exists, it is returned. If not, a new bucket is created
        with capacity set to the limit's capacity.

        Args:
            entity_id: Entity owning the bucket
            resource: Resource name (e.g., "gpt-4")
            limit: Limit configuration for the bucket

        Returns:
            Existing or newly created BucketState
        """
        existing = await self.get_bucket(entity_id, resource, limit.name)
        if existing is not None:
            return existing

        # Create new bucket at full capacity
        now_ms = self._now_ms()
        state = BucketState(
            entity_id=entity_id,
            resource=resource,
            limit_name=limit.name,
            tokens_milli=limit.capacity * 1000,
            last_refill_ms=now_ms,
            capacity_milli=limit.capacity * 1000,
            burst_milli=limit.burst * 1000,
            refill_amount_milli=limit.refill_amount * 1000,
            refill_period_ms=limit.refill_period_seconds * 1000,
            total_consumed_milli=0,
        )

        # Write bucket to DynamoDB
        put_item = self.build_bucket_put_item(state)
        await self.transact_write([put_item])

        return state

    def build_bucket_put_item(
        self,
        state: BucketState,
        ttl_seconds: int = 86400,
    ) -> dict[str, Any]:
        """Build a PutItem for a bucket (for use in transactions)."""
        now_ms = self._now_ms()
        item: dict[str, Any] = {
            "PK": {"S": schema.pk_entity(state.entity_id)},
            "SK": {"S": schema.sk_bucket(state.resource, state.limit_name)},
            "entity_id": {"S": state.entity_id},
            "resource": {"S": state.resource},
            "limit_name": {"S": state.limit_name},
            "tokens_milli": {"N": str(state.tokens_milli)},
            "last_refill_ms": {"N": str(state.last_refill_ms)},
            "capacity_milli": {"N": str(state.capacity_milli)},
            "burst_milli": {"N": str(state.burst_milli)},
            "refill_amount_milli": {"N": str(state.refill_amount_milli)},
            "refill_period_ms": {"N": str(state.refill_period_ms)},
            "GSI2PK": {"S": schema.gsi2_pk_resource(state.resource)},
            "GSI2SK": {"S": schema.gsi2_sk_bucket(state.entity_id, state.limit_name)},
            "ttl": {"N": str(schema.calculate_ttl(now_ms, ttl_seconds))},
        }
        if state.total_consumed_milli is not None:
            item["total_consumed_milli"] = {"N": str(state.total_consumed_milli)}
        return {"Put": {"TableName": self.table_name, "Item": item}}

    def build_bucket_update_item(
        self,
        entity_id: str,
        resource: str,
        limit_name: str,
        new_tokens_milli: int,
        new_last_refill_ms: int,
        expected_tokens_milli: int | None = None,
    ) -> dict[str, Any]:
        """Build an UpdateItem for a bucket (for use in transactions)."""
        update: dict[str, dict[str, Any]] = {
            "Update": {
                "TableName": self.table_name,
                "Key": {
                    "PK": {"S": schema.pk_entity(entity_id)},
                    "SK": {"S": schema.sk_bucket(resource, limit_name)},
                },
                "UpdateExpression": "SET #tokens = :tokens, #refill = :refill",
                "ExpressionAttributeNames": {
                    "#tokens": "tokens_milli",
                    "#refill": "last_refill_ms",
                },
                "ExpressionAttributeValues": {
                    ":tokens": {"N": str(new_tokens_milli)},
                    ":refill": {"N": str(new_last_refill_ms)},
                },
            }
        }

        # Add optimistic locking condition if provided
        if expected_tokens_milli is not None:
            update["Update"]["ConditionExpression"] = "#tokens = :expected"
            update["Update"]["ExpressionAttributeValues"][":expected"] = {
                "N": str(expected_tokens_milli)
            }

        return update

    async def transact_write(self, items: list[dict[str, Any]]) -> None:
        """Execute a transactional write."""
        if not items:
            return

        client = await self._get_client()
        await client.transact_write_items(TransactItems=items)

    # -------------------------------------------------------------------------
    # Limit config operations
    # -------------------------------------------------------------------------

    async def set_limits(
        self,
        entity_id: str,
        limits: list[Limit],
        resource: str = schema.DEFAULT_RESOURCE,
        principal: str | None = None,
    ) -> None:
        """
        Store limit configs for an entity.

        Args:
            entity_id: ID of the entity
            limits: List of Limit configurations to store
            resource: Resource name (defaults to "_default_")
            principal: Caller identity for audit logging
        """
        client = await self._get_client()

        # Delete existing limits for this resource first
        await self._delete_limits_for_resource(entity_id, resource)

        # Write new limits (flat schema)
        for limit in limits:
            item = {
                "PK": {"S": schema.pk_entity(entity_id)},
                "SK": {"S": schema.sk_limit(resource, limit.name)},
                "entity_id": {"S": entity_id},
                "resource": {"S": resource},
                "limit_name": {"S": limit.name},
                "capacity": {"N": str(limit.capacity)},
                "burst": {"N": str(limit.burst)},
                "refill_amount": {"N": str(limit.refill_amount)},
                "refill_period_seconds": {"N": str(limit.refill_period_seconds)},
            }
            await client.put_item(TableName=self.table_name, Item=item)

        # Log audit event
        await self._log_audit_event(
            action=AuditAction.LIMITS_SET,
            entity_id=entity_id,
            principal=principal,
            resource=resource,
            details={"limits": [limit.to_dict() for limit in limits]},
        )

    async def get_limits(
        self,
        entity_id: str,
        resource: str = schema.DEFAULT_RESOURCE,
    ) -> list[Limit]:
        """Get stored limit configs for an entity."""
        client = await self._get_client()

        # ADR-105: Use eventually consistent reads for config (0.5 RCU vs 1 RCU)
        response = await client.query(
            TableName=self.table_name,
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk_prefix)",
            ExpressionAttributeValues={
                ":pk": {"S": schema.pk_entity(entity_id)},
                ":sk_prefix": {"S": schema.sk_limit_prefix(resource)},
            },
            ConsistentRead=False,
        )

        limits = []
        for item in response.get("Items", []):
            # Flat format: capacity at top level
            if "capacity" in item and "N" in item.get("capacity", {}):
                limits.append(
                    Limit(
                        name=item.get("limit_name", {}).get("S", ""),
                        capacity=int(item["capacity"]["N"]),
                        burst=int(item.get("burst", {}).get("N", "0")),
                        refill_amount=int(item.get("refill_amount", {}).get("N", "0")),
                        refill_period_seconds=int(
                            item.get("refill_period_seconds", {}).get("N", "0")
                        ),
                    )
                )
            else:
                # Nested format: fields inside data.M
                data = self._deserialize_map(item.get("data", {}).get("M", {}))
                limits.append(
                    Limit(
                        name=data["limit_name"],
                        capacity=int(data["capacity"]),
                        burst=int(data["burst"]),
                        refill_amount=int(data["refill_amount"]),
                        refill_period_seconds=int(data["refill_period_seconds"]),
                    )
                )

        return limits

    async def delete_limits(
        self,
        entity_id: str,
        resource: str = schema.DEFAULT_RESOURCE,
        principal: str | None = None,
    ) -> None:
        """
        Delete stored limit configs for an entity.

        Args:
            entity_id: ID of the entity
            resource: Resource name (defaults to "_default_")
            principal: Caller identity for audit logging
        """
        await self._delete_limits_for_resource(entity_id, resource)

        # Log audit event
        await self._log_audit_event(
            action=AuditAction.LIMITS_DELETED,
            entity_id=entity_id,
            principal=principal,
            resource=resource,
        )

    async def _delete_limits_for_resource(self, entity_id: str, resource: str) -> None:
        """Delete all limits for a specific resource."""
        client = await self._get_client()

        response = await client.query(
            TableName=self.table_name,
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk_prefix)",
            ExpressionAttributeValues={
                ":pk": {"S": schema.pk_entity(entity_id)},
                ":sk_prefix": {"S": schema.sk_limit_prefix(resource)},
            },
            ProjectionExpression="PK, SK",
        )

        items = response.get("Items", [])
        if not items:
            return

        delete_requests = [
            {"DeleteRequest": {"Key": {"PK": item["PK"], "SK": item["SK"]}}} for item in items
        ]

        for i in range(0, len(delete_requests), 25):
            chunk = delete_requests[i : i + 25]
            await client.batch_write_item(RequestItems={self.table_name: chunk})

    # -------------------------------------------------------------------------
    # Resource-level limit config operations (flat schema)
    # -------------------------------------------------------------------------

    async def set_resource_defaults(
        self,
        resource: str,
        limits: list[Limit],
        principal: str | None = None,
    ) -> None:
        """
        Store default limit configs for a resource (flat schema).

        Args:
            resource: Resource name
            limits: List of Limit configurations to store
            principal: Caller identity for audit logging
        """
        validate_name(resource, "resource")
        client = await self._get_client()

        # Delete existing limits for this resource
        await self._delete_resource_defaults_internal(resource)

        # Write new limits with FLAT schema (no nested data.M)
        # SK uses sk_resource_limit (no resource in SK since PK has it)
        for limit in limits:
            item = {
                "PK": {"S": schema.pk_resource(resource)},
                "SK": {"S": schema.sk_resource_limit(limit.name)},
                "resource": {"S": resource},
                "limit_name": {"S": limit.name},
                "capacity": {"N": str(limit.capacity)},
                "burst": {"N": str(limit.burst)},
                "refill_amount": {"N": str(limit.refill_amount)},
                "refill_period_seconds": {"N": str(limit.refill_period_seconds)},
                "config_version": {"N": "1"},  # Initial version for future caching
            }
            await client.put_item(TableName=self.table_name, Item=item)

        # Log audit event with special prefix
        await self._log_audit_event(
            action=AuditAction.LIMITS_SET,
            entity_id=f"$RESOURCE:{resource}",
            principal=principal,
            resource=resource,
            details={"limits": [limit.to_dict() for limit in limits]},
        )

    async def get_resource_defaults(
        self,
        resource: str,
    ) -> list[Limit]:
        """Get stored default limit configs for a resource."""
        validate_name(resource, "resource")
        client = await self._get_client()

        # ADR-105: Use eventually consistent reads for config (0.5 RCU vs 1 RCU)
        response = await client.query(
            TableName=self.table_name,
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk_prefix)",
            ExpressionAttributeValues={
                ":pk": {"S": schema.pk_resource(resource)},
                ":sk_prefix": {"S": schema.sk_resource_limit_prefix()},
            },
            ConsistentRead=False,
        )

        limits = []
        for item in response.get("Items", []):
            # Flat schema - fields are top-level
            limits.append(
                Limit(
                    name=item.get("limit_name", {}).get("S", ""),
                    capacity=int(item.get("capacity", {}).get("N", "0")),
                    burst=int(item.get("burst", {}).get("N", "0")),
                    refill_amount=int(item.get("refill_amount", {}).get("N", "0")),
                    refill_period_seconds=int(item.get("refill_period_seconds", {}).get("N", "0")),
                )
            )

        return limits

    async def delete_resource_defaults(
        self,
        resource: str,
        principal: str | None = None,
    ) -> None:
        """
        Delete stored default limit configs for a resource.

        Args:
            resource: Resource name
            principal: Caller identity for audit logging
        """
        validate_name(resource, "resource")
        await self._delete_resource_defaults_internal(resource)

        # Log audit event
        await self._log_audit_event(
            action=AuditAction.LIMITS_DELETED,
            entity_id=f"$RESOURCE:{resource}",
            principal=principal,
            resource=resource,
        )

    async def _delete_resource_defaults_internal(self, resource: str) -> None:
        """Delete all default limits for a resource (internal, no audit)."""
        client = await self._get_client()

        # Query with pagination to handle large result sets
        all_items: list[dict[str, Any]] = []
        next_key: dict[str, Any] | None = None

        while True:
            params: dict[str, Any] = {
                "TableName": self.table_name,
                "KeyConditionExpression": "PK = :pk AND begins_with(SK, :sk_prefix)",
                "ExpressionAttributeValues": {
                    ":pk": {"S": schema.pk_resource(resource)},
                    ":sk_prefix": {"S": schema.sk_resource_limit_prefix()},
                },
                "ProjectionExpression": "PK, SK",
            }
            if next_key:
                params["ExclusiveStartKey"] = next_key

            response = await client.query(**params)
            all_items.extend(response.get("Items", []))

            next_key = response.get("LastEvaluatedKey")
            if next_key is None:
                break

        if not all_items:
            return

        delete_requests = [
            {"DeleteRequest": {"Key": {"PK": item["PK"], "SK": item["SK"]}}} for item in all_items
        ]

        for i in range(0, len(delete_requests), 25):
            chunk = delete_requests[i : i + 25]
            await client.batch_write_item(RequestItems={self.table_name: chunk})

    async def list_resources_with_defaults(self) -> list[str]:
        """List all resources that have default limit configs."""
        client = await self._get_client()

        # Query for all items with PK starting with RESOURCE#
        # This requires a scan since we don't have a GSI for this pattern
        # Use pagination to handle large result sets (>1MB)
        resources: set[str] = set()
        next_key: dict[str, Any] | None = None

        while True:
            params: dict[str, Any] = {
                "TableName": self.table_name,
                "FilterExpression": "begins_with(PK, :prefix)",
                "ExpressionAttributeValues": {
                    ":prefix": {"S": schema.RESOURCE_PREFIX},
                },
                "ProjectionExpression": "PK",
            }
            if next_key:
                params["ExclusiveStartKey"] = next_key

            response = await client.scan(**params)

            # Extract unique resource names from partition keys
            for item in response.get("Items", []):
                pk = item.get("PK", {}).get("S", "")
                if pk.startswith(schema.RESOURCE_PREFIX):
                    resource = pk[len(schema.RESOURCE_PREFIX) :]
                    resources.add(resource)

            next_key = response.get("LastEvaluatedKey")
            if next_key is None:
                break

        return sorted(resources)

    # -------------------------------------------------------------------------
    # System-level default config operations (flat schema)
    # -------------------------------------------------------------------------

    async def set_system_defaults(
        self,
        limits: list[Limit],
        on_unavailable: str | None = None,
        principal: str | None = None,
    ) -> None:
        """
        Store system-wide default limits and config (flat schema).

        System defaults apply to ALL resources unless overridden at resource
        or entity level.

        Args:
            limits: List of Limit configurations (apply to all resources)
            on_unavailable: Behavior when DynamoDB unavailable ("allow" or "block")
            principal: Caller identity for audit logging
        """
        client = await self._get_client()

        # Delete existing system limits
        await self._delete_system_limits_internal()

        # Write new limits with FLAT schema (no resource in SK)
        for limit in limits:
            item = {
                "PK": {"S": schema.pk_system()},
                "SK": {"S": schema.sk_system_limit(limit.name)},
                "limit_name": {"S": limit.name},
                "capacity": {"N": str(limit.capacity)},
                "burst": {"N": str(limit.burst)},
                "refill_amount": {"N": str(limit.refill_amount)},
                "refill_period_seconds": {"N": str(limit.refill_period_seconds)},
                "config_version": {"N": "1"},  # Initial version for future caching
            }
            await client.put_item(TableName=self.table_name, Item=item)

            # Log audit event per limit (ADR-106: use $SYSTEM for all system-level events)
            await self._log_audit_event(
                action=AuditAction.LIMITS_SET,
                entity_id="$SYSTEM",
                principal=principal,
                details={"limit": limit.to_dict()},
            )

        # Store system config (on_unavailable, etc.) if provided
        if on_unavailable is not None:
            await self._set_system_config(on_unavailable=on_unavailable, principal=principal)

    async def get_system_defaults(self) -> tuple[list[Limit], str | None]:
        """
        Get system-wide default limits and config.

        Returns:
            Tuple of (limits, on_unavailable). on_unavailable may be None if not set.
        """
        client = await self._get_client()

        # Get all system limits
        # ADR-105: Use eventually consistent reads for config (0.5 RCU vs 1 RCU)
        response = await client.query(
            TableName=self.table_name,
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk_prefix)",
            ExpressionAttributeValues={
                ":pk": {"S": schema.pk_system()},
                ":sk_prefix": {"S": schema.sk_system_limit_prefix()},
            },
            ConsistentRead=False,
        )

        limits = []
        for item in response.get("Items", []):
            # Skip the config record (SK=#CONFIG)
            sk = item.get("SK", {}).get("S", "")
            if sk == schema.sk_config():
                continue
            # Skip version record
            if sk == schema.sk_version():
                continue

            # Flat schema - fields are top-level
            limit_name = item.get("limit_name", {}).get("S", "")
            if limit_name:  # Only add if it's a valid limit record
                limits.append(
                    Limit(
                        name=limit_name,
                        capacity=int(item.get("capacity", {}).get("N", "0")),
                        burst=int(item.get("burst", {}).get("N", "0")),
                        refill_amount=int(item.get("refill_amount", {}).get("N", "0")),
                        refill_period_seconds=int(
                            item.get("refill_period_seconds", {}).get("N", "0")
                        ),
                    )
                )

        # Get system config
        on_unavailable = await self._get_system_config_value("on_unavailable")

        return limits, on_unavailable

    async def delete_system_defaults(
        self,
        principal: str | None = None,
    ) -> None:
        """
        Delete all system-wide default limits and config.

        Args:
            principal: Caller identity for audit logging
        """
        # Get existing limits for audit logging
        limits, _ = await self.get_system_defaults()

        await self._delete_system_limits_internal()
        await self._delete_system_config()

        # Log audit event (ADR-106: use $SYSTEM for all system-level events)
        for limit in limits:
            await self._log_audit_event(
                action=AuditAction.LIMITS_DELETED,
                entity_id="$SYSTEM",
                principal=principal,
                details={"limit": limit.name},
            )

    async def _delete_system_limits_internal(self) -> None:
        """Delete all system-level limits (internal, no audit)."""
        client = await self._get_client()

        response = await client.query(
            TableName=self.table_name,
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk_prefix)",
            ExpressionAttributeValues={
                ":pk": {"S": schema.pk_system()},
                ":sk_prefix": {"S": schema.sk_system_limit_prefix()},
            },
            ProjectionExpression="PK, SK",
        )

        items = response.get("Items", [])
        # Filter out config and version records
        limit_items = [
            item
            for item in items
            if item.get("SK", {}).get("S", "") not in (schema.sk_config(), schema.sk_version())
        ]

        if not limit_items:
            return

        delete_requests = [
            {"DeleteRequest": {"Key": {"PK": item["PK"], "SK": item["SK"]}}} for item in limit_items
        ]

        for i in range(0, len(delete_requests), 25):
            chunk = delete_requests[i : i + 25]
            await client.batch_write_item(RequestItems={self.table_name: chunk})

    async def _set_system_config(
        self,
        on_unavailable: str | None = None,
        principal: str | None = None,
    ) -> None:
        """Store system behavior config (on_unavailable, etc.)."""
        client = await self._get_client()

        item: dict[str, Any] = {
            "PK": {"S": schema.pk_system()},
            "SK": {"S": schema.sk_config()},
            "config_version": {"N": "1"},
        }

        if on_unavailable is not None:
            item["on_unavailable"] = {"S": on_unavailable}

        await client.put_item(TableName=self.table_name, Item=item)

        # Log audit event (ADR-106: use $SYSTEM for all system-level events)
        await self._log_audit_event(
            action=AuditAction.LIMITS_SET,  # Using LIMITS_SET for config changes too
            entity_id="$SYSTEM",
            principal=principal,
            details={"on_unavailable": on_unavailable},
        )

    async def _get_system_config_value(self, field: str) -> str | None:
        """Get a specific system config value."""
        client = await self._get_client()

        # ADR-105: Use eventually consistent reads for config (0.5 RCU vs 1 RCU)
        response = await client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_system()},
                "SK": {"S": schema.sk_config()},
            },
            ConsistentRead=False,
        )

        item = response.get("Item")
        if not item:
            return None

        field_value = item.get(field, {})
        result: str | None = field_value.get("S") if field_value else None
        return result

    async def _delete_system_config(self) -> None:
        """Delete system config record."""
        client = await self._get_client()

        await client.delete_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_system()},
                "SK": {"S": schema.sk_config()},
            },
        )

    async def get_system_limits(self) -> list[Limit]:
        """Get system-wide default limits (without config).

        This is a convenience method that returns only the limits.
        Use get_system_defaults() to also get on_unavailable config.
        """
        limits, _ = await self.get_system_defaults()
        return limits

    # -------------------------------------------------------------------------
    # Version record operations
    # -------------------------------------------------------------------------

    async def get_version_record(self) -> dict[str, Any] | None:
        """
        Get the infrastructure version record.

        Returns:
            Version record with schema_version, lambda_version, etc.
            None if no version record exists.
        """
        client = await self._get_client()

        response = await client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_system()},
                "SK": {"S": schema.sk_version()},
            },
        )

        item = response.get("Item")
        if not item:
            return None

        # Flat format: schema_version at top level
        if "schema_version" in item and "S" in item.get("schema_version", {}):
            result: dict[str, Any] = {}
            for key in (
                "schema_version",
                "lambda_version",
                "client_min_version",
                "updated_at",
                "updated_by",
            ):
                if key in item:
                    result[key] = self._deserialize_value(item[key])
            return result

        # Nested format: fields inside data.M
        return self._deserialize_map(item.get("data", {}).get("M", {}))

    async def ping(self) -> bool:
        """
        Check if the DynamoDB table is reachable.

        Performs a lightweight GetItem operation to verify connectivity.
        Does not verify the table is initialized or has valid data.

        Returns:
            True if the table is reachable, False otherwise.
        """
        try:
            client = await self._get_client()
            await client.get_item(
                TableName=self.table_name,
                Key={
                    "PK": {"S": schema.pk_system()},
                    "SK": {"S": schema.sk_version()},
                },
            )
            return True
        except Exception:
            return False

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
        client = await self._get_client()
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        # Flat schema (v1.1.0+)
        item: dict[str, Any] = {
            "PK": {"S": schema.pk_system()},
            "SK": {"S": schema.sk_version()},
            "schema_version": {"S": schema_version},
            "client_min_version": {"S": client_min_version},
            "updated_at": {"S": now},
            "lambda_version": {"S": lambda_version} if lambda_version else {"NULL": True},
            "updated_by": {"S": updated_by} if updated_by else {"NULL": True},
        }

        await client.put_item(TableName=self.table_name, Item=item)

    # -------------------------------------------------------------------------
    # Audit logging operations
    # -------------------------------------------------------------------------

    def _generate_event_id(self) -> str:
        """Generate a unique event ID using ULID (monotonic, collision-free)."""
        return str(ULID())

    async def _log_audit_event(
        self,
        action: str,
        entity_id: str,
        principal: str | None = None,
        resource: str | None = None,
        details: dict[str, Any] | None = None,
        ttl_seconds: int = 7776000,  # 90 days default
    ) -> AuditEvent:
        """
        Log an audit event to DynamoDB.

        Args:
            action: Type of action (see AuditAction constants)
            entity_id: ID of the entity affected
            principal: Caller identity who performed the action. If None,
                auto-detects from AWS STS caller identity (lazy cached).
            resource: Resource name for limit-related actions
            details: Additional action-specific details
            ttl_seconds: TTL for the audit record (default 90 days)

        Returns:
            The created AuditEvent

        Raises:
            InvalidIdentifierError: If principal is invalid
        """
        # Auto-detect principal from AWS caller identity if not provided
        if principal is None:
            principal = await self._get_caller_identity_arn()

        # Validate principal if provided (skip validation for ARNs from STS)
        # ARNs contain colons which would fail identifier validation
        # Only validate user-provided principals that aren't ARNs
        if principal is not None and not principal.startswith("arn:"):
            validate_identifier(principal, "principal")

        client = await self._get_client()
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        event_id = self._generate_event_id()

        event = AuditEvent(
            event_id=event_id,
            timestamp=now,
            action=action,
            entity_id=entity_id,
            principal=principal,
            resource=resource,
            details=details or {},
        )

        # Build DynamoDB item (flat schema v1.1.0+)
        item: dict[str, Any] = {
            "PK": {"S": schema.pk_audit(entity_id)},
            "SK": {"S": schema.sk_audit(event_id)},
            "entity_id": {"S": entity_id},
            "event_id": {"S": event_id},
            "timestamp": {"S": now},
            "action": {"S": action},
            "principal": {"S": principal} if principal else {"NULL": True},
            "resource": {"S": resource} if resource else {"NULL": True},
            "details": {"M": self._serialize_map(details or {})},
            "ttl": {"N": str(schema.calculate_ttl(self._now_ms(), ttl_seconds))},
        }

        await client.put_item(TableName=self.table_name, Item=item)
        return event

    async def get_audit_events(
        self,
        entity_id: str,
        limit: int = 100,
        start_event_id: str | None = None,
    ) -> list[AuditEvent]:
        """
        Get audit events for an entity.

        Args:
            entity_id: ID of the entity to query
            limit: Maximum number of events to return
            start_event_id: Event ID to start after (for pagination)

        Returns:
            List of AuditEvent objects, ordered by most recent first
        """
        client = await self._get_client()

        query_args: dict[str, Any] = {
            "TableName": self.table_name,
            "KeyConditionExpression": "PK = :pk AND begins_with(SK, :sk_prefix)",
            "ExpressionAttributeValues": {
                ":pk": {"S": schema.pk_audit(entity_id)},
                ":sk_prefix": {"S": schema.SK_AUDIT},
            },
            "ScanIndexForward": False,  # Most recent first
            "Limit": limit,
        }

        if start_event_id:
            query_args["ExclusiveStartKey"] = {
                "PK": {"S": schema.pk_audit(entity_id)},
                "SK": {"S": schema.sk_audit(start_event_id)},
            }

        response = await client.query(**query_args)

        events = []
        for item in response.get("Items", []):
            event = self._deserialize_audit_event(item)
            if event:
                events.append(event)

        return events

    def _deserialize_audit_event(self, item: dict[str, Any]) -> AuditEvent | None:
        """Deserialize a DynamoDB item to AuditEvent.

        Supports both flat (v1.1.0+) and nested data.M (v1.0.0) formats.
        """
        # Flat format: action at top level
        if "action" in item and "S" in item.get("action", {}):
            details_raw = item.get("details", {})
            details = self._deserialize_map(details_raw.get("M", {})) if "M" in details_raw else {}
            principal = self._deserialize_value(item["principal"]) if "principal" in item else None
            resource = self._deserialize_value(item["resource"]) if "resource" in item else None
            return AuditEvent(
                event_id=item.get("event_id", {}).get("S", ""),
                timestamp=item.get("timestamp", {}).get("S", ""),
                action=item["action"]["S"],
                entity_id=item.get("entity_id", {}).get("S", ""),
                principal=principal,
                resource=resource,
                details=details,
            )

        # Nested format: fields inside data.M
        data = self._deserialize_map(item.get("data", {}).get("M", {}))
        if not data:
            return None

        return AuditEvent(
            event_id=data.get("event_id", ""),
            timestamp=data.get("timestamp", ""),
            action=data.get("action", ""),
            entity_id=data.get("entity_id", ""),
            principal=data.get("principal"),
            resource=data.get("resource"),
            details=data.get("details", {}),
        )

    # -------------------------------------------------------------------------
    # Usage snapshot operations
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
    ) -> tuple[list[UsageSnapshot], dict[str, Any] | None]:
        """
        Query usage snapshots with filtering and pagination.

        Supports two query modes:
        1. Entity-scoped: Query by entity_id (uses primary key)
        2. Resource-scoped: Query by resource across all entities (uses GSI2)

        Args:
            entity_id: Entity to query (mutually exclusive for efficient queries)
            resource: Resource name filter (required if entity_id is None)
            window_type: Filter by window type ("hourly", "daily")
            start_time: Filter snapshots >= this timestamp (ISO format)
            end_time: Filter snapshots <= this timestamp (ISO format)
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
        """
        if entity_id is None and resource is None:
            raise ValueError("Either entity_id or resource must be provided")

        client = await self._get_client()
        snapshots: list[UsageSnapshot] = []

        if entity_id is not None:
            # Query by entity (primary key)
            key_condition = "PK = :pk AND begins_with(SK, :sk_prefix)"
            expression_values: dict[str, Any] = {
                ":pk": {"S": schema.pk_entity(entity_id)},
                ":sk_prefix": {"S": schema.SK_USAGE},
            }

            # If resource is also provided, narrow the SK prefix
            if resource:
                expression_values[":sk_prefix"] = {"S": f"{schema.SK_USAGE}{resource}#"}

            query_args: dict[str, Any] = {
                "TableName": self.table_name,
                "KeyConditionExpression": key_condition,
                "ExpressionAttributeValues": expression_values,
                "ScanIndexForward": False,  # Most recent first
                "Limit": limit,
            }

            if next_key:
                query_args["ExclusiveStartKey"] = next_key

            response = await client.query(**query_args)

        else:
            # Query by resource across entities (GSI2)
            key_condition = "GSI2PK = :pk AND begins_with(GSI2SK, :sk_prefix)"
            expression_values = {
                ":pk": {"S": schema.gsi2_pk_resource(resource)},  # type: ignore
                ":sk_prefix": {"S": "USAGE#"},
            }

            query_args = {
                "TableName": self.table_name,
                "IndexName": schema.GSI2_NAME,
                "KeyConditionExpression": key_condition,
                "ExpressionAttributeValues": expression_values,
                "ScanIndexForward": False,  # Most recent first
                "Limit": limit,
            }

            if next_key:
                query_args["ExclusiveStartKey"] = next_key

            response = await client.query(**query_args)

        # Deserialize and filter results
        for item in response.get("Items", []):
            snapshot = self._deserialize_usage_snapshot(item)
            if snapshot is None:
                continue

            # Apply filters
            if window_type and snapshot.window_type != window_type:
                continue
            if start_time and snapshot.window_start < start_time:
                continue
            if end_time and snapshot.window_start > end_time:
                continue

            snapshots.append(snapshot)

        # Get next pagination key
        returned_next_key = response.get("LastEvaluatedKey")

        return snapshots, returned_next_key

    async def get_usage_summary(
        self,
        entity_id: str | None = None,
        resource: str | None = None,
        window_type: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> UsageSummary:
        """
        Aggregate usage across snapshots into a summary.

        Fetches all matching snapshots (auto-paginates internally) and computes:
        - Total consumption per limit type
        - Average consumption per snapshot per limit type
        - Time range of aggregated data

        Args:
            entity_id: Entity to query
            resource: Resource name filter
            window_type: Filter by window type ("hourly", "daily")
            start_time: Filter snapshots >= this timestamp (ISO format)
            end_time: Filter snapshots <= this timestamp (ISO format)

        Returns:
            UsageSummary with aggregated statistics

        Raises:
            ValueError: If neither entity_id nor resource is provided
        """
        # Collect all matching snapshots with auto-pagination
        all_snapshots: list[UsageSnapshot] = []
        next_key: dict[str, Any] | None = None

        while True:
            snapshots, next_key = await self.get_usage_snapshots(
                entity_id=entity_id,
                resource=resource,
                window_type=window_type,
                start_time=start_time,
                end_time=end_time,
                limit=1000,  # Larger batch for efficiency
                next_key=next_key,
            )
            all_snapshots.extend(snapshots)

            if next_key is None:
                break

            # Safety limit to prevent unbounded memory usage
            if len(all_snapshots) >= 10000:
                break

        # Aggregate statistics
        total: dict[str, int] = {}
        counts: dict[str, int] = {}
        min_window: str | None = None
        max_window: str | None = None

        for snapshot in all_snapshots:
            # Track time range
            if min_window is None or snapshot.window_start < min_window:
                min_window = snapshot.window_start
            if max_window is None or snapshot.window_start > max_window:
                max_window = snapshot.window_start

            # Sum counters
            for limit_name, value in snapshot.counters.items():
                total[limit_name] = total.get(limit_name, 0) + value
                counts[limit_name] = counts.get(limit_name, 0) + 1

        # Calculate averages
        average: dict[str, float] = {}
        for limit_name, sum_value in total.items():
            count = counts.get(limit_name, 1)
            average[limit_name] = sum_value / count if count > 0 else 0.0

        return UsageSummary(
            snapshot_count=len(all_snapshots),
            total=total,
            average=average,
            min_window_start=min_window,
            max_window_start=max_window,
        )

    def _deserialize_usage_snapshot(self, item: dict[str, Any]) -> UsageSnapshot | None:
        """
        Deserialize a DynamoDB item to UsageSnapshot.

        Snapshots use FLAT schema (no nested data.M) to support atomic ADD
        operations. See issue #168.
        """
        # Extract from flat schema (not nested data.M)
        entity_id = item.get("entity_id", {}).get("S", "")
        resource = item.get("resource", {}).get("S", "")
        window_type = item.get("window", {}).get("S", "")
        window_start = item.get("window_start", {}).get("S", "")

        if not entity_id or not resource or not window_start:
            return None

        # Calculate window_end based on window_type
        window_end = self._calculate_window_end(window_start, window_type)

        # Extract total_events
        total_events = int(item.get("total_events", {}).get("N", "0"))

        # Extract counters (dynamic limit names stored as top-level attributes)
        # Known non-counter fields to exclude
        excluded_keys = {
            "PK",
            "SK",
            "entity_id",
            "resource",
            "window",
            "window_start",
            "total_events",
            "GSI2PK",
            "GSI2SK",
            "ttl",
        }

        counters: dict[str, int] = {}
        for key, value in item.items():
            if key in excluded_keys:
                continue
            # Counter values are stored as numbers
            if "N" in value:
                counters[key] = int(value["N"])

        return UsageSnapshot(
            entity_id=entity_id,
            resource=resource,
            window_start=window_start,
            window_end=window_end,
            window_type=window_type,
            counters=counters,
            total_events=total_events,
        )

    def _calculate_window_end(self, window_start: str, window_type: str) -> str:
        """Calculate window end timestamp from start and type."""
        from datetime import datetime, timedelta

        try:
            # Parse ISO timestamp
            dt = datetime.fromisoformat(window_start.replace("Z", "+00:00"))

            if window_type == "hourly":
                end_dt = dt.replace(minute=59, second=59, microsecond=999999)
            elif window_type == "daily":
                end_dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
            elif window_type == "monthly":
                # Last day of month
                if dt.month == 12:
                    end_dt = dt.replace(year=dt.year + 1, month=1, day=1) - timedelta(seconds=1)
                else:
                    end_dt = dt.replace(month=dt.month + 1, day=1) - timedelta(seconds=1)
            else:
                # Unknown window type - return window_start as window_end
                end_dt = dt

            return end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, AttributeError):
            return window_start

    # -------------------------------------------------------------------------
    # Resource aggregation
    # -------------------------------------------------------------------------

    async def get_resource_buckets(
        self,
        resource: str,
        limit_name: str | None = None,
    ) -> list[BucketState]:
        """Get all buckets for a resource across all entities."""
        client = await self._get_client()

        key_condition = "GSI2PK = :pk"
        expression_values: dict[str, Any] = {
            ":pk": {"S": schema.gsi2_pk_resource(resource)},
        }

        if limit_name:
            key_condition += " AND begins_with(GSI2SK, :sk_prefix)"
            expression_values[":sk_prefix"] = {"S": "BUCKET#"}

        response = await client.query(
            TableName=self.table_name,
            IndexName=schema.GSI2_NAME,
            KeyConditionExpression=key_condition,
            ExpressionAttributeValues=expression_values,
        )

        buckets = []
        for item in response.get("Items", []):
            bucket = self._deserialize_bucket(item)
            if limit_name is None or bucket.limit_name == limit_name:
                buckets.append(bucket)

        return buckets

    # -------------------------------------------------------------------------
    # Serialization helpers
    # -------------------------------------------------------------------------

    def _serialize_map(self, data: dict[str, Any]) -> dict[str, Any]:
        """Serialize a Python dict to DynamoDB map format."""
        result: dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(value, str):
                result[key] = {"S": value}
            elif isinstance(value, bool):
                result[key] = {"BOOL": value}
            elif isinstance(value, int | float):
                result[key] = {"N": str(value)}
            elif isinstance(value, dict):
                result[key] = {"M": self._serialize_map(value)}
            elif isinstance(value, list):
                result[key] = {"L": [self._serialize_value(v) for v in value]}
            elif value is None:
                result[key] = {"NULL": True}
        return result

    def _serialize_value(self, value: Any) -> dict[str, Any]:
        """Serialize a single value to DynamoDB format."""
        if isinstance(value, str):
            return {"S": value}
        elif isinstance(value, bool):
            return {"BOOL": value}
        elif isinstance(value, int | float):
            return {"N": str(value)}
        elif isinstance(value, dict):
            return {"M": self._serialize_map(value)}
        elif isinstance(value, list):
            return {"L": [self._serialize_value(v) for v in value]}
        elif value is None:
            return {"NULL": True}
        return {"S": str(value)}

    def _deserialize_map(self, data: dict[str, Any]) -> dict[str, Any]:
        """Deserialize a DynamoDB map to Python dict."""
        result = {}
        for key, value in data.items():
            result[key] = self._deserialize_value(value)
        return result

    def _deserialize_value(self, value: dict[str, Any]) -> Any:
        """Deserialize a single DynamoDB value."""
        if "S" in value:
            return value["S"]
        elif "N" in value:
            num_str = value["N"]
            return int(num_str) if "." not in num_str else float(num_str)
        elif "BOOL" in value:
            return value["BOOL"]
        elif "M" in value:
            return self._deserialize_map(value["M"])
        elif "L" in value:
            return [self._deserialize_value(v) for v in value["L"]]
        elif "NULL" in value:
            return None
        return None

    def _deserialize_entity(self, item: dict[str, Any]) -> Entity:
        """Deserialize a DynamoDB item to Entity.

        Supports both flat (v1.1.0+) and nested data.M (v1.0.0) formats.
        """
        entity_id = item.get("entity_id", {}).get("S", "")

        # Flat format: fields at top level
        if "name" in item and "S" in item.get("name", {}):
            name_val = item["name"].get("S")
            parent_val = self._deserialize_value(item["parent_id"]) if "parent_id" in item else None
            metadata_val = (
                self._deserialize_map(item["metadata"].get("M", {}))
                if "metadata" in item and "M" in item.get("metadata", {})
                else {}
            )
            created_val = item.get("created_at", {}).get("S")
        else:
            # Nested format: fields inside data.M
            data = self._deserialize_map(item.get("data", {}).get("M", {}))
            name_val = data.get("name")
            parent_val = data.get("parent_id")
            metadata_val = data.get("metadata", {})
            created_val = data.get("created_at")

        return Entity(
            id=entity_id,
            name=name_val,
            parent_id=parent_val,
            metadata=metadata_val,
            created_at=created_val,
        )

    def _deserialize_bucket(self, item: dict[str, Any]) -> BucketState:
        """Deserialize a DynamoDB item to BucketState.

        Supports both flat (v1.1.0+) and nested data.M (v1.0.0) formats.
        total_consumed_milli is always a flat top-level attribute.
        """
        # Counter is stored as FLAT top-level attribute (not in data.M).
        # None if not present (old bucket without counter). See issue #179.
        counter_attr = item.get("total_consumed_milli", {})
        total_consumed_milli = int(counter_attr["N"]) if "N" in counter_attr else None

        # Flat format: tokens_milli at top level
        if "tokens_milli" in item and "N" in item.get("tokens_milli", {}):
            return BucketState(
                entity_id=item.get("entity_id", {}).get("S", ""),
                resource=item.get("resource", {}).get("S", ""),
                limit_name=item.get("limit_name", {}).get("S", ""),
                tokens_milli=int(item["tokens_milli"]["N"]),
                last_refill_ms=int(item.get("last_refill_ms", {}).get("N", "0")),
                capacity_milli=int(item.get("capacity_milli", {}).get("N", "0")),
                burst_milli=int(item.get("burst_milli", {}).get("N", "0")),
                refill_amount_milli=int(item.get("refill_amount_milli", {}).get("N", "0")),
                refill_period_ms=int(item.get("refill_period_ms", {}).get("N", "0")),
                total_consumed_milli=total_consumed_milli,
            )

        # Nested format: fields inside data.M
        data = self._deserialize_map(item.get("data", {}).get("M", {}))
        return BucketState(
            entity_id=item.get("entity_id", {}).get("S", ""),
            resource=data.get("resource", ""),
            limit_name=data.get("limit_name", ""),
            tokens_milli=int(data.get("tokens_milli", 0)),
            last_refill_ms=int(data.get("last_refill_ms", 0)),
            capacity_milli=int(data.get("capacity_milli", 0)),
            burst_milli=int(data.get("burst_milli", 0)),
            refill_amount_milli=int(data.get("refill_amount_milli", 0)),
            refill_period_ms=int(data.get("refill_period_ms", 0)),
            total_consumed_milli=total_consumed_milli,
        )


# Type assertion: Repository implements RepositoryProtocol
# This is verified at type-check time by mypy, not at runtime
if TYPE_CHECKING:
    from .repository_protocol import RepositoryProtocol

    _: RepositoryProtocol = cast(Repository, None)
