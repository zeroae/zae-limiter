"""Integration tests for Repository using LocalStack.

These tests run against a real LocalStack instance with full AWS service emulation
including CloudFormation, DynamoDB, DynamoDB Streams, and Lambda.

To run these tests locally:
    # Start LocalStack (from project root)
    docker compose up -d

    # Set environment variables and run tests
    export AWS_ENDPOINT_URL=http://localhost:4566
    export AWS_ACCESS_KEY_ID=test
    export AWS_SECRET_ACCESS_KEY=test
    export AWS_DEFAULT_REGION=us-east-1
    pytest tests/integration/test_repository.py -v
"""

import time

import pytest

from zae_limiter import Limit, StackOptions
from zae_limiter.models import BucketState
from zae_limiter.repository import Repository

pytestmark = pytest.mark.integration

# Fixtures from conftest.py:
# - localstack_endpoint: LocalStack URL
# - localstack_repo_module: Module-scoped repository (shared across tests)
# - unique_entity_prefix: Unique prefix for data isolation within shared table


class TestRepositoryLocalStackCloudFormation:
    """Integration tests for CloudFormation stack operations."""

    @pytest.mark.asyncio
    async def test_ensure_infrastructure_uses_cloudformation(
        self, localstack_endpoint, minimal_stack_options, unique_name
    ):
        """Should create CloudFormation stack via ensure_infrastructure."""
        # Pass stack_options to constructor - this is the new API pattern
        repo = Repository(
            name=unique_name,
            endpoint_url=localstack_endpoint,
            region="us-east-1",
            stack_options=minimal_stack_options,
        )

        try:
            # ensure_infrastructure creates the stack (no-op if stack_options was None)
            await repo.ensure_infrastructure()

            # Verify table was created by trying to use it
            await repo.create_entity("test-entity", name="Test Entity")
            entity = await repo.get_entity("test-entity")
            assert entity is not None
            assert entity.id == "test-entity"
            assert entity.name == "Test Entity"

        finally:
            # Cleanup
            try:
                await repo.delete_table()
            except Exception:
                pass
            await repo.close()

    @pytest.mark.asyncio
    async def test_ensure_infrastructure_with_custom_parameters(
        self, localstack_endpoint, unique_name
    ):
        """Should pass custom parameters to CloudFormation stack."""
        # Pass stack_options to constructor - this is the new API pattern
        stack_options = StackOptions(
            snapshot_windows="hourly,daily",
            usage_retention_days=90,
            enable_aggregator=False,
            enable_alarms=False,
        )
        repo = Repository(
            name=unique_name,
            endpoint_url=localstack_endpoint,
            region="us-east-1",
            stack_options=stack_options,
        )

        try:
            # ensure_infrastructure creates the stack with the configured options
            await repo.ensure_infrastructure()

            # Verify table was created
            await repo.create_entity("test-entity")
            entity = await repo.get_entity("test-entity")
            assert entity is not None

        finally:
            try:
                await repo.delete_table()
            except Exception:
                pass
            await repo.close()


class TestRepositoryLocalStackTransactions:
    """Integration tests for transactions with real DynamoDB.

    Uses module-scoped localstack_repo_module fixture for shared infrastructure.
    Tests use unique_entity_prefix for data isolation.
    """

    @pytest.mark.asyncio(loop_scope="module")
    async def test_concurrent_updates_with_optimistic_locking(
        self, localstack_repo_module, unique_entity_prefix
    ):
        """Test optimistic locking detects concurrent updates."""
        repo = localstack_repo_module
        entity_id = f"{unique_entity_prefix}-entity-1"

        # Create entity and bucket
        await repo.create_entity(entity_id)
        limit = Limit.per_minute("rpm", 100)
        now_ms = int(time.time() * 1000)
        state = BucketState.from_limit(entity_id, "api", limit, now_ms)
        await repo.transact_write([repo.build_bucket_put_item(state)])

        # Get current bucket state
        bucket = await repo.get_bucket(entity_id, "api", "rpm")
        assert bucket is not None
        original_tokens = bucket.tokens_milli

        # Simulate concurrent update with correct expected value
        update_item = repo.build_bucket_update_item(
            entity_id=entity_id,
            resource="api",
            limit_name="rpm",
            new_tokens_milli=50_000,
            new_last_refill_ms=now_ms + 1000,
            expected_tokens_milli=original_tokens,  # Optimistic lock
        )

        # This should succeed
        await repo.transact_write([update_item])

        # Verify update worked
        bucket = await repo.get_bucket(entity_id, "api", "rpm")
        assert bucket.tokens_milli == 50_000

        # Now try to update again with stale expected value (should fail)
        update_item_stale = repo.build_bucket_update_item(
            entity_id=entity_id,
            resource="api",
            limit_name="rpm",
            new_tokens_milli=75_000,
            new_last_refill_ms=now_ms + 2000,
            expected_tokens_milli=original_tokens,  # Stale value!
        )

        # This should fail due to conditional check
        from botocore.exceptions import ClientError

        with pytest.raises(ClientError) as exc_info:
            await repo.transact_write([update_item_stale])

        # Verify it was a conditional check failure
        assert exc_info.value.response["Error"]["Code"] in [
            "TransactionCanceledException",
            "ConditionalCheckFailedException",
        ]

    @pytest.mark.asyncio(loop_scope="module")
    async def test_transaction_atomicity_on_partial_failure(
        self, localstack_repo_module, unique_entity_prefix
    ):
        """Verify transactions are all-or-nothing."""
        repo = localstack_repo_module
        entity_id_1 = f"{unique_entity_prefix}-entity-1"
        entity_id_2 = f"{unique_entity_prefix}-entity-2"

        # Create two entities
        await repo.create_entity(entity_id_1)
        await repo.create_entity(entity_id_2)

        # Create buckets for both
        limit = Limit.per_minute("rpm", 100)
        now_ms = int(time.time() * 1000)

        state1 = BucketState.from_limit(entity_id_1, "api", limit, now_ms)
        state2 = BucketState.from_limit(entity_id_2, "api", limit, now_ms)

        await repo.transact_write(
            [
                repo.build_bucket_put_item(state1),
                repo.build_bucket_put_item(state2),
            ]
        )

        # Get both bucket states
        bucket1 = await repo.get_bucket(entity_id_1, "api", "rpm")
        bucket2 = await repo.get_bucket(entity_id_2, "api", "rpm")
        assert bucket1 is not None
        assert bucket2 is not None

        # Create transaction with one valid update and one that will fail
        valid_update = repo.build_bucket_update_item(
            entity_id=entity_id_1,
            resource="api",
            limit_name="rpm",
            new_tokens_milli=50_000,
            new_last_refill_ms=now_ms + 1000,
        )

        failing_update = repo.build_bucket_update_item(
            entity_id=entity_id_2,
            resource="api",
            limit_name="rpm",
            new_tokens_milli=75_000,
            new_last_refill_ms=now_ms + 1000,
            expected_tokens_milli=999_999,  # Wrong value - will fail
        )

        # Transaction should fail
        from botocore.exceptions import ClientError

        with pytest.raises(ClientError):
            await repo.transact_write([valid_update, failing_update])

        # Verify NEITHER update was applied (atomicity)
        bucket1_after = await repo.get_bucket(entity_id_1, "api", "rpm")
        bucket2_after = await repo.get_bucket(entity_id_2, "api", "rpm")

        # Both should still have original values
        assert bucket1_after.tokens_milli == bucket1.tokens_milli
        assert bucket2_after.tokens_milli == bucket2.tokens_milli

    @pytest.mark.asyncio(loop_scope="module")
    async def test_batch_write_pagination_over_25_items(
        self, localstack_repo_module, unique_entity_prefix
    ):
        """Batch operations should handle >25 items via chunking."""
        repo = localstack_repo_module

        # Create 30 entities
        entity_ids = [f"{unique_entity_prefix}-entity-{i}" for i in range(30)]
        for entity_id in entity_ids:
            await repo.create_entity(entity_id)

        # Create buckets for all (tests internal batching)
        limit = Limit.per_minute("rpm", 100)
        now_ms = int(time.time() * 1000)

        for entity_id in entity_ids:
            state = BucketState.from_limit(entity_id, "api", limit, now_ms)
            await repo.transact_write([repo.build_bucket_put_item(state)])

        # Delete first entity - internally handles batch pagination
        first_entity = entity_ids[0]
        await repo.delete_entity(first_entity)

        # Verify entity and bucket are deleted
        entity = await repo.get_entity(first_entity)
        assert entity is None

        bucket = await repo.get_bucket(first_entity, "api", "rpm")
        assert bucket is None


class TestRepositoryLocalStackResourceAggregation:
    """Integration tests for GSI2 queries.

    Uses module-scoped localstack_repo_module fixture for shared infrastructure.
    Tests use unique_entity_prefix for data isolation.
    """

    @pytest.mark.asyncio(loop_scope="module")
    async def test_get_resource_buckets_across_multiple_entities(
        self, localstack_repo_module, unique_entity_prefix
    ):
        """Should query all buckets for a resource across entities."""
        repo = localstack_repo_module
        # Use unique resource name to avoid cross-test interference in GSI2 queries
        resource = f"{unique_entity_prefix}-gpt-4"

        # Create multiple entities
        entity_ids = [
            f"{unique_entity_prefix}-entity-a",
            f"{unique_entity_prefix}-entity-b",
            f"{unique_entity_prefix}-entity-c",
        ]
        for entity_id in entity_ids:
            await repo.create_entity(entity_id)

        # Create composite buckets: one item per entity+resource with all limits
        limits = [
            Limit.per_minute("rpm", 100),
            Limit.per_minute("tpm", 10_000),
        ]
        now_ms = int(time.time() * 1000)

        for entity_id in entity_ids:
            states = [
                BucketState.from_limit(entity_id, resource, limit, now_ms) for limit in limits
            ]
            put_item = repo.build_composite_create(entity_id, resource, states, now_ms)
            await repo.transact_write([put_item])

        # Query resource buckets via GSI2
        rpm_buckets = await repo.get_resource_buckets(resource, "rpm")
        tpm_buckets = await repo.get_resource_buckets(resource, "tpm")

        # Should get all entities for each limit
        assert len(rpm_buckets) == 3
        assert len(tpm_buckets) == 3

        # Verify correct entities
        rpm_entity_ids = {b.entity_id for b in rpm_buckets}
        tpm_entity_ids = {b.entity_id for b in tpm_buckets}
        assert rpm_entity_ids == set(entity_ids)
        assert tpm_entity_ids == set(entity_ids)

        # Verify all are for correct resource and limit
        assert all(b.resource == resource for b in rpm_buckets)
        assert all(b.limit_name == "rpm" for b in rpm_buckets)
        assert all(b.resource == resource for b in tpm_buckets)
        assert all(b.limit_name == "tpm" for b in tpm_buckets)


class TestRepositoryBatchGetBuckets:
    """Integration tests for batch_get_buckets() using BatchGetItem.

    Uses module-scoped localstack_repo_module fixture for shared infrastructure.
    Tests use unique_entity_prefix for data isolation.
    """

    @pytest.mark.asyncio(loop_scope="module")
    async def test_batch_get_buckets_multiple_buckets(
        self, localstack_repo_module, unique_entity_prefix
    ):
        """Should fetch multiple composite buckets in a single batch call."""
        repo = localstack_repo_module

        # Create test entities
        entity_ids = [
            f"{unique_entity_prefix}-batch-1",
            f"{unique_entity_prefix}-batch-2",
            f"{unique_entity_prefix}-batch-3",
        ]
        for entity_id in entity_ids:
            await repo.create_entity(entity_id)

        # Create composite buckets: one item per entity+resource with all limits
        limits = [
            Limit.per_minute("rpm", 100),
            Limit.per_minute("tpm", 10_000),
        ]
        now_ms = int(time.time() * 1000)

        for entity_id in entity_ids:
            states = [BucketState.from_limit(entity_id, "gpt-4", limit, now_ms) for limit in limits]
            put_item = repo.build_composite_create(entity_id, "gpt-4", states, now_ms)
            await repo.transact_write([put_item])

        # Batch get composite items (2-tuple keys: entity_id, resource)
        keys = [(entity_id, "gpt-4") for entity_id in entity_ids]
        result = await repo.batch_get_buckets(keys)

        # Should return all 6 bucket states (3 entities × 2 limits per composite item)
        assert len(result) == 6

        # Result uses 3-tuple keys for backward compatibility
        for entity_id in entity_ids:
            for limit in limits:
                key = (entity_id, "gpt-4", limit.name)
                assert key in result
                bucket = result[key]
                assert bucket.entity_id == entity_id
                assert bucket.resource == "gpt-4"
                assert bucket.limit_name == limit.name

    @pytest.mark.asyncio(loop_scope="module")
    async def test_batch_get_buckets_empty_key_list(self, localstack_repo_module):
        """Should return empty dict for empty key list."""
        result = await localstack_repo_module.batch_get_buckets([])
        assert result == {}

    @pytest.mark.asyncio(loop_scope="module")
    async def test_batch_get_buckets_deduplication(
        self, localstack_repo_module, unique_entity_prefix
    ):
        """Should deduplicate duplicate keys in the request."""
        repo = localstack_repo_module
        entity_id = f"{unique_entity_prefix}-dedup"

        # Create entity and composite bucket
        await repo.create_entity(entity_id)
        limit = Limit.per_minute("rpm", 100)
        now_ms = int(time.time() * 1000)
        state = BucketState.from_limit(entity_id, "api", limit, now_ms)
        await repo.transact_write([repo.build_bucket_put_item(state)])

        # Request with duplicate 2-tuple keys
        keys = [
            (entity_id, "api"),
            (entity_id, "api"),  # Duplicate
            (entity_id, "api"),  # Another duplicate
        ]
        result = await repo.batch_get_buckets(keys)

        # Should return single bucket state (deduplication worked)
        assert len(result) == 1
        assert (entity_id, "api", "rpm") in result

    @pytest.mark.asyncio(loop_scope="module")
    async def test_batch_get_buckets_missing_buckets(
        self, localstack_repo_module, unique_entity_prefix
    ):
        """Should only return existing buckets, omitting missing ones."""
        repo = localstack_repo_module
        entity_id = f"{unique_entity_prefix}-exists"
        missing_entity_id = f"{unique_entity_prefix}-missing"

        # Create one entity with one composite bucket (single limit)
        await repo.create_entity(entity_id)
        limit = Limit.per_minute("rpm", 100)
        now_ms = int(time.time() * 1000)
        state = BucketState.from_limit(entity_id, "api", limit, now_ms)
        await repo.transact_write([repo.build_bucket_put_item(state)])

        # Request mix of existing and non-existing composite items (2-tuple keys)
        keys = [
            (entity_id, "api"),  # Exists (has rpm)
            (entity_id, "other"),  # Resource doesn't exist
            (missing_entity_id, "api"),  # Entity doesn't exist
        ]
        result = await repo.batch_get_buckets(keys)

        # Should only return the one existing bucket
        assert len(result) == 1
        assert (entity_id, "api", "rpm") in result

    @pytest.mark.asyncio(loop_scope="module")
    async def test_batch_get_buckets_chunking_over_100_items(
        self, localstack_repo_module, unique_entity_prefix
    ):
        """Should automatically chunk requests for >100 items."""
        repo = localstack_repo_module

        # Create 110 entities (to test chunking at 100 items)
        entity_ids = [f"{unique_entity_prefix}-chunk-{i}" for i in range(110)]
        for entity_id in entity_ids:
            await repo.create_entity(entity_id)

        # Create one composite bucket per entity
        limit = Limit.per_minute("rpm", 100)
        now_ms = int(time.time() * 1000)

        for entity_id in entity_ids:
            state = BucketState.from_limit(entity_id, "api", limit, now_ms)
            await repo.transact_write([repo.build_bucket_put_item(state)])

        # Batch get all 110 composite items (2-tuple keys, requires 2 DynamoDB calls)
        keys = [(entity_id, "api") for entity_id in entity_ids]
        result = await repo.batch_get_buckets(keys)

        # Should return all 110 bucket states
        assert len(result) == 110

        # Result uses 3-tuple keys for backward compatibility
        for entity_id in entity_ids:
            key = (entity_id, "api", "rpm")
            assert key in result
            assert result[key].entity_id == entity_id


class TestRepositoryPing:
    """Integration tests for Repository.ping() health check.

    Uses module-scoped localstack_repo_module fixture for shared infrastructure.
    """

    @pytest.mark.asyncio(loop_scope="module")
    async def test_ping_returns_true_when_table_exists(self, localstack_repo_module):
        """ping() should return True when table is reachable."""
        result = await localstack_repo_module.ping()
        assert result is True

    @pytest.mark.asyncio
    async def test_ping_returns_false_when_table_missing(self, localstack_endpoint):
        """ping() should return False when table doesn't exist."""
        repo = Repository(
            name="ping-missing-table-test",
            endpoint_url=localstack_endpoint,
            region="us-east-1",
        )
        # Don't create the table - ping should return False
        result = await repo.ping()
        assert result is False
        await repo.close()


class TestBucketTTLDowngrade:
    """Integration tests for bucket TTL when entity downgrades from custom to default limits."""

    @pytest.mark.asyncio
    async def test_ttl_set_after_deleting_entity_config(
        self,
        localstack_limiter,
    ):
        """TTL is set on bucket when entity's custom limits are deleted (issue #293).

        Full workflow:
        1. Set system defaults
        2. Set entity-level custom limits
        3. Acquire (no TTL)
        4. Delete entity limits
        5. Acquire again (TTL should be set)
        """
        from zae_limiter import Limit
        from zae_limiter.schema import pk_entity, sk_bucket

        limiter = localstack_limiter

        # Set system defaults
        await limiter.set_system_defaults([Limit.per_minute("rpm", 100)])

        # Set entity-level config (custom limits)
        await limiter.set_limits("user-downgrade", [Limit.per_minute("rpm", 200)], resource="api")

        # Acquire with custom limits - should NOT have TTL
        async with limiter.acquire(
            entity_id="user-downgrade",
            resource="api",
            consume={"rpm": 1},
        ):
            pass

        # Verify no TTL (entity has custom config)
        item = await limiter._repository._get_item(pk_entity("user-downgrade"), sk_bucket("api"))
        assert item is not None, "Bucket should exist after acquire"
        assert "ttl" not in item, "Custom config entity should NOT have TTL"

        # Delete entity-level config (downgrade to defaults)
        await limiter.delete_limits("user-downgrade", resource="api")

        # Invalidate cache to ensure deleted config is recognized
        await limiter.invalidate_config_cache()

        # Acquire again - should now set TTL since entity uses defaults
        async with limiter.acquire(
            entity_id="user-downgrade",
            resource="api",
            consume={"rpm": 1},
        ):
            pass

        # Verify TTL is now set (entity uses default config)
        item = await limiter._repository._get_item(pk_entity("user-downgrade"), sk_bucket("api"))
        assert item is not None, "Bucket should still exist"
        assert "ttl" in item, "Default config entity should have TTL after downgrade"


class TestEntityConfigRegistry:
    """Integration tests for entity config registry (issue #288).

    Tests verify real transaction atomicity and race condition handling
    in LocalStack DynamoDB.

    Uses module-scoped localstack_repo_module fixture for shared infrastructure.
    Tests use unique_entity_prefix for data isolation.
    """

    @pytest.mark.asyncio(loop_scope="module")
    async def test_set_limits_transaction_atomicity(
        self, localstack_repo_module, unique_entity_prefix
    ):
        """Verify set_limits atomically creates config AND increments registry."""
        repo = localstack_repo_module
        entity_id = f"{unique_entity_prefix}-user-1"
        # Use unique resource name to avoid cross-test interference in registry
        resource = f"{unique_entity_prefix}-gpt-4"

        await repo.create_entity(entity_id)
        limits = [Limit.per_minute("rpm", 1000)]

        # Set limits - should atomically create config + increment registry
        await repo.set_limits(entity_id, limits, resource=resource)

        # Verify both operations succeeded
        stored_limits = await repo.get_limits(entity_id, resource=resource)
        assert len(stored_limits) == 1
        assert stored_limits[0].name == "rpm"

        resources = await repo.list_resources_with_entity_configs()
        assert resource in resources

    @pytest.mark.asyncio(loop_scope="module")
    async def test_delete_limits_transaction_atomicity(
        self, localstack_repo_module, unique_entity_prefix
    ):
        """Verify delete_limits atomically removes config AND decrements registry."""
        repo = localstack_repo_module
        entity_id = f"{unique_entity_prefix}-user-1"
        resource = f"{unique_entity_prefix}-gpt-4"

        await repo.create_entity(entity_id)
        limits = [Limit.per_minute("rpm", 1000)]
        await repo.set_limits(entity_id, limits, resource=resource)

        # Delete limits - should atomically delete config + decrement registry
        await repo.delete_limits(entity_id, resource=resource)

        # Verify both operations succeeded
        stored_limits = await repo.get_limits(entity_id, resource=resource)
        assert stored_limits == []

        resources = await repo.list_resources_with_entity_configs()
        assert resource not in resources

    @pytest.mark.asyncio(loop_scope="module")
    async def test_delete_nonexistent_config_no_side_effects(
        self, localstack_repo_module, unique_entity_prefix
    ):
        """Deleting non-existent config should not affect registry or create audit."""
        repo = localstack_repo_module
        entity_id = f"{unique_entity_prefix}-user-1"
        resource = f"{unique_entity_prefix}-gpt-4"

        await repo.create_entity(entity_id)

        # Delete limits that don't exist
        await repo.delete_limits(entity_id, resource=resource)

        # Registry should not contain this resource (no decrement to negative)
        resources = await repo.list_resources_with_entity_configs()
        assert resource not in resources

    @pytest.mark.asyncio(loop_scope="module")
    async def test_registry_cleanup_at_zero(self, localstack_repo_module, unique_entity_prefix):
        """Verify registry attribute is removed when count reaches zero."""
        repo = localstack_repo_module
        entity_id_1 = f"{unique_entity_prefix}-user-1"
        entity_id_2 = f"{unique_entity_prefix}-user-2"
        resource = f"{unique_entity_prefix}-gpt-4"

        await repo.create_entity(entity_id_1)
        await repo.create_entity(entity_id_2)
        limits = [Limit.per_minute("rpm", 1000)]

        # Create two configs for same resource
        await repo.set_limits(entity_id_1, limits, resource=resource)
        await repo.set_limits(entity_id_2, limits, resource=resource)

        resources = await repo.list_resources_with_entity_configs()
        assert resource in resources

        # Delete both - count should reach 0 and attribute removed
        await repo.delete_limits(entity_id_1, resource=resource)
        await repo.delete_limits(entity_id_2, resource=resource)

        resources = await repo.list_resources_with_entity_configs()
        assert resource not in resources

    @pytest.mark.asyncio(loop_scope="module")
    async def test_update_existing_config_no_double_count(
        self, localstack_repo_module, unique_entity_prefix
    ):
        """Updating existing config should not increment registry twice."""
        from zae_limiter import schema

        repo = localstack_repo_module
        entity_id = f"{unique_entity_prefix}-user-1"
        resource = f"{unique_entity_prefix}-gpt-4"

        await repo.create_entity(entity_id)
        limits1 = [Limit.per_minute("rpm", 1000)]
        limits2 = [Limit.per_minute("rpm", 2000)]

        # Set limits twice (second is UPDATE, not NEW)
        await repo.set_limits(entity_id, limits1, resource=resource)
        await repo.set_limits(entity_id, limits2, resource=resource)

        # Verify registry count is exactly 1
        client = await repo._get_client()
        response = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_system()},
                "SK": {"S": schema.sk_entity_config_resources()},
            },
        )
        item = response.get("Item", {})
        count = int(item.get(resource, {}).get("N", "0"))
        assert count == 1, "Registry should have count=1, not 2"
