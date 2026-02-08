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

# localstack_endpoint and StackOptions fixtures are defined in conftest.py


@pytest.fixture
async def localstack_repo(localstack_endpoint, unique_name):
    """Repository connected to LocalStack (direct table creation, no CloudFormation)."""
    repo = Repository(
        name=unique_name,
        endpoint_url=localstack_endpoint,
        region="us-east-1",
    )
    await repo.create_table()
    yield repo
    try:
        await repo.delete_table()
    except Exception:
        pass  # Table might not exist
    await repo.close()


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
    """Integration tests for transactions with real DynamoDB."""

    @pytest.mark.asyncio
    async def test_concurrent_updates_with_optimistic_locking(self, localstack_repo):
        """Test optimistic locking detects concurrent updates."""
        # Create entity and bucket
        await localstack_repo.create_entity("entity-1")
        limit = Limit.per_minute("rpm", 100)
        now_ms = int(time.time() * 1000)
        state = BucketState.from_limit("entity-1", "api", limit, now_ms)
        await localstack_repo.transact_write([localstack_repo.build_bucket_put_item(state)])

        # Get current bucket state
        bucket = await localstack_repo.get_bucket("entity-1", "api", "rpm")
        assert bucket is not None
        original_tokens = bucket.tokens_milli

        # Simulate concurrent update with correct expected value
        update_item = localstack_repo.build_bucket_update_item(
            entity_id="entity-1",
            resource="api",
            limit_name="rpm",
            new_tokens_milli=50_000,
            new_last_refill_ms=now_ms + 1000,
            expected_tokens_milli=original_tokens,  # Optimistic lock
        )

        # This should succeed
        await localstack_repo.transact_write([update_item])

        # Verify update worked
        bucket = await localstack_repo.get_bucket("entity-1", "api", "rpm")
        assert bucket.tokens_milli == 50_000

        # Now try to update again with stale expected value (should fail)
        update_item_stale = localstack_repo.build_bucket_update_item(
            entity_id="entity-1",
            resource="api",
            limit_name="rpm",
            new_tokens_milli=75_000,
            new_last_refill_ms=now_ms + 2000,
            expected_tokens_milli=original_tokens,  # Stale value!
        )

        # This should fail due to conditional check
        from botocore.exceptions import ClientError

        with pytest.raises(ClientError) as exc_info:
            await localstack_repo.transact_write([update_item_stale])

        # Verify it was a conditional check failure
        assert exc_info.value.response["Error"]["Code"] in [
            "TransactionCanceledException",
            "ConditionalCheckFailedException",
        ]

    @pytest.mark.asyncio
    async def test_transaction_atomicity_on_partial_failure(self, localstack_repo):
        """Verify transactions are all-or-nothing."""
        # Create two entities
        await localstack_repo.create_entity("entity-1")
        await localstack_repo.create_entity("entity-2")

        # Create buckets for both
        limit = Limit.per_minute("rpm", 100)
        now_ms = int(time.time() * 1000)

        state1 = BucketState.from_limit("entity-1", "api", limit, now_ms)
        state2 = BucketState.from_limit("entity-2", "api", limit, now_ms)

        await localstack_repo.transact_write(
            [
                localstack_repo.build_bucket_put_item(state1),
                localstack_repo.build_bucket_put_item(state2),
            ]
        )

        # Get both bucket states
        bucket1 = await localstack_repo.get_bucket("entity-1", "api", "rpm")
        bucket2 = await localstack_repo.get_bucket("entity-2", "api", "rpm")
        assert bucket1 is not None
        assert bucket2 is not None

        # Create transaction with one valid update and one that will fail
        valid_update = localstack_repo.build_bucket_update_item(
            entity_id="entity-1",
            resource="api",
            limit_name="rpm",
            new_tokens_milli=50_000,
            new_last_refill_ms=now_ms + 1000,
        )

        failing_update = localstack_repo.build_bucket_update_item(
            entity_id="entity-2",
            resource="api",
            limit_name="rpm",
            new_tokens_milli=75_000,
            new_last_refill_ms=now_ms + 1000,
            expected_tokens_milli=999_999,  # Wrong value - will fail
        )

        # Transaction should fail
        from botocore.exceptions import ClientError

        with pytest.raises(ClientError):
            await localstack_repo.transact_write([valid_update, failing_update])

        # Verify NEITHER update was applied (atomicity)
        bucket1_after = await localstack_repo.get_bucket("entity-1", "api", "rpm")
        bucket2_after = await localstack_repo.get_bucket("entity-2", "api", "rpm")

        # Both should still have original values
        assert bucket1_after.tokens_milli == bucket1.tokens_milli
        assert bucket2_after.tokens_milli == bucket2.tokens_milli

    @pytest.mark.asyncio
    async def test_batch_write_pagination_over_25_items(self, localstack_repo):
        """Batch operations should handle >25 items via chunking."""
        # Create 30 entities
        entity_ids = [f"entity-{i}" for i in range(30)]
        for entity_id in entity_ids:
            await localstack_repo.create_entity(entity_id)

        # Create buckets for all (tests internal batching)
        limit = Limit.per_minute("rpm", 100)
        now_ms = int(time.time() * 1000)

        for entity_id in entity_ids:
            state = BucketState.from_limit(entity_id, "api", limit, now_ms)
            await localstack_repo.transact_write([localstack_repo.build_bucket_put_item(state)])

        # Delete first entity - internally handles batch pagination
        await localstack_repo.delete_entity("entity-0")

        # Verify entity and bucket are deleted
        entity = await localstack_repo.get_entity("entity-0")
        assert entity is None

        bucket = await localstack_repo.get_bucket("entity-0", "api", "rpm")
        assert bucket is None


class TestRepositoryLocalStackResourceAggregation:
    """Integration tests for GSI2 queries."""

    @pytest.mark.asyncio
    async def test_get_resource_buckets_across_multiple_entities(self, localstack_repo):
        """Should query all buckets for a resource across entities."""
        # Create multiple entities
        entity_ids = ["entity-a", "entity-b", "entity-c"]
        for entity_id in entity_ids:
            await localstack_repo.create_entity(entity_id)

        # Create composite buckets: one item per entity+resource with all limits
        limits = [
            Limit.per_minute("rpm", 100),
            Limit.per_minute("tpm", 10_000),
        ]
        now_ms = int(time.time() * 1000)

        for entity_id in entity_ids:
            states = [BucketState.from_limit(entity_id, "gpt-4", limit, now_ms) for limit in limits]
            put_item = localstack_repo.build_composite_create(entity_id, "gpt-4", states, now_ms)
            await localstack_repo.transact_write([put_item])

        # Query resource buckets via GSI2
        rpm_buckets = await localstack_repo.get_resource_buckets("gpt-4", "rpm")
        tpm_buckets = await localstack_repo.get_resource_buckets("gpt-4", "tpm")

        # Should get all entities for each limit
        assert len(rpm_buckets) == 3
        assert len(tpm_buckets) == 3

        # Verify correct entities
        rpm_entity_ids = {b.entity_id for b in rpm_buckets}
        tpm_entity_ids = {b.entity_id for b in tpm_buckets}
        assert rpm_entity_ids == set(entity_ids)
        assert tpm_entity_ids == set(entity_ids)

        # Verify all are for correct resource and limit
        assert all(b.resource == "gpt-4" for b in rpm_buckets)
        assert all(b.limit_name == "rpm" for b in rpm_buckets)
        assert all(b.resource == "gpt-4" for b in tpm_buckets)
        assert all(b.limit_name == "tpm" for b in tpm_buckets)


class TestRepositoryBatchGetBuckets:
    """Integration tests for batch_get_buckets() using BatchGetItem."""

    @pytest.mark.asyncio
    async def test_batch_get_buckets_multiple_buckets(self, localstack_repo):
        """Should fetch multiple composite buckets in a single batch call."""
        # Create test entities
        entity_ids = ["batch-entity-1", "batch-entity-2", "batch-entity-3"]
        for entity_id in entity_ids:
            await localstack_repo.create_entity(entity_id)

        # Create composite buckets: one item per entity+resource with all limits
        limits = [
            Limit.per_minute("rpm", 100),
            Limit.per_minute("tpm", 10_000),
        ]
        now_ms = int(time.time() * 1000)

        for entity_id in entity_ids:
            states = [BucketState.from_limit(entity_id, "gpt-4", limit, now_ms) for limit in limits]
            put_item = localstack_repo.build_composite_create(entity_id, "gpt-4", states, now_ms)
            await localstack_repo.transact_write([put_item])

        # Batch get composite items (2-tuple keys: entity_id, resource)
        keys = [(entity_id, "gpt-4") for entity_id in entity_ids]
        result = await localstack_repo.batch_get_buckets(keys)

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

    @pytest.mark.asyncio
    async def test_batch_get_buckets_empty_key_list(self, localstack_repo):
        """Should return empty dict for empty key list."""
        result = await localstack_repo.batch_get_buckets([])
        assert result == {}

    @pytest.mark.asyncio
    async def test_batch_get_buckets_deduplication(self, localstack_repo):
        """Should deduplicate duplicate keys in the request."""
        # Create entity and composite bucket
        await localstack_repo.create_entity("dedup-entity")
        limit = Limit.per_minute("rpm", 100)
        now_ms = int(time.time() * 1000)
        state = BucketState.from_limit("dedup-entity", "api", limit, now_ms)
        await localstack_repo.transact_write([localstack_repo.build_bucket_put_item(state)])

        # Request with duplicate 2-tuple keys
        keys = [
            ("dedup-entity", "api"),
            ("dedup-entity", "api"),  # Duplicate
            ("dedup-entity", "api"),  # Another duplicate
        ]
        result = await localstack_repo.batch_get_buckets(keys)

        # Should return single bucket state (deduplication worked)
        assert len(result) == 1
        assert ("dedup-entity", "api", "rpm") in result

    @pytest.mark.asyncio
    async def test_batch_get_buckets_missing_buckets(self, localstack_repo):
        """Should only return existing buckets, omitting missing ones."""
        # Create one entity with one composite bucket (single limit)
        await localstack_repo.create_entity("exists-entity")
        limit = Limit.per_minute("rpm", 100)
        now_ms = int(time.time() * 1000)
        state = BucketState.from_limit("exists-entity", "api", limit, now_ms)
        await localstack_repo.transact_write([localstack_repo.build_bucket_put_item(state)])

        # Request mix of existing and non-existing composite items (2-tuple keys)
        keys = [
            ("exists-entity", "api"),  # Exists (has rpm)
            ("exists-entity", "other"),  # Resource doesn't exist
            ("missing-entity", "api"),  # Entity doesn't exist
        ]
        result = await localstack_repo.batch_get_buckets(keys)

        # Should only return the one existing bucket
        assert len(result) == 1
        assert ("exists-entity", "api", "rpm") in result

    @pytest.mark.asyncio
    async def test_batch_get_buckets_chunking_over_100_items(self, localstack_repo):
        """Should automatically chunk requests for >100 items."""
        # Create 110 entities (to test chunking at 100 items)
        entity_ids = [f"chunk-entity-{i}" for i in range(110)]
        for entity_id in entity_ids:
            await localstack_repo.create_entity(entity_id)

        # Create one composite bucket per entity
        limit = Limit.per_minute("rpm", 100)
        now_ms = int(time.time() * 1000)

        for entity_id in entity_ids:
            state = BucketState.from_limit(entity_id, "api", limit, now_ms)
            await localstack_repo.transact_write([localstack_repo.build_bucket_put_item(state)])

        # Batch get all 110 composite items (2-tuple keys, requires 2 DynamoDB calls)
        keys = [(entity_id, "api") for entity_id in entity_ids]
        result = await localstack_repo.batch_get_buckets(keys)

        # Should return all 110 bucket states
        assert len(result) == 110

        # Result uses 3-tuple keys for backward compatibility
        for entity_id in entity_ids:
            key = (entity_id, "api", "rpm")
            assert key in result
            assert result[key].entity_id == entity_id


class TestRepositoryPing:
    """Integration tests for Repository.ping() health check."""

    @pytest.mark.asyncio
    async def test_ping_returns_true_when_table_exists(self, localstack_repo):
        """ping() should return True when table is reachable."""
        result = await localstack_repo.ping()
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
    """

    @pytest.mark.asyncio
    async def test_set_limits_transaction_atomicity(self, localstack_repo):
        """Verify set_limits atomically creates config AND increments registry."""
        repo = localstack_repo

        await repo.create_entity("user-1")
        limits = [Limit.per_minute("rpm", 1000)]

        # Set limits - should atomically create config + increment registry
        await repo.set_limits("user-1", limits, resource="gpt-4")

        # Verify both operations succeeded
        stored_limits = await repo.get_limits("user-1", resource="gpt-4")
        assert len(stored_limits) == 1
        assert stored_limits[0].name == "rpm"

        resources = await repo.list_resources_with_entity_configs()
        assert "gpt-4" in resources

    @pytest.mark.asyncio
    async def test_delete_limits_transaction_atomicity(self, localstack_repo):
        """Verify delete_limits atomically removes config AND decrements registry."""
        repo = localstack_repo

        await repo.create_entity("user-1")
        limits = [Limit.per_minute("rpm", 1000)]
        await repo.set_limits("user-1", limits, resource="gpt-4")

        # Delete limits - should atomically delete config + decrement registry
        await repo.delete_limits("user-1", resource="gpt-4")

        # Verify both operations succeeded
        stored_limits = await repo.get_limits("user-1", resource="gpt-4")
        assert stored_limits == []

        resources = await repo.list_resources_with_entity_configs()
        assert "gpt-4" not in resources

    @pytest.mark.asyncio
    async def test_delete_nonexistent_config_no_side_effects(self, localstack_repo):
        """Deleting non-existent config should not affect registry or create audit."""
        repo = localstack_repo

        await repo.create_entity("user-1")

        # Delete limits that don't exist
        await repo.delete_limits("user-1", resource="gpt-4")

        # Registry should be empty (no decrement to negative)
        resources = await repo.list_resources_with_entity_configs()
        assert resources == []

    @pytest.mark.asyncio
    async def test_registry_cleanup_at_zero(self, localstack_repo):
        """Verify registry attribute is removed when count reaches zero."""
        repo = localstack_repo

        await repo.create_entity("user-1")
        await repo.create_entity("user-2")
        limits = [Limit.per_minute("rpm", 1000)]

        # Create two configs for same resource
        await repo.set_limits("user-1", limits, resource="gpt-4")
        await repo.set_limits("user-2", limits, resource="gpt-4")

        resources = await repo.list_resources_with_entity_configs()
        assert "gpt-4" in resources

        # Delete both - count should reach 0 and attribute removed
        await repo.delete_limits("user-1", resource="gpt-4")
        await repo.delete_limits("user-2", resource="gpt-4")

        resources = await repo.list_resources_with_entity_configs()
        assert "gpt-4" not in resources

    @pytest.mark.asyncio
    async def test_update_existing_config_no_double_count(self, localstack_repo):
        """Updating existing config should not increment registry twice."""
        from zae_limiter import schema

        repo = localstack_repo

        await repo.create_entity("user-1")
        limits1 = [Limit.per_minute("rpm", 1000)]
        limits2 = [Limit.per_minute("rpm", 2000)]

        # Set limits twice (second is UPDATE, not NEW)
        await repo.set_limits("user-1", limits1, resource="gpt-4")
        await repo.set_limits("user-1", limits2, resource="gpt-4")

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
        count = int(item.get("gpt-4", {}).get("N", "0"))
        assert count == 1, "Registry should have count=1, not 2"


class TestSpeculativeConsume:
    """Integration tests for speculative_consume() against real DynamoDB.

    Validates that DynamoDB condition expressions, ReturnValues=ALL_NEW, and
    ReturnValuesOnConditionCheckFailure=ALL_OLD work as expected for the
    speculative UpdateItem path (issue #315).
    """

    @pytest.mark.asyncio
    async def test_speculative_success_single_limit(self, localstack_repo):
        """Speculative consume succeeds when bucket has enough tokens."""
        repo = localstack_repo
        await repo.create_entity("spec-entity-1")

        # Create composite bucket with 100 rpm
        limit = Limit.per_minute("rpm", 100)
        now_ms = int(time.time() * 1000)
        state = BucketState.from_limit("spec-entity-1", "api", limit, now_ms)
        put_item = repo.build_composite_create("spec-entity-1", "api", [state], now_ms)
        await repo.transact_write([put_item])

        # Speculative consume 1 rpm (bucket has 100)
        result = await repo.speculative_consume(
            entity_id="spec-entity-1",
            resource="api",
            consume={"rpm": 1},
        )

        assert result.success is True
        assert len(result.buckets) == 1
        assert result.buckets[0].entity_id == "spec-entity-1"
        assert result.buckets[0].limit_name == "rpm"
        # Tokens should be reduced: 100_000 - 1_000 = 99_000
        assert result.buckets[0].tokens_milli == 99_000

    @pytest.mark.asyncio
    async def test_speculative_success_multi_limit(self, localstack_repo):
        """Speculative consume succeeds with multiple limits in one bucket."""
        repo = localstack_repo
        await repo.create_entity("spec-entity-2")

        rpm_limit = Limit.per_minute("rpm", 100)
        tpm_limit = Limit.per_minute("tpm", 10_000)
        now_ms = int(time.time() * 1000)
        rpm_state = BucketState.from_limit("spec-entity-2", "api", rpm_limit, now_ms)
        tpm_state = BucketState.from_limit("spec-entity-2", "api", tpm_limit, now_ms)
        put_item = repo.build_composite_create(
            "spec-entity-2", "api", [rpm_state, tpm_state], now_ms
        )
        await repo.transact_write([put_item])

        result = await repo.speculative_consume(
            entity_id="spec-entity-2",
            resource="api",
            consume={"rpm": 1, "tpm": 500},
        )

        assert result.success is True
        assert len(result.buckets) == 2
        bucket_map = {b.limit_name: b for b in result.buckets}
        assert bucket_map["rpm"].tokens_milli == 99_000  # 100_000 - 1_000
        assert bucket_map["tpm"].tokens_milli == 9_500_000  # 10_000_000 - 500_000

    @pytest.mark.asyncio
    async def test_speculative_success_returns_total_consumed(self, localstack_repo):
        """ALL_NEW response includes total_consumed_milli counter."""
        repo = localstack_repo
        await repo.create_entity("spec-entity-tc")

        limit = Limit.per_minute("rpm", 100)
        now_ms = int(time.time() * 1000)
        state = BucketState.from_limit("spec-entity-tc", "api", limit, now_ms)
        put_item = repo.build_composite_create("spec-entity-tc", "api", [state], now_ms)
        await repo.transact_write([put_item])

        # First consume
        result1 = await repo.speculative_consume(
            entity_id="spec-entity-tc", resource="api", consume={"rpm": 3}
        )
        assert result1.success is True
        assert result1.buckets[0].total_consumed_milli == 3_000

        # Second consume — counter accumulates
        result2 = await repo.speculative_consume(
            entity_id="spec-entity-tc", resource="api", consume={"rpm": 7}
        )
        assert result2.success is True
        assert result2.buckets[0].total_consumed_milli == 10_000

    @pytest.mark.asyncio
    async def test_speculative_failure_insufficient_tokens(self, localstack_repo):
        """Speculative consume fails when bucket has insufficient tokens."""
        repo = localstack_repo
        await repo.create_entity("spec-entity-3")

        limit = Limit.per_minute("rpm", 10)
        now_ms = int(time.time() * 1000)
        state = BucketState.from_limit("spec-entity-3", "api", limit, now_ms)
        put_item = repo.build_composite_create("spec-entity-3", "api", [state], now_ms)
        await repo.transact_write([put_item])

        # Try to consume more than available
        result = await repo.speculative_consume(
            entity_id="spec-entity-3",
            resource="api",
            consume={"rpm": 20},
        )

        assert result.success is False
        assert result.old_buckets is not None
        assert len(result.old_buckets) == 1
        assert result.old_buckets[0].limit_name == "rpm"
        # Old bucket should have original tokens (unconsumed)
        assert result.old_buckets[0].tokens_milli == 10_000

    @pytest.mark.asyncio
    async def test_speculative_failure_multi_limit_one_exhausted(self, localstack_repo):
        """Condition fails when ANY limit is insufficient (AND across limits)."""
        repo = localstack_repo
        await repo.create_entity("spec-entity-4")

        rpm_limit = Limit.per_minute("rpm", 100)
        tpm_limit = Limit.per_minute("tpm", 5)  # Very low
        now_ms = int(time.time() * 1000)
        rpm_state = BucketState.from_limit("spec-entity-4", "api", rpm_limit, now_ms)
        tpm_state = BucketState.from_limit("spec-entity-4", "api", tpm_limit, now_ms)
        put_item = repo.build_composite_create(
            "spec-entity-4", "api", [rpm_state, tpm_state], now_ms
        )
        await repo.transact_write([put_item])

        # rpm has plenty (100 >= 1), but tpm is insufficient (5 < 10)
        result = await repo.speculative_consume(
            entity_id="spec-entity-4",
            resource="api",
            consume={"rpm": 1, "tpm": 10},
        )

        assert result.success is False
        assert result.old_buckets is not None
        assert len(result.old_buckets) == 2
        # Tokens untouched (transaction-like: neither limit consumed)
        bucket_map = {b.limit_name: b for b in result.old_buckets}
        assert bucket_map["rpm"].tokens_milli == 100_000
        assert bucket_map["tpm"].tokens_milli == 5_000

    @pytest.mark.asyncio
    async def test_speculative_failure_bucket_missing(self, localstack_repo):
        """Speculative consume fails gracefully when bucket doesn't exist."""
        repo = localstack_repo
        await repo.create_entity("spec-entity-5")

        # Don't create any bucket — first acquire scenario
        result = await repo.speculative_consume(
            entity_id="spec-entity-5",
            resource="api",
            consume={"rpm": 1},
        )

        assert result.success is False
        assert result.old_buckets is None  # No ALL_OLD because item doesn't exist

    @pytest.mark.asyncio
    async def test_speculative_success_returns_cascade_and_parent(self, localstack_repo):
        """ALL_NEW response includes denormalized cascade and parent_id."""
        repo = localstack_repo
        await repo.create_entity("spec-child", parent_id="spec-parent", cascade=True)

        limit = Limit.per_minute("rpm", 100)
        now_ms = int(time.time() * 1000)
        state = BucketState.from_limit("spec-child", "api", limit, now_ms)
        put_item = repo.build_composite_create(
            "spec-child",
            "api",
            [state],
            now_ms,
            cascade=True,
            parent_id="spec-parent",
        )
        await repo.transact_write([put_item])

        result = await repo.speculative_consume(
            entity_id="spec-child",
            resource="api",
            consume={"rpm": 1},
        )

        assert result.success is True
        assert result.cascade is True
        assert result.parent_id == "spec-parent"

    @pytest.mark.asyncio
    async def test_speculative_success_no_cascade(self, localstack_repo):
        """ALL_NEW response correctly reports cascade=False."""
        repo = localstack_repo
        await repo.create_entity("spec-nocascade")

        limit = Limit.per_minute("rpm", 100)
        now_ms = int(time.time() * 1000)
        state = BucketState.from_limit("spec-nocascade", "api", limit, now_ms)
        put_item = repo.build_composite_create(
            "spec-nocascade",
            "api",
            [state],
            now_ms,
            cascade=False,
        )
        await repo.transact_write([put_item])

        result = await repo.speculative_consume(
            entity_id="spec-nocascade",
            resource="api",
            consume={"rpm": 1},
        )

        assert result.success is True
        assert result.cascade is False
        assert result.parent_id is None

    @pytest.mark.asyncio
    async def test_speculative_preserves_bucket_fields(self, localstack_repo):
        """ALL_NEW returns all bucket fields needed for BucketState reconstruction."""
        repo = localstack_repo
        await repo.create_entity("spec-fields")

        limit = Limit.per_minute("rpm", 100, burst=150)
        now_ms = int(time.time() * 1000)
        state = BucketState.from_limit("spec-fields", "api", limit, now_ms)
        put_item = repo.build_composite_create("spec-fields", "api", [state], now_ms)
        await repo.transact_write([put_item])

        result = await repo.speculative_consume(
            entity_id="spec-fields",
            resource="api",
            consume={"rpm": 5},
        )

        assert result.success is True
        bucket = result.buckets[0]
        assert bucket.entity_id == "spec-fields"
        assert bucket.resource == "api"
        assert bucket.limit_name == "rpm"
        assert bucket.capacity_milli == 100_000
        assert bucket.burst_milli == 150_000
        assert bucket.refill_amount_milli == 100_000
        assert bucket.refill_period_ms == 60_000
        assert bucket.tokens_milli == 150_000 - 5_000  # burst - consumed

    @pytest.mark.asyncio
    async def test_speculative_drain_then_fail(self, localstack_repo):
        """Repeated speculative consumes drain bucket until condition fails."""
        repo = localstack_repo
        await repo.create_entity("spec-drain")

        limit = Limit.per_minute("rpm", 10)
        now_ms = int(time.time() * 1000)
        state = BucketState.from_limit("spec-drain", "api", limit, now_ms)
        put_item = repo.build_composite_create("spec-drain", "api", [state], now_ms)
        await repo.transact_write([put_item])

        # Consume 3 at a time: 10 -> 7 -> 4 -> 1 -> fail (1 < 3)
        for expected_remaining in [7_000, 4_000, 1_000]:
            result = await repo.speculative_consume(
                entity_id="spec-drain", resource="api", consume={"rpm": 3}
            )
            assert result.success is True
            assert result.buckets[0].tokens_milli == expected_remaining

        # Fourth attempt fails (1 < 3)
        result = await repo.speculative_consume(
            entity_id="spec-drain", resource="api", consume={"rpm": 3}
        )
        assert result.success is False
        assert result.old_buckets is not None
        assert result.old_buckets[0].tokens_milli == 1_000

    @pytest.mark.asyncio
    async def test_speculative_rejects_expired_ttl_bucket(self, localstack_repo):
        """Speculative consume rejects buckets with expired TTL."""
        repo = localstack_repo
        await repo.create_entity("spec-expired")

        limit = Limit.per_minute("rpm", 100)
        now_ms = int(time.time() * 1000)
        state = BucketState.from_limit("spec-expired", "api", limit, now_ms)
        # Create bucket with TTL 1 second in the past
        put_item = repo.build_composite_create(
            "spec-expired", "api", [state], now_ms, ttl_seconds=-1
        )
        await repo.transact_write([put_item])

        # Speculative consume should fail (bucket TTL expired)
        result = await repo.speculative_consume(
            entity_id="spec-expired", resource="api", consume={"rpm": 1}
        )
        assert result.success is False
        # ALL_OLD is returned because item exists but condition failed
        assert result.old_buckets is not None

    @pytest.mark.asyncio
    async def test_speculative_accepts_no_ttl_bucket(self, localstack_repo):
        """Speculative consume accepts buckets without TTL attribute."""
        repo = localstack_repo
        await repo.create_entity("spec-no-ttl")

        limit = Limit.per_minute("rpm", 100)
        now_ms = int(time.time() * 1000)
        state = BucketState.from_limit("spec-no-ttl", "api", limit, now_ms)
        # Create bucket without TTL (custom config entity)
        put_item = repo.build_composite_create(
            "spec-no-ttl", "api", [state], now_ms, ttl_seconds=None
        )
        await repo.transact_write([put_item])

        # Speculative consume should succeed (no TTL = no expiry)
        result = await repo.speculative_consume(
            entity_id="spec-no-ttl", resource="api", consume={"rpm": 1}
        )
        assert result.success is True

    @pytest.mark.asyncio
    async def test_speculative_accepts_future_ttl_bucket(self, localstack_repo):
        """Speculative consume accepts buckets with TTL in the future."""
        repo = localstack_repo
        await repo.create_entity("spec-future-ttl")

        limit = Limit.per_minute("rpm", 100)
        now_ms = int(time.time() * 1000)
        state = BucketState.from_limit("spec-future-ttl", "api", limit, now_ms)
        # Create bucket with TTL 1 hour in the future
        put_item = repo.build_composite_create(
            "spec-future-ttl", "api", [state], now_ms, ttl_seconds=3600
        )
        await repo.transact_write([put_item])

        # Speculative consume should succeed (TTL not expired)
        result = await repo.speculative_consume(
            entity_id="spec-future-ttl", resource="api", consume={"rpm": 1}
        )
        assert result.success is True
