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
    validate_resource,
)
from .naming import normalize_stack_name


class Repository:
    """
    Async DynamoDB repository for rate limiter data.

    Handles all DynamoDB operations including entities, buckets,
    limit configs, and transactions.

    Args:
        name: Resource identifier (e.g., "my-app"). Used as the
            CloudFormation stack_name and DynamoDB table_name.
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
        # Validate and normalize name
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
        self._audit_retention_days_cache: int | None = None

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

    async def _get_item(self, pk: str, sk: str) -> dict[str, Any] | None:
        """Get a raw DynamoDB item by primary key (testing helper).

        Args:
            pk: Partition key value (already formatted, e.g., "ENTITY#user-1")
            sk: Sort key value (already formatted, e.g., "#BUCKET#api")

        Returns:
            Deserialized item dict or None if not found.
        """
        client = await self._get_client()
        response = await client.get_item(
            TableName=self.table_name,
            Key={"PK": {"S": pk}, "SK": {"S": sk}},
        )
        item = response.get("Item")
        if not item:
            return None
        # Deserialize DynamoDB types to Python types (S and N only for buckets)
        result: dict[str, Any] = {}
        for key, value in item.items():
            if "S" in value:
                result[key] = value["S"]
            elif "N" in value:
                result[key] = int(value["N"])
        return result

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

            # Deploy Lambda code if aggregator is enabled
            if self._stack_options.enable_aggregator:
                await manager.deploy_lambda_code()

        # Write retention config to system config item
        await self._write_audit_retention_config()

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
        cascade: bool = False,
        metadata: dict[str, str] | None = None,
        principal: str | None = None,
    ) -> Entity:
        """
        Create a new entity.

        Args:
            entity_id: Unique identifier for the entity
            name: Optional display name (defaults to entity_id)
            parent_id: Optional parent entity ID (for hierarchical limits)
            cascade: If True, acquire() will also consume from parent entity
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
            "cascade": {"BOOL": cascade},
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
                "cascade": cascade,
                "metadata": metadata or {},
            },
        )

        return Entity(
            id=entity_id,
            name=name or entity_id,
            parent_id=parent_id,
            cascade=cascade,
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
        """Get a single limit's bucket from the composite item."""
        client = await self._get_client()

        response = await client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_entity(entity_id)},
                "SK": {"S": schema.sk_bucket(resource)},
            },
        )

        item = response.get("Item")
        if not item:
            return None

        buckets = self._deserialize_composite_bucket(item)
        for b in buckets:
            if b.limit_name == limit_name:
                return b
        return None

    async def get_buckets(
        self,
        entity_id: str,
        resource: str | None = None,
    ) -> list[BucketState]:
        """Get all buckets for an entity, optionally filtered by resource.

        With composite items, each item contains all limits for one resource.
        """
        client = await self._get_client()

        if resource:
            # Single composite item for this entity+resource
            response = await client.get_item(
                TableName=self.table_name,
                Key={
                    "PK": {"S": schema.pk_entity(entity_id)},
                    "SK": {"S": schema.sk_bucket(resource)},
                },
            )
            item = response.get("Item")
            if not item:
                return []
            return self._deserialize_composite_bucket(item)

        # Query all composite bucket items for this entity
        key_condition = "PK = :pk AND begins_with(SK, :sk_prefix)"
        expression_values: dict[str, Any] = {
            ":pk": {"S": schema.pk_entity(entity_id)},
            ":sk_prefix": {"S": schema.SK_BUCKET},
        }

        response = await client.query(
            TableName=self.table_name,
            KeyConditionExpression=key_condition,
            ExpressionAttributeValues=expression_values,
        )

        buckets: list[BucketState] = []
        for item in response.get("Items", []):
            buckets.extend(self._deserialize_composite_bucket(item))
        return buckets

    async def batch_get_buckets(
        self,
        keys: list[tuple[str, str]],
    ) -> dict[tuple[str, str, str], BucketState]:
        """
        Batch get composite buckets in a single DynamoDB call.

        With composite items, each (entity_id, resource) pair is a single
        DynamoDB item containing all limits. Returns individual BucketStates
        keyed by (entity_id, resource, limit_name) for backward compatibility.

        Args:
            keys: List of (entity_id, resource) tuples

        Returns:
            Dict mapping (entity_id, resource, limit_name) to BucketState.
            Missing composite items are not included in the result.

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
                    "SK": {"S": schema.sk_bucket(resource)},
                }
                for entity_id, resource in chunk
            ]

            response = await client.batch_get_item(
                RequestItems={
                    self.table_name: {
                        "Keys": request_keys,
                    }
                }
            )

            # Process responses — each item is a composite bucket
            items = response.get("Responses", {}).get(self.table_name, [])
            for item in items:
                buckets = self._deserialize_composite_bucket(item)
                for bucket in buckets:
                    key = (bucket.entity_id, bucket.resource, bucket.limit_name)
                    result[key] = bucket

        return result

    async def batch_get_entity_and_buckets(
        self,
        entity_id: str,
        bucket_keys: list[tuple[str, str]],
    ) -> tuple[Entity | None, dict[tuple[str, str, str], BucketState]]:
        """
        Fetch entity metadata and composite buckets in a single BatchGetItem.

        With composite items, each (entity_id, resource) pair is a single
        DynamoDB item. Includes the entity's #META record alongside bucket
        records to avoid a separate get_entity() round trip.

        Args:
            entity_id: Entity whose META record to include
            bucket_keys: List of (entity_id, resource) for composite buckets

        Returns:
            Tuple of (entity_or_none, bucket_dict) where bucket_dict maps
            (entity_id, resource, limit_name) to BucketState.

        Note:
            DynamoDB BatchGetItem supports up to 100 items per request.
            The META key counts toward that limit.
        """
        client = await self._get_client()

        # Build all keys: META key + composite bucket keys
        meta_key = {
            "PK": {"S": schema.pk_entity(entity_id)},
            "SK": {"S": schema.sk_meta()},
        }

        request_keys = [meta_key]
        unique_bucket_keys = list(set(bucket_keys))
        for eid, resource in unique_bucket_keys:
            request_keys.append(
                {
                    "PK": {"S": schema.pk_entity(eid)},
                    "SK": {"S": schema.sk_bucket(resource)},
                }
            )

        entity: Entity | None = None
        buckets: dict[tuple[str, str, str], BucketState] = {}

        # BatchGetItem in chunks of 100
        for i in range(0, len(request_keys), 100):
            chunk = request_keys[i : i + 100]

            response = await client.batch_get_item(
                RequestItems={
                    self.table_name: {
                        "Keys": chunk,
                    }
                }
            )

            items = response.get("Responses", {}).get(self.table_name, [])
            for item in items:
                sk = item.get("SK", {}).get("S", "")
                if sk == schema.sk_meta():
                    entity = self._deserialize_entity(item)
                elif sk.startswith(schema.SK_BUCKET):
                    for bucket in self._deserialize_composite_bucket(item):
                        key = (bucket.entity_id, bucket.resource, bucket.limit_name)
                        buckets[key] = bucket

        return entity, buckets

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
        """Build a PutItem for a composite bucket (for use in transactions).

        Wraps build_composite_create for backward compatibility with protocol.
        """
        now_ms = self._now_ms()
        return self.build_composite_create(
            entity_id=state.entity_id,
            resource=state.resource,
            states=[state],
            now_ms=now_ms,
            ttl_seconds=ttl_seconds,
        )

    def build_bucket_update_item(
        self,
        entity_id: str,
        resource: str,
        limit_name: str,
        new_tokens_milli: int,
        new_last_refill_ms: int,
        expected_tokens_milli: int | None = None,
    ) -> dict[str, Any]:
        """Build an UpdateItem for a single limit in a composite bucket.

        Legacy method — prefer build_composite_normal/retry/adjust for
        composite writes. This updates one limit's tk within the composite item.
        """
        tk_attr = schema.bucket_attr(limit_name, schema.BUCKET_FIELD_TK)
        update: dict[str, dict[str, Any]] = {
            "Update": {
                "TableName": self.table_name,
                "Key": {
                    "PK": {"S": schema.pk_entity(entity_id)},
                    "SK": {"S": schema.sk_bucket(resource)},
                },
                "UpdateExpression": "SET #tokens = :tokens, #refill = :refill",
                "ExpressionAttributeNames": {
                    "#tokens": tk_attr,
                    "#refill": schema.BUCKET_FIELD_RF,
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

    # -------------------------------------------------------------------------
    # Composite bucket write paths (ADR-114, ADR-115)
    # -------------------------------------------------------------------------

    def build_composite_create(
        self,
        entity_id: str,
        resource: str,
        states: list[BucketState],
        now_ms: int,
        ttl_seconds: int | None = 86400,
    ) -> dict[str, Any]:
        """Build a PutItem for creating a new composite bucket.

        Used on first acquire for an entity+resource. Condition ensures no
        concurrent creation race (attribute_not_exists).

        Args:
            entity_id: Entity owning the bucket
            resource: Resource name
            states: BucketState objects for each limit
            now_ms: Current timestamp in milliseconds
            ttl_seconds: TTL in seconds from now, or None to omit TTL
        """
        item: dict[str, Any] = {
            "PK": {"S": schema.pk_entity(entity_id)},
            "SK": {"S": schema.sk_bucket(resource)},
            "entity_id": {"S": entity_id},
            "resource": {"S": resource},
            schema.BUCKET_FIELD_RF: {"N": str(now_ms)},
            "GSI2PK": {"S": schema.gsi2_pk_resource(resource)},
            "GSI2SK": {"S": schema.gsi2_sk_bucket(entity_id)},
        }
        # Only add TTL if specified (None means no TTL for entity-level config)
        if ttl_seconds is not None:
            item["ttl"] = {"N": str(schema.calculate_ttl(now_ms, ttl_seconds))}
        for state in states:
            name = state.limit_name
            item[schema.bucket_attr(name, schema.BUCKET_FIELD_TK)] = {
                "N": str(state.tokens_milli),
            }
            item[schema.bucket_attr(name, schema.BUCKET_FIELD_CP)] = {
                "N": str(state.capacity_milli),
            }
            item[schema.bucket_attr(name, schema.BUCKET_FIELD_BX)] = {
                "N": str(state.burst_milli),
            }
            item[schema.bucket_attr(name, schema.BUCKET_FIELD_RA)] = {
                "N": str(state.refill_amount_milli),
            }
            item[schema.bucket_attr(name, schema.BUCKET_FIELD_RP)] = {
                "N": str(state.refill_period_ms),
            }
            tc = state.total_consumed_milli if state.total_consumed_milli is not None else 0
            item[schema.bucket_attr(name, schema.BUCKET_FIELD_TC)] = {
                "N": str(tc),
            }

        return {
            "Put": {
                "TableName": self.table_name,
                "Item": item,
                "ConditionExpression": "attribute_not_exists(PK)",
            }
        }

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

        ADD tk:(refill - consumed), tc:consumed for each limit.
        SET rf:now. CONDITION rf = :expected.

        Args:
            entity_id: Entity owning the bucket
            resource: Resource name
            consumed: Amount consumed per limit (millitokens)
            refill_amounts: Refill amount per limit (millitokens)
            now_ms: Current timestamp in milliseconds
            expected_rf: Expected refill timestamp for optimistic lock
            ttl_seconds: TTL behavior:
                - None: Don't change TTL
                - 0: REMOVE ttl (entity has custom limits)
                - >0: SET ttl to (now + ttl_seconds)
        """
        add_parts: list[str] = []
        set_parts: list[str] = ["#rf = :now"]
        remove_parts: list[str] = []
        attr_names: dict[str, str] = {"#rf": schema.BUCKET_FIELD_RF}
        attr_values: dict[str, Any] = {
            ":now": {"N": str(now_ms)},
            ":expected_rf": {"N": str(expected_rf)},
        }

        # Handle TTL
        if ttl_seconds is not None:
            attr_names["#ttl"] = "ttl"
            if ttl_seconds > 0:
                # SET ttl to new value
                set_parts.append("#ttl = :ttl_val")
                attr_values[":ttl_val"] = {"N": str(schema.calculate_ttl(now_ms, ttl_seconds))}
            else:
                # REMOVE ttl (entity has custom limits, should persist)
                remove_parts.append("#ttl")

        for name in consumed:
            c = consumed[name]
            r = refill_amounts.get(name, 0)
            tk_delta = r - c  # refill minus consumption

            tk_alias = f"#b_{name}_tk"
            tc_alias = f"#b_{name}_tc"
            tk_val = f":b_{name}_tk_delta"
            tc_val = f":b_{name}_tc_delta"

            attr_names[tk_alias] = schema.bucket_attr(name, schema.BUCKET_FIELD_TK)
            attr_names[tc_alias] = schema.bucket_attr(name, schema.BUCKET_FIELD_TC)
            attr_values[tk_val] = {"N": str(tk_delta)}
            attr_values[tc_val] = {"N": str(c)}

            add_parts.append(f"{tk_alias} {tk_val}")
            add_parts.append(f"{tc_alias} {tc_val}")

        # Build update expression
        update_expr = f"SET {', '.join(set_parts)} ADD {', '.join(add_parts)}"
        if remove_parts:
            update_expr += f" REMOVE {', '.join(remove_parts)}"

        return {
            "Update": {
                "TableName": self.table_name,
                "Key": {
                    "PK": {"S": schema.pk_entity(entity_id)},
                    "SK": {"S": schema.sk_bucket(resource)},
                },
                "UpdateExpression": update_expr,
                "ConditionExpression": "#rf = :expected_rf",
                "ExpressionAttributeNames": attr_names,
                "ExpressionAttributeValues": attr_values,
            }
        }

    def build_composite_retry(
        self,
        entity_id: str,
        resource: str,
        consumed: dict[str, int],
    ) -> dict[str, Any]:
        """Build an UpdateItem for the retry write path (ADR-115 path 3).

        Lost optimistic lock — skip refill, only consume.
        ADD tk:(-consumed), tc:consumed for each limit.
        CONDITION: tk >= consumed per limit (prevent negative on acquire).
        """
        add_parts: list[str] = []
        condition_parts: list[str] = []
        attr_names: dict[str, str] = {}
        attr_values: dict[str, Any] = {}

        for name in consumed:
            c = consumed[name]
            tk_alias = f"#b_{name}_tk"
            tc_alias = f"#b_{name}_tc"
            tk_neg_val = f":b_{name}_tk_neg"
            tc_val = f":b_{name}_tc_delta"
            tk_threshold = f":b_{name}_tk_min"

            attr_names[tk_alias] = schema.bucket_attr(name, schema.BUCKET_FIELD_TK)
            attr_names[tc_alias] = schema.bucket_attr(name, schema.BUCKET_FIELD_TC)
            attr_values[tk_neg_val] = {"N": str(-c)}
            attr_values[tc_val] = {"N": str(c)}
            attr_values[tk_threshold] = {"N": str(c)}

            add_parts.append(f"{tk_alias} {tk_neg_val}")
            add_parts.append(f"{tc_alias} {tc_val}")
            condition_parts.append(f"{tk_alias} >= {tk_threshold}")

        update_expr = f"ADD {', '.join(add_parts)}"
        condition_expr = " AND ".join(condition_parts)

        return {
            "Update": {
                "TableName": self.table_name,
                "Key": {
                    "PK": {"S": schema.pk_entity(entity_id)},
                    "SK": {"S": schema.sk_bucket(resource)},
                },
                "UpdateExpression": update_expr,
                "ConditionExpression": condition_expr,
                "ExpressionAttributeNames": attr_names,
                "ExpressionAttributeValues": attr_values,
            }
        }

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
        """
        add_parts: list[str] = []
        attr_names: dict[str, str] = {}
        attr_values: dict[str, Any] = {}

        for name, delta in deltas.items():
            if delta == 0:
                continue
            tk_alias = f"#b_{name}_tk"
            tc_alias = f"#b_{name}_tc"
            tk_val = f":b_{name}_tk_delta"
            tc_val = f":b_{name}_tc_delta"

            attr_names[tk_alias] = schema.bucket_attr(name, schema.BUCKET_FIELD_TK)
            attr_names[tc_alias] = schema.bucket_attr(name, schema.BUCKET_FIELD_TC)
            # delta > 0 means consumed more: subtract from tk, add to tc
            attr_values[tk_val] = {"N": str(-delta)}
            attr_values[tc_val] = {"N": str(delta)}

            add_parts.append(f"{tk_alias} {tk_val}")
            add_parts.append(f"{tc_alias} {tc_val}")

        if not add_parts:
            # Nothing to adjust
            return {}

        update_expr = f"ADD {', '.join(add_parts)}"

        return {
            "Update": {
                "TableName": self.table_name,
                "Key": {
                    "PK": {"S": schema.pk_entity(entity_id)},
                    "SK": {"S": schema.sk_bucket(resource)},
                },
                "UpdateExpression": update_expr,
                "ExpressionAttributeNames": attr_names,
                "ExpressionAttributeValues": attr_values,
            }
        }

    async def transact_write(self, items: list[dict[str, Any]]) -> None:
        """Execute a write, using single-item API when possible to halve WCU cost."""
        if not items:
            return

        client = await self._get_client()

        if len(items) == 1:
            item = items[0]
            if "Put" in item:
                await client.put_item(**item["Put"])
            elif "Update" in item:
                await client.update_item(**item["Update"])
            elif "Delete" in item:
                await client.delete_item(**item["Delete"])
            else:
                await client.transact_write_items(TransactItems=items)
        else:
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
        Store limit configs for an entity (composite format, ADR-114).

        All limits for an entity+resource are stored in a single composite item
        with SK '#CONFIG#{resource}'. This reduces cache-miss cost from N GetItem
        calls to 1 GetItem call.

        Args:
            entity_id: ID of the entity
            limits: List of Limit configurations to store
            resource: Resource name (defaults to "_default_")
            principal: Caller identity for audit logging
        """
        client = await self._get_client()

        # Build composite config item with all limits
        item: dict[str, Any] = {
            "PK": {"S": schema.pk_entity(entity_id)},
            "SK": {"S": schema.sk_config(resource)},
            "entity_id": {"S": entity_id},
            "resource": {"S": resource},
            "config_version": {"N": "1"},
            # GSI3 attributes for sparse indexing (entity config queries)
            "GSI3PK": {"S": schema.gsi3_pk_entity_config(resource)},
            "GSI3SK": {"S": schema.gsi3_sk_entity(entity_id)},
        }

        # Add l_* attributes for each limit
        self._serialize_composite_limits(limits, item)

        # Use transaction to atomically create config + increment registry (issue #288)
        # This prevents race conditions where concurrent creates both increment
        try:
            await client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self.table_name,
                            "Item": item,
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                    {
                        "Update": {
                            "TableName": self.table_name,
                            "Key": {
                                "PK": {"S": schema.pk_system()},
                                "SK": {"S": schema.sk_entity_config_resources()},
                            },
                            "UpdateExpression": "ADD #resource :one",
                            "ExpressionAttributeNames": {"#resource": resource},
                            "ExpressionAttributeValues": {":one": {"N": "1"}},
                        }
                    },
                ]
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "TransactionCanceledException":
                # Check if cancellation was due to condition failure (UPDATE case)
                reasons = e.response.get("CancellationReasons", [])
                if reasons and reasons[0].get("Code") == "ConditionalCheckFailed":
                    # Config already exists - UPDATE case, just overwrite
                    await client.put_item(TableName=self.table_name, Item=item)
                else:
                    raise
            else:
                raise

        # Sync bucket static params if bucket exists (issue #294)
        await self._sync_bucket_params(entity_id, resource, limits)

        # Log audit event
        await self._log_audit_event(
            action=AuditAction.LIMITS_SET,
            entity_id=entity_id,
            principal=principal,
            resource=resource,
            details={"limits": [limit.to_dict() for limit in limits]},
        )

    async def _sync_bucket_params(
        self,
        entity_id: str,
        resource: str,
        limits: list[Limit],
    ) -> None:
        """Sync bucket static params when limits change (issue #294).

        Updates capacity, burst, refill_amount, and refill_period for existing
        buckets. Uses conditional update with attribute_exists(PK) to skip if
        bucket doesn't exist yet.

        Args:
            entity_id: ID of the entity
            resource: Resource name
            limits: New limit configurations
        """
        if not limits:
            return

        client = await self._get_client()

        # Build SET expression for static bucket params
        # Use numeric index for expression names since limit names can contain hyphens
        set_parts: list[str] = []
        expr_names: dict[str, str] = {}
        expr_values: dict[str, dict[str, str]] = {}

        for i, limit in enumerate(limits):
            name = limit.name
            # Capacity (millitokens)
            cp_attr = schema.bucket_attr(name, schema.BUCKET_FIELD_CP)
            set_parts.append(f"#cp{i} = :cp{i}")
            expr_names[f"#cp{i}"] = cp_attr
            expr_values[f":cp{i}"] = {"N": str(limit.capacity * 1000)}

            # Burst (millitokens)
            bx_attr = schema.bucket_attr(name, schema.BUCKET_FIELD_BX)
            set_parts.append(f"#bx{i} = :bx{i}")
            expr_names[f"#bx{i}"] = bx_attr
            expr_values[f":bx{i}"] = {"N": str(limit.burst * 1000)}

            # Refill amount (millitokens)
            ra_attr = schema.bucket_attr(name, schema.BUCKET_FIELD_RA)
            set_parts.append(f"#ra{i} = :ra{i}")
            expr_names[f"#ra{i}"] = ra_attr
            expr_values[f":ra{i}"] = {"N": str(limit.refill_amount * 1000)}

            # Refill period (milliseconds)
            rp_attr = schema.bucket_attr(name, schema.BUCKET_FIELD_RP)
            set_parts.append(f"#rp{i} = :rp{i}")
            expr_names[f"#rp{i}"] = rp_attr
            expr_values[f":rp{i}"] = {"N": str(limit.refill_period_seconds * 1000)}

        try:
            await client.update_item(
                TableName=self.table_name,
                Key={
                    "PK": {"S": schema.pk_entity(entity_id)},
                    "SK": {"S": schema.sk_bucket(resource)},
                },
                UpdateExpression=f"SET {', '.join(set_parts)}",
                ConditionExpression="attribute_exists(PK)",
                ExpressionAttributeNames=expr_names,
                ExpressionAttributeValues=expr_values,
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                # Bucket doesn't exist yet - that's fine, it will be created
                # with the correct params on first acquire()
                return
            raise

    async def _cleanup_entity_config_registry(self, resource: str) -> None:
        """Remove resource from entity config registry if count <= 0.

        Called after decrementing to clean up zero/negative counts.
        Uses conditional REMOVE to avoid race conditions.
        """
        client = await self._get_client()

        try:
            await client.update_item(
                TableName=self.table_name,
                Key={
                    "PK": {"S": schema.pk_system()},
                    "SK": {"S": schema.sk_entity_config_resources()},
                },
                UpdateExpression="REMOVE #resource",
                ConditionExpression="#resource <= :zero",
                ExpressionAttributeNames={"#resource": resource},
                ExpressionAttributeValues={":zero": {"N": "0"}},
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            # Count > 0, which is expected - nothing to clean up

    async def get_limits(
        self,
        entity_id: str,
        resource: str = schema.DEFAULT_RESOURCE,
    ) -> list[Limit]:
        """Get stored limit configs for an entity (composite format, ADR-114).

        Reads a single composite item with SK '#CONFIG#{resource}' containing
        all limits for the entity+resource pair.
        """
        client = await self._get_client()

        # ADR-105: Use eventually consistent reads for config (0.5 RCU vs 1 RCU)
        response = await client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_entity(entity_id)},
                "SK": {"S": schema.sk_config(resource)},
            },
            ConsistentRead=False,
        )

        item = response.get("Item")
        if not item:
            return []

        return self._deserialize_composite_limits(item)

    async def delete_limits(
        self,
        entity_id: str,
        resource: str = schema.DEFAULT_RESOURCE,
        principal: str | None = None,
    ) -> None:
        """
        Delete stored limit configs for an entity (composite format, ADR-114).

        Deletes the single composite config item for this entity+resource.

        Args:
            entity_id: ID of the entity
            resource: Resource name (defaults to "_default_")
            principal: Caller identity for audit logging
        """
        client = await self._get_client()

        # Use transaction to atomically delete config + decrement registry (issue #288)
        # This prevents double-decrement if delete_limits is called twice
        try:
            await client.transact_write_items(
                TransactItems=[
                    {
                        "Delete": {
                            "TableName": self.table_name,
                            "Key": {
                                "PK": {"S": schema.pk_entity(entity_id)},
                                "SK": {"S": schema.sk_config(resource)},
                            },
                            "ConditionExpression": "attribute_exists(PK)",
                        }
                    },
                    {
                        "Update": {
                            "TableName": self.table_name,
                            "Key": {
                                "PK": {"S": schema.pk_system()},
                                "SK": {"S": schema.sk_entity_config_resources()},
                            },
                            "UpdateExpression": "ADD #resource :minus_one",
                            "ExpressionAttributeNames": {"#resource": resource},
                            "ExpressionAttributeValues": {":minus_one": {"N": "-1"}},
                        }
                    },
                ]
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "TransactionCanceledException":
                # Check if cancellation was due to condition failure (item doesn't exist)
                reasons = e.response.get("CancellationReasons", [])
                if reasons and reasons[0].get("Code") == "ConditionalCheckFailed":
                    # Config doesn't exist - nothing to delete, skip audit
                    return
                else:
                    raise
            else:
                raise

        # Cleanup: remove registry attribute if count <= 0
        await self._cleanup_entity_config_registry(resource)

        # Log audit event
        await self._log_audit_event(
            action=AuditAction.LIMITS_DELETED,
            entity_id=entity_id,
            principal=principal,
            resource=resource,
        )

    async def list_entities_with_custom_limits(
        self,
        resource: str,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> tuple[list[str], str | None]:
        """
        List all entities that have custom limit configurations for a resource.

        Uses GSI3 sparse index for efficient queries. Only entity-level configs
        have GSI3 attributes, so this query returns only entities with custom
        limits (not system or resource defaults).

        Args:
            resource: Resource to filter by (required).
            limit: Maximum number of entities to return. None for all.
            cursor: Pagination cursor from previous call. None for first page.

        Returns:
            Tuple of (entity_ids, next_cursor). next_cursor is None if no more results.
        """
        import base64
        import json

        client = await self._get_client()

        query_params: dict[str, Any] = {
            "TableName": self.table_name,
            "IndexName": schema.GSI3_NAME,
            "KeyConditionExpression": "GSI3PK = :pk",
            "ExpressionAttributeValues": {":pk": {"S": schema.gsi3_pk_entity_config(resource)}},
        }

        if limit is not None:
            query_params["Limit"] = limit
        if cursor is not None:
            # Decode cursor (base64 encoded LastEvaluatedKey)
            query_params["ExclusiveStartKey"] = json.loads(base64.b64decode(cursor))

        response = await client.query(**query_params)

        entity_ids: list[str] = []
        for item in response.get("Items", []):
            entity_id = item.get("GSI3SK", {}).get("S")
            if entity_id:
                entity_ids.append(entity_id)

        # Encode next cursor if more results
        next_cursor: str | None = None
        if "LastEvaluatedKey" in response:
            next_cursor = base64.b64encode(
                json.dumps(response["LastEvaluatedKey"]).encode()
            ).decode()

        return entity_ids, next_cursor

    async def list_resources_with_entity_configs(self) -> list[str]:
        """
        List all resources that have entity-level custom limit configs.

        Uses the entity config resources registry (wide column with ref counts)
        for efficient O(1) lookup. Returns resources with count > 0.

        Returns:
            Sorted list of resource names with at least one entity having custom limits
        """
        client = await self._get_client()

        # Read from entity config resources registry (single GetItem: 1 RCU)
        response = await client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_system()},
                "SK": {"S": schema.sk_entity_config_resources()},
            },
            ConsistentRead=False,
        )

        # Extract resource names from numeric attributes (wide column pattern)
        item = response.get("Item", {})
        resources = []
        for attr_name, attr_value in item.items():
            # Skip key attributes
            if attr_name in ("PK", "SK"):
                continue
            # Include resources with count > 0
            count_str = attr_value.get("N")
            if count_str is not None and int(count_str) > 0:
                resources.append(attr_name)

        return sorted(resources)

    # -------------------------------------------------------------------------
    # Resource-level limit config operations (composite format, ADR-114)
    # -------------------------------------------------------------------------

    async def set_resource_defaults(
        self,
        resource: str,
        limits: list[Limit],
        principal: str | None = None,
    ) -> None:
        """
        Store default limit configs for a resource (composite format, ADR-114).

        All limits for a resource are stored in a single composite item
        with SK '#CONFIG'. This reduces cache-miss cost.

        Args:
            resource: Resource name
            limits: List of Limit configurations to store
            principal: Caller identity for audit logging
        """
        validate_resource(resource)
        client = await self._get_client()

        # Build composite config item with all limits
        item: dict[str, Any] = {
            "PK": {"S": schema.pk_resource(resource)},
            "SK": {"S": schema.sk_config()},
            "resource": {"S": resource},
            "config_version": {"N": "1"},
        }

        # Add l_* attributes for each limit
        self._serialize_composite_limits(limits, item)

        # Single PutItem replaces any existing config for this resource
        await client.put_item(TableName=self.table_name, Item=item)

        # Add resource to the registry using atomic ADD operation
        await client.update_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_system()},
                "SK": {"S": schema.sk_resources()},
            },
            UpdateExpression="ADD resources :resource",
            ExpressionAttributeValues={
                ":resource": {"SS": [resource]},
            },
        )

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
        """Get stored default limit configs for a resource (composite format, ADR-114).

        Reads a single composite item with SK '#CONFIG' containing all limits
        for the resource.
        """
        validate_resource(resource)
        client = await self._get_client()

        # ADR-105: Use eventually consistent reads for config (0.5 RCU vs 1 RCU)
        response = await client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_resource(resource)},
                "SK": {"S": schema.sk_config()},
            },
            ConsistentRead=False,
        )

        item = response.get("Item")
        if not item:
            return []

        return self._deserialize_composite_limits(item)

    async def delete_resource_defaults(
        self,
        resource: str,
        principal: str | None = None,
    ) -> None:
        """
        Delete stored default limit configs for a resource (composite format, ADR-114).

        Deletes the single composite config item for this resource.

        Args:
            resource: Resource name
            principal: Caller identity for audit logging
        """
        validate_resource(resource)
        client = await self._get_client()

        # Single DeleteItem removes the composite config
        await client.delete_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_resource(resource)},
                "SK": {"S": schema.sk_config()},
            },
        )

        # Remove resource from the registry using atomic DELETE operation
        await client.update_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_system()},
                "SK": {"S": schema.sk_resources()},
            },
            UpdateExpression="DELETE resources :resource",
            ExpressionAttributeValues={
                ":resource": {"SS": [resource]},
            },
        )

        # Log audit event
        await self._log_audit_event(
            action=AuditAction.LIMITS_DELETED,
            entity_id=f"$RESOURCE:{resource}",
            principal=principal,
            resource=resource,
        )

    async def list_resources_with_defaults(self) -> list[str]:
        """List all resources that have default limit configs from the resource registry."""
        client = await self._get_client()

        # Read from resource registry (single GetItem: 1 RCU)
        response = await client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_system()},
                "SK": {"S": schema.sk_resources()},
            },
            ConsistentRead=False,
        )

        # Extract resources from the string set, or return empty list if registry doesn't exist
        item = response.get("Item", {})
        resources_set = item.get("resources", {}).get("SS", [])
        return sorted(resources_set)

    # -------------------------------------------------------------------------
    # System-level default config operations (composite format, ADR-114)
    # -------------------------------------------------------------------------

    async def set_system_defaults(
        self,
        limits: list[Limit],
        on_unavailable: str | None = None,
        principal: str | None = None,
    ) -> None:
        """
        Store system-wide default limits and config (composite format, ADR-114).

        All system limits and config (on_unavailable) are stored in a single
        composite item with SK '#CONFIG'. This reduces cache-miss cost.

        Args:
            limits: List of Limit configurations (apply to all resources)
            on_unavailable: Behavior when DynamoDB unavailable ("allow" or "block")
            principal: Caller identity for audit logging
        """
        client = await self._get_client()

        # Build composite config item with all limits + on_unavailable
        item: dict[str, Any] = {
            "PK": {"S": schema.pk_system()},
            "SK": {"S": schema.sk_config()},
            "config_version": {"N": "1"},
        }

        # Add on_unavailable if provided
        if on_unavailable is not None:
            item["on_unavailable"] = {"S": on_unavailable}

        # Add l_* attributes for each limit
        self._serialize_composite_limits(limits, item)

        # Single PutItem replaces any existing system config
        await client.put_item(TableName=self.table_name, Item=item)

        # Log audit event (ADR-106: use $SYSTEM for all system-level events)
        await self._log_audit_event(
            action=AuditAction.LIMITS_SET,
            entity_id="$SYSTEM",
            principal=principal,
            details={
                "limits": [limit.to_dict() for limit in limits],
                "on_unavailable": on_unavailable,
            },
        )

    async def get_system_defaults(self) -> tuple[list[Limit], str | None]:
        """
        Get system-wide default limits and config (composite format, ADR-114).

        Reads a single composite item with SK '#CONFIG' containing all limits
        and on_unavailable setting.

        Returns:
            Tuple of (limits, on_unavailable). on_unavailable may be None if not set.
        """
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
            return [], None

        # Extract limits from composite attributes
        limits = self._deserialize_composite_limits(item)

        # Extract on_unavailable
        on_unavailable_attr = item.get("on_unavailable", {})
        on_unavailable: str | None = on_unavailable_attr.get("S") if on_unavailable_attr else None

        return limits, on_unavailable

    async def delete_system_defaults(
        self,
        principal: str | None = None,
    ) -> None:
        """
        Delete all system-wide default limits and config (composite format, ADR-114).

        Deletes the single composite config item for system defaults.

        Args:
            principal: Caller identity for audit logging
        """
        client = await self._get_client()

        # Get existing limits for audit logging before deleting
        limits, on_unavailable = await self.get_system_defaults()

        # Single DeleteItem removes the composite config
        await client.delete_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_system()},
                "SK": {"S": schema.sk_config()},
            },
        )

        # Log audit event (ADR-106: use $SYSTEM for all system-level events)
        await self._log_audit_event(
            action=AuditAction.LIMITS_DELETED,
            entity_id="$SYSTEM",
            principal=principal,
            details={
                "limits": [limit.name for limit in limits],
                "on_unavailable": on_unavailable,
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
    # Audit retention configuration
    # -------------------------------------------------------------------------

    async def _write_audit_retention_config(self) -> None:
        """
        Write audit_retention_days to system config item.

        Uses atomic UpdateItem to avoid overwriting other system config fields.
        Called from ensure_infrastructure() after stack creation.
        """
        if self._stack_options is None:
            return
        client = await self._get_client()
        await client.update_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_system()},
                "SK": {"S": schema.sk_config()},
            },
            UpdateExpression="SET audit_retention_days = :ard",
            ExpressionAttributeValues={
                ":ard": {"N": str(self._stack_options.audit_retention_days)},
            },
        )
        # Update cache
        self._audit_retention_days_cache = self._stack_options.audit_retention_days

    async def _get_audit_retention_days(self) -> int:
        """
        Get audit retention days from system config or default.

        Returns cached value if available, otherwise reads from DynamoDB.
        Falls back to stack_options if available (saves DynamoDB call).
        Default is 90 days if not configured anywhere.
        """
        if self._audit_retention_days_cache is not None:
            return self._audit_retention_days_cache

        # Try to read from stack_options first (saves DynamoDB call)
        if self._stack_options is not None:
            self._audit_retention_days_cache = self._stack_options.audit_retention_days
            return self._audit_retention_days_cache

        # Read from DynamoDB system config
        client = await self._get_client()
        response = await client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_system()},
                "SK": {"S": schema.sk_config()},
            },
            ConsistentRead=False,
        )

        item = response.get("Item", {})
        ard = item.get("audit_retention_days", {}).get("N")
        self._audit_retention_days_cache = int(ard) if ard else 90  # Default 90 days
        return self._audit_retention_days_cache

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

        # Flat schema (v0.6.0+)
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

        # Get TTL from system config (cached)
        audit_retention_days = await self._get_audit_retention_days()
        ttl_seconds = audit_retention_days * 86400

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

        # Build DynamoDB item (flat schema v0.6.0+)
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
        """Deserialize a DynamoDB item to AuditEvent (flat format only)."""
        if "action" not in item or "S" not in item.get("action", {}):
            return None

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

        elif resource is not None:
            # Query by resource across entities (GSI2)
            key_condition = "GSI2PK = :pk AND begins_with(GSI2SK, :sk_prefix)"
            expression_values = {
                ":pk": {"S": schema.gsi2_pk_resource(resource)},
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

        else:
            raise ValueError("Either entity_id or resource must be provided")

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
        """Get all buckets for a resource across all entities.

        With composite items, each GSI2 entry is one composite item per
        entity. Returns individual BucketStates, optionally filtered by limit_name.
        """
        client = await self._get_client()

        key_condition = "GSI2PK = :pk AND begins_with(GSI2SK, :sk_prefix)"
        expression_values: dict[str, Any] = {
            ":pk": {"S": schema.gsi2_pk_resource(resource)},
            ":sk_prefix": {"S": "BUCKET#"},
        }

        response = await client.query(
            TableName=self.table_name,
            IndexName=schema.GSI2_NAME,
            KeyConditionExpression=key_condition,
            ExpressionAttributeValues=expression_values,
        )

        buckets: list[BucketState] = []
        for item in response.get("Items", []):
            for bucket in self._deserialize_composite_bucket(item):
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
        """Deserialize a DynamoDB item to Entity (flat format only)."""
        entity_id = item.get("entity_id", {}).get("S", "")
        name_val = item["name"].get("S") if "name" in item else None
        parent_val = self._deserialize_value(item["parent_id"]) if "parent_id" in item else None
        cascade_val = item.get("cascade", {}).get("BOOL", False)
        metadata_val = (
            self._deserialize_map(item["metadata"].get("M", {}))
            if "metadata" in item and "M" in item.get("metadata", {})
            else {}
        )
        created_val = item.get("created_at", {}).get("S")

        return Entity(
            id=entity_id,
            name=name_val,
            parent_id=parent_val,
            cascade=cascade_val,
            metadata=metadata_val,
            created_at=created_val,
        )

    def _deserialize_bucket(self, item: dict[str, Any]) -> BucketState:
        """Deserialize a DynamoDB item to BucketState (flat format only)."""
        # Counter is stored as FLAT top-level attribute.
        # None if not present (old bucket without counter). See issue #179.
        counter_attr = item.get("total_consumed_milli", {})
        total_consumed_milli = int(counter_attr["N"]) if "N" in counter_attr else None

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

    def _deserialize_composite_bucket(self, item: dict[str, Any]) -> list[BucketState]:
        """Deserialize a composite DynamoDB item to a list of BucketStates.

        A composite bucket item stores all limits for an entity+resource in a
        single DynamoDB item. Per-limit attributes use the prefix b_{name}_{field}
        with a shared rf (refill timestamp). See ADR-114.
        """
        entity_id = item.get("entity_id", {}).get("S", "")
        resource = item.get("resource", {}).get("S", "")
        rf = int(item.get(schema.BUCKET_FIELD_RF, {}).get("N", "0"))

        # Discover limit names by scanning for b_{name}_tk attributes
        limit_names: list[str] = []
        suffix = f"_{schema.BUCKET_FIELD_TK}"
        for attr_name in item:
            if attr_name.startswith(schema.BUCKET_ATTR_PREFIX) and attr_name.endswith(suffix):
                name = attr_name[len(schema.BUCKET_ATTR_PREFIX) : -len(suffix)]
                if name:
                    limit_names.append(name)

        buckets: list[BucketState] = []
        for name in limit_names:

            def _get(field: str) -> int:
                attr = schema.bucket_attr(name, field)
                return int(item.get(attr, {}).get("N", "0"))

            tc_attr = item.get(schema.bucket_attr(name, schema.BUCKET_FIELD_TC), {})
            total_consumed = int(tc_attr["N"]) if "N" in tc_attr else None

            buckets.append(
                BucketState(
                    entity_id=entity_id,
                    resource=resource,
                    limit_name=name,
                    tokens_milli=_get(schema.BUCKET_FIELD_TK),
                    last_refill_ms=rf,
                    capacity_milli=_get(schema.BUCKET_FIELD_CP),
                    burst_milli=_get(schema.BUCKET_FIELD_BX),
                    refill_amount_milli=_get(schema.BUCKET_FIELD_RA),
                    refill_period_ms=_get(schema.BUCKET_FIELD_RP),
                    total_consumed_milli=total_consumed,
                )
            )

        return buckets

    # -------------------------------------------------------------------------
    # Composite limit config serialization (ADR-114 for configs)
    # -------------------------------------------------------------------------

    def _serialize_composite_limits(
        self,
        limits: list[Limit],
        base_item: dict[str, Any],
    ) -> dict[str, Any]:
        """Add l_* attributes to a DynamoDB item for composite limit storage.

        Args:
            limits: List of Limit objects to serialize
            base_item: Base DynamoDB item to add attributes to (mutated in place)

        Returns:
            The modified base_item with l_{name}_{field} attributes added
        """
        for limit in limits:
            name = limit.name
            base_item[schema.limit_attr(name, schema.LIMIT_FIELD_CP)] = {"N": str(limit.capacity)}
            base_item[schema.limit_attr(name, schema.LIMIT_FIELD_BX)] = {"N": str(limit.burst)}
            base_item[schema.limit_attr(name, schema.LIMIT_FIELD_RA)] = {
                "N": str(limit.refill_amount)
            }
            base_item[schema.limit_attr(name, schema.LIMIT_FIELD_RP)] = {
                "N": str(limit.refill_period_seconds)
            }
        return base_item

    def _deserialize_composite_limits(self, item: dict[str, Any]) -> list[Limit]:
        """Deserialize l_* attributes from a DynamoDB item to Limit objects.

        Discovers limit names by scanning for l_{name}_cp attributes.

        Args:
            item: DynamoDB item with l_{name}_{field} attributes

        Returns:
            List of Limit objects reconstructed from composite attributes
        """
        # Discover limit names by scanning for l_{name}_cp attributes
        limit_names: list[str] = []
        suffix = f"_{schema.LIMIT_FIELD_CP}"
        for attr_name in item:
            if attr_name.startswith(schema.LIMIT_ATTR_PREFIX) and attr_name.endswith(suffix):
                name = attr_name[len(schema.LIMIT_ATTR_PREFIX) : -len(suffix)]
                if name:
                    limit_names.append(name)

        limits: list[Limit] = []
        for name in limit_names:

            def _get(field: str) -> int:
                attr = schema.limit_attr(name, field)
                return int(item.get(attr, {}).get("N", "0"))

            limits.append(
                Limit(
                    name=name,
                    capacity=_get(schema.LIMIT_FIELD_CP),
                    burst=_get(schema.LIMIT_FIELD_BX),
                    refill_amount=_get(schema.LIMIT_FIELD_RA),
                    refill_period_seconds=_get(schema.LIMIT_FIELD_RP),
                )
            )

        return limits


# Type assertion: Repository implements RepositoryProtocol
# This is verified at type-check time by mypy, not at runtime
if TYPE_CHECKING:
    from .repository_protocol import RepositoryProtocol

    _: RepositoryProtocol = cast(Repository, None)
