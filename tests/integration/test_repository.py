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
            retention_days=90,
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
