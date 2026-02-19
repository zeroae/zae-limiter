"""Unit tests for Repository."""

import time
from unittest.mock import AsyncMock, patch

import pytest
from botocore.exceptions import ClientError

from zae_limiter import AuditAction, Limit
from zae_limiter.exceptions import EntityExistsError, InvalidIdentifierError
from zae_limiter.models import BucketState
from zae_limiter.repository import Repository
from zae_limiter.schema import (
    calculate_bucket_ttl,
    limit_attr,
    parse_bucket_attr,
    parse_bucket_sk,
    parse_limit_attr,
    sk_config,
)


@pytest.fixture
async def repo(mock_dynamodb):
    """Basic repository instance."""
    repo = Repository(name="test-repo", region="us-east-1", _skip_deprecation_warning=True)
    await repo.create_table()
    yield repo
    await repo.close()


@pytest.fixture
async def repo_with_buckets(repo):
    """Repository pre-populated with test buckets (composite items, ADR-114)."""
    # Create test entities
    await repo.create_entity("entity-1", parent_id=None, name="Entity 1")
    await repo.create_entity("entity-2", parent_id="entity-1", name="Entity 2")

    # Create composite buckets: one item per entity+resource with all limits
    limits = [
        Limit.per_minute("rpm", 100),
        Limit.per_minute("tpm", 10000),
    ]
    now_ms = int(time.time() * 1000)

    for entity_id in ["entity-1", "entity-2"]:
        for resource in ["gpt-4", "gpt-3.5"]:
            states = [
                BucketState.from_limit(entity_id, resource, limit, now_ms) for limit in limits
            ]
            put_item = repo.build_composite_create(entity_id, resource, states, now_ms)
            await repo.transact_write([put_item])

    yield repo


class TestBucketTTLCalculation:
    """Tests for calculate_bucket_ttl (Issue #271, #296: Time-to-fill based TTL)."""

    def test_calculate_bucket_ttl_single_limit(self):
        """TTL = now + time_to_fill × multiplier for single limit.

        For Limit.per_minute("rpm", 100): capacity=100, refill_amount=100
        time_to_fill = (100/100) × 60 = 60 seconds
        """
        now_ms = 1700000000000  # Example timestamp
        limits = [Limit.per_minute("rpm", 100)]  # time_to_fill = 60s
        multiplier = 7

        ttl = calculate_bucket_ttl(now_ms, limits, multiplier)

        # Expected: (now_ms // 1000) + (60 * 7) = 1700000000 + 420 = 1700000420
        assert ttl == 1700000420

    def test_calculate_bucket_ttl_slow_refill_limit(self):
        """TTL accounts for slow refill rate (Issue #296).

        For slow-refill limit: capacity=1000, refill_amount=10, refill_period=60s
        time_to_fill = (1000/10) × 60 = 6000 seconds (100 minutes)
        TTL should be 6000 × 7 = 42000 seconds, NOT 60 × 7 = 420 seconds
        """
        now_ms = 1700000000000
        # Slow refill: 1000 capacity, refills 10 per minute
        slow_refill_limit = Limit(
            name="tokens",
            capacity=1000,
            burst=1000,
            refill_amount=10,
            refill_period_seconds=60,
        )
        limits = [slow_refill_limit]
        multiplier = 7

        ttl = calculate_bucket_ttl(now_ms, limits, multiplier)

        # time_to_fill = (1000/10) × 60 = 6000 seconds
        # Expected: (now_ms // 1000) + (6000 * 7) = 1700000000 + 42000 = 1700042000
        assert ttl == 1700042000

    def test_calculate_bucket_ttl_multiple_limits_uses_max_time_to_fill(self):
        """TTL uses maximum time_to_fill when multiple limits exist."""
        now_ms = 1700000000000
        limits = [
            Limit.per_minute("rpm", 100),  # time_to_fill = 60s
            Limit.per_day("tpd", 1000000),  # time_to_fill = 86400s
        ]
        multiplier = 7

        ttl = calculate_bucket_ttl(now_ms, limits, multiplier)

        # Expected: (now_ms // 1000) + (86400 * 7) = 1700000000 + 604800 = 1700604800
        assert ttl == 1700604800

    def test_calculate_bucket_ttl_multiple_limits_slow_refill_wins(self):
        """Slow refill limit should dominate even with shorter refill_period.

        Fast limit: per_minute(100) -> time_to_fill = 60s
        Slow limit: capacity=1000, refill_amount=10, period=60s -> time_to_fill = 6000s
        The slow limit should determine TTL even though both have same refill_period.
        """
        now_ms = 1700000000000
        limits = [
            Limit.per_minute("rpm", 100),  # time_to_fill = 60s
            Limit(  # time_to_fill = 6000s
                name="slow",
                capacity=1000,
                burst=1000,
                refill_amount=10,
                refill_period_seconds=60,
            ),
        ]
        multiplier = 7

        ttl = calculate_bucket_ttl(now_ms, limits, multiplier)

        # max time_to_fill = 6000s (from slow limit)
        # Expected: (now_ms // 1000) + (6000 * 7) = 1700000000 + 42000 = 1700042000
        assert ttl == 1700042000

    def test_calculate_bucket_ttl_returns_none_when_multiplier_zero(self):
        """TTL is None when multiplier is 0 (disabled)."""
        now_ms = 1700000000000
        limits = [Limit.per_minute("rpm", 100)]
        multiplier = 0

        ttl = calculate_bucket_ttl(now_ms, limits, multiplier)

        assert ttl is None

    def test_calculate_bucket_ttl_returns_none_when_multiplier_negative(self):
        """TTL is None when multiplier is negative (disabled)."""
        now_ms = 1700000000000
        limits = [Limit.per_minute("rpm", 100)]
        multiplier = -1

        ttl = calculate_bucket_ttl(now_ms, limits, multiplier)

        assert ttl is None


class TestSchemaCompositeKeys:
    """Tests for composite bucket schema key builders."""

    def test_parse_bucket_attr_valid(self):
        """parse_bucket_attr returns (limit_name, field) for valid attributes."""
        assert parse_bucket_attr("b_rpm_tk") == ("rpm", "tk")
        assert parse_bucket_attr("b_tpm_cp") == ("tpm", "cp")
        assert parse_bucket_attr("b_my_limit_bx") == ("my_limit", "bx")

    def test_parse_bucket_attr_not_bucket(self):
        """parse_bucket_attr returns None for non-bucket attributes."""
        assert parse_bucket_attr("entity_id") is None
        assert parse_bucket_attr("PK") is None
        assert parse_bucket_attr("rf") is None

    def test_parse_bucket_attr_no_field_separator(self):
        """parse_bucket_attr returns None when no underscore after prefix."""
        assert parse_bucket_attr("b_") is None
        assert parse_bucket_attr("b_x") is None

    def test_parse_bucket_sk_valid(self):
        """parse_bucket_sk extracts resource from composite SK."""
        assert parse_bucket_sk("#BUCKET#gpt-4") == "gpt-4"
        assert parse_bucket_sk("#BUCKET#api") == "api"

    def test_parse_bucket_sk_invalid_prefix(self):
        """parse_bucket_sk raises ValueError for non-bucket SK."""
        with pytest.raises(ValueError, match="Invalid bucket SK"):
            parse_bucket_sk("#META")

    def test_parse_bucket_sk_empty_resource(self):
        """parse_bucket_sk raises ValueError for empty resource."""
        with pytest.raises(ValueError, match="Invalid bucket SK format"):
            parse_bucket_sk("#BUCKET#")


class TestSchemaCompositeLimitKeys:
    """Tests for composite limit config schema key builders."""

    def test_limit_attr_builds_correct_format(self):
        """limit_attr builds l_{name}_{field} format."""
        assert limit_attr("rpm", "cp") == "l_rpm_cp"
        assert limit_attr("tpm", "bx") == "l_tpm_bx"
        assert limit_attr("my_limit", "ra") == "l_my_limit_ra"

    def test_parse_limit_attr_valid(self):
        """parse_limit_attr returns (limit_name, field) for valid attributes."""
        assert parse_limit_attr("l_rpm_cp") == ("rpm", "cp")
        assert parse_limit_attr("l_tpm_bx") == ("tpm", "bx")
        assert parse_limit_attr("l_my_limit_ra") == ("my_limit", "ra")

    def test_parse_limit_attr_not_limit(self):
        """parse_limit_attr returns None for non-limit attributes."""
        assert parse_limit_attr("entity_id") is None
        assert parse_limit_attr("PK") is None
        assert parse_limit_attr("b_rpm_tk") is None  # bucket, not limit

    def test_parse_limit_attr_no_field_separator(self):
        """parse_limit_attr returns None when no underscore after prefix."""
        assert parse_limit_attr("l_") is None
        assert parse_limit_attr("l_x") is None

    def test_sk_config_without_resource(self):
        """sk_config() returns #CONFIG when no resource provided."""
        assert sk_config() == "#CONFIG"
        assert sk_config(None) == "#CONFIG"

    def test_sk_config_with_resource(self):
        """sk_config(resource) returns #CONFIG#{resource}."""
        assert sk_config("gpt-4") == "#CONFIG#gpt-4"
        assert sk_config("_default_") == "#CONFIG#_default_"


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

        # Check update expression (composite bucket attributes)
        expected_expr = "SET #tokens = :tokens, #refill = :refill"
        assert update_spec["UpdateExpression"] == expected_expr

        # Check attribute names (composite: b_rpm_tk, rf)
        assert "#data" not in update_spec["ExpressionAttributeNames"]
        assert update_spec["ExpressionAttributeNames"]["#tokens"] == "b_rpm_tk"
        assert update_spec["ExpressionAttributeNames"]["#refill"] == "rf"

        # Check attribute values
        assert update_spec["ExpressionAttributeValues"][":tokens"] == {"N": "75000"}
        assert update_spec["ExpressionAttributeValues"][":refill"] == {"N": "1234567890"}
        assert update_spec["ExpressionAttributeValues"][":expected"] == {"N": "100000"}

        # Check condition (composite attribute path)
        assert update_spec["ConditionExpression"] == "#tokens = :expected"

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

        # Verify update expression uses composite attributes
        expected_expr = "SET #tokens = :tokens, #refill = :refill"
        assert update_spec["UpdateExpression"] == expected_expr

    @pytest.mark.asyncio
    async def test_batch_get_buckets_empty_keys(self, repo):
        """batch_get_buckets should return empty dict for empty keys list."""
        result = await repo.batch_get_buckets([])
        assert result == {}

    # -------------------------------------------------------------------------
    # batch_get_configs tests (issue #298)
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_batch_get_configs_empty_keys(self, repo):
        """batch_get_configs should return empty dict for empty keys list."""
        result = await repo.batch_get_configs([])
        assert result == {}

    @pytest.mark.asyncio
    async def test_batch_get_configs_all_present(self, repo):
        """batch_get_configs returns deserialized (limits, on_unavailable) tuples."""
        from zae_limiter import Limit, schema

        # Set up configs at all 4 levels
        await repo.set_system_defaults([Limit.per_minute("rpm", 1000)], on_unavailable="allow")
        await repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 500)])
        await repo.create_entity("user-1")
        await repo.set_limits("user-1", [Limit.per_minute("rpm", 100)], resource="gpt-4")
        await repo.set_limits("user-1", [Limit.per_minute("rpm", 200)], resource="_default_")

        keys = [
            (schema.pk_system("default"), schema.sk_config()),
            (schema.pk_resource("default", "gpt-4"), schema.sk_config()),
            (schema.pk_entity("default", "user-1"), schema.sk_config("gpt-4")),
            (schema.pk_entity("default", "user-1"), schema.sk_config("_default_")),
        ]

        result = await repo.batch_get_configs(keys)

        assert len(result) == 4
        for key in keys:
            assert key in result
            limits, on_unavailable = result[key]
            assert isinstance(limits, list)

        # Verify system config has on_unavailable
        sys_limits, sys_ou = result[(schema.pk_system("default"), schema.sk_config())]
        assert len(sys_limits) == 1
        assert sys_limits[0].name == "rpm"
        assert sys_limits[0].capacity == 1000
        assert sys_ou == "allow"

        # Verify resource config has no on_unavailable
        res_limits, res_ou = result[(schema.pk_resource("default", "gpt-4"), schema.sk_config())]
        assert len(res_limits) == 1
        assert res_limits[0].capacity == 500
        assert res_ou is None

    @pytest.mark.asyncio
    async def test_batch_get_configs_partial_results(self, repo):
        """batch_get_configs returns only present items (missing ones omitted)."""
        from zae_limiter import Limit, schema

        # Only set system defaults
        await repo.set_system_defaults([Limit.per_minute("rpm", 1000)])

        keys = [
            (schema.pk_system("default"), schema.sk_config()),
            (schema.pk_resource("default", "gpt-4"), schema.sk_config()),  # Not set
            (schema.pk_entity("default", "user-1"), schema.sk_config("gpt-4")),  # Not set
        ]

        result = await repo.batch_get_configs(keys)

        assert len(result) == 1
        sys_key = (schema.pk_system("default"), schema.sk_config())
        assert sys_key in result
        limits, on_unavailable = result[sys_key]
        assert len(limits) == 1
        assert limits[0].name == "rpm"

    @pytest.mark.asyncio
    async def test_get_or_create_bucket_creates_new(self, repo):
        """get_or_create_bucket should create a new bucket if it doesn't exist."""
        from zae_limiter import Limit

        limit = Limit.per_minute("rpm", 100)
        bucket = await repo.get_or_create_bucket("entity-1", "gpt-4", limit)

        assert bucket is not None
        assert bucket.entity_id == "entity-1"
        assert bucket.resource == "gpt-4"
        assert bucket.limit_name == "rpm"
        assert bucket.tokens_milli == 100000  # 100 * 1000

    @pytest.mark.asyncio
    async def test_get_or_create_bucket_returns_existing(self, repo):
        """get_or_create_bucket should return existing bucket if it exists."""
        from zae_limiter import Limit

        limit = Limit.per_minute("rpm", 100)

        # Create first
        bucket1 = await repo.get_or_create_bucket("entity-2", "gpt-4", limit)

        # Get again - should return existing
        bucket2 = await repo.get_or_create_bucket("entity-2", "gpt-4", limit)

        assert bucket1.entity_id == bucket2.entity_id
        assert bucket1.resource == bucket2.resource
        assert bucket1.limit_name == bucket2.limit_name


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
    async def test_transact_write_single_delete(self, repo):
        """transact_write dispatches single Delete item via delete_item API."""
        await repo.create_entity("tw-delete-test")

        delete_item = {
            "Delete": {
                "TableName": repo.table_name,
                "Key": {
                    "PK": {"S": "default/ENTITY#tw-delete-test"},
                    "SK": {"S": "#META"},
                },
            }
        }
        await repo.transact_write([delete_item])

        entity = await repo.get_entity("tw-delete-test")
        assert entity is None

    @pytest.mark.asyncio
    async def test_transact_write_single_unknown_type_falls_through(self, repo):
        """transact_write falls through to transact_write_items for unknown item types."""
        # ConditionCheck is a valid TransactWriteItems type but not handled by
        # the single-item optimization — it should fall through to transact_write_items.
        await repo.create_entity("tw-condcheck-test")

        condition_item = {
            "ConditionCheck": {
                "TableName": repo.table_name,
                "Key": {
                    "PK": {"S": "default/ENTITY#tw-condcheck-test"},
                    "SK": {"S": "#META"},
                },
                "ConditionExpression": "attribute_exists(PK)",
            }
        }
        await repo.transact_write([condition_item])

    @pytest.mark.asyncio
    async def test_write_each_empty_items_list(self, repo):
        """write_each should handle empty items list."""
        await repo.write_each([])

    @pytest.mark.asyncio
    async def test_write_each_dispatches_each_item_independently(self, repo):
        """write_each dispatches Put, Update, and Delete as independent calls."""
        limit = Limit.per_minute("rpm", 100)
        now_ms = int(time.time() * 1000)

        # Create entity and bucket via Put
        await repo.create_entity("we-test")
        state = BucketState.from_limit("we-test", "api", limit, now_ms)
        put_item = repo.build_bucket_put_item(state)
        await repo.write_each([put_item])

        # Verify bucket was written
        buckets = await repo.get_buckets("we-test", "api")
        assert len(buckets) == 1

        # Update via write_each
        adjust_item = repo.build_composite_adjust(
            entity_id="we-test",
            resource="api",
            deltas={"rpm": -5000},
        )
        await repo.write_each([adjust_item])

        # Delete entity via write_each
        delete_item = {
            "Delete": {
                "TableName": repo.table_name,
                "Key": {
                    "PK": {"S": "default/ENTITY#we-test"},
                    "SK": {"S": "#META"},
                },
            }
        }
        await repo.write_each([delete_item])

        # Verify entity deleted
        entity = await repo.get_entity("we-test")
        assert entity is None

    @pytest.mark.asyncio
    async def test_build_bucket_put_item_structure(self, repo):
        """build_bucket_put_item should create composite DynamoDB structure (ADR-114)."""
        limit = Limit.per_minute("rpm", 100)
        now_ms = int(time.time() * 1000)
        state = BucketState.from_limit("entity-1", "gpt-4", limit, now_ms)

        put_item = repo.build_bucket_put_item(state)

        # Verify structure
        assert "Put" in put_item
        put_spec = put_item["Put"]

        assert put_spec["TableName"] == "test-repo"

        # Verify keys (composite: no limit_name in SK)
        assert "PK" in put_spec["Item"]
        assert "SK" in put_spec["Item"]
        assert put_spec["Item"]["PK"]["S"] == "default/ENTITY#entity-1"
        assert put_spec["Item"]["SK"]["S"] == "#BUCKET#gpt-4"

        # Verify composite bucket attributes (b_{name}_{field} format)
        assert "data" not in put_spec["Item"]
        item = put_spec["Item"]

        assert item["b_rpm_tk"]["N"] == str(100_000)  # burst capacity
        assert item["b_rpm_cp"]["N"] == str(100_000)
        assert item["b_rpm_bx"]["N"] == str(100_000)
        assert item["b_rpm_ra"]["N"] == str(100_000)
        assert item["b_rpm_rp"]["N"] == str(60_000)
        assert item["resource"]["S"] == "gpt-4"

        # Shared refill timestamp
        assert "rf" in item

        # Condition prevents overwriting existing composite item
        assert "attribute_not_exists(PK)" in put_spec.get("ConditionExpression", "")

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


class TestCompositeWritePaths:
    """Tests for composite bucket write path builders (ADR-115)."""

    @pytest.mark.asyncio
    async def test_build_composite_retry_structure(self, repo):
        """build_composite_retry produces ADD for tk and tc per limit."""
        result = repo.build_composite_retry(
            entity_id="entity-1",
            resource="gpt-4",
            consumed={"rpm": 5000, "tpm": 100000},
        )

        assert "Update" in result
        update = result["Update"]
        assert update["Key"]["PK"]["S"] == "default/ENTITY#entity-1"
        assert update["Key"]["SK"]["S"] == "#BUCKET#gpt-4"

        # Verify ADD expression contains both limits
        expr = update["UpdateExpression"]
        assert "ADD" in expr
        assert "#b_rpm_tk" in expr
        assert "#b_rpm_tc" in expr
        assert "#b_tpm_tk" in expr
        assert "#b_tpm_tc" in expr

        # Verify condition requires sufficient tokens
        cond = update["ConditionExpression"]
        assert "#b_rpm_tk >= " in cond
        assert "#b_tpm_tk >= " in cond

        # Verify attribute mappings
        names = update["ExpressionAttributeNames"]
        assert names["#b_rpm_tk"] == "b_rpm_tk"
        assert names["#b_rpm_tc"] == "b_rpm_tc"

        # Verify values: tk gets negative (consumption), tc gets positive
        vals = update["ExpressionAttributeValues"]
        assert vals[":b_rpm_tk_neg"]["N"] == "-5000"
        assert vals[":b_rpm_tc_delta"]["N"] == "5000"

    @pytest.mark.asyncio
    async def test_build_composite_adjust_structure(self, repo):
        """build_composite_adjust produces unconditional ADD for tk and tc."""
        result = repo.build_composite_adjust(
            entity_id="entity-1",
            resource="gpt-4",
            deltas={"rpm": 3000, "tpm": -500},
        )

        assert "Update" in result
        update = result["Update"]

        # Should have ADD but no condition
        assert "ADD" in update["UpdateExpression"]
        assert "ConditionExpression" not in update

        # Positive delta: subtract from tk, add to tc
        vals = update["ExpressionAttributeValues"]
        assert vals[":b_rpm_tk_delta"]["N"] == "-3000"
        assert vals[":b_rpm_tc_delta"]["N"] == "3000"
        # Negative delta: add to tk, subtract from tc
        assert vals[":b_tpm_tk_delta"]["N"] == "500"
        assert vals[":b_tpm_tc_delta"]["N"] == "-500"

    @pytest.mark.asyncio
    async def test_build_composite_adjust_zero_deltas(self, repo):
        """build_composite_adjust with all-zero deltas returns empty dict."""
        result = repo.build_composite_adjust(
            entity_id="entity-1",
            resource="gpt-4",
            deltas={"rpm": 0},
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_get_bucket_returns_none_for_missing_limit(
        self,
        repo_with_buckets,
    ):
        """get_bucket returns None when limit_name not found in composite item."""
        result = await repo_with_buckets.get_bucket(
            "entity-1",
            "gpt-4",
            "nonexistent",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_get_buckets_returns_empty_for_missing_resource(self, repo):
        """get_buckets returns empty list when no composite item exists."""
        await repo.create_entity("entity-1")
        result = await repo.get_buckets("entity-1", resource="nonexistent")
        assert result == []


class TestCompositeBucketTTL:
    """Tests for TTL in composite bucket build methods (Issue #271)."""

    @pytest.mark.asyncio
    async def test_build_composite_create_sets_ttl_when_provided(self, repo):
        """build_composite_create includes ttl attribute when ttl_seconds provided."""
        now_ms = 1700000000000
        state = BucketState(
            entity_id="e1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=100000,
            last_refill_ms=now_ms,
            capacity_milli=100000,
            burst_milli=100000,
            refill_amount_milli=100000,
            refill_period_ms=60000,
            total_consumed_milli=0,
        )

        result = repo.build_composite_create(
            entity_id="e1",
            resource="gpt-4",
            states=[state],
            now_ms=now_ms,
            ttl_seconds=604800,  # 7 days
        )

        # Verify TTL attribute is set
        item = result["Put"]["Item"]
        assert "ttl" in item
        # Expected: (now_ms // 1000) + 604800 = 1700000000 + 604800 = 1700604800
        assert item["ttl"]["N"] == "1700604800"

    @pytest.mark.asyncio
    async def test_build_composite_create_no_ttl_when_none(self, repo):
        """build_composite_create omits ttl when ttl_seconds is None."""
        now_ms = 1700000000000
        state = BucketState(
            entity_id="e1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=100000,
            last_refill_ms=now_ms,
            capacity_milli=100000,
            burst_milli=100000,
            refill_amount_milli=100000,
            refill_period_ms=60000,
            total_consumed_milli=0,
        )

        result = repo.build_composite_create(
            entity_id="e1",
            resource="gpt-4",
            states=[state],
            now_ms=now_ms,
            ttl_seconds=None,  # TTL disabled
        )

        # Verify TTL attribute is NOT set
        item = result["Put"]["Item"]
        assert "ttl" not in item

    @pytest.mark.asyncio
    async def test_build_composite_normal_updates_ttl_when_provided(self, repo):
        """build_composite_normal includes SET ttl in expression."""
        now_ms = 1700000000000

        result = repo.build_composite_normal(
            entity_id="e1",
            resource="gpt-4",
            consumed={"rpm": 1000},
            refill_amounts={"rpm": 500},
            now_ms=now_ms,
            expected_rf=now_ms - 1000,
            ttl_seconds=604800,  # Update TTL
        )

        update = result["Update"]
        expr = update["UpdateExpression"]

        # Verify SET #ttl = :ttl_val in expression
        assert "#ttl" in expr
        assert ":ttl_val" in update["ExpressionAttributeValues"]
        assert update["ExpressionAttributeNames"]["#ttl"] == "ttl"
        # Expected TTL value
        assert update["ExpressionAttributeValues"][":ttl_val"]["N"] == "1700604800"

    @pytest.mark.asyncio
    async def test_build_composite_normal_removes_ttl_when_zero(self, repo):
        """build_composite_normal includes REMOVE ttl when ttl_seconds is 0."""
        now_ms = 1700000000000

        result = repo.build_composite_normal(
            entity_id="e1",
            resource="gpt-4",
            consumed={"rpm": 1000},
            refill_amounts={"rpm": 500},
            now_ms=now_ms,
            expected_rf=now_ms - 1000,
            ttl_seconds=0,  # Remove TTL (entity has custom limits)
        )

        update = result["Update"]
        expr = update["UpdateExpression"]

        # Verify REMOVE #ttl in expression
        assert "REMOVE" in expr
        assert "#ttl" in expr
        assert update["ExpressionAttributeNames"]["#ttl"] == "ttl"
        # Should NOT have :ttl_val in values
        assert ":ttl_val" not in update.get("ExpressionAttributeValues", {})

    @pytest.mark.asyncio
    async def test_build_composite_normal_no_ttl_change_when_none(self, repo):
        """build_composite_normal doesn't touch ttl when ttl_seconds is None."""
        now_ms = 1700000000000

        result = repo.build_composite_normal(
            entity_id="e1",
            resource="gpt-4",
            consumed={"rpm": 1000},
            refill_amounts={"rpm": 500},
            now_ms=now_ms,
            expected_rf=now_ms - 1000,
            ttl_seconds=None,  # Don't change TTL
        )

        update = result["Update"]
        expr = update["UpdateExpression"]

        # Verify no ttl references
        assert "#ttl" not in update.get("ExpressionAttributeNames", {})
        assert "REMOVE" not in expr or "#ttl" not in expr


class TestCompositeLimitConfig:
    """Tests for composite limit config serialization and CRUD (ADR-114 for configs)."""

    @pytest.mark.asyncio
    async def test_serialize_composite_limits(self, repo):
        """_serialize_composite_limits adds l_* attributes to item."""
        limits = [
            Limit.per_minute("rpm", 100),
            Limit.per_minute("tpm", 10000),
        ]
        item: dict = {}
        repo._serialize_composite_limits(limits, item)

        # Check rpm attributes
        assert item["l_rpm_cp"]["N"] == "100"
        assert item["l_rpm_bx"]["N"] == "100"
        assert item["l_rpm_ra"]["N"] == "100"
        assert item["l_rpm_rp"]["N"] == "60"

        # Check tpm attributes
        assert item["l_tpm_cp"]["N"] == "10000"
        assert item["l_tpm_bx"]["N"] == "10000"
        assert item["l_tpm_ra"]["N"] == "10000"
        assert item["l_tpm_rp"]["N"] == "60"

    @pytest.mark.asyncio
    async def test_deserialize_composite_limits(self, repo):
        """_deserialize_composite_limits reconstructs Limit objects from l_* attrs."""
        item = {
            "l_rpm_cp": {"N": "100"},
            "l_rpm_bx": {"N": "150"},
            "l_rpm_ra": {"N": "100"},
            "l_rpm_rp": {"N": "60"},
            "l_tpm_cp": {"N": "10000"},
            "l_tpm_bx": {"N": "10000"},
            "l_tpm_ra": {"N": "10000"},
            "l_tpm_rp": {"N": "60"},
            "entity_id": {"S": "test"},  # non-limit attr should be ignored
        }
        limits = repo._deserialize_composite_limits(item)

        assert len(limits) == 2
        limit_map = {limit.name: limit for limit in limits}

        assert limit_map["rpm"].capacity == 100
        assert limit_map["rpm"].burst == 150
        assert limit_map["rpm"].refill_amount == 100
        assert limit_map["rpm"].refill_period_seconds == 60

        assert limit_map["tpm"].capacity == 10000
        assert limit_map["tpm"].burst == 10000

    @pytest.mark.asyncio
    async def test_deserialize_composite_limits_empty_item(self, repo):
        """_deserialize_composite_limits returns empty list for item without l_* attrs."""
        item = {
            "entity_id": {"S": "test"},
            "resource": {"S": "gpt-4"},
        }
        limits = repo._deserialize_composite_limits(item)
        assert limits == []

    @pytest.mark.asyncio
    async def test_set_get_limits_roundtrip(self, repo):
        """set_limits and get_limits should round-trip correctly."""
        await repo.create_entity("limit-test")

        limits = [
            Limit.per_minute("rpm", 100),
            Limit.per_minute("tpm", 10000),
        ]
        await repo.set_limits("limit-test", limits, resource="gpt-4")

        retrieved = await repo.get_limits("limit-test", resource="gpt-4")
        assert len(retrieved) == 2

        limit_map = {lim.name: lim for lim in retrieved}
        assert limit_map["rpm"].capacity == 100
        assert limit_map["tpm"].capacity == 10000

    @pytest.mark.asyncio
    async def test_delete_limits(self, repo):
        """delete_limits should remove the composite config item."""
        await repo.create_entity("delete-limit-test")

        limits = [Limit.per_minute("rpm", 100)]
        await repo.set_limits("delete-limit-test", limits, resource="api")

        # Verify limits exist
        retrieved = await repo.get_limits("delete-limit-test", resource="api")
        assert len(retrieved) == 1

        # Delete
        await repo.delete_limits("delete-limit-test", resource="api")

        # Verify limits are gone
        retrieved = await repo.get_limits("delete-limit-test", resource="api")
        assert retrieved == []

    @pytest.mark.asyncio
    async def test_set_resource_defaults_roundtrip(self, repo):
        """set_resource_defaults and get_resource_defaults should round-trip correctly."""
        limits = [
            Limit.per_minute("rpm", 500),
            Limit.per_minute("tpm", 50000),
        ]
        await repo.set_resource_defaults("gpt-4", limits)

        retrieved = await repo.get_resource_defaults("gpt-4")
        assert len(retrieved) == 2

        limit_map = {lim.name: lim for lim in retrieved}
        assert limit_map["rpm"].capacity == 500
        assert limit_map["tpm"].capacity == 50000

    @pytest.mark.asyncio
    async def test_delete_resource_defaults(self, repo):
        """delete_resource_defaults should remove the composite config item."""
        limits = [Limit.per_minute("rpm", 100)]
        await repo.set_resource_defaults("test-resource", limits)

        # Verify exists
        retrieved = await repo.get_resource_defaults("test-resource")
        assert len(retrieved) == 1

        # Delete
        await repo.delete_resource_defaults("test-resource")

        # Verify gone
        retrieved = await repo.get_resource_defaults("test-resource")
        assert retrieved == []

    @pytest.mark.asyncio
    async def test_set_system_defaults_roundtrip(self, repo):
        """set_system_defaults and get_system_defaults should round-trip correctly."""
        limits = [
            Limit.per_minute("rpm", 1000),
            Limit.per_minute("tpm", 100000),
        ]
        await repo.set_system_defaults(limits, on_unavailable="allow")

        retrieved_limits, on_unavailable = await repo.get_system_defaults()
        assert len(retrieved_limits) == 2
        assert on_unavailable == "allow"

        limit_map = {lim.name: lim for lim in retrieved_limits}
        assert limit_map["rpm"].capacity == 1000
        assert limit_map["tpm"].capacity == 100000

    @pytest.mark.asyncio
    async def test_set_system_defaults_without_on_unavailable(self, repo):
        """set_system_defaults should work without on_unavailable."""
        limits = [Limit.per_minute("rpm", 500)]
        await repo.set_system_defaults(limits)

        retrieved_limits, on_unavailable = await repo.get_system_defaults()
        assert len(retrieved_limits) == 1
        assert on_unavailable is None

    @pytest.mark.asyncio
    async def test_delete_system_defaults(self, repo):
        """delete_system_defaults should remove the composite config item."""
        limits = [Limit.per_minute("rpm", 100)]
        await repo.set_system_defaults(limits, on_unavailable="block")

        # Verify exists
        retrieved_limits, on_unavailable = await repo.get_system_defaults()
        assert len(retrieved_limits) == 1
        assert on_unavailable == "block"

        # Delete
        await repo.delete_system_defaults()

        # Verify gone
        retrieved_limits, on_unavailable = await repo.get_system_defaults()
        assert retrieved_limits == []
        assert on_unavailable is None

    @pytest.mark.asyncio
    async def test_get_limits_returns_empty_for_nonexistent(self, repo):
        """get_limits returns empty list for entity without config."""
        await repo.create_entity("no-limits")
        limits = await repo.get_limits("no-limits", resource="gpt-4")
        assert limits == []

    @pytest.mark.asyncio
    async def test_get_resource_defaults_returns_empty_for_nonexistent(self, repo):
        """get_resource_defaults returns empty list for unconfigured resource."""
        limits = await repo.get_resource_defaults("nonexistent-resource")
        assert limits == []


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


class TestRepositoryEntityValidation:
    """Tests for input validation in Repository.create_entity()."""

    @pytest.mark.asyncio
    async def test_create_entity_valid(self, repo):
        """Valid entity_id should be accepted."""
        entity = await repo.create_entity("user-123", name="Test User")
        assert entity.id == "user-123"
        assert entity.name == "Test User"

    @pytest.mark.asyncio
    async def test_create_entity_rejects_hash_in_id(self, repo):
        """Entity ID with # delimiter should be rejected."""
        with pytest.raises(InvalidIdentifierError) as exc_info:
            await repo.create_entity("user#123")
        assert exc_info.value.field == "entity_id"
        assert "#" in exc_info.value.reason

    @pytest.mark.asyncio
    async def test_create_entity_rejects_empty_id(self, repo):
        """Empty entity ID should be rejected."""
        with pytest.raises(InvalidIdentifierError) as exc_info:
            await repo.create_entity("")
        assert exc_info.value.field == "entity_id"
        assert "empty" in exc_info.value.reason

    @pytest.mark.asyncio
    async def test_create_entity_rejects_too_long_id(self, repo):
        """Entity ID exceeding max length should be rejected."""
        with pytest.raises(InvalidIdentifierError) as exc_info:
            await repo.create_entity("a" * 300)
        assert exc_info.value.field == "entity_id"
        assert "length" in exc_info.value.reason

    @pytest.mark.asyncio
    async def test_create_entity_rejects_invalid_start_char(self, repo):
        """Entity ID must start with alphanumeric."""
        with pytest.raises(InvalidIdentifierError):
            await repo.create_entity("_user123")

    @pytest.mark.asyncio
    async def test_create_entity_valid_parent_id(self, repo):
        """Valid parent_id should be accepted."""
        await repo.create_entity("parent-1")
        entity = await repo.create_entity("child-1", parent_id="parent-1")
        assert entity.parent_id == "parent-1"

    @pytest.mark.asyncio
    async def test_create_entity_rejects_hash_in_parent_id(self, repo):
        """Parent ID with # delimiter should be rejected."""
        with pytest.raises(InvalidIdentifierError) as exc_info:
            await repo.create_entity("child-1", parent_id="parent#123")
        assert exc_info.value.field == "parent_id"
        assert "#" in exc_info.value.reason

    @pytest.mark.asyncio
    async def test_create_entity_rejects_empty_parent_id(self, repo):
        """Empty parent ID should be rejected (use None instead)."""
        with pytest.raises(InvalidIdentifierError) as exc_info:
            await repo.create_entity("child-1", parent_id="")
        assert exc_info.value.field == "parent_id"
        assert "empty" in exc_info.value.reason

    @pytest.mark.asyncio
    async def test_create_entity_accepts_uuid(self, repo):
        """UUID format should be accepted."""
        entity = await repo.create_entity("550e8400-e29b-41d4-a716-446655440000")
        assert entity.id == "550e8400-e29b-41d4-a716-446655440000"

    @pytest.mark.asyncio
    async def test_create_entity_accepts_api_key_format(self, repo):
        """API key format (sk-proj-xxx) should be accepted."""
        entity = await repo.create_entity("sk-proj-abc123_xyz")
        assert entity.id == "sk-proj-abc123_xyz"

    @pytest.mark.asyncio
    async def test_create_entity_accepts_email_like(self, repo):
        """Email-like format should be accepted."""
        entity = await repo.create_entity("user@example.com")
        assert entity.id == "user@example.com"


class TestRepositoryNoTTLOnConfigRecords:
    """Tests verifying entity metadata and config records do NOT have TTL.

    Issue #234: Entity metadata and config are intentional configuration that
    must not be auto-deleted. Only usage snapshots and audit records have TTL.
    """

    @pytest.mark.asyncio
    async def test_entity_metadata_has_no_ttl(self, repo):
        """Entity metadata record (SK=#META) should NOT have ttl attribute."""
        from zae_limiter import schema

        await repo.create_entity(entity_id="no-ttl-meta-test", name="Test")

        # Get the raw DynamoDB item to check for ttl attribute
        client = await repo._get_client()
        response = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_entity("default", "no-ttl-meta-test")},
                "SK": {"S": schema.sk_meta()},
            },
        )

        item = response.get("Item", {})
        assert item, "Entity metadata record should exist"
        assert "ttl" not in item, "Entity metadata should NOT have ttl attribute"

    @pytest.mark.asyncio
    async def test_entity_config_has_no_ttl(self, repo):
        """Entity config record (SK=#CONFIG#) should NOT have ttl attribute."""
        from zae_limiter import schema
        from zae_limiter.models import Limit

        await repo.create_entity(entity_id="no-ttl-config-test")
        await repo.set_limits(
            entity_id="no-ttl-config-test",
            limits=[Limit.per_minute("rpm", 100)],
            resource="api",
        )

        # Get the raw DynamoDB item to check for ttl attribute
        client = await repo._get_client()
        response = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_entity("default", "no-ttl-config-test")},
                "SK": {"S": schema.sk_config("api")},
            },
        )

        item = response.get("Item", {})
        assert item, "Entity config record should exist"
        assert "ttl" not in item, "Entity config should NOT have ttl attribute"

    @pytest.mark.asyncio
    async def test_system_config_has_no_ttl(self, repo):
        """System config record (SYSTEM# / #CONFIG) should NOT have ttl attribute."""
        from zae_limiter import schema
        from zae_limiter.models import Limit

        await repo.set_system_defaults(limits=[Limit.per_minute("rpm", 1000)])

        # Get the raw DynamoDB item to check for ttl attribute
        client = await repo._get_client()
        response = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_system("default")},
                "SK": {"S": schema.sk_config()},
            },
        )

        item = response.get("Item", {})
        assert item, "System config record should exist"
        assert "ttl" not in item, "System config should NOT have ttl attribute"

    @pytest.mark.asyncio
    async def test_resource_config_has_no_ttl(self, repo):
        """Resource config record (RESOURCE# / #CONFIG) should NOT have ttl attribute."""
        from zae_limiter import schema
        from zae_limiter.models import Limit

        await repo.set_resource_defaults(
            resource="gpt-4",
            limits=[Limit.per_minute("rpm", 500)],
        )

        # Get the raw DynamoDB item to check for ttl attribute
        client = await repo._get_client()
        response = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_resource("default", "gpt-4")},
                "SK": {"S": schema.sk_config()},
            },
        )

        item = response.get("Item", {})
        assert item, "Resource config record should exist"
        assert "ttl" not in item, "Resource config should NOT have ttl attribute"


class TestRepositoryAuditLogging:
    """Tests for security audit logging."""

    @pytest.mark.asyncio
    async def test_create_entity_logs_audit_event(self, repo):
        """Creating an entity should log an audit event."""
        await repo.create_entity(
            entity_id="audit-test-entity",
            name="Audit Test",
            principal="user@example.com",
        )

        events = await repo.get_audit_events("audit-test-entity")
        assert len(events) == 1

        event = events[0]
        assert event.action == AuditAction.ENTITY_CREATED
        assert event.entity_id == "audit-test-entity"
        assert event.principal == "user@example.com"
        assert event.details["name"] == "Audit Test"

    @pytest.mark.asyncio
    async def test_create_entity_logs_audit_with_auto_detected_principal(self, repo):
        """Creating an entity without explicit principal auto-detects from AWS identity."""
        await repo.create_entity(
            entity_id="audit-test-entity-2",
            name="No Principal",
        )

        events = await repo.get_audit_events("audit-test-entity-2")
        assert len(events) == 1
        # In moto tests, principal is auto-detected from STS (may be None or an ARN)
        # In real AWS, it would be the caller's ARN
        principal = events[0].principal
        if principal is not None:
            assert principal.startswith("arn:aws:")

    @pytest.mark.asyncio
    async def test_delete_entity_logs_audit_event(self, repo):
        """Deleting an entity should log an audit event."""
        await repo.create_entity(entity_id="to-delete")
        await repo.delete_entity(
            entity_id="to-delete",
            principal="admin@example.com",
        )

        events = await repo.get_audit_events("to-delete")
        # Should have both create and delete events
        assert len(events) == 2

        # Most recent first
        delete_event = events[0]
        assert delete_event.action == AuditAction.ENTITY_DELETED
        assert delete_event.principal == "admin@example.com"
        assert "records_deleted" in delete_event.details

    @pytest.mark.asyncio
    async def test_set_limits_logs_audit_event(self, repo):
        """Setting limits should log an audit event."""
        await repo.create_entity(entity_id="limits-test")

        limits = [
            Limit.per_minute("rpm", 100),
            Limit.per_minute("tpm", 10000),
        ]
        await repo.set_limits(
            entity_id="limits-test",
            limits=limits,
            resource="gpt-4",
            principal="api-admin@example.com",
        )

        events = await repo.get_audit_events("limits-test")
        # Should have entity create + limits set events
        assert len(events) == 2

        limits_event = events[0]  # Most recent first
        assert limits_event.action == AuditAction.LIMITS_SET
        assert limits_event.principal == "api-admin@example.com"
        assert limits_event.resource == "gpt-4"
        assert len(limits_event.details["limits"]) == 2

    @pytest.mark.asyncio
    async def test_delete_limits_logs_audit_event(self, repo):
        """Deleting limits should log an audit event."""
        await repo.create_entity(entity_id="delete-limits-test")
        await repo.set_limits(
            entity_id="delete-limits-test",
            limits=[Limit.per_minute("rpm", 100)],
        )
        await repo.delete_limits(
            entity_id="delete-limits-test",
            principal="cleanup-service",
        )

        events = await repo.get_audit_events("delete-limits-test")
        # Should have entity create + limits set + limits delete events
        assert len(events) == 3

        delete_event = events[0]  # Most recent first
        assert delete_event.action == AuditAction.LIMITS_DELETED
        assert delete_event.principal == "cleanup-service"

    @pytest.mark.asyncio
    async def test_get_audit_events_pagination(self, repo):
        """Should support pagination for audit events."""
        # Create entity and perform multiple operations
        await repo.create_entity(entity_id="pagination-test")
        for i in range(5):
            await repo.set_limits(
                entity_id="pagination-test",
                limits=[Limit.per_minute(f"limit-{i}", 100 * (i + 1))],
                principal=f"user-{i}",
            )

        # Query with limit
        events = await repo.get_audit_events("pagination-test", limit=3)
        assert len(events) == 3

        # Query with pagination
        all_events = await repo.get_audit_events("pagination-test", limit=10)
        assert len(all_events) == 6  # 1 create + 5 set_limits

    @pytest.mark.asyncio
    async def test_get_audit_events_empty_for_nonexistent(self, repo):
        """Should return empty list for entity with no audit events."""
        events = await repo.get_audit_events("nonexistent-entity")
        assert events == []

    @pytest.mark.asyncio
    async def test_audit_event_includes_parent_id(self, repo):
        """Audit event for child entity should include parent_id."""
        await repo.create_entity(entity_id="parent-entity")
        await repo.create_entity(
            entity_id="child-entity",
            parent_id="parent-entity",
            principal="admin",
        )

        events = await repo.get_audit_events("child-entity")
        assert len(events) == 1
        assert events[0].details["parent_id"] == "parent-entity"

    @pytest.mark.asyncio
    async def test_audit_event_includes_metadata(self, repo):
        """Audit event should include entity metadata."""
        await repo.create_entity(
            entity_id="metadata-test",
            metadata={"tier": "premium", "region": "us-west-2"},
            principal="onboarding-service",
        )

        events = await repo.get_audit_events("metadata-test")
        assert len(events) == 1
        assert events[0].details["metadata"]["tier"] == "premium"
        assert events[0].details["metadata"]["region"] == "us-west-2"

    @pytest.mark.asyncio
    async def test_create_entity_rejects_invalid_principal(self, repo):
        """Principal with # delimiter should be rejected."""
        with pytest.raises(InvalidIdentifierError) as exc_info:
            await repo.create_entity(
                entity_id="valid-entity",
                principal="user#admin",
            )
        assert exc_info.value.field == "principal"
        assert "#" in exc_info.value.reason

    @pytest.mark.asyncio
    async def test_create_entity_rejects_empty_principal(self, repo):
        """Empty principal should be rejected (use None instead)."""
        with pytest.raises(InvalidIdentifierError) as exc_info:
            await repo.create_entity(
                entity_id="valid-entity",
                principal="",
            )
        assert exc_info.value.field == "principal"
        assert "empty" in exc_info.value.reason

    @pytest.mark.asyncio
    async def test_create_entity_accepts_email_principal(self, repo):
        """Email-like principal should be accepted."""
        await repo.create_entity(
            entity_id="email-principal-test",
            principal="admin@example.com",
        )
        events = await repo.get_audit_events("email-principal-test")
        assert events[0].principal == "admin@example.com"

    @pytest.mark.asyncio
    async def test_create_entity_accepts_service_principal(self, repo):
        """Service name principal should be accepted."""
        await repo.create_entity(
            entity_id="service-principal-test",
            principal="auth-service-v2",
        )
        events = await repo.get_audit_events("service-principal-test")
        assert events[0].principal == "auth-service-v2"

    @pytest.mark.asyncio
    async def test_audit_event_id_is_ulid_format(self, repo):
        """Event ID should be a valid 26-character ULID."""
        await repo.create_entity(entity_id="ulid-test")
        events = await repo.get_audit_events("ulid-test")
        assert len(events) == 1

        event_id = events[0].event_id
        # ULID is 26 characters, uppercase alphanumeric (Crockford Base32)
        assert len(event_id) == 26
        assert event_id.isalnum()
        # ULID uses Crockford Base32: 0-9 and A-Z excluding I, L, O, U
        valid_chars = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
        assert all(c in valid_chars for c in event_id.upper())

    @pytest.mark.asyncio
    async def test_audit_event_ids_are_monotonic(self, repo):
        """Multiple events should have monotonically increasing ULIDs."""
        await repo.create_entity(entity_id="monotonic-test")
        # Create multiple events rapidly
        for i in range(5):
            await repo.set_limits(
                entity_id="monotonic-test",
                limits=[Limit.per_minute(f"limit-{i}", 100)],
            )

        events = await repo.get_audit_events("monotonic-test", limit=10)
        # Events are returned most recent first, so reverse for chronological order
        event_ids = [e.event_id for e in reversed(events)]

        # Each ULID should be greater than the previous (lexicographic order)
        for i in range(1, len(event_ids)):
            assert event_ids[i] > event_ids[i - 1], (
                f"Event IDs not monotonic: {event_ids[i - 1]} >= {event_ids[i]}"
            )

    @pytest.mark.asyncio
    async def test_get_caller_identity_handles_sts_failure(self, repo):
        """STS failures should be handled gracefully, returning None."""
        from unittest.mock import AsyncMock, MagicMock, patch

        # Reset cached identity
        repo._caller_identity_fetched = False
        repo._caller_identity_arn = None

        # Mock STS client to raise exception
        mock_sts_client = AsyncMock()
        mock_sts_client.get_caller_identity.side_effect = Exception("STS unavailable")
        mock_sts_client.__aenter__ = AsyncMock(return_value=mock_sts_client)
        mock_sts_client.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.client.return_value = mock_sts_client

        with patch.object(repo, "_session", mock_session):
            arn = await repo._get_caller_identity_arn()

        # Should return None on failure
        assert arn is None
        # Should be cached
        assert repo._caller_identity_fetched is True
        assert repo._caller_identity_arn is None

    @pytest.mark.asyncio
    async def test_get_audit_retention_days_from_stack_options(self, repo):
        """Should return audit_retention_days from stack_options if available."""
        from zae_limiter.models import StackOptions

        repo._stack_options = StackOptions(audit_retention_days=30)
        repo._audit_retention_days_cache = None

        days = await repo._get_audit_retention_days()

        assert days == 30
        assert repo._audit_retention_days_cache == 30

    @pytest.mark.asyncio
    async def test_get_audit_retention_days_from_cache(self, repo):
        """Should return cached value if available."""
        repo._audit_retention_days_cache = 45

        days = await repo._get_audit_retention_days()

        assert days == 45

    @pytest.mark.asyncio
    async def test_get_audit_retention_days_from_dynamodb(self, repo):
        """Should read from DynamoDB system config when no stack_options."""
        from zae_limiter import schema

        repo._stack_options = None
        repo._audit_retention_days_cache = None

        # Write audit_retention_days to system config
        client = await repo._get_client()
        await client.update_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_system("default")},
                "SK": {"S": schema.sk_config()},
            },
            UpdateExpression="SET audit_retention_days = :ard",
            ExpressionAttributeValues={
                ":ard": {"N": "60"},
            },
        )

        days = await repo._get_audit_retention_days()

        assert days == 60
        assert repo._audit_retention_days_cache == 60

    @pytest.mark.asyncio
    async def test_get_audit_retention_days_default_when_not_set(self, repo):
        """Should return default 90 days when not configured."""
        repo._stack_options = None
        repo._audit_retention_days_cache = None

        days = await repo._get_audit_retention_days()

        assert days == 90
        assert repo._audit_retention_days_cache == 90

    @pytest.mark.asyncio
    async def test_write_audit_retention_config(self, repo):
        """Should write audit_retention_days to system config."""
        from zae_limiter import schema
        from zae_limiter.models import StackOptions

        repo._stack_options = StackOptions(audit_retention_days=14)

        await repo._write_audit_retention_config()

        # Verify it was written
        client = await repo._get_client()
        response = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_system("default")},
                "SK": {"S": schema.sk_config()},
            },
        )

        item = response.get("Item", {})
        assert item.get("audit_retention_days", {}).get("N") == "14"
        assert repo._audit_retention_days_cache == 14

    @pytest.mark.asyncio
    async def test_write_audit_retention_config_noop_without_stack_options(self, repo):
        """Should be a no-op when stack_options is None."""
        repo._stack_options = None

        # Should not raise
        await repo._write_audit_retention_config()

    @pytest.mark.asyncio
    async def test_audit_event_uses_configured_ttl(self, repo):
        """Audit event TTL should be based on audit_retention_days."""
        from zae_limiter import schema
        from zae_limiter.models import StackOptions

        # Set audit retention to 7 days
        repo._stack_options = StackOptions(audit_retention_days=7)
        repo._audit_retention_days_cache = None

        # Create entity to trigger audit event
        await repo.create_entity(entity_id="ttl-test")

        # Get the audit record directly to check TTL
        client = await repo._get_client()
        response = await client.query(
            TableName=repo.table_name,
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk_prefix)",
            ExpressionAttributeValues={
                ":pk": {"S": schema.pk_audit("default", "ttl-test")},
                ":sk_prefix": {"S": schema.SK_AUDIT},
            },
        )

        items = response.get("Items", [])
        assert len(items) >= 1

        # Check TTL attribute exists and is reasonable
        # 7 days = 604800 seconds from now
        ttl = int(items[0]["ttl"]["N"])
        now_seconds = int(repo._now_ms() / 1000)
        expected_ttl = now_seconds + (7 * 86400)

        # Allow 10 second tolerance
        assert abs(ttl - expected_ttl) < 10


class TestRepositoryUsageSnapshots:
    """Tests for usage snapshot queries."""

    @pytest.fixture
    async def repo_with_snapshots(self, repo):
        """Repository pre-populated with test usage snapshots."""
        from zae_limiter import schema

        client = await repo._get_client()

        # Create snapshots for multiple entities, resources, and time windows
        snapshots_data = [
            # Entity 1, gpt-4, hourly snapshots
            ("entity-1", "gpt-4", "hourly", "2024-01-15T10:00:00Z", {"tpm": 1000, "rpm": 5}),
            ("entity-1", "gpt-4", "hourly", "2024-01-15T11:00:00Z", {"tpm": 2000, "rpm": 10}),
            ("entity-1", "gpt-4", "hourly", "2024-01-15T12:00:00Z", {"tpm": 1500, "rpm": 8}),
            # Entity 1, gpt-4, daily snapshot
            ("entity-1", "gpt-4", "daily", "2024-01-15T00:00:00Z", {"tpm": 4500, "rpm": 23}),
            # Entity 1, gpt-3.5, hourly
            ("entity-1", "gpt-3.5", "hourly", "2024-01-15T10:00:00Z", {"tpm": 500, "rpm": 3}),
            # Entity 2, gpt-4, hourly
            ("entity-2", "gpt-4", "hourly", "2024-01-15T10:00:00Z", {"tpm": 3000, "rpm": 15}),
            ("entity-2", "gpt-4", "hourly", "2024-01-15T11:00:00Z", {"tpm": 2500, "rpm": 12}),
        ]

        for entity_id, resource, window_type, window_start, counters in snapshots_data:
            item = {
                "PK": {"S": schema.pk_entity("default", entity_id)},
                "SK": {"S": schema.sk_usage(resource, window_start)},
                "entity_id": {"S": entity_id},
                "resource": {"S": resource},
                "window": {"S": window_type},
                "window_start": {"S": window_start},
                "total_events": {"N": str(sum(counters.values()))},
                "GSI2PK": {"S": schema.gsi2_pk_resource("default", resource)},
                "GSI2SK": {"S": f"USAGE#{window_start}#{entity_id}"},
            }
            # Add counters as top-level attributes
            for name, value in counters.items():
                item[name] = {"N": str(value)}

            await client.put_item(TableName=repo.table_name, Item=item)

        yield repo

    @pytest.mark.asyncio
    async def test_get_usage_snapshots_by_entity(self, repo_with_snapshots):
        """Query snapshots for a single entity."""
        snapshots, next_key = await repo_with_snapshots.get_usage_snapshots(entity_id="entity-1")

        # Entity 1 has 5 snapshots total
        assert len(snapshots) == 5
        assert all(s.entity_id == "entity-1" for s in snapshots)
        assert next_key is None  # All results fit in one page

    @pytest.mark.asyncio
    async def test_get_usage_snapshots_by_entity_and_resource(self, repo_with_snapshots):
        """Query snapshots for entity + resource filter."""
        snapshots, next_key = await repo_with_snapshots.get_usage_snapshots(
            entity_id="entity-1",
            resource="gpt-4",
        )

        # Entity 1, gpt-4 has 4 snapshots (3 hourly + 1 daily)
        assert len(snapshots) == 4
        assert all(s.entity_id == "entity-1" for s in snapshots)
        assert all(s.resource == "gpt-4" for s in snapshots)

    @pytest.mark.asyncio
    async def test_get_usage_snapshots_by_resource_gsi2(self, repo_with_snapshots):
        """Query snapshots for a resource across all entities (GSI2)."""
        snapshots, next_key = await repo_with_snapshots.get_usage_snapshots(resource="gpt-4")

        # gpt-4 has snapshots from entity-1 (4) and entity-2 (2) = 6 total
        assert len(snapshots) == 6
        assert all(s.resource == "gpt-4" for s in snapshots)
        # Verify both entities are present
        entity_ids = {s.entity_id for s in snapshots}
        assert entity_ids == {"entity-1", "entity-2"}

    @pytest.mark.asyncio
    async def test_get_usage_snapshots_filter_by_window_type(self, repo_with_snapshots):
        """Filter snapshots by window type."""
        snapshots, _ = await repo_with_snapshots.get_usage_snapshots(
            entity_id="entity-1",
            resource="gpt-4",
            window_type="hourly",
        )

        # Entity 1, gpt-4 has 3 hourly snapshots
        assert len(snapshots) == 3
        assert all(s.window_type == "hourly" for s in snapshots)

    @pytest.mark.asyncio
    async def test_get_usage_snapshots_filter_by_time_range(self, repo_with_snapshots):
        """Filter snapshots by start_time and end_time."""
        snapshots, _ = await repo_with_snapshots.get_usage_snapshots(
            entity_id="entity-1",
            resource="gpt-4",
            start_time="2024-01-15T10:00:00Z",
            end_time="2024-01-15T11:00:00Z",
        )

        # Should include 10:00 and 11:00 hourly snapshots (window_start <= end_time)
        assert len(snapshots) == 2
        window_starts = {s.window_start for s in snapshots}
        assert "2024-01-15T10:00:00Z" in window_starts
        assert "2024-01-15T11:00:00Z" in window_starts

    @pytest.mark.asyncio
    async def test_get_usage_snapshots_empty_result(self, repo_with_snapshots):
        """Query for nonexistent entity returns empty list."""
        snapshots, next_key = await repo_with_snapshots.get_usage_snapshots(entity_id="nonexistent")

        assert snapshots == []
        assert next_key is None

    @pytest.mark.asyncio
    async def test_get_usage_snapshots_requires_entity_or_resource(self, repo):
        """Should raise ValueError if neither entity_id nor resource provided."""
        with pytest.raises(ValueError, match="Either entity_id or resource"):
            await repo.get_usage_snapshots()

    @pytest.mark.asyncio
    async def test_get_usage_snapshots_pagination(self, repo_with_snapshots):
        """Test pagination with limit parameter."""
        # First page
        snapshots1, next_key1 = await repo_with_snapshots.get_usage_snapshots(
            entity_id="entity-1",
            limit=2,
        )

        assert len(snapshots1) == 2
        assert next_key1 is not None  # More results available

        # Second page
        snapshots2, next_key2 = await repo_with_snapshots.get_usage_snapshots(
            entity_id="entity-1",
            limit=2,
            next_key=next_key1,
        )

        assert len(snapshots2) == 2
        assert next_key2 is not None  # Still more results

        # Third page (final)
        snapshots3, next_key3 = await repo_with_snapshots.get_usage_snapshots(
            entity_id="entity-1",
            limit=2,
            next_key=next_key2,
        )

        assert len(snapshots3) == 1  # Only 1 remaining
        assert next_key3 is None  # No more results

        # Verify no duplicates (use resource+window_start as unique key)
        all_keys = [(s.resource, s.window_start) for s in snapshots1 + snapshots2 + snapshots3]
        assert len(all_keys) == len(set(all_keys))

    @pytest.mark.asyncio
    async def test_get_usage_snapshots_counters_extracted(self, repo_with_snapshots):
        """Verify counters are correctly extracted from flat schema."""
        snapshots, _ = await repo_with_snapshots.get_usage_snapshots(
            entity_id="entity-1",
            resource="gpt-4",
            window_type="hourly",
            start_time="2024-01-15T10:00:00Z",
            end_time="2024-01-15T10:00:00Z",
        )

        assert len(snapshots) == 1
        snapshot = snapshots[0]
        assert snapshot.counters == {"tpm": 1000, "rpm": 5}
        assert snapshot.total_events == 1005  # sum of counters

    @pytest.mark.asyncio
    async def test_get_usage_snapshots_window_end_calculated(self, repo_with_snapshots):
        """Verify window_end is correctly calculated."""
        snapshots, _ = await repo_with_snapshots.get_usage_snapshots(
            entity_id="entity-1",
            resource="gpt-4",
            window_type="hourly",
            start_time="2024-01-15T10:00:00Z",
            end_time="2024-01-15T10:00:00Z",
        )

        assert len(snapshots) == 1
        snapshot = snapshots[0]
        assert snapshot.window_start == "2024-01-15T10:00:00Z"
        # Hourly window_end should be :59:59
        assert "10:59:59" in snapshot.window_end

    @pytest.mark.asyncio
    async def test_get_usage_summary_aggregation(self, repo_with_snapshots):
        """Test summary aggregation across snapshots."""
        summary = await repo_with_snapshots.get_usage_summary(
            entity_id="entity-1",
            resource="gpt-4",
            window_type="hourly",
        )

        # 3 hourly snapshots for entity-1, gpt-4
        assert summary.snapshot_count == 3

        # Total: 1000 + 2000 + 1500 = 4500 tpm, 5 + 10 + 8 = 23 rpm
        assert summary.total["tpm"] == 4500
        assert summary.total["rpm"] == 23

        # Average: 4500/3 = 1500 tpm, 23/3 ≈ 7.67 rpm
        assert summary.average["tpm"] == 1500.0
        assert abs(summary.average["rpm"] - 7.666666666666667) < 0.001

        # Time range
        assert summary.min_window_start == "2024-01-15T10:00:00Z"
        assert summary.max_window_start == "2024-01-15T12:00:00Z"

    @pytest.mark.asyncio
    async def test_get_usage_summary_empty(self, repo_with_snapshots):
        """Summary for nonexistent entity returns zeros."""
        summary = await repo_with_snapshots.get_usage_summary(entity_id="nonexistent")

        assert summary.snapshot_count == 0
        assert summary.total == {}
        assert summary.average == {}
        assert summary.min_window_start is None
        assert summary.max_window_start is None

    @pytest.mark.asyncio
    async def test_get_usage_summary_requires_entity_or_resource(self, repo):
        """Should raise ValueError if neither entity_id nor resource provided."""
        with pytest.raises(ValueError, match="Either entity_id or resource"):
            await repo.get_usage_summary()

    @pytest.mark.asyncio
    async def test_get_usage_snapshots_skips_malformed_items(self, repo):
        """Malformed snapshot items are skipped during deserialization."""
        from zae_limiter import schema

        client = await repo._get_client()

        # Item missing entity_id (malformed)
        await client.put_item(
            TableName=repo.table_name,
            Item={
                "PK": {"S": schema.pk_entity("default", "test-malformed")},
                "SK": {"S": schema.sk_usage("gpt-4", "2024-01-15T10:00:00Z")},
                # Missing entity_id, resource, window_start
                "window": {"S": "hourly"},
                "tpm": {"N": "100"},
            },
        )

        # Item with valid data
        await client.put_item(
            TableName=repo.table_name,
            Item={
                "PK": {"S": schema.pk_entity("default", "test-malformed")},
                "SK": {"S": schema.sk_usage("gpt-4", "2024-01-15T11:00:00Z")},
                "entity_id": {"S": "test-malformed"},
                "resource": {"S": "gpt-4"},
                "window": {"S": "hourly"},
                "window_start": {"S": "2024-01-15T11:00:00Z"},
                "tpm": {"N": "200"},
                "total_events": {"N": "10"},
                "GSI2PK": {"S": "default/RESOURCE#gpt-4"},
                "GSI2SK": {"S": "USAGE#2024-01-15T11:00:00Z#test-malformed"},
            },
        )

        # Query should skip malformed item and return only valid one
        snapshots, _ = await repo.get_usage_snapshots(entity_id="test-malformed")

        assert len(snapshots) == 1
        assert snapshots[0].entity_id == "test-malformed"
        assert snapshots[0].window_start == "2024-01-15T11:00:00Z"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "window_type,window_start,expected_end_contains",
        [
            # Hourly window ends at :59:59
            ("hourly", "2024-01-15T10:00:00Z", "10:59:59"),
            # Daily window ends at 23:59:59
            ("daily", "2024-01-15T00:00:00Z", "23:59:59"),
            # Monthly window (January) ends on Jan 31
            ("monthly", "2024-01-01T00:00:00Z", "2024-01-31"),
            # Monthly window (December) - year rollover ends on Dec 31
            ("monthly", "2024-12-01T00:00:00Z", "2024-12-31"),
            # Monthly window (February leap year) ends on Feb 29
            ("monthly", "2024-02-01T00:00:00Z", "2024-02-29"),
            # Monthly window (February non-leap year) ends on Feb 28
            ("monthly", "2023-02-01T00:00:00Z", "2023-02-28"),
        ],
    )
    async def test_get_usage_snapshots_window_end_by_type(
        self, repo, window_type, window_start, expected_end_contains
    ):
        """Test window_end calculation for all supported window types."""
        from zae_limiter import schema

        client = await repo._get_client()
        entity_id = f"test-{window_type}-{window_start[:10]}"

        await client.put_item(
            TableName=repo.table_name,
            Item={
                "PK": {"S": schema.pk_entity("default", entity_id)},
                "SK": {"S": schema.sk_usage("gpt-4", window_start)},
                "entity_id": {"S": entity_id},
                "resource": {"S": "gpt-4"},
                "window": {"S": window_type},
                "window_start": {"S": window_start},
                "tpm": {"N": "1000"},
                "total_events": {"N": "10"},
                "GSI2PK": {"S": "default/RESOURCE#gpt-4"},
                "GSI2SK": {"S": f"USAGE#{window_start}#{entity_id}"},
            },
        )

        snapshots, _ = await repo.get_usage_snapshots(entity_id=entity_id)

        assert len(snapshots) == 1
        assert snapshots[0].window_type == window_type
        assert expected_end_contains in snapshots[0].window_end

    @pytest.mark.asyncio
    async def test_get_usage_snapshots_unknown_window_type(self, repo):
        """Test window_end for unknown window type returns window_start."""
        from zae_limiter import schema

        client = await repo._get_client()

        await client.put_item(
            TableName=repo.table_name,
            Item={
                "PK": {"S": schema.pk_entity("default", "unknown-window")},
                "SK": {"S": schema.sk_usage("gpt-4", "2024-01-15T10:00:00Z")},
                "entity_id": {"S": "unknown-window"},
                "resource": {"S": "gpt-4"},
                "window": {"S": "unknown"},  # Unknown window type
                "window_start": {"S": "2024-01-15T10:00:00Z"},
                "tpm": {"N": "100"},
                "total_events": {"N": "5"},
                "GSI2PK": {"S": "default/RESOURCE#gpt-4"},
                "GSI2SK": {"S": "USAGE#2024-01-15T10:00:00Z#unknown-window"},
            },
        )

        snapshots, _ = await repo.get_usage_snapshots(entity_id="unknown-window")

        assert len(snapshots) == 1
        # Unknown window type should fall through to window_start
        assert snapshots[0].window_end == "2024-01-15T10:00:00Z"

    @pytest.mark.asyncio
    async def test_get_usage_snapshots_invalid_window_start(self, repo):
        """Test window_end for invalid window_start returns window_start."""
        from zae_limiter import schema

        client = await repo._get_client()

        await client.put_item(
            TableName=repo.table_name,
            Item={
                "PK": {"S": schema.pk_entity("default", "invalid-date")},
                "SK": {"S": schema.sk_usage("gpt-4", "invalid-date")},
                "entity_id": {"S": "invalid-date"},
                "resource": {"S": "gpt-4"},
                "window": {"S": "hourly"},
                "window_start": {"S": "invalid-date"},  # Invalid date format
                "tpm": {"N": "100"},
                "total_events": {"N": "5"},
                "GSI2PK": {"S": "default/RESOURCE#gpt-4"},
                "GSI2SK": {"S": "USAGE#invalid-date#invalid-date"},
            },
        )

        snapshots, _ = await repo.get_usage_snapshots(entity_id="invalid-date")

        assert len(snapshots) == 1
        # Invalid date should return original value
        assert snapshots[0].window_end == "invalid-date"


class TestRepositoryDeprecation:
    """Tests for deprecated Repository methods."""

    @pytest.mark.asyncio
    async def test_create_stack_emits_deprecation_warning(self):
        """create_stack() should emit DeprecationWarning pointing to ensure_infrastructure()."""
        from unittest.mock import AsyncMock, patch

        repo = Repository(name="test-deprecation", region="us-east-1")

        # Mock StackManager to avoid actual CloudFormation calls
        with patch("zae_limiter.infra.stack_manager.StackManager") as mock_manager_class:
            mock_manager = AsyncMock()
            mock_manager.__aenter__ = AsyncMock(return_value=mock_manager)
            mock_manager.__aexit__ = AsyncMock(return_value=None)
            mock_manager.create_stack = AsyncMock(return_value={"StackId": "test"})
            mock_manager_class.return_value = mock_manager

            # Also mock _write_audit_retention_config to avoid DynamoDB call
            with patch.object(repo, "_write_audit_retention_config", AsyncMock()):
                # Verify deprecation warning is raised
                with pytest.warns(DeprecationWarning, match="create_stack.*deprecated"):
                    from zae_limiter import StackOptions

                    await repo.create_stack(stack_options=StackOptions())

        await repo.close()

    @pytest.mark.asyncio
    async def test_create_stack_deprecation_message_mentions_ensure_infrastructure(
        self,
    ):
        """Deprecation message should direct users to ensure_infrastructure()."""
        import warnings
        from unittest.mock import AsyncMock, patch

        repo = Repository(name="test-deprecation-msg", region="us-east-1")

        with patch("zae_limiter.infra.stack_manager.StackManager") as mock_manager_class:
            mock_manager = AsyncMock()
            mock_manager.__aenter__ = AsyncMock(return_value=mock_manager)
            mock_manager.__aexit__ = AsyncMock(return_value=None)
            mock_manager.create_stack = AsyncMock(return_value={"StackId": "test"})
            mock_manager_class.return_value = mock_manager

            # Also mock _write_audit_retention_config to avoid DynamoDB call
            with patch.object(repo, "_write_audit_retention_config", AsyncMock()):
                with warnings.catch_warnings(record=True) as w:
                    warnings.simplefilter("always")
                    from zae_limiter import StackOptions

                    await repo.create_stack(stack_options=StackOptions())

                    # Filter for the create_stack deprecation (not the StackOptions one)
                    create_stack_warnings = [
                        x
                        for x in w
                        if issubclass(x.category, DeprecationWarning)
                        and "create_stack" in str(x.message)
                    ]
                    assert len(create_stack_warnings) == 1

                    # Message should mention ensure_infrastructure
                    msg = str(create_stack_warnings[0].message)
                    assert "ensure_infrastructure" in msg
                    assert "v1.0.0" in msg

        await repo.close()

    @pytest.mark.asyncio
    async def test_create_stack_without_options_uses_constructor_options(self):
        """create_stack() without args should use constructor-provided stack_options."""
        import warnings
        from unittest.mock import AsyncMock, patch

        from zae_limiter import StackOptions

        # Create repo with stack_options in constructor
        repo = Repository(
            name="test-constructor-opts",
            region="us-east-1",
            stack_options=StackOptions(lambda_memory=512),
        )

        with patch("zae_limiter.infra.stack_manager.StackManager") as mock_manager_class:
            mock_manager = AsyncMock()
            mock_manager.__aenter__ = AsyncMock(return_value=mock_manager)
            mock_manager.__aexit__ = AsyncMock(return_value=None)
            mock_manager.create_stack = AsyncMock(return_value={"StackId": "test"})
            mock_manager_class.return_value = mock_manager

            # Also mock _write_audit_retention_config to avoid DynamoDB call
            with patch.object(repo, "_write_audit_retention_config", AsyncMock()):
                # Suppress the deprecation warning - we're testing the functionality
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    await repo.create_stack()  # No stack_options arg

                # Verify create_stack was called with the constructor-provided options
                mock_manager.create_stack.assert_called_once()
                call_kwargs = mock_manager.create_stack.call_args[1]
                assert call_kwargs["stack_options"].lambda_memory == 512

        await repo.close()


class TestGSI3EntityConfigIndex:
    """Tests for GSI3 sparse index for entity config queries."""

    @pytest.mark.asyncio
    async def test_set_limits_writes_gsi3_attributes(self, repo):
        """set_limits should write GSI3PK and GSI3SK for entity-level configs."""
        await repo.create_entity("user-123")
        limits = [Limit.per_minute("rpm", 1000)]
        await repo.set_limits("user-123", limits, resource="gpt-4")

        # Read the raw item to verify GSI3 attributes
        client = await repo._get_client()
        from zae_limiter.schema import gsi3_pk_entity_config, gsi3_sk_entity, pk_entity, sk_config

        response = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": pk_entity("default", "user-123")},
                "SK": {"S": sk_config("gpt-4")},
            },
        )
        item = response.get("Item")
        assert item is not None

        # Verify GSI3 attributes
        assert "GSI3PK" in item
        assert item["GSI3PK"]["S"] == gsi3_pk_entity_config("default", "gpt-4")
        assert "GSI3SK" in item
        assert item["GSI3SK"]["S"] == gsi3_sk_entity("user-123")

    @pytest.mark.asyncio
    async def test_delete_limits_removes_from_gsi3(self, repo):
        """delete_limits removes entity from GSI3 (via DeleteItem)."""
        await repo.create_entity("user-123")
        limits = [Limit.per_minute("rpm", 1000)]
        await repo.set_limits("user-123", limits, resource="gpt-4")

        # Verify exists
        entities, _ = await repo.list_entities_with_custom_limits("gpt-4")
        assert "user-123" in entities

        # Delete
        await repo.delete_limits("user-123", resource="gpt-4")

        # Verify removed
        entities, _ = await repo.list_entities_with_custom_limits("gpt-4")
        assert "user-123" not in entities

    @pytest.mark.asyncio
    async def test_system_config_not_in_gsi3(self, repo):
        """System config should not have GSI3 attributes."""
        limits = [Limit.per_minute("rpm", 500)]
        await repo.set_system_defaults(limits)

        # Read the raw item to verify no GSI3 attributes
        client = await repo._get_client()
        from zae_limiter.schema import pk_system, sk_config

        response = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": pk_system("default")},
                "SK": {"S": sk_config()},
            },
        )
        item = response.get("Item")
        assert item is not None

        # Verify no GSI3 attributes (system config is not indexed)
        assert "GSI3PK" not in item
        assert "GSI3SK" not in item

    @pytest.mark.asyncio
    async def test_resource_config_not_in_gsi3(self, repo):
        """Resource config should not have GSI3 attributes."""
        limits = [Limit.per_minute("rpm", 500)]
        await repo.set_resource_defaults("gpt-4", limits)

        # Read the raw item to verify no GSI3 attributes
        client = await repo._get_client()
        from zae_limiter.schema import pk_resource, sk_config

        response = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": pk_resource("default", "gpt-4")},
                "SK": {"S": sk_config()},
            },
        )
        item = response.get("Item")
        assert item is not None

        # Verify no GSI3 attributes (resource config is not indexed)
        assert "GSI3PK" not in item
        assert "GSI3SK" not in item

    @pytest.mark.asyncio
    async def test_list_entities_with_custom_limits(self, repo):
        """list_entities_with_custom_limits returns correct entities."""
        # Create multiple entities with custom limits for different resources
        await repo.create_entity("user-1")
        await repo.create_entity("user-2")
        await repo.create_entity("user-3")

        limits = [Limit.per_minute("rpm", 1000)]
        await repo.set_limits("user-1", limits, resource="gpt-4")
        await repo.set_limits("user-2", limits, resource="gpt-4")
        await repo.set_limits("user-3", limits, resource="claude-3")

        # Query for gpt-4
        entities, cursor = await repo.list_entities_with_custom_limits("gpt-4")
        assert set(entities) == {"user-1", "user-2"}
        assert cursor is None  # No more results

        # Query for claude-3
        entities, cursor = await repo.list_entities_with_custom_limits("claude-3")
        assert set(entities) == {"user-3"}
        assert cursor is None

        # Query for nonexistent resource
        entities, cursor = await repo.list_entities_with_custom_limits("nonexistent")
        assert entities == []
        assert cursor is None

    @pytest.mark.asyncio
    async def test_list_entities_with_custom_limits_pagination(self, repo):
        """list_entities_with_custom_limits should support pagination."""
        # Create multiple entities
        limits = [Limit.per_minute("rpm", 1000)]
        for i in range(5):
            await repo.create_entity(f"user-{i}")
            await repo.set_limits(f"user-{i}", limits, resource="gpt-4")

        # Fetch with limit
        entities, cursor = await repo.list_entities_with_custom_limits("gpt-4", limit=2)
        assert len(entities) == 2
        # DynamoDB pagination may or may not return a cursor depending on result size
        # If cursor is returned, verify we can use it to get more results
        if cursor is not None:
            more_entities, _ = await repo.list_entities_with_custom_limits("gpt-4", cursor=cursor)
            # Combined results should include remaining entities
            all_entities = set(entities + more_entities)
            assert len(all_entities) >= 2  # At least got more than first page


class TestEntityConfigRegistry:
    """Tests for entity config registry (issue #288)."""

    @pytest.mark.asyncio
    async def test_set_limits_increments_registry_on_new(self, repo):
        """set_limits should increment registry count for NEW entity configs."""
        await repo.create_entity("user-1")
        limits = [Limit.per_minute("rpm", 1000)]

        # Set limits for first time (NEW)
        await repo.set_limits("user-1", limits, resource="gpt-4")

        # Verify registry was updated
        resources = await repo.list_resources_with_entity_configs()
        assert "gpt-4" in resources

    @pytest.mark.asyncio
    async def test_set_limits_no_increment_on_update(self, repo):
        """set_limits should NOT increment registry count on UPDATE."""
        await repo.create_entity("user-1")
        limits1 = [Limit.per_minute("rpm", 1000)]
        limits2 = [Limit.per_minute("rpm", 2000)]

        # Set limits twice (second is UPDATE)
        await repo.set_limits("user-1", limits1, resource="gpt-4")
        await repo.set_limits("user-1", limits2, resource="gpt-4")

        # Verify limits were updated
        stored = await repo.get_limits("user-1", resource="gpt-4")
        assert stored[0].capacity == 2000

        # Verify registry count is still 1 (not 2)
        # We can verify by checking raw DynamoDB item
        from zae_limiter import schema

        client = await repo._get_client()
        response = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_system("default")},
                "SK": {"S": schema.sk_entity_config_resources()},
            },
        )
        item = response.get("Item", {})
        count = int(item.get("gpt-4", {}).get("N", "0"))
        assert count == 1

    @pytest.mark.asyncio
    async def test_delete_limits_decrements_registry(self, repo):
        """delete_limits should decrement registry count."""
        await repo.create_entity("user-1")
        await repo.create_entity("user-2")
        limits = [Limit.per_minute("rpm", 1000)]

        # Create two entity configs for same resource
        await repo.set_limits("user-1", limits, resource="gpt-4")
        await repo.set_limits("user-2", limits, resource="gpt-4")

        # Verify both are registered
        resources = await repo.list_resources_with_entity_configs()
        assert "gpt-4" in resources

        # Delete one
        await repo.delete_limits("user-1", resource="gpt-4")

        # Resource should still be listed (count = 1)
        resources = await repo.list_resources_with_entity_configs()
        assert "gpt-4" in resources

        # Delete the other
        await repo.delete_limits("user-2", resource="gpt-4")

        # Resource should no longer be listed (count = 0, attribute removed)
        resources = await repo.list_resources_with_entity_configs()
        assert "gpt-4" not in resources

    @pytest.mark.asyncio
    async def test_delete_limits_nonexistent_config_is_silent(self, repo):
        """delete_limits silently succeeds when config doesn't exist."""
        await repo.create_entity("user-1")

        # Delete limits that were never set - should not raise
        await repo.delete_limits("user-1", resource="gpt-4")

        # Registry should be unaffected (no resource was ever added)
        resources = await repo.list_resources_with_entity_configs()
        assert "gpt-4" not in resources

    @pytest.mark.asyncio
    async def test_list_resources_with_entity_configs_empty(self, repo):
        """list_resources_with_entity_configs returns empty list when no configs exist."""
        resources = await repo.list_resources_with_entity_configs()
        assert resources == []

    @pytest.mark.asyncio
    async def test_list_resources_with_entity_configs_multiple_resources(self, repo):
        """list_resources_with_entity_configs returns all resources with entity configs."""
        await repo.create_entity("user-1")
        await repo.create_entity("user-2")
        limits = [Limit.per_minute("rpm", 1000)]

        # Create configs for multiple resources
        await repo.set_limits("user-1", limits, resource="gpt-4")
        await repo.set_limits("user-2", limits, resource="claude-3")

        resources = await repo.list_resources_with_entity_configs()
        assert set(resources) == {"gpt-4", "claude-3"}

    @pytest.mark.asyncio
    async def test_list_resources_with_entity_configs_sorted(self, repo):
        """list_resources_with_entity_configs returns sorted list."""
        await repo.create_entity("user-1")
        limits = [Limit.per_minute("rpm", 1000)]

        # Create in non-alphabetical order
        await repo.set_limits("user-1", limits, resource="zebra")
        await repo.set_limits("user-1", limits, resource="alpha")
        await repo.set_limits("user-1", limits, resource="middle")

        resources = await repo.list_resources_with_entity_configs()
        assert resources == ["alpha", "middle", "zebra"]

    @pytest.mark.asyncio
    async def test_set_limits_reraises_non_conditional_transaction_error(self, repo):
        """set_limits re-raises transaction errors that aren't ConditionalCheckFailed."""
        from unittest.mock import AsyncMock, patch

        from botocore.exceptions import ClientError

        await repo.create_entity("user-1")
        limits = [Limit.per_minute("rpm", 1000)]

        # Mock the client to raise a non-conditional transaction error
        error_response = {
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [{"Code": "ValidationError"}],  # Not ConditionalCheckFailed
        }
        mock_client = AsyncMock()
        mock_client.transact_write_items = AsyncMock(
            side_effect=ClientError(error_response, "TransactWriteItems")
        )

        with patch.object(repo, "_get_client", return_value=mock_client):
            with pytest.raises(ClientError) as exc_info:
                await repo.set_limits("user-1", limits, resource="gpt-4")
            assert exc_info.value.response["Error"]["Code"] == "TransactionCanceledException"

    @pytest.mark.asyncio
    async def test_set_limits_reraises_non_transaction_error(self, repo):
        """set_limits re-raises non-transaction ClientErrors."""
        from unittest.mock import AsyncMock, patch

        from botocore.exceptions import ClientError

        await repo.create_entity("user-1")
        limits = [Limit.per_minute("rpm", 1000)]

        # Mock the client to raise a different error type
        error_response = {"Error": {"Code": "InternalServerError"}}
        mock_client = AsyncMock()
        mock_client.transact_write_items = AsyncMock(
            side_effect=ClientError(error_response, "TransactWriteItems")
        )

        with patch.object(repo, "_get_client", return_value=mock_client):
            with pytest.raises(ClientError) as exc_info:
                await repo.set_limits("user-1", limits, resource="gpt-4")
            assert exc_info.value.response["Error"]["Code"] == "InternalServerError"

    @pytest.mark.asyncio
    async def test_delete_limits_reraises_non_conditional_transaction_error(self, repo):
        """delete_limits re-raises transaction errors that aren't ConditionalCheckFailed."""
        from unittest.mock import AsyncMock, patch

        from botocore.exceptions import ClientError

        await repo.create_entity("user-1")
        limits = [Limit.per_minute("rpm", 1000)]
        await repo.set_limits("user-1", limits, resource="gpt-4")

        # Mock the client to raise a non-conditional transaction error
        error_response = {
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [{"Code": "ValidationError"}],  # Not ConditionalCheckFailed
        }
        mock_client = AsyncMock()
        mock_client.transact_write_items = AsyncMock(
            side_effect=ClientError(error_response, "TransactWriteItems")
        )

        with patch.object(repo, "_get_client", return_value=mock_client):
            with pytest.raises(ClientError) as exc_info:
                await repo.delete_limits("user-1", resource="gpt-4")
            assert exc_info.value.response["Error"]["Code"] == "TransactionCanceledException"

    @pytest.mark.asyncio
    async def test_delete_limits_reraises_non_transaction_error(self, repo):
        """delete_limits re-raises non-transaction ClientErrors."""
        from unittest.mock import AsyncMock, patch

        from botocore.exceptions import ClientError

        await repo.create_entity("user-1")
        limits = [Limit.per_minute("rpm", 1000)]
        await repo.set_limits("user-1", limits, resource="gpt-4")

        # Mock the client to raise a different error type
        error_response = {"Error": {"Code": "InternalServerError"}}
        mock_client = AsyncMock()
        mock_client.transact_write_items = AsyncMock(
            side_effect=ClientError(error_response, "TransactWriteItems")
        )

        with patch.object(repo, "_get_client", return_value=mock_client):
            with pytest.raises(ClientError) as exc_info:
                await repo.delete_limits("user-1", resource="gpt-4")
            assert exc_info.value.response["Error"]["Code"] == "InternalServerError"

    @pytest.mark.asyncio
    async def test_cleanup_registry_reraises_non_conditional_error(self, repo):
        """_cleanup_entity_config_registry re-raises non-ConditionalCheckFailedException."""
        from unittest.mock import AsyncMock, patch

        from botocore.exceptions import ClientError

        # Mock the client to raise a different error type
        error_response = {"Error": {"Code": "InternalServerError"}}
        mock_client = AsyncMock()
        mock_client.update_item = AsyncMock(side_effect=ClientError(error_response, "UpdateItem"))

        with patch.object(repo, "_get_client", return_value=mock_client):
            with pytest.raises(ClientError) as exc_info:
                await repo._cleanup_entity_config_registry("gpt-4")
            assert exc_info.value.response["Error"]["Code"] == "InternalServerError"


class TestRepositoryEntityDuplicates:
    """Tests for duplicate entity creation handling."""

    @pytest.mark.asyncio
    async def test_create_entity_raises_entity_exists_on_duplicate(self, repo):
        """Creating an entity that already exists should raise EntityExistsError."""
        await repo.create_entity("existing-entity", name="Original")

        with pytest.raises(EntityExistsError) as exc_info:
            await repo.create_entity("existing-entity", name="Duplicate")

        assert exc_info.value.entity_id == "existing-entity"

    @pytest.mark.asyncio
    async def test_delete_entity_noop_for_nonexistent(self, repo):
        """Deleting a nonexistent entity should be a no-op (no error)."""
        # Should not raise
        await repo.delete_entity("nonexistent-entity")

        # Verify entity still doesn't exist
        entity = await repo.get_entity("nonexistent-entity")
        assert entity is None


class TestRepositoryTableOperations:
    """Tests for table-level operations."""

    @pytest.mark.asyncio
    async def test_delete_table_ignores_nonexistent(self, repo):
        """delete_table should not raise when table doesn't exist."""
        # Delete the table first
        await repo.delete_table()

        # Deleting again should not raise (ResourceNotFoundException is swallowed)
        await repo.delete_table()


class TestRepositoryPing:
    """Tests for ping connectivity check."""

    @pytest.mark.asyncio
    async def test_ping_returns_true(self, repo):
        """ping should return True when table is reachable."""
        result = await repo.ping()
        assert result is True

    @pytest.mark.asyncio
    async def test_ping_returns_false_on_error(self, repo):
        """ping should return False when DynamoDB is unreachable."""
        mock_client = AsyncMock()
        mock_client.get_item = AsyncMock(
            side_effect=ClientError(
                {"Error": {"Code": "ServiceUnavailable", "Message": "down"}},
                "GetItem",
            )
        )

        with patch.object(repo, "_get_client", return_value=mock_client):
            result = await repo.ping()

        assert result is False


class TestCompositeLimitConfigConvenience:
    """Tests for convenience methods on composite limit configs."""

    @pytest.mark.asyncio
    async def test_get_system_limits_convenience(self, repo):
        """get_system_limits should return only limits, not on_unavailable."""
        limits = [
            Limit.per_minute("rpm", 500),
            Limit.per_minute("tpm", 50000),
        ]
        await repo.set_system_defaults(limits, on_unavailable="allow")

        result = await repo.get_system_limits()

        assert len(result) == 2
        limit_map = {lim.name: lim for lim in result}
        assert limit_map["rpm"].capacity == 500
        assert limit_map["tpm"].capacity == 50000


class TestRepositoryAuditPagination:
    """Tests for audit event pagination with start_event_id."""

    @pytest.mark.asyncio
    async def test_get_audit_events_with_start_event_id(self, repo):
        """get_audit_events with start_event_id returns events after that ID."""
        # Create entity and multiple audit events
        await repo.create_entity(entity_id="pagination-cursor-test")
        for i in range(5):
            await repo.set_limits(
                entity_id="pagination-cursor-test",
                limits=[Limit.per_minute(f"limit-{i}", 100 * (i + 1))],
                principal=f"user-{i}",
            )

        # Get all events
        all_events = await repo.get_audit_events("pagination-cursor-test", limit=10)
        assert len(all_events) == 6  # 1 create + 5 set_limits

        # Use start_event_id to skip the first few events
        # Events are returned most recent first, so we use the event_id of the 3rd event
        middle_event_id = all_events[2].event_id
        remaining_events = await repo.get_audit_events(
            "pagination-cursor-test",
            limit=10,
            start_event_id=middle_event_id,
        )

        # Should get events after the middle one
        assert len(remaining_events) > 0
        # Remaining events should not include the middle event or anything newer
        remaining_ids = {e.event_id for e in remaining_events}
        assert middle_event_id not in remaining_ids


class TestRepositoryAuditResourceEntityId:
    """Tests for audit logging entity_id for resource-level operations."""

    @pytest.mark.asyncio
    async def test_delete_resource_defaults_audit_resource_entity_id(self, repo):
        """delete_resource_defaults should log audit with $RESOURCE:{name} entity_id."""
        limits = [Limit.per_minute("rpm", 100)]
        await repo.set_resource_defaults("gpt-4", limits, principal="admin")

        # Delete resource defaults
        await repo.delete_resource_defaults("gpt-4", principal="cleanup")

        # Check audit events for the resource entity (ADR-106: $RESOURCE:{name})
        events = await repo.get_audit_events("$RESOURCE:gpt-4")

        # Should have set + delete events
        assert len(events) >= 2

        delete_event = events[0]  # Most recent first
        assert delete_event.action == AuditAction.LIMITS_DELETED
        assert delete_event.principal == "cleanup"


class TestRepositoryDeserializationEdgeCases:
    """Tests for edge cases in DynamoDB deserialization."""

    @pytest.mark.asyncio
    async def test_deserialize_value_unknown_type_returns_none(self, repo):
        """_deserialize_value should return None for unknown DynamoDB types."""
        # Pass a dict with an unrecognized DynamoDB type key
        result = repo._deserialize_value({"UNKNOWN_TYPE": "some_value"})
        assert result is None

    @pytest.mark.asyncio
    async def test_deserialize_composite_bucket_with_total_consumed(self, repo):
        """_deserialize_bucket should extract total_consumed_milli from composite item."""
        now_ms = 1700000000000
        limits = [Limit.per_minute("rpm", 100)]
        state = BucketState.from_limit("e1", "gpt-4", limits[0], now_ms)

        # Create a composite bucket
        put_item = repo.build_composite_create("e1", "gpt-4", [state], now_ms)
        await repo.transact_write([put_item])

        # Consume some tokens to set total_consumed_milli
        consumed = {"rpm": 5000}
        adjust_item = repo.build_composite_adjust("e1", "gpt-4", consumed)
        if adjust_item:
            await repo.transact_write([adjust_item])

        # Get the bucket and verify total_consumed_milli is set
        bucket = await repo.get_bucket("e1", "gpt-4", "rpm")
        assert bucket is not None
        assert bucket.total_consumed_milli is not None
        assert bucket.total_consumed_milli == 5000


class TestSpeculativeConsume:
    """Tests for speculative_consume method."""

    async def test_speculative_success(self, repo):
        """Speculative consume succeeds when tokens available."""
        now_ms = int(time.time() * 1000)
        limits = [Limit.per_minute("rpm", 1000)]
        state = BucketState.from_limit("e1", "gpt-4", limits[0], now_ms)

        # Create bucket
        put_item = repo.build_composite_create("e1", "gpt-4", [state], now_ms)
        await repo.transact_write([put_item])

        # Speculative consume should succeed
        result = await repo.speculative_consume("e1", "gpt-4", {"rpm": 10})
        assert result.success is True
        assert len(result.buckets) == 1
        assert result.buckets[0].limit_name == "rpm"

    async def test_speculative_failure_insufficient_tokens(self, repo):
        """Speculative consume fails when tokens insufficient."""
        now_ms = int(time.time() * 1000)
        limits = [Limit.per_minute("rpm", 10)]
        state = BucketState.from_limit("e1", "gpt-4", limits[0], now_ms)

        # Create bucket with few tokens
        put_item = repo.build_composite_create("e1", "gpt-4", [state], now_ms)
        await repo.transact_write([put_item])

        # Exhaust tokens
        result = await repo.speculative_consume("e1", "gpt-4", {"rpm": 10})
        assert result.success is True

        # Now should fail
        result = await repo.speculative_consume("e1", "gpt-4", {"rpm": 5})
        assert result.success is False

    async def test_speculative_missing_item(self, repo):
        """Speculative consume fails when item doesn't exist."""
        result = await repo.speculative_consume("nonexistent", "gpt-4", {"rpm": 1})
        assert result.success is False
        assert result.old_buckets is None

    async def test_speculative_with_ttl(self, repo):
        """Speculative consume handles TTL correctly."""
        now_ms = int(time.time() * 1000)
        limits = [Limit.per_minute("rpm", 1000)]
        state = BucketState.from_limit("e1", "gpt-4", limits[0], now_ms)

        # Create bucket
        put_item = repo.build_composite_create("e1", "gpt-4", [state], now_ms)
        await repo.transact_write([put_item])

        # Speculative consume with TTL
        result = await repo.speculative_consume("e1", "gpt-4", {"rpm": 10}, ttl_seconds=3600)
        assert result.success is True
        assert len(result.buckets) == 1

    async def test_speculative_cascade_parent_id(self, repo):
        """Speculative consume returns cascade/parent_id from item."""
        now_ms = int(time.time() * 1000)
        limits = [Limit.per_minute("rpm", 1000)]
        state = BucketState.from_limit("e1", "gpt-4", limits[0], now_ms)

        # Create bucket with cascade and parent_id
        put_item = repo.build_composite_create(
            "e1", "gpt-4", [state], now_ms, cascade=True, parent_id="parent-1"
        )
        await repo.transact_write([put_item])

        result = await repo.speculative_consume("e1", "gpt-4", {"rpm": 1})
        assert result.success is True
        assert result.cascade is True
        assert result.parent_id == "parent-1"

    async def test_speculative_non_condition_error_reraises(self, repo):
        """Non-ConditionalCheckFailed errors are re-raised."""
        now_ms = int(time.time() * 1000)
        limits = [Limit.per_minute("rpm", 1000)]
        state = BucketState.from_limit("e1", "gpt-4", limits[0], now_ms)

        put_item = repo.build_composite_create("e1", "gpt-4", [state], now_ms)
        await repo.transact_write([put_item])

        # Patch client.update_item to raise a non-condition ClientError
        client = await repo._get_client()
        original = client.update_item

        async def failing_update(*args, **kwargs):
            raise ClientError(
                {"Error": {"Code": "InternalServerError", "Message": "boom"}},
                "UpdateItem",
            )

        client.update_item = failing_update
        try:
            with pytest.raises(ClientError) as exc_info:
                await repo.speculative_consume("e1", "gpt-4", {"rpm": 1})
            assert exc_info.value.response["Error"]["Code"] == "InternalServerError"
        finally:
            client.update_item = original


class TestCompositeNormalGuard:
    """Tests for tk >= floor guard in build_composite_normal."""

    async def test_normal_write_rejects_when_speculative_drained_tokens(self, repo):
        """build_composite_normal rejects when concurrent speculative drained tk.

        Sequence:
        1. Create bucket: tk=100_000 (100 rpm), rf=T1
        2. Read bucket (simulating slow path read): see tk=100_000, rf=T1
        3. Concurrent speculative_consume: ADD tk:-80_000 → tk=20_000, rf unchanged
        4. build_composite_normal with expected_rf=T1, consume=50_000, refill=0
           → rf lock passes (T1==T1), but tk guard catches it:
             tk(20_000) < floor(50_000) → ConditionalCheckFailedException
        """
        now_ms = int(time.time() * 1000)
        limit = Limit.per_minute("rpm", 100)
        state = BucketState.from_limit("e1", "gpt-4", limit, now_ms)

        # Step 1: Create bucket with 100 rpm tokens
        put_item = repo.build_composite_create("e1", "gpt-4", [state], now_ms)
        await repo.transact_write([put_item])

        # Step 2: Read bucket (slow path would do this)
        buckets = await repo.get_buckets("e1", resource="gpt-4")
        assert len(buckets) == 1
        original_rf = buckets[0].last_refill_ms

        # Step 3: Concurrent speculative drains 80 of 100 tokens
        spec_result = await repo.speculative_consume("e1", "gpt-4", {"rpm": 80})
        assert spec_result.success is True

        # Step 4: build_composite_normal with stale read, consume 50, refill 0
        # The rf lock passes (speculative didn't touch rf), but the tk guard
        # catches it: tk(20_000) < floor(50_000)
        normal_item = repo.build_composite_normal(
            entity_id="e1",
            resource="gpt-4",
            consumed={"rpm": 50_000},  # millitokens
            refill_amounts={"rpm": 0},
            now_ms=now_ms,
            expected_rf=original_rf,
        )
        with pytest.raises(ClientError) as exc_info:
            await repo.transact_write([normal_item])

        assert "ConditionalCheckFailedException" in str(exc_info.value)

    async def test_normal_write_allows_when_refill_covers_consumption(self, repo):
        """build_composite_normal allows when refill >= consumed (net positive).

        When refill >= consumed, the tk floor is 0 and the guard is a no-op.
        """
        now_ms = int(time.time() * 1000)
        limit = Limit.per_minute("rpm", 100)
        state = BucketState.from_limit("e1", "gpt-4", limit, now_ms)

        put_item = repo.build_composite_create("e1", "gpt-4", [state], now_ms)
        await repo.transact_write([put_item])

        buckets = await repo.get_buckets("e1", resource="gpt-4")
        original_rf = buckets[0].last_refill_ms

        # Speculative drains 80 of 100
        spec_result = await repo.speculative_consume("e1", "gpt-4", {"rpm": 80})
        assert spec_result.success is True

        # Normal write: consume 10_000 with refill 50_000 (net +40_000)
        # Floor = max(0, 10_000 - 50_000) = 0 → guard always passes
        normal_item = repo.build_composite_normal(
            entity_id="e1",
            resource="gpt-4",
            consumed={"rpm": 10_000},
            refill_amounts={"rpm": 50_000},
            now_ms=now_ms,
            expected_rf=original_rf,
        )
        # Should succeed — net positive change, no over-admission risk
        await repo.transact_write([normal_item])

        buckets_after = await repo.get_buckets("e1", resource="gpt-4")
        rpm_bucket = [b for b in buckets_after if b.limit_name == "rpm"][0]
        # tk was 20_000 after speculative, +50_000 refill -10_000 consume = 60_000
        assert rpm_bucket.tokens_milli == 60_000


class TestGSI4Attributes:
    """Test GSI4PK/GSI4SK on all creation paths."""

    @pytest.mark.asyncio
    async def test_create_entity_sets_gsi4(self, repo):
        """create_entity() sets GSI4PK/GSI4SK on entity metadata."""
        from zae_limiter import schema

        await repo.create_entity(entity_id="gsi4-entity", name="Test")

        client = await repo._get_client()
        response = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_entity("default", "gsi4-entity")},
                "SK": {"S": schema.sk_meta()},
            },
        )
        item = response["Item"]
        assert item["GSI4PK"]["S"] == "default"
        assert item["GSI4SK"]["S"] == schema.pk_entity("default", "gsi4-entity")

    @pytest.mark.asyncio
    async def test_build_composite_create_sets_gsi4(self, repo):
        """build_composite_create() sets GSI4PK/GSI4SK on bucket items."""
        from zae_limiter import schema
        from zae_limiter.models import Limit

        await repo.create_entity(entity_id="gsi4-bucket")
        limits = [Limit.per_minute("rpm", 100)]
        now_ms = int(time.time() * 1000)
        states = [BucketState.from_limit("gsi4-bucket", "api", lim, now_ms) for lim in limits]

        create_item = repo.build_composite_create(
            entity_id="gsi4-bucket",
            resource="api",
            states=states,
            now_ms=now_ms,
        )
        await repo.transact_write([create_item])

        client = await repo._get_client()
        response = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_entity("default", "gsi4-bucket")},
                "SK": {"S": schema.sk_bucket("api")},
            },
        )
        item = response["Item"]
        assert item["GSI4PK"]["S"] == "default"
        assert item["GSI4SK"]["S"] == schema.pk_entity("default", "gsi4-bucket")

    @pytest.mark.asyncio
    async def test_set_limits_sets_gsi4_on_config(self, repo):
        """set_limits() sets GSI4PK/GSI4SK on entity config item."""
        from zae_limiter import schema
        from zae_limiter.models import Limit

        await repo.create_entity(entity_id="gsi4-config")
        await repo.set_limits(
            entity_id="gsi4-config",
            limits=[Limit.per_minute("rpm", 100)],
            resource="api",
        )

        client = await repo._get_client()
        response = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_entity("default", "gsi4-config")},
                "SK": {"S": sk_config("api")},
            },
        )
        item = response["Item"]
        assert item["GSI4PK"]["S"] == "default"
        assert item["GSI4SK"]["S"] == schema.pk_entity("default", "gsi4-config")

    @pytest.mark.asyncio
    async def test_set_limits_sets_gsi4_on_entity_config_resources(self, repo):
        """set_limits() sets GSI4PK/GSI4SK on entity config resources registry."""
        from zae_limiter import schema

        await repo.create_entity(entity_id="gsi4-ecr")
        await repo.set_limits(
            entity_id="gsi4-ecr",
            limits=[Limit.per_minute("rpm", 100)],
            resource="api",
        )

        client = await repo._get_client()
        response = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_system("default")},
                "SK": {"S": schema.sk_entity_config_resources()},
            },
        )
        item = response["Item"]
        assert item["GSI4PK"]["S"] == "default"
        assert item["GSI4SK"]["S"] == schema.pk_system("default")

    @pytest.mark.asyncio
    async def test_set_resource_defaults_sets_gsi4_on_config(self, repo):
        """set_resource_defaults() sets GSI4PK/GSI4SK on resource config."""
        from zae_limiter import schema

        await repo.set_resource_defaults(
            resource="gpt-4",
            limits=[Limit.per_minute("rpm", 100)],
        )

        client = await repo._get_client()
        response = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_resource("default", "gpt-4")},
                "SK": {"S": sk_config()},
            },
        )
        item = response["Item"]
        assert item["GSI4PK"]["S"] == "default"
        assert item["GSI4SK"]["S"] == schema.pk_resource("default", "gpt-4")

    @pytest.mark.asyncio
    async def test_set_resource_defaults_sets_gsi4_on_resource_registry(self, repo):
        """set_resource_defaults() sets GSI4PK/GSI4SK on resource list."""
        from zae_limiter import schema

        await repo.set_resource_defaults(
            resource="gpt-4",
            limits=[Limit.per_minute("rpm", 100)],
        )

        client = await repo._get_client()
        response = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_system("default")},
                "SK": {"S": schema.sk_resources()},
            },
        )
        item = response["Item"]
        assert item["GSI4PK"]["S"] == "default"
        assert item["GSI4SK"]["S"] == schema.pk_system("default")

    @pytest.mark.asyncio
    async def test_set_system_defaults_sets_gsi4(self, repo):
        """set_system_defaults() sets GSI4PK/GSI4SK on system config."""
        from zae_limiter import schema

        await repo.set_system_defaults(
            limits=[Limit.per_minute("rpm", 100)],
        )

        client = await repo._get_client()
        response = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_system("default")},
                "SK": {"S": sk_config()},
            },
        )
        item = response["Item"]
        assert item["GSI4PK"]["S"] == "default"
        assert item["GSI4SK"]["S"] == schema.pk_system("default")

    @pytest.mark.asyncio
    async def test_set_version_record_sets_gsi4(self, repo):
        """set_version_record() sets GSI4PK/GSI4SK on version record using RESERVED_NAMESPACE."""
        from zae_limiter import schema

        await repo.set_version_record(schema_version="1.0.0")

        client = await repo._get_client()
        response = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_system(schema.RESERVED_NAMESPACE)},
                "SK": {"S": schema.sk_version()},
            },
        )
        item = response["Item"]
        assert item["GSI4PK"]["S"] == schema.RESERVED_NAMESPACE
        assert item["GSI4SK"]["S"] == schema.pk_system(schema.RESERVED_NAMESPACE)

    @pytest.mark.asyncio
    async def test_log_audit_event_sets_gsi4(self, repo):
        """_log_audit_event() sets GSI4PK/GSI4SK on audit records."""
        from zae_limiter import schema

        event = await repo._log_audit_event(
            action="test_action",
            entity_id="audit-entity",
            principal="test-user",
        )

        client = await repo._get_client()
        response = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_audit("default", "audit-entity")},
                "SK": {"S": schema.sk_audit(event.event_id)},
            },
        )
        item = response["Item"]
        assert item["GSI4PK"]["S"] == "default"
        assert item["GSI4SK"]["S"] == schema.pk_audit("default", "audit-entity")

    @pytest.mark.asyncio
    async def test_speculative_consume_does_not_set_gsi4(self, repo):
        """speculative_consume() does NOT set GSI4 (update path, not creation)."""
        from zae_limiter import schema
        from zae_limiter.models import Limit

        await repo.create_entity(entity_id="spec-gsi4")
        limits = [Limit.per_minute("rpm", 100)]
        now_ms = int(time.time() * 1000)
        states = [BucketState.from_limit("spec-gsi4", "api", lim, now_ms) for lim in limits]
        create_item = repo.build_composite_create(
            entity_id="spec-gsi4",
            resource="api",
            states=states,
            now_ms=now_ms,
        )
        await repo.transact_write([create_item])

        # Speculative consume (may fail in moto, but we only care about GSI4)
        await repo.speculative_consume(
            entity_id="spec-gsi4",
            resource="api",
            consume={"rpm": 1_000},
        )

        # Verify GSI4 was NOT changed by speculative (still from create)
        client = await repo._get_client()
        response = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_entity("default", "spec-gsi4")},
                "SK": {"S": schema.sk_bucket("api")},
            },
        )
        item = response["Item"]
        # GSI4 should exist from build_composite_create, not from speculative
        assert item["GSI4PK"]["S"] == "default"
        assert item["GSI4SK"]["S"] == schema.pk_entity("default", "spec-gsi4")

    @pytest.mark.asyncio
    async def test_adjust_does_not_set_gsi4(self, repo):
        """build_composite_adjust() does NOT set GSI4 (update path, not creation)."""
        from zae_limiter import schema
        from zae_limiter.models import Limit

        await repo.create_entity(entity_id="adj-gsi4")
        limits = [Limit.per_minute("rpm", 100)]
        now_ms = int(time.time() * 1000)
        states = [BucketState.from_limit("adj-gsi4", "api", lim, now_ms) for lim in limits]
        create_item = repo.build_composite_create(
            entity_id="adj-gsi4",
            resource="api",
            states=states,
            now_ms=now_ms,
        )
        await repo.transact_write([create_item])

        # Adjust
        adjust_item = repo.build_composite_adjust(
            entity_id="adj-gsi4",
            resource="api",
            deltas={"rpm": -500},
        )
        await repo.write_each([adjust_item])

        # Verify GSI4 still exists from create, was NOT modified
        client = await repo._get_client()
        response = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_entity("default", "adj-gsi4")},
                "SK": {"S": schema.sk_bucket("api")},
            },
        )
        item = response["Item"]
        assert item["GSI4PK"]["S"] == "default"
        assert item["GSI4SK"]["S"] == schema.pk_entity("default", "adj-gsi4")


class TestDeleteStack:
    """Tests for Repository.delete_stack()."""

    async def test_delete_stack_delegates_to_stack_manager(self, mock_dynamodb):
        """delete_stack() creates a StackManager and calls delete_stack."""
        from unittest.mock import AsyncMock, MagicMock, patch

        repo = Repository(name="test-stack", region="us-east-1", _skip_deprecation_warning=True)

        mock_manager = MagicMock()
        mock_manager.delete_stack = AsyncMock()
        mock_manager.__aenter__ = AsyncMock(return_value=mock_manager)
        mock_manager.__aexit__ = AsyncMock(return_value=False)

        with patch("zae_limiter.infra.stack_manager.StackManager", return_value=mock_manager):
            await repo.delete_stack()

        mock_manager.delete_stack.assert_called_once_with("test-stack")
        await repo.close()


class TestResolveOnUnavailable:
    """Tests for Repository.resolve_on_unavailable() edge cases."""

    async def test_cached_value_returned_when_system_config_has_no_on_unavailable(
        self, mock_dynamodb
    ):
        """When system config exists but on_unavailable is None, cached value is used."""
        from unittest.mock import AsyncMock, patch

        repo = Repository(name="test-cache", region="us-east-1", _skip_deprecation_warning=True)
        repo._on_unavailable_cache = "allow"

        # Mock config cache to return (None limits, None on_unavailable)
        with patch.object(
            repo._config_cache, "get_system_defaults", new_callable=AsyncMock
        ) as mock_get:
            mock_get.return_value = (None, None)
            result = await repo.resolve_on_unavailable()

        assert result == "allow"
        await repo.close()

    async def test_default_block_when_no_cache_and_no_system_config(self, mock_dynamodb):
        """When no cache and system config has no on_unavailable, default to 'block'."""
        from unittest.mock import AsyncMock, patch

        repo = Repository(name="test-default", region="us-east-1", _skip_deprecation_warning=True)
        assert repo._on_unavailable_cache is None

        with patch.object(
            repo._config_cache, "get_system_defaults", new_callable=AsyncMock
        ) as mock_get:
            mock_get.return_value = (None, None)
            result = await repo.resolve_on_unavailable()

        assert result == "block"
        await repo.close()

    async def test_cached_value_on_dynamodb_error(self, mock_dynamodb):
        """When DynamoDB is unreachable, cached on_unavailable is returned."""
        from unittest.mock import AsyncMock, patch

        repo = Repository(name="test-fallback", region="us-east-1", _skip_deprecation_warning=True)
        repo._on_unavailable_cache = "allow"

        with patch.object(
            repo._config_cache,
            "get_system_defaults",
            new_callable=AsyncMock,
            side_effect=Exception("DynamoDB unavailable"),
        ):
            result = await repo.resolve_on_unavailable()

        assert result == "allow"
        await repo.close()

    async def test_default_block_on_dynamodb_error_without_cache(self, mock_dynamodb):
        """When DynamoDB is unreachable and no cache, default to 'block'."""
        from unittest.mock import AsyncMock, patch

        repo = Repository(name="test-no-cache", region="us-east-1", _skip_deprecation_warning=True)
        assert repo._on_unavailable_cache is None

        with patch.object(
            repo._config_cache,
            "get_system_defaults",
            new_callable=AsyncMock,
            side_effect=Exception("DynamoDB unavailable"),
        ):
            result = await repo.resolve_on_unavailable()

        assert result == "block"
        await repo.close()
