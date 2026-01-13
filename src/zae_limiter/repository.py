"""DynamoDB repository for rate limiter data."""

import time
from typing import Any

import aioboto3  # type: ignore[import-untyped]
from botocore.exceptions import ClientError
from ulid import ULID

from . import schema
from .exceptions import EntityExistsError
from .models import (
    AuditAction,
    AuditEvent,
    BucketState,
    Entity,
    Limit,
    StackOptions,
    validate_identifier,
)
from .naming import normalize_stack_name


class Repository:
    """
    Async DynamoDB repository for rate limiter data.

    Handles all DynamoDB operations including entities, buckets,
    limit configs, and transactions.

    The stack_name is automatically prefixed with 'ZAEL-' if not already present.
    The table_name is always identical to the stack_name.
    """

    def __init__(
        self,
        stack_name: str,
        region: str | None = None,
        endpoint_url: str | None = None,
    ) -> None:
        # Validate and normalize stack name (adds ZAEL- prefix)
        self.stack_name = normalize_stack_name(stack_name)
        # Table name is always identical to stack name
        self.table_name = self.stack_name
        self.region = region
        self.endpoint_url = endpoint_url
        self._session: aioboto3.Session | None = None
        self._client: Any = None

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

    async def create_stack(
        self,
        stack_options: StackOptions | None = None,
    ) -> None:
        """
        Create DynamoDB infrastructure via CloudFormation.

        Args:
            stack_options: Configuration for CloudFormation stack

        Raises:
            StackCreationError: If CloudFormation stack creation fails
        """
        from .infra.stack_manager import StackManager

        async with StackManager(self.stack_name, self.region, self.endpoint_url) as manager:
            await manager.create_stack(
                stack_options=stack_options,
            )

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
            "data": {
                "M": {
                    "name": {"S": name or entity_id},
                    "parent_id": {"S": parent_id} if parent_id else {"NULL": True},
                    "metadata": {"M": self._serialize_map(metadata or {})},
                    "created_at": {"S": now},
                }
            },
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

    def build_bucket_put_item(
        self,
        state: BucketState,
        ttl_seconds: int = 86400,
    ) -> dict[str, Any]:
        """Build a PutItem for a bucket (for use in transactions)."""
        now_ms = self._now_ms()
        return {
            "Put": {
                "TableName": self.table_name,
                "Item": {
                    "PK": {"S": schema.pk_entity(state.entity_id)},
                    "SK": {"S": schema.sk_bucket(state.resource, state.limit_name)},
                    "entity_id": {"S": state.entity_id},
                    "data": {
                        "M": {
                            "resource": {"S": state.resource},
                            "limit_name": {"S": state.limit_name},
                            "tokens_milli": {"N": str(state.tokens_milli)},
                            "last_refill_ms": {"N": str(state.last_refill_ms)},
                            "capacity_milli": {"N": str(state.capacity_milli)},
                            "burst_milli": {"N": str(state.burst_milli)},
                            "refill_amount_milli": {"N": str(state.refill_amount_milli)},
                            "refill_period_ms": {"N": str(state.refill_period_ms)},
                        }
                    },
                    "GSI2PK": {"S": schema.gsi2_pk_resource(state.resource)},
                    "GSI2SK": {"S": schema.gsi2_sk_bucket(state.entity_id, state.limit_name)},
                    "ttl": {"N": str(schema.calculate_ttl(now_ms, ttl_seconds))},
                },
            }
        }

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
                "UpdateExpression": "SET #data.#tokens = :tokens, #data.#refill = :refill",
                "ExpressionAttributeNames": {
                    "#data": "data",
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
            update["Update"]["ConditionExpression"] = "#data.#tokens = :expected"
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

        # Write new limits
        for limit in limits:
            item = {
                "PK": {"S": schema.pk_entity(entity_id)},
                "SK": {"S": schema.sk_limit(resource, limit.name)},
                "entity_id": {"S": entity_id},
                "data": {
                    "M": {
                        "resource": {"S": resource},
                        "limit_name": {"S": limit.name},
                        "capacity": {"N": str(limit.capacity)},
                        "burst": {"N": str(limit.burst)},
                        "refill_amount": {"N": str(limit.refill_amount)},
                        "refill_period_seconds": {"N": str(limit.refill_period_seconds)},
                    }
                },
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

        response = await client.query(
            TableName=self.table_name,
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk_prefix)",
            ExpressionAttributeValues={
                ":pk": {"S": schema.pk_entity(entity_id)},
                ":sk_prefix": {"S": schema.sk_limit_prefix(resource)},
            },
        )

        limits = []
        for item in response.get("Items", []):
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

        return self._deserialize_map(item.get("data", {}).get("M", {}))

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

        data: dict[str, Any] = {
            "schema_version": {"S": schema_version},
            "client_min_version": {"S": client_min_version},
            "updated_at": {"S": now},
        }

        if lambda_version:
            data["lambda_version"] = {"S": lambda_version}
        else:
            data["lambda_version"] = {"NULL": True}

        if updated_by:
            data["updated_by"] = {"S": updated_by}
        else:
            data["updated_by"] = {"NULL": True}

        item = {
            "PK": {"S": schema.pk_system()},
            "SK": {"S": schema.sk_version()},
            "data": {"M": data},
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
            principal: Caller identity who performed the action
            resource: Resource name for limit-related actions
            details: Additional action-specific details
            ttl_seconds: TTL for the audit record (default 90 days)

        Returns:
            The created AuditEvent

        Raises:
            InvalidIdentifierError: If principal is invalid
        """
        # Validate principal if provided
        if principal is not None:
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

        # Build DynamoDB item
        data: dict[str, Any] = {
            "event_id": {"S": event_id},
            "timestamp": {"S": now},
            "action": {"S": action},
            "entity_id": {"S": entity_id},
        }

        if principal:
            data["principal"] = {"S": principal}
        else:
            data["principal"] = {"NULL": True}

        if resource:
            data["resource"] = {"S": resource}
        else:
            data["resource"] = {"NULL": True}

        if details:
            data["details"] = {"M": self._serialize_map(details)}
        else:
            data["details"] = {"M": {}}

        item = {
            "PK": {"S": schema.pk_audit(entity_id)},
            "SK": {"S": schema.sk_audit(event_id)},
            "entity_id": {"S": entity_id},
            "data": {"M": data},
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
        """Deserialize a DynamoDB item to AuditEvent."""
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
            elif isinstance(value, (int, float)):
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
        elif isinstance(value, (int, float)):
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
        """Deserialize a DynamoDB item to Entity."""
        data = self._deserialize_map(item.get("data", {}).get("M", {}))
        return Entity(
            id=item.get("entity_id", {}).get("S", ""),
            name=data.get("name"),
            parent_id=data.get("parent_id"),
            metadata=data.get("metadata", {}),
            created_at=data.get("created_at"),
        )

    def _deserialize_bucket(self, item: dict[str, Any]) -> BucketState:
        """Deserialize a DynamoDB item to BucketState."""
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
        )
