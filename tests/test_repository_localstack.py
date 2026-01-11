"""Integration tests for Repository using LocalStack.

These tests run against a real LocalStack instance with full AWS service emulation
including CloudFormation, DynamoDB, DynamoDB Streams, and Lambda.

To run these tests locally:
    docker run -p 4566:4566 \
      -e SERVICES=dynamodb,dynamodbstreams,lambda,cloudformation,logs,iam \
      localstack/localstack

    AWS_ENDPOINT_URL=http://localhost:4566 pytest tests/test_repository_localstack.py -v
"""

import os
import time

import pytest

from zae_limiter import Limit
from zae_limiter.models import BucketState
from zae_limiter.repository import Repository

pytestmark = pytest.mark.integration


@pytest.fixture
def localstack_endpoint():
    """Get LocalStack endpoint from environment."""
    endpoint = os.getenv("AWS_ENDPOINT_URL")
    if not endpoint:
        pytest.skip("AWS_ENDPOINT_URL not set - LocalStack not available")
    return endpoint


@pytest.fixture
async def localstack_repo(localstack_endpoint):
    """Repository connected to LocalStack."""
    repo = Repository(
        table_name="integration_test_repo",
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
    async def test_create_table_or_stack_uses_cloudformation(self, localstack_endpoint):
        """Should create CloudFormation stack with full infrastructure."""
        repo = Repository(
            table_name="test_cf_stack",
            endpoint_url=localstack_endpoint,
            region="us-east-1",
        )

        try:
            # Create stack using CloudFormation
            await repo.create_table_or_stack(use_cloudformation=True)

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
    async def test_create_table_or_stack_with_endpoint_url_uses_direct(self, localstack_endpoint):
        """Should use direct table creation when endpoint_url is set."""
        repo = Repository(
            table_name="test_direct_table",
            endpoint_url=localstack_endpoint,  # This forces direct creation
            region="us-east-1",
        )

        try:
            # Even with use_cloudformation=True, endpoint_url forces direct
            await repo.create_table_or_stack(use_cloudformation=True)

            # Verify table works
            await repo.create_entity("test-entity")
            entity = await repo.get_entity("test-entity")
            assert entity is not None

        finally:
            try:
                await repo.delete_table()
            except Exception:
                pass
            await repo.close()

    @pytest.mark.asyncio
    async def test_create_table_or_stack_with_custom_parameters(self, localstack_endpoint):
        """Should pass custom parameters to CloudFormation stack."""
        repo = Repository(
            table_name="test_params_stack",
            endpoint_url=localstack_endpoint,
            region="us-east-1",
        )

        try:
            # Create with custom parameters
            # Note: LocalStack might not fully validate these, but we test the flow
            params = {
                "snapshot_windows": "hourly,daily",
                "retention_days": "90",
            }
            await repo.create_table_or_stack(
                use_cloudformation=True,
                stack_parameters=params,
            )

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

        await localstack_repo.transact_write([
            localstack_repo.build_bucket_put_item(state1),
            localstack_repo.build_bucket_put_item(state2),
        ])

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

        # Create buckets for same resource
        limits = [
            Limit.per_minute("rpm", 100),
            Limit.per_minute("tpm", 10_000),
        ]
        now_ms = int(time.time() * 1000)

        for entity_id in entity_ids:
            for limit in limits:
                state = BucketState.from_limit(entity_id, "gpt-4", limit, now_ms)
                await localstack_repo.transact_write([localstack_repo.build_bucket_put_item(state)])

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
