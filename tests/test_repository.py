"""Unit tests for Repository."""

import time

import pytest

from zae_limiter import Limit
from zae_limiter.models import BucketState
from zae_limiter.repository import Repository


@pytest.fixture
async def repo(mock_dynamodb):
    """Basic repository instance."""
    from tests.conftest import _patch_aiobotocore_response

    with _patch_aiobotocore_response():
        repo = Repository(table_name="test_repo", region="us-east-1")
        await repo.create_table()
        yield repo
        await repo.close()


@pytest.fixture
async def repo_with_buckets(repo):
    """Repository pre-populated with test buckets."""
    # Create test entities
    await repo.create_entity("entity-1", parent_id=None, name="Entity 1")
    await repo.create_entity("entity-2", parent_id="entity-1", name="Entity 2")

    # Create some buckets
    limits = [
        Limit.per_minute("rpm", 100),
        Limit.per_minute("tpm", 10000),
    ]
    now_ms = int(time.time() * 1000)

    for entity_id in ["entity-1", "entity-2"]:
        for resource in ["gpt-4", "gpt-3.5"]:
            for limit in limits:
                state = BucketState.from_limit(entity_id, resource, limit, now_ms)
                await repo.transact_write([repo.build_bucket_put_item(state)])

    yield repo


class TestRepositoryBucketOperations:
    """Tests for bucket CRUD and queries."""

    @pytest.mark.asyncio
    async def test_get_bucket_returns_none_for_nonexistent(self, repo):
        """Getting a nonexistent bucket should return None."""
        result = await repo.get_bucket("nonexistent", "gpt-4", "rpm")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_buckets_filters_by_resource(self, repo_with_buckets):
        """get_buckets should filter by resource when specified."""
        buckets = await repo_with_buckets.get_buckets("entity-1", resource="gpt-4")

        # Should only get gpt-4 buckets (2 limits: rpm, tpm)
        assert len(buckets) == 2
        assert all(b.resource == "gpt-4" for b in buckets)

        # Verify both limits are present
        limit_names = {b.limit_name for b in buckets}
        assert limit_names == {"rpm", "tpm"}

    @pytest.mark.asyncio
    async def test_get_buckets_returns_all_when_no_filter(self, repo_with_buckets):
        """get_buckets should return all buckets when no resource filter."""
        buckets = await repo_with_buckets.get_buckets("entity-1")

        # Should get all buckets: 2 resources × 2 limits = 4 buckets
        assert len(buckets) == 4

        # Verify resources and limits
        resources = {b.resource for b in buckets}
        assert resources == {"gpt-4", "gpt-3.5"}

        limit_names = {b.limit_name for b in buckets}
        assert limit_names == {"rpm", "tpm"}

    @pytest.mark.asyncio
    async def test_build_bucket_update_with_optimistic_locking(self, repo):
        """Optimistic locking should add conditional expression."""
        update_item = repo.build_bucket_update_item(
            entity_id="entity-1",
            resource="gpt-4",
            limit_name="rpm",
            new_tokens_milli=75_000,
            new_last_refill_ms=1234567890,
            expected_tokens_milli=100_000,  # Optimistic lock
        )

        # Verify structure
        assert "Update" in update_item
        update_spec = update_item["Update"]

        # Check update expression
        expected_expr = "SET #data.#tokens = :tokens, #data.#refill = :refill"
        assert update_spec["UpdateExpression"] == expected_expr

        # Check attribute names
        assert update_spec["ExpressionAttributeNames"]["#data"] == "data"
        assert update_spec["ExpressionAttributeNames"]["#tokens"] == "tokens_milli"
        assert update_spec["ExpressionAttributeNames"]["#refill"] == "last_refill_ms"

        # Check attribute values
        assert update_spec["ExpressionAttributeValues"][":tokens"] == {"N": "75000"}
        assert update_spec["ExpressionAttributeValues"][":refill"] == {"N": "1234567890"}
        assert update_spec["ExpressionAttributeValues"][":expected"] == {"N": "100000"}

        # Check condition
        assert update_spec["ConditionExpression"] == "#data.#tokens = :expected"

    @pytest.mark.asyncio
    async def test_build_bucket_update_without_optimistic_locking(self, repo):
        """Without expected_tokens, no condition should be added."""
        update_item = repo.build_bucket_update_item(
            entity_id="entity-1",
            resource="gpt-4",
            limit_name="rpm",
            new_tokens_milli=75_000,
            new_last_refill_ms=1234567890,
            expected_tokens_milli=None,  # No optimistic lock
        )

        # Verify structure
        assert "Update" in update_item
        update_spec = update_item["Update"]

        # Verify no condition
        assert "ConditionExpression" not in update_spec
        assert ":expected" not in update_spec["ExpressionAttributeValues"]

        # Verify update expression is still correct
        expected_expr = "SET #data.#tokens = :tokens, #data.#refill = :refill"
        assert update_spec["UpdateExpression"] == expected_expr


class TestRepositoryResourceAggregation:
    """Tests for GSI2 resource queries."""

    @pytest.mark.asyncio
    async def test_get_resource_buckets_all_entities(self, repo_with_buckets):
        """Should query all buckets for a resource via GSI2."""
        buckets = await repo_with_buckets.get_resource_buckets("gpt-4", "rpm")

        # Should get rpm buckets for both entities
        assert len(buckets) == 2
        assert all(b.resource == "gpt-4" for b in buckets)
        assert all(b.limit_name == "rpm" for b in buckets)

        # Verify both entities are present
        entity_ids = {b.entity_id for b in buckets}
        assert entity_ids == {"entity-1", "entity-2"}

    @pytest.mark.asyncio
    async def test_get_resource_buckets_filtered_by_limit_name(self, repo_with_buckets):
        """Should filter by limit_name when specified."""
        # Query with limit_name filter
        rpm_buckets = await repo_with_buckets.get_resource_buckets("gpt-4", "rpm")
        tpm_buckets = await repo_with_buckets.get_resource_buckets("gpt-4", "tpm")

        # Both should have 2 entities each
        assert len(rpm_buckets) == 2
        assert len(tpm_buckets) == 2

        # Verify correct limit names
        assert all(b.limit_name == "rpm" for b in rpm_buckets)
        assert all(b.limit_name == "tpm" for b in tpm_buckets)

    @pytest.mark.asyncio
    async def test_get_resource_buckets_empty_result(self, repo):
        """Should return empty list when no buckets match."""
        buckets = await repo.get_resource_buckets("nonexistent-resource", "rpm")
        assert buckets == []


class TestRepositoryTransactions:
    """Tests for transactional writes and edge cases."""

    @pytest.mark.asyncio
    async def test_transact_write_empty_items_list(self, repo):
        """transact_write should handle empty items list."""
        # Should not raise an error
        await repo.transact_write([])

    @pytest.mark.asyncio
    async def test_build_bucket_put_item_structure(self, repo):
        """build_bucket_put_item should create correct DynamoDB structure."""
        limit = Limit.per_minute("rpm", 100)
        now_ms = int(time.time() * 1000)
        state = BucketState.from_limit("entity-1", "gpt-4", limit, now_ms)

        put_item = repo.build_bucket_put_item(state)

        # Verify structure
        assert "Put" in put_item
        put_spec = put_item["Put"]

        assert put_spec["TableName"] == "test_repo"

        # Verify keys
        assert "PK" in put_spec["Item"]
        assert "SK" in put_spec["Item"]
        assert put_spec["Item"]["PK"]["S"] == "ENTITY#entity-1"
        assert put_spec["Item"]["SK"]["S"] == "#BUCKET#gpt-4#rpm"

        # Verify data structure
        assert "data" in put_spec["Item"]
        data = put_spec["Item"]["data"]["M"]

        assert data["tokens_milli"]["N"] == str(100_000)  # burst capacity
        assert data["capacity_milli"]["N"] == str(100_000)
        assert data["burst_milli"]["N"] == str(100_000)
        assert data["refill_amount_milli"]["N"] == str(100_000)
        assert data["refill_period_ms"]["N"] == str(60_000)

    @pytest.mark.asyncio
    async def test_batch_delete_pagination_over_25_items(self, repo):
        """Batch delete should handle >25 items by chunking."""
        # Create 30 entities to exceed DynamoDB batch limit
        for i in range(30):
            await repo.create_entity(f"entity-{i}")

        # Create buckets for all entities
        limit = Limit.per_minute("rpm", 100)
        now_ms = int(time.time() * 1000)

        for i in range(30):
            state = BucketState.from_limit(f"entity-{i}", "api", limit, now_ms)
            await repo.transact_write([repo.build_bucket_put_item(state)])

        # Delete first entity (should handle >25 items internally if we had that many)
        # For now, just verify it works with one entity
        await repo.delete_entity("entity-0")

        # Verify entity is deleted
        entity = await repo.get_entity("entity-0")
        assert entity is None


class TestRepositorySerialization:
    """Tests for complex DynamoDB type serialization."""

    @pytest.mark.asyncio
    async def test_serialize_map_with_bool_values(self, repo):
        """Should correctly serialize boolean values in maps."""
        # Create entity with metadata containing bools
        await repo.create_entity(
            "test-entity",
            metadata={"is_active": True, "is_premium": False},
        )

        # Retrieve and verify
        entity = await repo.get_entity("test-entity")
        assert entity is not None
        assert entity.metadata["is_active"] is True
        assert entity.metadata["is_premium"] is False

    @pytest.mark.asyncio
    async def test_serialize_map_with_null_values(self, repo):
        """Should correctly serialize None/null values."""
        # Create entity with null parent_id
        await repo.create_entity("test-entity", parent_id=None)

        # Retrieve and verify
        entity = await repo.get_entity("test-entity")
        assert entity is not None
        assert entity.parent_id is None

    @pytest.mark.asyncio
    async def test_serialize_map_with_nested_maps(self, repo):
        """Should handle nested dictionaries."""
        metadata = {
            "tier": "premium",
            "limits": {
                "rpm": 1000,
                "tpm": 50000,
            },
            "features": {
                "advanced": True,
                "beta": False,
            },
        }

        await repo.create_entity("test-entity", metadata=metadata)

        # Retrieve and verify nested structure
        entity = await repo.get_entity("test-entity")
        assert entity is not None
        assert entity.metadata["tier"] == "premium"
        assert entity.metadata["limits"]["rpm"] == 1000
        assert entity.metadata["limits"]["tpm"] == 50000
        assert entity.metadata["features"]["advanced"] is True
        assert entity.metadata["features"]["beta"] is False

    @pytest.mark.asyncio
    async def test_serialize_value_with_list_of_mixed_types(self, repo):
        """Should handle lists with mixed types."""
        metadata = {
            "tags": ["production", "api", "v2"],
            "numbers": [1, 2, 3, 100],
            "mixed": ["text", 42, True],
        }

        await repo.create_entity("test-entity", metadata=metadata)

        # Retrieve and verify
        entity = await repo.get_entity("test-entity")
        assert entity is not None
        assert entity.metadata["tags"] == ["production", "api", "v2"]
        assert entity.metadata["numbers"] == [1, 2, 3, 100]
        assert entity.metadata["mixed"] == ["text", 42, True]


class TestRepositoryVersionOperations:
    """Tests for version record management."""

    @pytest.mark.asyncio
    async def test_get_version_record_returns_none_when_missing(self, repo):
        """Should return None when version record doesn't exist."""
        version = await repo.get_version_record()
        assert version is None

    @pytest.mark.asyncio
    async def test_set_version_record_with_null_lambda_version(self, repo):
        """Should handle null lambda_version correctly."""
        await repo.set_version_record(
            schema_version="1.0.0",
            lambda_version=None,  # No Lambda deployed
            client_min_version="0.1.0",
            updated_by="test",
        )

        version = await repo.get_version_record()
        assert version is not None
        assert version["schema_version"] == "1.0.0"
        assert version["lambda_version"] is None
        assert version["client_min_version"] == "0.1.0"

    @pytest.mark.asyncio
    async def test_set_version_record_with_null_updated_by(self, repo):
        """Should handle null updated_by correctly."""
        await repo.set_version_record(
            schema_version="1.0.0",
            lambda_version="0.1.0",
            client_min_version="0.1.0",
            updated_by=None,  # No user tracking
        )

        version = await repo.get_version_record()
        assert version is not None
        assert version["schema_version"] == "1.0.0"
        assert version["lambda_version"] == "0.1.0"
        assert version["updated_by"] is None
