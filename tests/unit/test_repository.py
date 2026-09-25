"""Unit tests for Repository."""

import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from botocore.exceptions import ClientError

from zae_limiter import AuditAction, Limit
from zae_limiter.exceptions import (
    EntityExistsError,
    InvalidIdentifierError,
    RateLimiterUnavailable,
)
from zae_limiter.models import BucketState
from zae_limiter.repository import Repository
from zae_limiter.repository_protocol import SpeculativeFailureReason
from zae_limiter.schedule import ScheduleEntry
from zae_limiter.schema import (
    BUCKET_FIELD_DISABLED,
    BUCKET_FIELD_RSA,
    BUCKET_FIELD_RSCHED,
    BUCKET_FIELD_SCHED,
    BUCKET_FIELD_SCHED_TZ,
    BUCKET_FIELD_TK,
    BUCKET_FIELD_VU,
    BUCKET_FIELD_WS,
    BUCKET_SCHED_NONE,
    CONFIG_FIELD_SCHED_TZ,
    LIMIT_FIELD_RSA,
    LIMIT_FIELD_RSCHED,
    WCU_LIMIT_NAME,
    bucket_attr,
    calculate_bucket_ttl,
    limit_attr,
    parse_bucket_attr,
    parse_bucket_sk,
    parse_limit_attr,
    pk_bucket,
    sk_config,
    sk_state,
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
        assert parse_bucket_attr("b_my_limit_ra") == ("my_limit", "ra")

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
        assert limit_attr("tpm", "ra") == "l_tpm_ra"
        assert limit_attr("my_limit", "ra") == "l_my_limit_ra"

    def test_parse_limit_attr_valid(self):
        """parse_limit_attr returns (limit_name, field) for valid attributes."""
        assert parse_limit_attr("l_rpm_cp") == ("rpm", "cp")
        assert parse_limit_attr("l_tpm_ra") == ("tpm", "ra")
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

        # Should only get gpt-4 buckets (2 user limits: rpm, tpm; wcu filtered)
        assert len(buckets) == 2
        assert all(b.resource == "gpt-4" for b in buckets)

        # Verify only user limits are present (wcu infra limit filtered out)
        limit_names = {b.limit_name for b in buckets}
        assert limit_names == {"rpm", "tpm"}

    @pytest.mark.asyncio
    async def test_get_buckets_returns_all_when_no_filter(self, repo_with_buckets):
        """get_buckets should return all buckets when no resource filter."""
        buckets = await repo_with_buckets.get_buckets("entity-1")

        # Should get all buckets: 2 resources × 2 user limits each (rpm, tpm) = 4 (wcu filtered)
        assert len(buckets) == 4

        # Verify resources and limits (wcu infra limit filtered out)
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

    @pytest.mark.asyncio
    async def test_batch_get_entity_and_buckets_returns_existing_buckets(self, repo_with_buckets):
        """The acquire slow-path read must return buckets that exist.

        Regression for the stale bucket discriminator: buckets use SK=#STATE
        since the per-shard migration (GHSA-76rv), but the response filter still
        checked the pre-shard SK_BUCKET prefix ("#BUCKET#"), silently dropping
        every bucket. That made the slow path treat existing buckets as new.
        """
        entity, buckets = await repo_with_buckets.batch_get_entity_and_buckets(
            "entity-1", [("entity-1", "gpt-4", 0)]
        )

        assert entity is not None  # entity-1 was created via create_entity
        # Bucket must be discovered (the bug returned an empty dict here)
        assert ("entity-1", "gpt-4", "rpm") in buckets
        assert ("entity-1", "gpt-4", "tpm") in buckets

    @pytest.mark.asyncio
    async def test_batch_get_buckets_reads_the_requested_shard(self, repo):
        """The slow-path read targets the shard the key names, not shard 0.

        Issue #439: with the shard hardcoded to 0, a speculative BUCKET_MISSING
        on shard 1 fell back to a read of shard 0, so the client could never
        create (or later find) a shard N>0 item.
        """
        limit = Limit.per_minute("rpm", 100)
        now_ms = int(time.time() * 1000)
        states = [BucketState.from_limit("sharded-1", "gpt-4", limit, now_ms)]
        await repo.transact_write(
            [
                repo.build_composite_create(
                    "sharded-1", "gpt-4", states, now_ms, shard_id=1, shard_count=2
                )
            ]
        )

        assert ("sharded-1", "gpt-4", "rpm") in await repo.batch_get_buckets(
            [("sharded-1", "gpt-4", 1)]
        )
        assert await repo.batch_get_buckets([("sharded-1", "gpt-4", 0)]) == {}

    @pytest.mark.asyncio
    async def test_batch_get_entity_and_buckets_reads_the_requested_shard(self, repo):
        """Same as above for the META + bucket read (issue #439)."""
        await repo.create_entity("sharded-2")
        limit = Limit.per_minute("rpm", 100)
        now_ms = int(time.time() * 1000)
        states = [BucketState.from_limit("sharded-2", "gpt-4", limit, now_ms)]
        await repo.transact_write(
            [
                repo.build_composite_create(
                    "sharded-2", "gpt-4", states, now_ms, shard_id=1, shard_count=2
                )
            ]
        )

        entity, buckets = await repo.batch_get_entity_and_buckets(
            "sharded-2", [("sharded-2", "gpt-4", 1)]
        )
        assert entity is not None
        assert ("sharded-2", "gpt-4", "rpm") in buckets

        _entity, buckets0 = await repo.batch_get_entity_and_buckets(
            "sharded-2", [("sharded-2", "gpt-4", 0)]
        )
        assert buckets0 == {}

    @pytest.mark.asyncio
    async def test_speculative_images_never_lower_a_warm_shard_count(self, repo):
        """A shard N>0 item can carry a stale, lower shard_count (propagation
        lag). Neither a failure image nor a success image may shrink the
        cached count learned from shard 0; the cache is monotonic (max)."""
        ns = repo._namespace_id
        limit = Limit.custom("rpm", 10, refill_amount=1, refill_period_seconds=3600)
        now_ms = int(time.time() * 1000)
        for shard_id, tokens in ((0, 0), (1, 10_000)):
            state = BucketState.from_limit("mono-1", "gpt-4", limit, now_ms, 2)
            state.tokens_milli = tokens
            await repo.transact_write(
                [
                    repo.build_composite_create(
                        "mono-1", "gpt-4", [state], now_ms, shard_id=shard_id, shard_count=2
                    )
                ]
            )
        repo._entity_cache[(ns, "mono-1")] = (False, None, {"gpt-4": 4})

        # Failure image (shard 0 drained) says shard_count=2
        failed = await repo._speculative_consume_single("mono-1", "gpt-4", {"rpm": 1}, shard_id=0)
        assert failed.success is False and failed.shard_count == 2
        assert repo._entity_cache[(ns, "mono-1")][2]["gpt-4"] == 4

        # Success image (shard 1) also says 2 — via the cached, non-cascade path
        with patch("zae_limiter.repository.random.randrange", return_value=1):
            ok = await repo.speculative_consume("mono-1", "gpt-4", {"rpm": 1})
        assert ok.success is True and ok.shard_count == 2
        assert repo._entity_cache[(ns, "mono-1")][2]["gpt-4"] == 4

    @pytest.mark.asyncio
    async def test_learn_shard_count_leaves_an_unknown_entity_uncached(self, repo):
        """Without cascade/parent_id to store, an unknown entity stays out of
        the cache; the observed count is still handed back to the caller."""
        ns = repo._namespace_id
        assert repo._learn_shard_count("nobody", "gpt-4", 3) == 3
        assert (ns, "nobody") not in repo._entity_cache
        assert repo._learn_shard_count("nobody", "gpt-4", 3, meta=(False, None)) == 3
        assert repo._entity_cache[(ns, "nobody")] == (False, None, {"gpt-4": 3})

    @pytest.mark.asyncio
    async def test_select_shard_uses_the_cached_shard_count(self, repo):
        """select_shard is the single place a shard is picked (issue #439)."""
        ns = repo._namespace_id
        repo._entity_cache[(ns, "sel-1")] = (False, None, {"gpt-4": 4})

        with patch("zae_limiter.repository.random.randrange", return_value=2) as randrange:
            assert repo.select_shard("sel-1", "gpt-4") == (2, 4)
            randrange.assert_called_once_with(4)

        # An explicit shard is honoured verbatim, with the cached count alongside
        assert repo.select_shard("sel-1", "gpt-4", shard_id=3) == (3, 4)
        # Unknown entity/resource: single shard, no random draw
        assert repo.select_shard("sel-1", "other") == (0, 1)
        assert repo.select_shard("nobody", "gpt-4") == (0, 1)

    @pytest.mark.asyncio
    async def test_batch_get_entity_and_buckets_finds_bucket_without_meta(self, repo):
        """Buckets must be returned even when the entity has no #META record.

        This is the real-world trigger: acquire() on an entity that was never
        registered via create_entity() creates a bucket but no META record.
        The slow-path read still must find the bucket (entity is None, but the
        bucket dict is populated).
        """
        limits = [Limit.per_minute("rpm", 100)]
        now_ms = int(time.time() * 1000)
        states = [BucketState.from_limit("bare-1", "gpt-4", limit, now_ms) for limit in limits]
        await repo.transact_write([repo.build_composite_create("bare-1", "gpt-4", states, now_ms)])

        entity, buckets = await repo.batch_get_entity_and_buckets(
            "bare-1", [("bare-1", "gpt-4", 0)]
        )

        assert entity is None  # never created via create_entity
        assert ("bare-1", "gpt-4", "rpm") in buckets  # bug returned {} here

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

        # Verify bucket was written (rpm only; wcu infra limit filtered)
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

        # Verify keys (bucket PK with shard, SK=#STATE)
        assert "PK" in put_spec["Item"]
        assert "SK" in put_spec["Item"]
        assert put_spec["Item"]["PK"]["S"] == "default/BUCKET#entity-1#gpt-4#0"
        assert put_spec["Item"]["SK"]["S"] == "#STATE"

        # Verify composite bucket attributes (b_{name}_{field} format)
        assert "data" not in put_spec["Item"]
        item = put_spec["Item"]

        assert item["b_rpm_tk"]["N"] == str(100_000)  # capacity
        assert item["b_rpm_cp"]["N"] == str(100_000)
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
        assert update["Key"]["PK"]["S"] == "default/BUCKET#entity-1#gpt-4#0"
        assert update["Key"]["SK"]["S"] == "#STATE"

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

    @pytest.mark.asyncio
    async def test_get_buckets_gsi3_returns_empty_for_entity_without_buckets(self, repo):
        """get_buckets with resource=None returns empty list via GSI3 when no buckets exist."""
        await repo.create_entity("entity-no-buckets")
        result = await repo.get_buckets("entity-no-buckets")
        assert result == []


class TestDurationWindowStamp:
    """Tests for stamping/reading the ADR-139 duration window on bucket items."""

    @pytest.mark.asyncio
    async def test_create_stamps_the_window(self, repo):
        """build_composite_create writes b_{name}_ws/rsa from the state, unsharded."""
        limit = Limit.quota("session", 10_000, reset_after=timedelta(hours=5))
        now = 1_757_000_000_000
        state = BucketState.from_limit("e1", "gpt-4", limit, now_ms=now, shard_count=1)
        item = repo.build_composite_create("e1", "gpt-4", [state], now_ms=now)["Put"]["Item"]

        assert item[bucket_attr("session", BUCKET_FIELD_WS)] == {"N": str(now)}
        assert item[bucket_attr("session", BUCKET_FIELD_RSA)] == {"N": "18000"}

    @pytest.mark.asyncio
    async def test_create_never_divides_rsa_by_shard_count(self, repo):
        """`rsa` is NEVER divided by shard_count -- only the balance is."""
        limit = Limit.quota("session", 10_000, reset_after=timedelta(hours=5))
        now = 1_757_000_000_000
        sharded = BucketState.from_limit("e1", "gpt-4", limit, now_ms=now, shard_count=4)
        item4 = repo.build_composite_create("e1", "gpt-4", [sharded], now_ms=now, shard_count=4)[
            "Put"
        ]["Item"]
        assert item4[bucket_attr("session", BUCKET_FIELD_RSA)] == {"N": "18000"}
        assert item4[bucket_attr("session", "tk")] == {"N": "2500000"}

    @pytest.mark.asyncio
    async def test_normal_write_sets_a_new_window_start(self, repo):
        """build_composite_normal SETs b_{name}_ws via an alias when window_starts is given."""
        upd = repo.build_composite_normal(
            "e1",
            "gpt-4",
            consumed={"session": 1_000},
            refill_amounts={"session": 0},
            now_ms=2_000,
            expected_rf=1_000,
            window_starts={"session": 2_000},
        )["Update"]

        expr = upd["UpdateExpression"]
        names = upd["ExpressionAttributeNames"]
        values = upd["ExpressionAttributeValues"]

        # The attribute name is never interpolated raw into the expression
        # (NAME_PATTERN allows `-` and `.`, illegal in a raw path) -- it must
        # go through an ExpressionAttributeNames alias, exactly like every
        # other attribute this builder writes.
        ws_aliases = [
            alias
            for alias, target in names.items()
            if target == bucket_attr("session", BUCKET_FIELD_WS)
        ]
        assert len(ws_aliases) == 1
        alias = ws_aliases[0]
        assert f"{alias} = " in expr
        placeholder = expr.split(f"{alias} = ")[1].split(",")[0].split(" ")[0]
        assert values[placeholder] == {"N": "2000"}

    @pytest.mark.asyncio
    async def test_normal_write_omits_ws_when_no_window_rolled(self, repo):
        """No `window_starts` means no `ws` attribute is touched at all."""
        upd = repo.build_composite_normal(
            "e1",
            "gpt-4",
            consumed={"session": 1_000},
            refill_amounts={"session": 0},
            now_ms=2_000,
            expected_rf=1_000,
        )["Update"]

        assert (
            bucket_attr("session", BUCKET_FIELD_WS) not in upd["ExpressionAttributeNames"].values()
        )

    @pytest.mark.asyncio
    async def test_deserialize_reads_the_window_back(self, repo):
        """_deserialize_composite_bucket reads ws/rsa back into BucketState."""
        limit = Limit.quota("session", 10_000, reset_after=timedelta(hours=5))
        now = 1_757_000_000_000
        state = BucketState.from_limit("e1", "gpt-4", limit, now_ms=now, shard_count=1)
        item = repo.build_composite_create("e1", "gpt-4", [state], now_ms=now)["Put"]["Item"]

        back = {s.limit_name: s for s in repo._deserialize_composite_bucket(item)}
        assert back["session"].window_start_ms == now
        assert back["session"].reset_after_seconds == 18_000
        assert back["session"].window_end_ms == now + 18_000_000
        # `wcu` never carries a window -- it is the per-partition write ceiling.
        assert back["wcu"].window_start_ms is None
        assert back["wcu"].reset_after_seconds is None

    @pytest.mark.asyncio
    async def test_deserialize_missing_window_attrs_reads_as_none(self, repo):
        """A pre-#622 bucket item (no ws/rsa attrs at all) still deserializes."""
        limit = Limit.per_minute("rpm", 100)
        now = 1_757_000_000_000
        state = BucketState.from_limit("e1", "gpt-4", limit, now_ms=now, shard_count=1)
        item = repo.build_composite_create("e1", "gpt-4", [state], now_ms=now)["Put"]["Item"]
        assert bucket_attr("rpm", BUCKET_FIELD_WS) not in item
        assert bucket_attr("rpm", BUCKET_FIELD_RSA) not in item

        back = {s.limit_name: s for s in repo._deserialize_composite_bucket(item)}
        assert back["rpm"].window_start_ms is None
        assert back["rpm"].reset_after_seconds is None

    @pytest.mark.asyncio
    async def test_deserialize_raises_unavailable_on_corrupt_ws(self, repo):
        """A non-integral b_{name}_ws surfaces as RateLimiterUnavailable, not a bare ValueError."""
        limit = Limit.quota("session", 10_000, reset_after=timedelta(hours=5))
        now = 1_757_000_000_000
        state = BucketState.from_limit("e1", "gpt-4", limit, now_ms=now, shard_count=1)
        item = repo.build_composite_create("e1", "gpt-4", [state], now_ms=now)["Put"]["Item"]
        item[bucket_attr("session", BUCKET_FIELD_WS)] = {"N": "18000.5"}

        with pytest.raises(RateLimiterUnavailable, match="18000.5"):
            repo._deserialize_composite_bucket(item)

    @pytest.mark.asyncio
    async def test_deserialize_raises_unavailable_on_corrupt_rsa(self, repo):
        """A non-integral b_{name}_rsa surfaces as RateLimiterUnavailable, not a bare ValueError."""
        limit = Limit.quota("session", 10_000, reset_after=timedelta(hours=5))
        now = 1_757_000_000_000
        state = BucketState.from_limit("e1", "gpt-4", limit, now_ms=now, shard_count=1)
        item = repo.build_composite_create("e1", "gpt-4", [state], now_ms=now)["Put"]["Item"]
        item[bucket_attr("session", BUCKET_FIELD_RSA)] = {"N": "not-a-number"}

        with pytest.raises(RateLimiterUnavailable, match="not-a-number"):
            repo._deserialize_composite_bucket(item)


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
        assert item["l_rpm_ra"]["N"] == "100"
        assert item["l_rpm_rp"]["N"] == "60"

        # Check tpm attributes
        assert item["l_tpm_cp"]["N"] == "10000"
        assert item["l_tpm_ra"]["N"] == "10000"
        assert item["l_tpm_rp"]["N"] == "60"

    @pytest.mark.asyncio
    async def test_deserialize_composite_limits(self, repo):
        """_deserialize_composite_limits reconstructs Limit objects from l_* attrs."""
        item = {
            "l_rpm_cp": {"N": "100"},
            "l_rpm_ra": {"N": "100"},
            "l_rpm_rp": {"N": "60"},
            "l_tpm_cp": {"N": "10000"},
            "l_tpm_ra": {"N": "10000"},
            "l_tpm_rp": {"N": "60"},
            "entity_id": {"S": "test"},  # non-limit attr should be ignored
        }
        limits = repo._deserialize_composite_limits(item)

        assert len(limits) == 2
        limit_map = {limit.name: limit for limit in limits}

        assert limit_map["rpm"].capacity == 100
        assert limit_map["rpm"].refill_amount == 100
        assert limit_map["rpm"].refill_period_seconds == 60

        assert limit_map["tpm"].capacity == 10000

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
        mock_session.create_client.return_value = mock_sts_client

        with patch.object(repo, "_session", mock_session):
            arn = await repo._get_caller_identity_arn()

        # Should return None on failure
        assert arn is None
        # Should be cached
        assert repo._caller_identity_fetched is True
        assert repo._caller_identity_arn is None

    @pytest.mark.asyncio
    async def test_get_caller_identity_creates_session_when_missing(self, repo):
        """When no session exists yet, the STS path creates one via get_session()."""
        from unittest.mock import AsyncMock, MagicMock, patch

        # Force the session-creation branch (no cached session/identity)
        repo._session = None
        repo._caller_identity_fetched = False
        repo._caller_identity_arn = None

        mock_sts_client = AsyncMock()
        mock_sts_client.get_caller_identity.return_value = {
            "Arn": "arn:aws:iam::123456789012:user/test"
        }
        mock_sts_client.__aenter__ = AsyncMock(return_value=mock_sts_client)
        mock_sts_client.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.create_client.return_value = mock_sts_client

        with patch(
            "zae_limiter.repository.get_session", return_value=mock_session
        ) as mock_get_session:
            arn = await repo._get_caller_identity_arn()

        # Session was created and the ARN resolved
        assert arn == "arn:aws:iam::123456789012:user/test"
        mock_get_session.assert_called_once()
        assert mock_session.create_client.call_args.args[0] == "sts"
        assert repo._caller_identity_fetched is True

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
        mock_client.get_item = AsyncMock(return_value={})
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
        mock_client.get_item = AsyncMock(return_value={})
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
        # delete_limits now probes the config item for `disabled` before the
        # transaction, to skip a fan-out that cannot change any resolution. A
        # bare AsyncMock returns a coroutine from .get(), so give this call a
        # real response; the empty dict means "no disabled value", which is
        # what this test's config item actually has.
        mock_client.get_item = AsyncMock(return_value={})
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
        # delete_limits now probes the config item for `disabled` before the
        # transaction, to skip a fan-out that cannot change any resolution. A
        # bare AsyncMock returns a coroutine from .get(), so give this call a
        # real response; the empty dict means "no disabled value", which is
        # what this test's config item actually has.
        mock_client.get_item = AsyncMock(return_value={})
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
        # Buckets include rpm + wcu infra limit
        assert len(result.buckets) == 2
        bucket_names = {b.limit_name for b in result.buckets}
        assert "rpm" in bucket_names

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
        assert result.failure_reason == SpeculativeFailureReason.BUCKET_MISSING

    @pytest.mark.asyncio
    async def test_speculative_failure_reason_app_limit_exhausted(self, repo):
        """Failure reason is APP_LIMIT_EXHAUSTED when user limit exhausted but wcu ok."""
        now_ms = int(time.time() * 1000)
        limits = [Limit.per_minute("rpm", 10)]
        state = BucketState.from_limit("e1", "gpt-4", limits[0], now_ms)

        put_item = repo.build_composite_create("e1", "gpt-4", [state], now_ms)
        await repo.transact_write([put_item])

        # Exhaust rpm tokens
        result = await repo.speculative_consume("e1", "gpt-4", {"rpm": 10})
        assert result.success is True

        # Next attempt should fail with APP_LIMIT_EXHAUSTED (wcu still has tokens)
        result = await repo.speculative_consume("e1", "gpt-4", {"rpm": 5})
        assert result.success is False
        assert result.failure_reason == SpeculativeFailureReason.APP_LIMIT_EXHAUSTED

    async def test_speculative_failure_reason_both_exhausted(self, repo):
        """Failure reason is BOTH_EXHAUSTED when both wcu and app limit exhausted."""
        now_ms = int(time.time() * 1000)
        # Use a very small capacity so both wcu and rpm exhaust quickly
        limits = [Limit.per_minute("rpm", 1)]
        state = BucketState.from_limit("e1", "gpt-4", limits[0], now_ms)

        put_item = repo.build_composite_create("e1", "gpt-4", [state], now_ms)
        await repo.transact_write([put_item])

        # First consume exhausts the 1 rpm token AND uses 1 wcu
        result = await repo.speculative_consume("e1", "gpt-4", {"rpm": 1})
        assert result.success is True

        # Spend the other 999 wcu in one ADD instead of 999 writes
        from zae_limiter import schema

        client = await repo._get_client()
        await client.update_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(repo._namespace_id, "e1", "gpt-4", 0)},
                "SK": {"S": schema.sk_state()},
            },
            UpdateExpression="ADD #wtk :neg",
            ExpressionAttributeNames={
                "#wtk": schema.bucket_attr(schema.WCU_LIMIT_NAME, schema.BUCKET_FIELD_TK)
            },
            ExpressionAttributeValues={":neg": {"N": str(-999 * 1000)}},
        )

        # Now both rpm (0 tokens) and wcu (0 tokens) should be exhausted
        result = await repo.speculative_consume("e1", "gpt-4", {"rpm": 1})
        assert result.success is False
        assert result.failure_reason == SpeculativeFailureReason.BOTH_EXHAUSTED

    async def test_speculative_with_ttl(self, repo):
        """Speculative consume handles TTL correctly."""
        now_ms = int(time.time() * 1000)
        limits = [Limit.per_minute("rpm", 1000)]
        state = BucketState.from_limit("e1", "gpt-4", limits[0], now_ms)

        # Create bucket
        put_item = repo.build_composite_create("e1", "gpt-4", [state], now_ms)
        await repo.transact_write([put_item])

        # Speculative consume with TTL (rpm + wcu infra limit)
        result = await repo.speculative_consume("e1", "gpt-4", {"rpm": 10}, ttl_seconds=3600)
        assert result.success is True
        assert len(result.buckets) == 2

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


class TestScheduleBoundaryClassification:
    """``vu`` gates the fast path; an expired one routes to the slow path (#222 §2.1).

    ``vu`` (valid-until, epoch ms) is the earliest instant at which any limit
    on the item changes effective params. Past it, ``tk`` was materialised
    under parameters that no longer apply, so the write must not be admitted
    and — crucially — must not be reported as a rejection either.
    """

    LIMIT = Limit.per_minute("rpm", 100)

    async def _make_bucket(self, repo, entity_id="vu-1", **kwargs):
        """Create a real composite bucket item (wcu included) for ``entity_id``."""
        now_ms = repo._now_ms()
        state = BucketState.from_limit(entity_id, "gpt-4", self.LIMIT, now_ms)
        put_item = repo.build_composite_create(entity_id, "gpt-4", [state], now_ms, **kwargs)
        await repo.transact_write([put_item])
        return entity_id

    @staticmethod
    async def _set_attrs(repo, entity_id, expression, names, values):
        client = await repo._get_client()
        await client.update_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": pk_bucket(repo._namespace_id, entity_id, "gpt-4", 0)},
                "SK": {"S": sk_state()},
            },
            UpdateExpression=expression,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

    async def _set_vu(self, repo, entity_id, vu_ms):
        await self._set_attrs(
            repo,
            entity_id,
            "SET #vu = :vu",
            {"#vu": BUCKET_FIELD_VU},
            {":vu": {"N": str(vu_ms)}},
        )

    async def test_expired_vu_classifies_as_schedule_boundary(self, repo):
        entity_id = await self._make_bucket(repo)
        await self._set_vu(repo, entity_id, repo._now_ms() - 1)

        result = await repo.speculative_consume(entity_id, "gpt-4", {"rpm": 1})
        assert result.success is False
        assert result.failure_reason is SpeculativeFailureReason.SCHEDULE_BOUNDARY

    async def test_vu_exactly_now_is_expired(self, repo):
        """``vu`` is the first instant at which the item is stale, not the last
        at which it is fresh: the condition is ``vu > now``, so ``vu == now``
        fails. The two sides must agree — the condition and the classifier both
        use ``>`` / ``<=`` against the same bound ``now_ms``, or a write could
        fail the condition and then be classified as something else."""
        entity_id = await self._make_bucket(repo, "vu-boundary")
        pinned = repo._now_ms()
        repo._now_ms = lambda: pinned
        await self._set_vu(repo, entity_id, pinned)

        result = await repo.speculative_consume(entity_id, "gpt-4", {"rpm": 1})
        assert result.success is False
        assert result.failure_reason is SpeculativeFailureReason.SCHEDULE_BOUNDARY

    async def test_schedule_boundary_wins_over_app_limit_exhausted(self, repo):
        """An empty bucket whose ``vu`` also expired must re-materialise, not
        reject — otherwise the caller sees RateLimitExceeded against a limit
        that may have just been raised."""
        entity_id = await self._make_bucket(repo, "vu-drained")
        assert (await repo.speculative_consume(entity_id, "gpt-4", {"rpm": 100})).success is True
        await self._set_vu(repo, entity_id, repo._now_ms() - 1)

        result = await repo.speculative_consume(entity_id, "gpt-4", {"rpm": 1})
        assert result.failure_reason is SpeculativeFailureReason.SCHEDULE_BOUNDARY

    async def test_schedule_boundary_wins_over_wcu_exhausted(self, repo):
        """Ordering matters against the *infrastructure* limit too: classifying
        a closed window as WCU_EXHAUSTED would make the limiter double
        ``shard_count`` at every boundary, permanently shrinking every shard's
        share, instead of re-materialising once."""
        entity_id = await self._make_bucket(repo, "vu-wcu")
        await self._set_attrs(
            repo,
            entity_id,
            "SET #wcu = :zero, #vu = :vu",
            {"#wcu": bucket_attr(WCU_LIMIT_NAME, BUCKET_FIELD_TK), "#vu": BUCKET_FIELD_VU},
            {":zero": {"N": "0"}, ":vu": {"N": str(repo._now_ms() - 1)}},
        )

        result = await repo.speculative_consume(entity_id, "gpt-4", {"rpm": 1})
        assert result.failure_reason is SpeculativeFailureReason.SCHEDULE_BOUNDARY

    async def test_disabled_still_wins_over_schedule_boundary(self, repo):
        """A disabled bucket stays disabled across a schedule boundary: no
        re-materialisation can admit it, and ADR-125 requires ResourceDisabled
        rather than a slow-path pass that would only rediscover the stamp."""
        entity_id = await self._make_bucket(repo, "vu-disabled")
        await self._set_attrs(
            repo,
            entity_id,
            "SET #disabled = :true, #vu = :vu",
            {"#disabled": BUCKET_FIELD_DISABLED, "#vu": BUCKET_FIELD_VU},
            {":true": {"BOOL": True}, ":vu": {"N": str(repo._now_ms() - 1)}},
        )

        result = await repo.speculative_consume(entity_id, "gpt-4", {"rpm": 1})
        assert result.failure_reason is SpeculativeFailureReason.DISABLED

    async def test_future_vu_does_not_affect_the_fast_path(self, repo):
        entity_id = await self._make_bucket(repo, "vu-future")
        await self._set_vu(repo, entity_id, repo._now_ms() + 3_600_000)

        result = await repo.speculative_consume(entity_id, "gpt-4", {"rpm": 1})
        assert result.success is True

    async def test_absent_vu_does_not_affect_the_fast_path(self, repo):
        """Unscheduled buckets carry no ``vu`` at all — every bucket written
        before this feature, and every bucket written by a limit that has no
        schedule. ``attribute_not_exists`` must let them straight through."""
        entity_id = await self._make_bucket(repo, "vu-absent")
        result = await repo.speculative_consume(entity_id, "gpt-4", {"rpm": 1})
        assert result.success is True
        assert result.failure_reason is None

    async def test_absent_vu_still_classifies_exhaustion_normally(self, repo):
        """The unscheduled rejection path is unchanged: no ``vu``, no
        SCHEDULE_BOUNDARY. Guards against a classifier that treats a missing
        attribute as ``vu = 0``."""
        entity_id = await self._make_bucket(repo, "vu-absent-drained")
        assert (await repo.speculative_consume(entity_id, "gpt-4", {"rpm": 100})).success is True

        result = await repo.speculative_consume(entity_id, "gpt-4", {"rpm": 1})
        assert result.failure_reason is SpeculativeFailureReason.APP_LIMIT_EXHAUSTED


class TestScheduleBoundaryFastPathCost:
    """The ``vu`` guard must cost nothing on the fast path (#222 §2.1).

    The load-bearing claim of the whole design is that the fast path gains
    exactly one comparison: no config read, no schedule evaluation, no extra
    round trip. Measured here rather than asserted in prose.
    """

    LIMIT = Limit.per_minute("rpm", 100)

    @staticmethod
    async def _count_calls(repo, coro_factory):
        """Run ``coro_factory()`` with every DynamoDB verb counted."""
        client = await repo._get_client()
        counts: dict[str, int] = {}
        captured: list[dict] = []
        originals = {}

        def wrap(verb):
            original = getattr(client, verb)
            originals[verb] = original

            async def counting(*args, **kwargs):
                counts[verb] = counts.get(verb, 0) + 1
                if verb == "update_item":
                    captured.append(kwargs)
                return await original(*args, **kwargs)

            setattr(client, verb, counting)

        for verb in ("get_item", "batch_get_item", "query", "scan", "update_item", "put_item"):
            wrap(verb)
        try:
            await coro_factory()
        finally:
            for verb, original in originals.items():
                setattr(client, verb, original)
        return counts, captured

    async def test_fast_path_reads_nothing_and_writes_once(self, repo):
        """One UpdateItem, no reads — with and without ``vu`` on the item."""
        now_ms = repo._now_ms()
        state = BucketState.from_limit("cost-1", "gpt-4", self.LIMIT, now_ms)
        await repo.transact_write([repo.build_composite_create("cost-1", "gpt-4", [state], now_ms)])

        counts, captured = await self._count_calls(
            repo, lambda: repo.speculative_consume("cost-1", "gpt-4", {"rpm": 1})
        )
        assert counts == {"update_item": 1}, f"fast path issued {counts}"

        # The condition gains exactly one clause naming `vu`, and nothing else
        # changed: the SET/ADD expression is untouched.
        condition = captured[0]["ConditionExpression"]
        assert condition.count("#vu") == 2, condition  # attribute_not_exists(#vu) OR #vu > :vu_now
        assert condition.count(":vu_now") == 1, condition
        assert "#vu" not in captured[0]["UpdateExpression"]

    async def test_vu_clause_costs_no_extra_call_when_present(self, repo):
        now_ms = repo._now_ms()
        state = BucketState.from_limit("cost-2", "gpt-4", self.LIMIT, now_ms)
        await repo.transact_write([repo.build_composite_create("cost-2", "gpt-4", [state], now_ms)])
        client = await repo._get_client()
        await client.update_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": pk_bucket(repo._namespace_id, "cost-2", "gpt-4", 0)},
                "SK": {"S": sk_state()},
            },
            UpdateExpression="SET #vu = :vu",
            ExpressionAttributeNames={"#vu": BUCKET_FIELD_VU},
            ExpressionAttributeValues={":vu": {"N": str(now_ms + 3_600_000)}},
        )

        counts, _ = await self._count_calls(
            repo, lambda: repo.speculative_consume("cost-2", "gpt-4", {"rpm": 1})
        )
        assert counts == {"update_item": 1}, f"scheduled fast path issued {counts}"


class TestScheduleBoundaryUsesOneClockReading:
    """The ``vu`` comparison must reuse the bound ``now_ms`` (#430)."""

    async def test_adding_vu_does_not_add_a_clock_reading(self, repo):
        now_ms = int(time.time() * 1000)
        state = BucketState.from_limit("vu-clock", "gpt-4", Limit.per_minute("rpm", 100), now_ms)
        await repo.transact_write(
            [repo.build_composite_create("vu-clock", "gpt-4", [state], now_ms)]
        )

        readings: list[int] = []
        base = repo._now_ms()

        def fake_now_ms() -> int:
            readings.append(base + 60_000 * len(readings))
            return readings[-1]

        repo._now_ms = fake_now_ms
        result = await repo.speculative_consume("vu-clock", "gpt-4", {"rpm": 1})

        assert result.success is True
        assert len(readings) == 1, (
            f"speculative_consume read the clock {len(readings)} times: {readings}"
        )


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
        # rpm only (wcu infra limit filtered from get_buckets)
        assert len(buckets) == 1
        rpm_bucket = next(b for b in buckets if b.limit_name == "rpm")
        original_rf = rpm_bucket.last_refill_ms

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
        rpm_bucket = next(b for b in buckets if b.limit_name == "rpm")
        original_rf = rpm_bucket.last_refill_ms

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
                "PK": {"S": schema.pk_bucket("default", "gsi4-bucket", "api", 0)},
                "SK": {"S": schema.sk_state()},
            },
        )
        item = response["Item"]
        assert item["GSI4PK"]["S"] == "default"
        assert item["GSI4SK"]["S"] == "BUCKET#gsi4-bucket#api#0"

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
                "PK": {"S": schema.pk_bucket("default", "spec-gsi4", "api", 0)},
                "SK": {"S": schema.sk_state()},
            },
        )
        item = response["Item"]
        # GSI4 should exist from build_composite_create, not from speculative
        assert item["GSI4PK"]["S"] == "default"
        assert item["GSI4SK"]["S"] == "BUCKET#spec-gsi4#api#0"

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
                "PK": {"S": schema.pk_bucket("default", "adj-gsi4", "api", 0)},
                "SK": {"S": schema.sk_state()},
            },
        )
        item = response["Item"]
        assert item["GSI4PK"]["S"] == "default"
        assert item["GSI4SK"]["S"] == "BUCKET#adj-gsi4#api#0"


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


# =============================================================================
# Provisioner state (Issue #405)
# =============================================================================


class TestProvisionerState:
    """Tests for provisioner state CRUD (declarative limits management)."""

    async def test_get_provisioner_state_empty(self, repo):
        """get_provisioner_state returns empty state when no record exists."""
        state = await repo.get_provisioner_state()
        assert state["managed_system"] is False
        assert state["managed_resources"] == []
        assert state["managed_entities"] == {}
        assert state["last_applied"] is None
        assert state["applied_hash"] is None

    async def test_put_get_provisioner_state_roundtrip(self, repo):
        """put_provisioner_state and get_provisioner_state round-trip correctly."""
        state = {
            "managed_system": True,
            "managed_resources": ["gpt-4", "claude-3"],
            "managed_entities": {"user-123": ["gpt-4"], "org-456": ["_default_"]},
            "last_applied": "2026-02-19T12:00:00Z",
            "applied_hash": "sha256:abc123",
        }
        await repo.put_provisioner_state(state)

        retrieved = await repo.get_provisioner_state()
        assert retrieved["managed_system"] is True
        assert sorted(retrieved["managed_resources"]) == ["claude-3", "gpt-4"]
        assert retrieved["managed_entities"] == {
            "user-123": ["gpt-4"],
            "org-456": ["_default_"],
        }
        assert retrieved["last_applied"] == "2026-02-19T12:00:00Z"
        assert retrieved["applied_hash"] == "sha256:abc123"

    async def test_put_provisioner_state_overwrites(self, repo):
        """put_provisioner_state replaces previous state entirely."""
        state1 = {
            "managed_system": True,
            "managed_resources": ["gpt-4"],
            "managed_entities": {},
            "last_applied": "2026-02-19T12:00:00Z",
            "applied_hash": "sha256:aaa",
        }
        await repo.put_provisioner_state(state1)

        state2 = {
            "managed_system": False,
            "managed_resources": ["claude-3"],
            "managed_entities": {"user-1": ["claude-3"]},
            "last_applied": "2026-02-19T13:00:00Z",
            "applied_hash": "sha256:bbb",
        }
        await repo.put_provisioner_state(state2)

        retrieved = await repo.get_provisioner_state()
        assert retrieved["managed_system"] is False
        assert retrieved["managed_resources"] == ["claude-3"]
        assert retrieved["managed_entities"] == {"user-1": ["claude-3"]}


# =============================================================================
# Pre-Shard Buckets (GHSA-76rv)
# =============================================================================


class TestPreShardBuckets:
    """Tests for pre-shard bucket PK scheme."""

    @pytest.mark.asyncio
    async def test_build_composite_create_new_pk(self, repo):
        """Bucket items use new PK scheme with wcu limit auto-injected."""
        from zae_limiter import schema

        now_ms = 1700000000000
        limits = [Limit.per_minute("rpm", 100)]
        states = [BucketState.from_limit("user-1", "gpt-4", lim, now_ms) for lim in limits]

        item = repo.build_composite_create(
            entity_id="user-1",
            resource="gpt-4",
            states=states,
            now_ms=now_ms,
            shard_id=0,
            shard_count=1,
        )

        put_item = item["Put"]["Item"]
        # New PK scheme
        assert put_item["PK"]["S"] == schema.pk_bucket(repo._namespace_id, "user-1", "gpt-4", 0)
        assert put_item["SK"]["S"] == schema.sk_state()

        # GSI3 projection for bucket discovery
        assert put_item["GSI3PK"]["S"] == schema.gsi3_pk_entity(repo._namespace_id, "user-1")
        assert put_item["GSI3SK"]["S"] == schema.gsi3_sk_bucket("gpt-4", 0)

        # wcu limit auto-injected
        assert schema.bucket_attr("wcu", "tk") in put_item
        assert schema.bucket_attr("wcu", "cp") in put_item

        # shard_count stored
        assert put_item["shard_count"]["N"] == "1"

    @pytest.mark.asyncio
    async def test_build_composite_create_multi_shard(self, repo):
        """Bucket with shard_id > 0 uses correct PK."""
        from zae_limiter import schema

        now_ms = 1700000000000
        limits = [Limit.per_minute("rpm", 100)]
        states = [BucketState.from_limit("user-1", "gpt-4", lim, now_ms) for lim in limits]

        item = repo.build_composite_create(
            entity_id="user-1",
            resource="gpt-4",
            states=states,
            now_ms=now_ms,
            shard_id=3,
            shard_count=4,
        )

        put_item = item["Put"]["Item"]
        assert put_item["PK"]["S"] == schema.pk_bucket(repo._namespace_id, "user-1", "gpt-4", 3)
        assert put_item["shard_count"]["N"] == "4"
        assert put_item["GSI3SK"]["S"] == schema.gsi3_sk_bucket("gpt-4", 3)

    @pytest.mark.asyncio
    async def test_speculative_consume_includes_wcu_consumption(self, repo):
        """Speculative consume adds wcu consumption (1 WCU = 1000 milli per write)."""
        from zae_limiter import schema

        now_ms = int(time.time() * 1000)
        limits = [Limit.per_minute("rpm", 1000)]
        states = [BucketState.from_limit("e1", "gpt-4", lim, now_ms) for lim in limits]

        put_item = repo.build_composite_create("e1", "gpt-4", states, now_ms)
        await repo.transact_write([put_item])

        result = await repo.speculative_consume("e1", "gpt-4", {"rpm": 10})
        assert result.success is True

        # Verify wcu tokens were consumed: initial = 1_000_000, consumed 1 WCU = 1000 milli
        wcu_bucket = next(b for b in result.buckets if b.limit_name == "wcu")
        assert wcu_bucket.tokens_milli == schema.WCU_LIMIT_CAPACITY * 1000 - 1000

    @pytest.mark.asyncio
    async def test_speculative_consume_returns_shard_count(self, repo):
        """Speculative consume returns shard_count from SpeculativeResult."""
        now_ms = int(time.time() * 1000)
        limits = [Limit.per_minute("rpm", 1000)]
        states = [BucketState.from_limit("e1", "gpt-4", lim, now_ms) for lim in limits]

        put_item = repo.build_composite_create("e1", "gpt-4", states, now_ms)
        await repo.transact_write([put_item])

        result = await repo.speculative_consume("e1", "gpt-4", {"rpm": 1})
        assert result.success is True
        assert result.shard_id == 0
        assert result.shard_count == 1

    @pytest.mark.asyncio
    async def test_entity_cache_stores_shard_count(self, repo):
        """Entity cache includes shard_count per resource after speculative consume."""
        now_ms = int(time.time() * 1000)
        limits = [Limit.per_minute("rpm", 1000)]
        states = [BucketState.from_limit("e1", "gpt-4", lim, now_ms) for lim in limits]

        put_item = repo.build_composite_create("e1", "gpt-4", states, now_ms)
        await repo.transact_write([put_item])

        result = await repo.speculative_consume("e1", "gpt-4", {"rpm": 1})
        assert result.success is True

        cache_entry = repo._entity_cache[(repo._namespace_id, "e1")]
        # cache_entry: (cascade, parent_id, {resource: shard_count})
        assert len(cache_entry) == 3
        assert cache_entry[2]["gpt-4"] == 1

    @pytest.mark.asyncio
    async def test_entity_cache_merges_shard_counts_across_resources(self, repo):
        """Entity cache merges shard_count from different resources."""
        now_ms = int(time.time() * 1000)

        # Create buckets for two resources
        for resource in ["gpt-4", "claude-3"]:
            limits = [Limit.per_minute("rpm", 1000)]
            states = [BucketState.from_limit("e1", resource, lim, now_ms) for lim in limits]
            put_item = repo.build_composite_create("e1", resource, states, now_ms)
            await repo.transact_write([put_item])

        # Consume from first resource
        result1 = await repo.speculative_consume("e1", "gpt-4", {"rpm": 1})
        assert result1.success is True

        # Consume from second resource
        result2 = await repo.speculative_consume("e1", "claude-3", {"rpm": 1})
        assert result2.success is True

        cache_entry = repo._entity_cache[(repo._namespace_id, "e1")]
        assert cache_entry[2]["gpt-4"] == 1
        assert cache_entry[2]["claude-3"] == 1

    @pytest.mark.asyncio
    async def test_speculative_consume_fails_when_wcu_exhausted(self, repo):
        """Speculative consume fails when wcu tokens are exhausted."""
        from zae_limiter import schema

        now_ms = int(time.time() * 1000)
        limits = [Limit.per_minute("rpm", 100_000)]
        states = [BucketState.from_limit("e1", "gpt-4", lim, now_ms) for lim in limits]

        put_item = repo.build_composite_create("e1", "gpt-4", states, now_ms)
        await repo.transact_write([put_item])

        # Exhaust wcu tokens (1000 capacity, 1 per write). One real write, then
        # the remaining 999 writes' wcu debit applied in a single ADD.
        result = await repo.speculative_consume("e1", "gpt-4", {"rpm": 1})
        assert result.success is True
        client = await repo._get_client()
        await client.update_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(repo._namespace_id, "e1", "gpt-4", 0)},
                "SK": {"S": schema.sk_state()},
            },
            UpdateExpression="ADD #wtk :neg",
            ExpressionAttributeNames={
                "#wtk": schema.bucket_attr(schema.WCU_LIMIT_NAME, schema.BUCKET_FIELD_TK)
            },
            ExpressionAttributeValues={":neg": {"N": str(-(schema.WCU_LIMIT_CAPACITY - 1) * 1000)}},
        )

        # Next write should fail due to wcu exhaustion
        result = await repo.speculative_consume("e1", "gpt-4", {"rpm": 1})
        assert result.success is False

    @pytest.mark.asyncio
    async def test_speculative_consume_routes_to_random_shard(self, repo):
        """With cached shard_count > 1, speculative_consume routes to random shard."""
        now_ms = int(time.time() * 1000)
        limits = [Limit.per_minute("rpm", 1000)]

        # Create buckets at shard 0 and shard 1
        for shard_id in range(2):
            states = [BucketState.from_limit("e1", "gpt-4", lim, now_ms) for lim in limits]
            put_item = repo.build_composite_create(
                "e1", "gpt-4", states, now_ms, shard_id=shard_id, shard_count=2
            )
            await repo.transact_write([put_item])

        # Pre-populate entity cache with shard_count=2
        repo._entity_cache[(repo._namespace_id, "e1")] = (False, None, {"gpt-4": 2})

        shard_ids_hit = set()
        for _ in range(30):
            result = await repo.speculative_consume("e1", "gpt-4", {"rpm": 1})
            assert result.success is True
            shard_ids_hit.add(result.shard_id)

        assert len(shard_ids_hit) == 2  # both shards hit


class TestBumpShardCount:
    """Tests for bump_shard_count conditional write behavior."""

    @pytest.mark.asyncio
    async def test_bump_shard_count_doubles_on_success(self, repo):
        """bump_shard_count doubles shard_count and updates entity cache."""
        now_ms = int(time.time() * 1000)
        limits = [Limit.per_minute("rpm", 100_000)]
        states = [BucketState.from_limit("e1", "gpt-4", lim, now_ms) for lim in limits]
        put_item = repo.build_composite_create(
            "e1", "gpt-4", states, now_ms, shard_id=0, shard_count=1
        )
        await repo.transact_write([put_item])

        result = await repo.bump_shard_count("e1", "gpt-4", current_count=1)
        assert result == 2

        cache_key = (repo._namespace_id, "e1")
        assert repo._entity_cache[cache_key][2]["gpt-4"] == 2

    @pytest.mark.asyncio
    async def test_bump_propagates_the_new_count_to_existing_shards(self, repo):
        """A won bump stamps the new count on the shards that already exist.

        Every shard refills toward ``capacity_milli // shard_count``, so a
        shard left on a stale lower count would refill to a larger share and
        the shares would sum to more than the limit. The aggregator's Path 1
        does this from the stream; with --no-aggregator nothing else would
        (issue #439).
        """
        now_ms = int(time.time() * 1000)
        limits = [Limit.per_minute("rpm", 100_000)]
        # Shards 0 and 1 exist at shard_count=2; shard 1 is the stale one.
        for shard in (0, 1):
            states = [BucketState.from_limit("e1", "gpt-4", lim, now_ms) for lim in limits]
            await repo.transact_write(
                [
                    repo.build_composite_create(
                        "e1", "gpt-4", states, now_ms, shard_id=shard, shard_count=2
                    )
                ]
            )

        assert await repo.bump_shard_count("e1", "gpt-4", current_count=2) == 4

        for shard in (0, 1):
            bucket = await repo.get_bucket("e1", "gpt-4", "rpm", shard_id=shard)
            assert bucket is not None
            assert bucket.shard_count == 4, f"shard {shard} kept a stale count"

    @pytest.mark.asyncio
    async def test_bump_propagation_is_monotonic(self, repo):
        """Propagation never lowers a shard already at a higher count, so
        racing the aggregator (or another client) is a no-op, not a conflict."""
        now_ms = int(time.time() * 1000)
        limits = [Limit.per_minute("rpm", 100_000)]
        for shard, count in ((0, 2), (1, 8)):
            states = [BucketState.from_limit("e1", "gpt-4", lim, now_ms) for lim in limits]
            await repo.transact_write(
                [
                    repo.build_composite_create(
                        "e1", "gpt-4", states, now_ms, shard_id=shard, shard_count=count
                    )
                ]
            )

        await repo.bump_shard_count("e1", "gpt-4", current_count=2)

        bucket = await repo.get_bucket("e1", "gpt-4", "rpm", shard_id=1)
        assert bucket is not None
        assert bucket.shard_count == 8, "propagation lowered a shard"

    @pytest.mark.asyncio
    async def test_propagation_reraises_unexpected_client_errors(self, repo):
        """Only ConditionalCheckFailedException means "already caught up"; any
        other error must surface rather than be silently counted as skipped."""
        with patch.object(repo, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.update_item.side_effect = ClientError(
                {"Error": {"Code": "ProvisionedThroughputExceededException"}},
                "UpdateItem",
            )
            mock_get_client.return_value = mock_client

            with pytest.raises(ClientError) as exc_info:
                await repo._propagate_shard_count("e1", "gpt-4", old_count=2, new_count=4)
        assert exc_info.value.response["Error"]["Code"] == "ProvisionedThroughputExceededException"

    @pytest.mark.asyncio
    async def test_bump_from_one_propagates_nothing(self, repo):
        """At shard_count=1 there are no sibling shards to stamp."""
        now_ms = int(time.time() * 1000)
        limits = [Limit.per_minute("rpm", 100_000)]
        states = [BucketState.from_limit("e1", "gpt-4", lim, now_ms) for lim in limits]
        await repo.transact_write(
            [repo.build_composite_create("e1", "gpt-4", states, now_ms, shard_id=0, shard_count=1)]
        )

        assert await repo._propagate_shard_count("e1", "gpt-4", old_count=1, new_count=2) == 0

    @pytest.mark.asyncio
    async def test_bump_shard_count_returns_current_on_race(self, repo):
        """When another client already doubled, the loser learns the winner's
        shard_count from the failed conditional write's ALL_OLD image, caches
        it, and returns it — not its own stale current_count (issue #439)."""
        ns = repo._namespace_id
        repo._entity_cache[(ns, "e1")] = (False, None, {"gpt-4": 1})
        now_ms = int(time.time() * 1000)
        limits = [Limit.per_minute("rpm", 100_000)]
        states = [BucketState.from_limit("e1", "gpt-4", lim, now_ms) for lim in limits]
        # Create bucket with shard_count=2 (already doubled)
        put_item = repo.build_composite_create(
            "e1", "gpt-4", states, now_ms, shard_id=0, shard_count=2
        )
        await repo.transact_write([put_item])

        # Try to bump from 1 to 2, but actual shard_count is already 2
        result = await repo.bump_shard_count("e1", "gpt-4", current_count=1)
        assert result == 2  # the winner's count, read from ALL_OLD
        assert repo._entity_cache[(ns, "e1")][2]["gpt-4"] == 2

    @pytest.mark.asyncio
    async def test_bump_shard_count_never_lowers_a_warm_cache(self, repo):
        """Shard 0 can lag its siblings (TTL-recreated at shard_count=1). A
        losing bump must not adopt that lower count: the cache keeps the
        higher count it already learned so draws still cover every shard."""
        ns = repo._namespace_id
        repo._entity_cache[(ns, "e1")] = (False, None, {"gpt-4": 4})
        now_ms = int(time.time() * 1000)
        states = [BucketState.from_limit("e1", "gpt-4", Limit.per_minute("rpm", 100), now_ms)]
        await repo.transact_write(
            [repo.build_composite_create("e1", "gpt-4", states, now_ms, shard_id=0, shard_count=1)]
        )

        # Our view is 4; shard 0 says 1 -> condition fails, image says 1
        assert await repo.bump_shard_count("e1", "gpt-4", current_count=4) == 4
        assert repo._entity_cache[(ns, "e1")][2]["gpt-4"] == 4
        assert repo.select_shard("e1", "gpt-4")[1] == 4

    @pytest.mark.asyncio
    async def test_bump_shard_count_refuses_to_exceed_the_cap(self, repo, caplog):
        """shard_count is capped at MAX_SHARD_COUNT: the bump is refused (no
        write), the current count is returned, and it warns once."""
        import logging

        from zae_limiter.schema import MAX_SHARD_COUNT

        ns = repo._namespace_id
        repo._entity_cache[(ns, "e1")] = (False, None, {"gpt-4": MAX_SHARD_COUNT})
        now_ms = int(time.time() * 1000)
        states = [BucketState.from_limit("e1", "gpt-4", Limit.per_minute("rpm", 100), now_ms)]
        await repo.transact_write(
            [
                repo.build_composite_create(
                    "e1", "gpt-4", states, now_ms, shard_id=0, shard_count=MAX_SHARD_COUNT
                )
            ]
        )

        with caplog.at_level(logging.WARNING, logger="zae_limiter.repository"):
            assert await repo.bump_shard_count("e1", "gpt-4", MAX_SHARD_COUNT) == MAX_SHARD_COUNT
            assert await repo.bump_shard_count("e1", "gpt-4", MAX_SHARD_COUNT) == MAX_SHARD_COUNT
        assert sum("MAX_SHARD_COUNT" in r.getMessage() for r in caplog.records) == 1
        # Entity ids are routinely API keys: deduplicated per entity, never
        # logged in clear text (py/clear-text-logging-sensitive-data).
        assert not any("e1" in r.getMessage() for r in caplog.records)
        assert repo._entity_cache[(ns, "e1")][2]["gpt-4"] == MAX_SHARD_COUNT
        bucket = await repo.get_bucket("e1", "gpt-4", "rpm")
        assert bucket is not None and bucket.shard_count == MAX_SHARD_COUNT

    @pytest.mark.asyncio
    async def test_bump_shard_count_reraises_other_errors(self, repo):
        """bump_shard_count re-raises non-ConditionalCheckFailedException errors."""
        with patch.object(repo, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.update_item.side_effect = ClientError(
                {"Error": {"Code": "ProvisionedThroughputExceededException"}},
                "UpdateItem",
            )
            mock_get_client.return_value = mock_client

            with pytest.raises(ClientError) as exc_info:
                await repo.bump_shard_count("e1", "gpt-4", current_count=1)
            assert (
                exc_info.value.response["Error"]["Code"] == "ProvisionedThroughputExceededException"
            )


class TestSyncBucketParamsFansOutToAllShards:
    """`_sync_bucket_params` must reach every shard, not only shard 0 (#468).

    Keying the update on `pk_bucket(..., 0)` left shards 1..N-1 enforcing the
    limits they were born with forever: nothing compares a bucket's stored
    `cp`/`ra` against config on either path. Named residual of
    GHSA-w6c2-33wf-qfwf.
    """

    OLD = 1000
    NEW = 100

    @staticmethod
    async def _seed_shards(repo, entity_id, resource, shard_count):
        """Create one bucket item per shard on the old (undivided) limits."""
        now_ms = int(time.time() * 1000)
        old = Limit.custom("rpm", 1000, refill_amount=1000, refill_period_seconds=60)
        for shard_id in range(shard_count):
            states = [BucketState.from_limit(entity_id, resource, old, now_ms)]
            await repo.transact_write(
                [
                    repo.build_composite_create(
                        entity_id,
                        resource,
                        states,
                        now_ms,
                        shard_id=shard_id,
                        shard_count=shard_count,
                    )
                ]
            )

    @staticmethod
    async def _params(repo, entity_id, resource, shard_id):
        bucket = await repo.get_bucket(entity_id, resource, "rpm", shard_id=shard_id)
        assert bucket is not None, f"shard {shard_id} missing"
        return (bucket.capacity_milli, bucket.refill_amount_milli, bucket.refill_period_ms)

    @pytest.mark.asyncio
    async def test_set_limits_updates_every_shard(self, repo):
        """Entity-level set_limits rewrites cp/ra/rp on all 4 shards."""
        await repo.create_entity("user-1")
        await self._seed_shards(repo, "user-1", "gpt-4", 4)

        await repo.set_limits(
            "user-1",
            [Limit.custom("rpm", self.NEW, refill_amount=self.NEW, refill_period_seconds=30)],
            resource="gpt-4",
        )

        for shard_id in range(4):
            # Stored values stay UNDIVIDED on every shard: the per-shard share
            # is derived at read time by BucketState.effective_*.
            assert await self._params(repo, "user-1", "gpt-4", shard_id) == (
                self.NEW * 1000,
                self.NEW * 1000,
                30_000,
            )

    @pytest.mark.asyncio
    async def test_reconcile_to_defaults_updates_every_shard(self, repo):
        """The delete path (reconcile_bucket_to_defaults) fans out too."""
        await repo.create_entity("user-2")
        await self._seed_shards(repo, "user-2", "gpt-4", 2)

        await repo.reconcile_bucket_to_defaults(
            "user-2",
            "gpt-4",
            [Limit.custom("rpm", self.NEW, refill_amount=self.NEW, refill_period_seconds=60)],
        )

        for shard_id in (0, 1):
            assert await self._params(repo, "user-2", "gpt-4", shard_id) == (
                self.NEW * 1000,
                self.NEW * 1000,
                60_000,
            )

    @pytest.mark.asyncio
    async def test_stale_limit_attributes_are_removed_from_every_shard(self, repo):
        """A limit dropped from the config is removed from all shards (#327)."""
        now_ms = int(time.time() * 1000)
        await repo.create_entity("user-3")
        limits = [
            Limit.custom("rpm", 1000, refill_amount=1000, refill_period_seconds=60),
            Limit.custom("tpm", 50_000, refill_amount=50_000, refill_period_seconds=60),
        ]
        for shard_id in (0, 1):
            states = [BucketState.from_limit("user-3", "gpt-4", lim, now_ms) for lim in limits]
            await repo.transact_write(
                [
                    repo.build_composite_create(
                        "user-3", "gpt-4", states, now_ms, shard_id=shard_id, shard_count=2
                    )
                ]
            )

        await repo.reconcile_bucket_to_defaults(
            "user-3",
            "gpt-4",
            [Limit.custom("rpm", self.NEW, refill_amount=self.NEW, refill_period_seconds=60)],
            stale_limit_names={"tpm"},
        )

        for shard_id in (0, 1):
            assert await repo.get_bucket("user-3", "gpt-4", "tpm", shard_id=shard_id) is None
            assert await repo.get_bucket("user-3", "gpt-4", "rpm", shard_id=shard_id) is not None

    @pytest.mark.asyncio
    async def test_a_shard_that_vanished_mid_fanout_is_tolerated(self, repo):
        """A shard TTL-expired between discovery and the write must not raise."""
        await repo.create_entity("user-4")
        await self._seed_shards(repo, "user-4", "gpt-4", 2)

        from zae_limiter import schema

        ghost = schema.pk_bucket(repo._namespace_id, "user-4", "gpt-4", 7)
        real = await repo._discover_entity_bucket_pks("user-4", "gpt-4")
        with patch.object(
            repo, "_discover_entity_bucket_pks", AsyncMock(return_value=[*real, ghost])
        ):
            await repo.reconcile_bucket_to_defaults(
                "user-4",
                "gpt-4",
                [Limit.custom("rpm", self.NEW, refill_amount=self.NEW, refill_period_seconds=60)],
            )

        assert await repo.get_bucket("user-4", "gpt-4", "rpm", shard_id=7) is None
        for shard_id in (0, 1):
            assert (await self._params(repo, "user-4", "gpt-4", shard_id))[0] == self.NEW * 1000

    @pytest.mark.asyncio
    async def test_other_client_errors_propagate(self, repo):
        """A non-conditional failure is not swallowed by the fan-out.

        It surfaces as `FanoutIncomplete` carrying the underlying error, the
        same contract the ADR-125 disable fan-out uses on this same method.
        """
        from zae_limiter import schema
        from zae_limiter.exceptions import FanoutIncomplete

        pk = schema.pk_bucket(repo._namespace_id, "user-5", "gpt-4", 0)
        with patch.object(repo, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.query.return_value = {"Items": [{"PK": {"S": pk}}]}
            mock_client.update_item.side_effect = ClientError(
                {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "UpdateItem"
            )
            mock_get_client.return_value = mock_client

            with pytest.raises(FanoutIncomplete) as exc_info:
                await repo.reconcile_bucket_to_defaults(
                    "user-5", "gpt-4", [Limit.per_minute("rpm", 10)]
                )
            assert isinstance(exc_info.value.cause, ClientError)
            assert exc_info.value.stamped == 0


class TestSyncBucketParamsBoundsConcurrencyAndReportsProgress:
    """The fan-out is serial and reports partial progress on failure.

    Unscoped, the write set is O(resources x shards), not the <=32 shards of
    one resource, so a single unbounded `asyncio.gather` could issue thousands
    of concurrent `UpdateItem`s. It is now serial, exactly like the ADR-125
    `_fanout_entity` whose shape the docstring claims — which also makes an
    exact partial-progress count possible, on a path that has ALREADY
    committed the config item.
    """

    @staticmethod
    def _pks(repo, entity_id, count):
        from zae_limiter import schema

        return [
            {"PK": {"S": schema.pk_bucket(repo._namespace_id, entity_id, "gpt-4", shard)}}
            for shard in range(count)
        ]

    @pytest.mark.asyncio
    async def test_stops_at_the_failing_write_rather_than_issuing_the_rest(self, repo):
        """A gather would have issued all 5; serial stops after the third."""
        from zae_limiter.exceptions import FanoutIncomplete

        throttle = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "UpdateItem"
        )
        with patch.object(repo, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.query.return_value = {"Items": self._pks(repo, "user-p1", 5)}
            mock_client.update_item.side_effect = [None, None, throttle]
            mock_get_client.return_value = mock_client

            with pytest.raises(FanoutIncomplete) as exc_info:
                await repo.reconcile_bucket_to_defaults(
                    "user-p1", "gpt-4", [Limit.per_minute("rpm", 10)]
                )

        assert mock_client.update_item.call_count == 3, (
            "a concurrent gather would have issued all five writes"
        )
        assert exc_info.value.stamped == 2, "two writes landed before the failure"
        assert exc_info.value.entity_id == "user-p1"
        assert exc_info.value.resource == "gpt-4"

    @pytest.mark.asyncio
    async def test_unscoped_failure_reports_no_resource(self, repo):
        """The entity-wide scope is not one resource, so none is named."""
        from zae_limiter.exceptions import FanoutIncomplete

        await repo.create_entity("user-p2")
        await repo.set_limits("user-p2", [Limit.per_minute("rpm", 100)])
        now_ms = int(time.time() * 1000)
        for resource in ("gpt-4", "claude-3"):
            states = [
                BucketState.from_limit("user-p2", resource, Limit.per_minute("rpm", 100), now_ms)
            ]
            await repo.transact_write(
                [repo.build_composite_create("user-p2", resource, states, now_ms)]
            )

        throttle = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "UpdateItem"
        )
        with (
            patch.object(repo, "_sync_one_bucket_shard", AsyncMock(side_effect=throttle)),
            pytest.raises(FanoutIncomplete) as exc_info,
        ):
            await repo.set_limits("user-p2", [Limit.per_minute("rpm", 500)])

        assert exc_info.value.entity_id == "user-p2"
        assert exc_info.value.resource is None
        assert exc_info.value.stamped == 0

    @pytest.mark.asyncio
    async def test_a_vanished_shard_does_not_count_as_written(self, repo):
        """A TTL-expired shard is tolerated, but it is not progress."""
        from zae_limiter.exceptions import FanoutIncomplete

        conditional = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
        )
        throttle = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "UpdateItem"
        )
        with patch.object(repo, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.query.return_value = {"Items": self._pks(repo, "user-p3", 3)}
            mock_client.update_item.side_effect = [None, conditional, throttle]
            mock_get_client.return_value = mock_client

            with pytest.raises(FanoutIncomplete) as exc_info:
                await repo.reconcile_bucket_to_defaults(
                    "user-p3", "gpt-4", [Limit.per_minute("rpm", 10)]
                )

        assert exc_info.value.stamped == 1, "the vanished shard wrote nothing"


class TestDefaultResourceSyncReachesEveryResource:
    """An entity-wide `_default_` limit change must reach existing buckets (#487).

    `_default_` is a config scope, not a resource: no bucket item ever carries
    the GSI3SK `BUCKET#_default_#`, so passing it straight through to the GSI3
    discovery query matched zero items and the sync was a silent no-op that
    wrote nothing and raised nothing. Entity configs carry no TTL, so the
    affected buckets enforced the params they were born with forever.

    Widening discovery is necessary but not sufficient: precedence is
    Entity(resource) > Entity(`_default_`) > Resource > System, so an unscoped
    sync that stamped the caller's `_default_` limits onto every discovered
    bucket would clobber a resource that has its own, higher-precedence entity
    config. Each bucket is therefore re-stamped from the limits resolved for
    its OWN resource, exactly as `_fanout_entity` re-resolves `disabled`.
    """

    @staticmethod
    async def _seed(repo, entity_id, resource, *limits):
        """Create a bucket item for one (entity, resource) on the given limits."""
        now_ms = int(time.time() * 1000)
        states = [BucketState.from_limit(entity_id, resource, lim, now_ms) for lim in limits]
        await repo.transact_write(
            [repo.build_composite_create(entity_id, resource, states, now_ms)]
        )

    @staticmethod
    async def _raw(repo, entity_id, resource):
        from zae_limiter import schema

        client = await repo._get_client()
        response = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(repo._namespace_id, entity_id, resource, 0)},
                "SK": {"S": schema.sk_state()},
            },
        )
        return response.get("Item") or {}

    @classmethod
    async def _cap(cls, repo, entity_id, resource, limit_name="rpm"):
        from zae_limiter.schema import BUCKET_FIELD_CP, bucket_attr

        item = await cls._raw(repo, entity_id, resource)
        attr = bucket_attr(limit_name, BUCKET_FIELD_CP)
        assert attr in item, f"{limit_name} missing from {entity_id}/{resource}"
        return int(item[attr]["N"])

    @pytest.mark.asyncio
    async def test_entity_wide_change_reaches_an_existing_bucket(self, repo):
        """The regression: `set_limits` with no resource must reach the bucket."""
        await repo.create_entity("user-1")
        await repo.set_limits("user-1", [Limit.per_minute("rpm", 100)])
        await self._seed(repo, "user-1", "gpt-4", Limit.per_minute("rpm", 100))

        await repo.set_limits("user-1", [Limit.per_minute("rpm", 500)])

        assert await self._cap(repo, "user-1", "gpt-4") == 500_000

    @pytest.mark.asyncio
    async def test_entity_wide_change_does_not_clobber_a_resource_specific_config(self, repo):
        """The guard against the naive `resource=None` fix.

        `gpt-4` has its own entity config, which outranks the entity-wide
        `_default_` one. An unscoped sync that stamped the caller's limits on
        every discovered bucket would overwrite the more specific config with
        the less specific one.
        """
        await repo.create_entity("user-2")
        await repo.set_limits("user-2", [Limit.per_minute("rpm", 100)])
        await repo.set_limits("user-2", [Limit.per_minute("rpm", 900)], resource="gpt-4")
        await self._seed(repo, "user-2", "gpt-4", Limit.per_minute("rpm", 900))
        await self._seed(repo, "user-2", "claude-3", Limit.per_minute("rpm", 100))

        await repo.set_limits("user-2", [Limit.per_minute("rpm", 500)])

        assert await self._cap(repo, "user-2", "gpt-4") == 900_000, (
            "the resource-specific entity config outranks `_default_`"
        )
        assert await self._cap(repo, "user-2", "claude-3") == 500_000

    @pytest.mark.asyncio
    async def test_entity_wide_change_keeps_no_ttl_on_any_bucket(self, repo):
        """Every bucket still resolves at an entity level, so none gets a TTL."""
        await repo.create_entity("user-3")
        await repo.set_limits("user-3", [Limit.per_minute("rpm", 100)])
        await repo.set_limits("user-3", [Limit.per_minute("rpm", 900)], resource="gpt-4")
        await self._seed(repo, "user-3", "gpt-4", Limit.per_minute("rpm", 900))
        await self._seed(repo, "user-3", "claude-3", Limit.per_minute("rpm", 100))

        await repo.set_limits("user-3", [Limit.per_minute("rpm", 500)])

        for resource in ("gpt-4", "claude-3"):
            assert "ttl" not in await self._raw(repo, "user-3", resource)

    @pytest.mark.asyncio
    async def test_reconcile_resolves_and_ttls_each_bucket_at_its_own_level(self, repo):
        """The `delete_limits('_default_')` path, and per-bucket TTL (#271, #296).

        The caller hands `reconcile_bucket_to_defaults` the limits that
        `_default_` itself falls back to (system). Those are right for a bucket
        with nothing more specific and wrong for every other bucket, so the
        unscoped path ignores them and re-resolves per resource. TTL follows
        whichever level answered: entity means persist, resource or system
        means expire.
        """
        await repo.create_entity("user-4")
        await repo.set_system_defaults([Limit.per_minute("rpm", 50)])
        await repo.set_resource_defaults("claude-3", [Limit.per_minute("rpm", 200)])
        await repo.set_limits("user-4", [Limit.per_minute("rpm", 100)])
        await repo.set_limits("user-4", [Limit.per_minute("rpm", 900)], resource="gpt-4")
        for resource in ("gpt-4", "claude-3", "llama3"):
            await self._seed(repo, "user-4", resource, Limit.per_minute("rpm", 100))

        await repo.delete_limits("user-4")
        await repo.reconcile_bucket_to_defaults(
            "user-4", "_default_", [Limit.per_minute("rpm", 50)]
        )

        assert await self._cap(repo, "user-4", "gpt-4") == 900_000, "entity config still wins"
        assert await self._cap(repo, "user-4", "claude-3") == 200_000, "resource default applies"
        assert await self._cap(repo, "user-4", "llama3") == 50_000, "system default applies"

        assert "ttl" not in await self._raw(repo, "user-4", "gpt-4")
        assert "ttl" in await self._raw(repo, "user-4", "claude-3")
        assert "ttl" in await self._raw(repo, "user-4", "llama3")

    @pytest.mark.asyncio
    async def test_stale_names_are_intersected_against_each_bucket_resolution(self, repo):
        """A caller's stale name that the bucket's own level still defines is kept.

        `delete_limits` computes stale names against the `_default_` fallback
        (system). Applying that set verbatim to a bucket whose own entity
        config still declares the limit would SET and REMOVE the same attribute
        in one expression — a DynamoDB ValidationException — and, if it landed,
        would strip a configured limit.
        """
        rpm, tpm = Limit.per_minute("rpm", 100), Limit.per_minute("tpm", 10_000)
        await repo.create_entity("user-5")
        await repo.set_system_defaults([Limit.per_minute("rpm", 50)])
        await repo.set_limits("user-5", [rpm, tpm])
        await repo.set_limits("user-5", [rpm, tpm], resource="gpt-4")
        await self._seed(repo, "user-5", "gpt-4", rpm, tpm)
        await self._seed(repo, "user-5", "llama3", rpm, tpm)

        await repo.delete_limits("user-5")
        await repo.reconcile_bucket_to_defaults(
            "user-5",
            "_default_",
            [Limit.per_minute("rpm", 50)],
            stale_limit_names={"tpm"},
        )

        assert await self._cap(repo, "user-5", "gpt-4", "tpm") == 10_000_000, (
            "gpt-4's own entity config still declares tpm"
        )
        assert await repo.get_bucket("user-5", "llama3", "tpm") is None, (
            "llama3 falls back to system, which has no tpm"
        )

    @pytest.mark.asyncio
    async def test_a_resource_that_resolves_to_nothing_is_left_alone(self, repo):
        """No configured level means no correct value to write.

        The same choice the delete path already makes when no fallback config
        exists at all — better a bucket on stale params than one stamped with
        limits nothing actually configures.
        """
        await repo.create_entity("user-7")
        await self._seed(repo, "user-7", "ghost", Limit.per_minute("rpm", 100))

        await repo.reconcile_bucket_to_defaults(
            "user-7", "_default_", [Limit.per_minute("rpm", 50)]
        )

        assert await self._cap(repo, "user-7", "ghost") == 100_000
        assert "ttl" in await self._raw(repo, "user-7", "ghost"), (
            "the seeded TTL is untouched, not recomputed from limits that do not apply"
        )

    @pytest.mark.asyncio
    async def test_a_real_resource_is_still_scoped_to_that_resource(self, repo):
        """The scoped path is untouched: a sibling resource is not rewritten."""
        await repo.create_entity("user-6")
        await repo.set_limits("user-6", [Limit.per_minute("rpm", 100)], resource="gpt-4")
        await self._seed(repo, "user-6", "gpt-4", Limit.per_minute("rpm", 100))
        await self._seed(repo, "user-6", "claude-3", Limit.per_minute("rpm", 100))

        await repo.set_limits("user-6", [Limit.per_minute("rpm", 700)], resource="gpt-4")

        assert await self._cap(repo, "user-6", "gpt-4") == 700_000
        assert await self._cap(repo, "user-6", "claude-3") == 100_000


class TestStaleLimitAliasesAreExpressionSafe:
    """Stale-limit REMOVE aliases must be legal expression attribute names.

    `NAME_PATTERN` (models.py) allows `-` and `.` in a limit name. Neither is
    legal in an `ExpressionAttributeNames` alias, and `.` is parsed as a
    document-path separator, so interpolating the limit name into the alias
    builds an expression DynamoDB rejects with a `ValidationException` —
    raised out of `set_limits()`/`delete_limits()` *after* the config item has
    already been written, leaving half-applied state.

    The provisioner mirror (`bucket_sync.build_bucket_param_update`) already
    used a monotonic counter for exactly this reason; the two halves of #487
    disagreed until now.
    """

    ILLEGAL_IN_ALIAS = ("-", ".", " ")

    @staticmethod
    def _aliases(expr: str, names: dict[str, str]) -> list[str]:
        assert "REMOVE" in expr, expr
        return [part.strip() for part in expr.split("REMOVE", 1)[1].split(",")]

    @pytest.mark.asyncio
    async def test_hyphenated_and_dotted_stale_names_build_legal_aliases(self, repo):
        from zae_limiter.schema import bucket_attr

        expr, names, _values = repo._build_bucket_param_update(
            [Limit.per_minute("rpm", 100)],
            None,
            {"req-min", "tok.sec"},
        )
        aliases = self._aliases(expr, names)
        assert aliases, "expected REMOVE clauses for the stale limits"
        for alias in aliases:
            assert alias.startswith("#")
            for bad in self.ILLEGAL_IN_ALIAS:
                assert bad not in alias, f"{alias!r} is not a legal expression alias"
        # The aliases must still resolve to the right attributes. The stale
        # names lose both schedule overrides along with everything else, and
        # the unscheduled branch clears the item-level `sched` / `rsched` pair
        # and `sched_tz` plus the surviving limit's own overrides (#222 Task
        # 13 for `sched`, surface Task 4 for `rsched`).
        removed = {names[alias] for alias in aliases}
        assert removed == {
            bucket_attr(name, field)
            for name in ("req-min", "tok.sec")
            for field in ("tk", "cp", "ra", "rp", "tc", "sched", "rsched")
        } | {
            "sched",
            "rsched",
            "sched_tz",
            bucket_attr("rpm", "sched"),
            bucket_attr("rpm", "rsched"),
        }

    @pytest.mark.asyncio
    async def test_scoped_reconcile_with_a_hyphenated_stale_name(self, repo):
        """The pre-existing #327 reach: `delete_limits` computes stale names."""
        await repo.create_entity("user-h1")
        await self._seed(repo, "user-h1", "gpt-4")

        await repo.reconcile_bucket_to_defaults(
            "user-h1",
            "gpt-4",
            [Limit.per_minute("rpm", 50)],
            stale_limit_names={"req-min"},
        )

        assert await repo.get_bucket("user-h1", "gpt-4", "req-min") is None
        assert await repo.get_bucket("user-h1", "gpt-4", "rpm") is not None

    @pytest.mark.asyncio
    async def test_entity_wide_set_limits_with_a_hyphenated_stale_name(self, repo):
        """The reach #487 added: the entity-wide scope derives stale names too.

        `gpt-4` has its own entity config declaring only `rpm`, so the
        entity-wide directive's `req-min` is stale for that bucket — a stale
        name `set_limits()` never produced before this PR.
        """
        await repo.create_entity("user-h2")
        await repo.set_limits("user-h2", [Limit.per_minute("rpm", 100)], resource="gpt-4")
        await self._seed(repo, "user-h2", "gpt-4")

        await repo.set_limits("user-h2", [Limit.per_minute("req-min", 900)])

        assert await repo.get_bucket("user-h2", "gpt-4", "req-min") is None
        assert await repo.get_bucket("user-h2", "gpt-4", "rpm") is not None

    @staticmethod
    async def _seed(repo, entity_id, resource):
        """A bucket carrying both a plain and a hyphenated limit."""
        now_ms = int(time.time() * 1000)
        limits = [Limit.per_minute("rpm", 100), Limit.per_minute("req-min", 100)]
        states = [BucketState.from_limit(entity_id, resource, lim, now_ms) for lim in limits]
        await repo.transact_write(
            [repo.build_composite_create(entity_id, resource, states, now_ms)]
        )


class TestScheduleConfigRoundTrip:
    """`l_{name}_sched` round-trips through set_limits/get_limits (#222 §1.4, §4.1).

    The cron here is spelled with a numeric weekday because storage is
    **canonical**: names normalise to numbers on the way in (§4.3), so
    `MON-FRI` would not come back as `MON-FRI`. That normalisation is pinned
    on its own below rather than papered over by weakening every assertion.
    """

    SCHED = (ScheduleEntry(cron="* 9-17 * * 1-5", tz="America/New_York", scale=0.5),)

    async def test_schedule_survives_set_and_get(self, repo):
        await repo.set_limits(
            "user-1",
            [Limit.per_minute("rpm", 1000).with_schedule(self.SCHED)],
            resource="gpt-4",
        )
        (limit,) = await repo.get_limits("user-1", resource="gpt-4")
        assert limit.schedule == self.SCHED

    async def test_limit_without_schedule_round_trips_as_empty(self, repo):
        await repo.set_limits("user-1", [Limit.per_minute("rpm", 1000)], resource="gpt-4")
        (limit,) = await repo.get_limits("user-1", resource="gpt-4")
        assert limit.schedule == ()

    async def test_replacing_a_limit_without_a_schedule_removes_the_stored_one(self, repo):
        """Override, not merge: a limit with no schedule has no schedule (§1.6).

        This is the test that catches a write path which merely *skips*
        `l_{name}_sched` when the schedule is empty instead of REMOVEing it.
        """
        await repo.set_limits(
            "user-1",
            [Limit.per_minute("rpm", 1000).with_schedule(self.SCHED)],
            resource="gpt-4",
        )
        await repo.set_limits("user-1", [Limit.per_minute("rpm", 1000)], resource="gpt-4")
        (limit,) = await repo.get_limits("user-1", resource="gpt-4")
        assert limit.schedule == ()

    async def test_two_limits_with_different_schedules(self, repo):
        """Schedules are per-limit (§2.2); tz is shared, the crons need not be."""
        night = (ScheduleEntry(cron="* 0-6 * * *", tz="America/New_York", capacity=2000),)
        await repo.set_limits(
            "user-1",
            [
                Limit.per_minute("rpm", 1000).with_schedule(self.SCHED),
                Limit.per_minute("tpm", 100_000).with_schedule(night),
            ],
            resource="gpt-4",
        )
        by_name = {lim.name: lim for lim in await repo.get_limits("user-1", resource="gpt-4")}
        assert by_name["rpm"].schedule == self.SCHED
        assert by_name["tpm"].schedule == night

    async def test_a_named_cron_comes_back_canonical(self, repo):
        """Storage normalises weekday/month names to numbers (§4.3).

        `differ.py` compares manifest against stored state, so keeping the
        operator's verbatim text would read `MON-FRI` against `1-5` as a change
        on every apply. The consequence users see is here: what comes back is
        semantically identical but not textually what went in.
        """
        await repo.set_limits(
            "user-1",
            [
                Limit.per_minute("rpm", 1000).with_schedule(
                    (ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),)
                )
            ],
            resource="gpt-4",
        )
        (limit,) = await repo.get_limits("user-1", resource="gpt-4")
        assert limit.schedule == self.SCHED
        assert limit.schedule[0].cron == "* 9-17 * * 1-5"

    async def test_stored_form_is_compact_with_one_hoisted_timezone(self, repo):
        """Pins the on-item shape: the compact string, and `sched_tz` written once.

        Without this the round-trip tests would pass against a JSON blob, or
        against a per-limit `l_{name}_sched_tz` — both of which cost bytes the
        design measured out (§4.2) and neither of which downstream tasks read.
        """
        await repo.set_limits(
            "user-1",
            [
                Limit.per_minute("rpm", 1000).with_schedule(self.SCHED),
                Limit.per_minute("tpm", 100_000),
            ],
            resource="gpt-4",
        )
        client = await repo._get_client()
        item = (
            await client.get_item(
                TableName=repo.table_name,
                Key={
                    "PK": {"S": f"{repo._namespace_id}/ENTITY#user-1"},
                    "SK": {"S": sk_config("gpt-4")},
                },
            )
        )["Item"]

        assert item["l_rpm_sched"]["S"] == "1h9-17w1-5s500"
        assert item["sched_tz"]["S"] == "America/New_York"
        assert "l_tpm_sched" not in item
        assert "l_rpm_sched_tz" not in item

    async def test_no_schedule_writes_no_timezone_attribute(self, repo):
        """`sched_tz` is only meaningful next to a schedule; don't write it otherwise."""
        await repo.set_limits("user-1", [Limit.per_minute("rpm", 1000)], resource="gpt-4")
        client = await repo._get_client()
        item = (
            await client.get_item(
                TableName=repo.table_name,
                Key={
                    "PK": {"S": f"{repo._namespace_id}/ENTITY#user-1"},
                    "SK": {"S": sk_config("gpt-4")},
                },
            )
        )["Item"]
        assert "sched_tz" not in item

    async def test_resource_defaults_round_trip(self, repo):
        await repo.set_resource_defaults(
            "gpt-4", [Limit.per_minute("rpm", 500).with_schedule(self.SCHED)]
        )
        (limit,) = await repo.get_resource_defaults("gpt-4")
        assert limit.schedule == self.SCHED

    async def test_system_defaults_round_trip(self, repo):
        await repo.set_system_defaults([Limit.per_minute("rpm", 100).with_schedule(self.SCHED)])
        limits, _on_unavailable = await repo.get_system_defaults()
        assert limits[0].schedule == self.SCHED

    async def test_resolve_limits_carries_the_schedule(self, repo):
        """The batch config read is a second deserialization call site (#222 Task 12)."""
        await repo.set_limits(
            "user-1",
            [Limit.per_minute("rpm", 1000).with_schedule(self.SCHED)],
            resource="gpt-4",
        )
        limits, _on_unavailable, source = await repo.resolve_limits("user-1", "gpt-4")
        assert source == "entity"
        assert limits is not None
        assert limits[0].schedule == self.SCHED

    async def test_read_of_an_item_written_by_an_older_client(self, repo):
        """No `sched`, no `sched_tz` — the pre-#222 shape must not raise."""
        item = {
            "l_rpm_cp": {"N": "1000"},
            "l_rpm_ra": {"N": "1000"},
            "l_rpm_rp": {"N": "60"},
        }
        (limit,) = repo._deserialize_composite_limits(item)
        assert limit.schedule == ()
        assert limit.capacity == 1000

    async def test_schedule_without_a_hoisted_timezone_falls_back_to_utc(self, repo):
        """Defensive: a `sched` with no `sched_tz` decodes as UTC rather than raising."""
        item = {
            "l_rpm_cp": {"N": "1000"},
            "l_rpm_ra": {"N": "1000"},
            "l_rpm_rp": {"N": "60"},
            "l_rpm_sched": {"S": "1h9-17s500"},
        }
        (limit,) = repo._deserialize_composite_limits(item)
        assert limit.schedule == (ScheduleEntry(cron="* 9-17 * * *", tz="UTC", scale=0.5),)

    async def test_two_limits_disagreeing_on_timezone_are_rejected_at_the_write(self, repo):
        """`sched_tz` is one attribute per item, so the whole item must agree (§4.1).

        `Limit.__post_init__` only sees one limit's entries; nothing below it
        notices that a second limit on the same config item brought a different
        zone, and the item-level write is last-one-wins — so the first limit's
        schedule would silently be reinterpreted in the second's timezone.
        """
        with pytest.raises(ValueError, match="timezone"):
            await repo.set_limits(
                "user-1",
                [
                    Limit.per_minute("rpm", 1000).with_schedule(self.SCHED),
                    Limit.per_minute("tpm", 100_000).with_schedule(
                        (ScheduleEntry(cron="* 0-6 * * *", tz="Europe/Berlin", capacity=2000),)
                    ),
                ],
                resource="gpt-4",
            )

    async def test_an_unscheduled_limit_does_not_constrain_the_timezone(self, repo):
        """Only limits that actually carry a schedule vote on `sched_tz`."""
        await repo.set_limits(
            "user-1",
            [
                Limit.per_minute("rpm", 1000).with_schedule(self.SCHED),
                Limit.per_minute("tpm", 100_000),
            ],
            resource="gpt-4",
        )
        by_name = {lim.name: lim for lim in await repo.get_limits("user-1", resource="gpt-4")}
        assert by_name["rpm"].schedule == self.SCHED
        assert by_name["tpm"].schedule == ()


class TestScheduleValidation:
    def test_rejects_entries_disagreeing_on_timezone(self):
        """`sched_tz` is one item-level attribute, so entries must agree (§4.1)."""
        with pytest.raises(ValueError, match="timezone"):
            Limit.per_minute("rpm", 1000).with_schedule(
                (
                    ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),
                    ScheduleEntry(cron="* 0-6 * * *", tz="UTC", scale=0.5),
                )
            )

    def test_with_schedule_returns_a_new_limit(self):
        """`Limit` is frozen; `with_schedule` must not mutate."""
        base = Limit.per_minute("rpm", 1000)
        scheduled = base.with_schedule((ScheduleEntry(cron="* * * * *", tz="UTC", scale=0.5),))
        assert base.schedule == ()
        assert scheduled is not base
        assert scheduled.capacity == base.capacity

    def test_with_schedule_clears_an_existing_schedule(self):
        entry = ScheduleEntry(cron="* * * * *", tz="UTC", scale=0.5)
        assert (
            Limit.per_minute("rpm", 1000).with_schedule((entry,)).with_schedule(()).schedule == ()
        )

    def test_per_shard_materialises_the_schedule(self):
        """`per_shard` narrows on both axes at once (#222 §3.5): the window in
        force scales the undivided base, then the shard takes its share. The
        result is a point-in-time value, so it carries no schedule of its own —
        leaving one attached would invite a second application."""
        entry = ScheduleEntry(cron="* * * * *", tz="UTC", scale=0.5)
        limit = Limit.per_minute("rpm", 1000).with_schedule((entry,))
        shard = limit.per_shard(4, now_ms=0)
        assert shard.capacity == 125
        assert shard.schedule == ()
        assert limit.schedule == (entry,), "must not mutate"

    def test_a_scheduled_limit_is_hashable(self):
        """`Limit` is a frozen dataclass; a mutable schedule field would break that."""
        entry = ScheduleEntry(cron="* * * * *", tz="UTC", scale=0.5)
        assert len({Limit.per_minute("rpm", 1000).with_schedule((entry,))}) == 1

    def test_carrier_limits_have_an_empty_schedule(self):
        """`_carrier` bypasses `__init__`, so the default has to reach it anyway."""
        state = BucketState(
            entity_id="e",
            resource="r",
            limit_name="wcu",
            tokens_milli=1000,
            last_refill_ms=0,
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=1000,
        )
        assert Limit._carrier(state).schedule == ()

    def test_to_dict_round_trips_the_schedule(self):
        """`from_dict(to_dict(x))` must not silently drop the schedule."""
        limit = Limit.per_minute("rpm", 1000).with_schedule(
            (
                ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),
                ScheduleEntry(cron="* 0-6 * * *", tz="America/New_York", capacity=2000),
            )
        )
        assert Limit.from_dict(limit.to_dict()) == limit

    def test_to_dict_omits_an_empty_schedule(self):
        """Existing payloads (audit details) must not grow a null key."""
        assert "schedule" not in Limit.per_minute("rpm", 1000).to_dict()

    def test_to_dict_uses_standard_cron_not_the_storage_encoding(self):
        """Standard 5-field cron at every boundary; compact is storage only (§4)."""
        limit = Limit.per_minute("rpm", 1000).with_schedule(
            (ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),)
        )
        assert limit.to_dict()["schedule"] == [
            {"cron": "* 9-17 * * MON-FRI", "tz": "America/New_York", "scale": 0.5}
        ]


class TestSlowPathWritesVu:
    """Both composite builders carry the ``vu`` stamp (#222 §2.1, Task 12).

    ``vu`` is what the fast path's condition gates on (Task 11). Task 11 taught
    it to *read* the stamp; these pin the writes that produce one, on the only
    two shapes the slow path emits — an update to an existing item and the
    create of a new one.
    """

    NOW = 1_789_000_000_000
    HORIZON = NOW + 3_600_000

    @staticmethod
    def _state(limit, now_ms, entity_id="user-1", resource="gpt-4"):
        return BucketState.from_limit(entity_id, resource, limit, now_ms)

    def test_normal_writes_vu_when_given_one(self, repo):
        item = repo.build_composite_normal(
            "user-1",
            "gpt-4",
            consumed={"rpm": 1000},
            refill_amounts={"rpm": 0},
            now_ms=self.NOW,
            expected_rf=self.NOW - 1000,
            vu=self.HORIZON,
        )
        upd = item["Update"]
        assert "#vu = :vu" in upd["UpdateExpression"]
        assert upd["ExpressionAttributeValues"][":vu"] == {"N": str(self.HORIZON)}
        assert upd["ExpressionAttributeNames"]["#vu"] == BUCKET_FIELD_VU

    def test_normal_omits_vu_when_there_is_no_schedule(self, repo):
        """``None`` must omit the attribute, never write a null or a zero."""
        item = repo.build_composite_normal(
            "user-1",
            "gpt-4",
            consumed={"rpm": 1000},
            refill_amounts={"rpm": 0},
            now_ms=self.NOW,
            expected_rf=self.NOW - 1000,
            vu=None,
        )
        upd = item["Update"]
        assert "#vu" not in upd["ExpressionAttributeNames"]
        assert ":vu" not in upd["ExpressionAttributeValues"]
        assert "vu" not in upd["UpdateExpression"]

    def test_normal_clears_vu_when_asked(self, repo):
        """`clear_vu` REMOVEs the stamp. `vu=None` cannot mean this: leaving
        `vu` alone is the right behaviour for a pass that has nothing to say
        about the boundary, but a pass that knows nothing on the item is
        scheduled has to strip the `vu = 0` the #468 fan-out wrote — or the
        item fails `(attribute_not_exists(vu) OR vu > now)` forever."""
        item = repo.build_composite_normal(
            "user-1",
            "gpt-4",
            consumed={"rpm": 1000},
            refill_amounts={"rpm": 0},
            now_ms=self.NOW,
            expected_rf=self.NOW - 1000,
            vu=None,
            clear_vu=True,
        )
        expr = item["Update"]["UpdateExpression"]
        set_clause, remove_clause = expr.split(" REMOVE ")
        assert "#vu" in remove_clause
        assert "#vu" not in set_clause
        assert item["Update"]["ExpressionAttributeNames"]["#vu"] == BUCKET_FIELD_VU
        assert ":vu" not in item["Update"]["ExpressionAttributeValues"]

    def test_a_boundary_wins_over_clear_vu(self, repo):
        """The two are mutually exclusive by construction, never both in one
        expression (#488). A caller passing both must get the SET."""
        item = repo.build_composite_normal(
            "user-1",
            "gpt-4",
            consumed={"rpm": 1000},
            refill_amounts={"rpm": 0},
            now_ms=self.NOW,
            expected_rf=self.NOW - 1000,
            vu=self.HORIZON,
            clear_vu=True,
        )
        expr = item["Update"]["UpdateExpression"]
        assert "#vu = :vu" in expr
        assert " REMOVE " not in expr or "#vu" not in expr.split(" REMOVE ")[1]

    def test_clear_vu_rides_beside_a_ttl_remove(self, repo):
        """Both REMOVEs in one clause, which is the shape the unscheduled
        entity-config bucket actually takes (ADR-136 REMOVEs `ttl`)."""
        item = repo.build_composite_normal(
            "user-1",
            "gpt-4",
            consumed={"rpm": 1000},
            refill_amounts={"rpm": 0},
            now_ms=self.NOW,
            expected_rf=self.NOW - 1000,
            ttl_seconds=0,
            vu=None,
            clear_vu=True,
        )
        remove_clause = item["Update"]["UpdateExpression"].split(" REMOVE ")[1]
        assert "#ttl" in remove_clause
        assert "#vu" in remove_clause

    def test_normal_never_sets_and_removes_vu_together(self, repo):
        """``ttl`` can be REMOVEd in the same expression; ``vu`` must not join
        it. SET and REMOVE on one attribute is the ValidationException #488
        hit, and the TTL branch is the only REMOVE this builder emits."""
        item = repo.build_composite_normal(
            "user-1",
            "gpt-4",
            consumed={"rpm": 1000},
            refill_amounts={"rpm": 0},
            now_ms=self.NOW,
            expected_rf=self.NOW - 1000,
            ttl_seconds=0,  # drives the REMOVE branch
            vu=self.HORIZON,
        )
        expr = item["Update"]["UpdateExpression"]
        set_clause, remove_clause = expr.split(" REMOVE ")
        assert "#vu" in set_clause
        assert "#vu" not in remove_clause

    def test_negative_refill_delta_trims_the_surplus(self, repo):
        """#496 clamps inside ``refill_bucket``, so the lease's delta goes
        negative on a shrink and the unconditional ADD carries the trim. A
        second clamp in the builder would double-apply it."""
        item = repo.build_composite_normal(
            "user-1",
            "gpt-4",
            consumed={"rpm": 1000},
            refill_amounts={"rpm": -400_000},
            now_ms=self.NOW,
            expected_rf=self.NOW - 1000,
            vu=None,
        )
        values = item["Update"]["ExpressionAttributeValues"]
        assert values[":b_rpm_tk_delta"] == {"N": str(-400_000 - 1000)}

    def test_create_stamps_vu(self, repo):
        """A bucket created on the slow path needs its first ``vu``, or the
        very next acquire takes the fast path against an unmaterialised item."""
        state = self._state(Limit.per_minute("rpm", 100), self.NOW)
        item = repo.build_composite_create("user-1", "gpt-4", [state], self.NOW, vu=self.HORIZON)
        assert item["Put"]["Item"][BUCKET_FIELD_VU] == {"N": str(self.HORIZON)}

    def test_create_omits_vu_when_there_is_no_schedule(self, repo):
        state = self._state(Limit.per_minute("rpm", 100), self.NOW)
        item = repo.build_composite_create("user-1", "gpt-4", [state], self.NOW)
        assert BUCKET_FIELD_VU not in item["Put"]["Item"]


class TestCreateStampsSchedule:
    """A created bucket carries its schedule (#222 §2.2, design line 89).

    The aggregator reads the item and nothing else. Without ``sched`` on it, a
    bucket born inside a ``0.5x`` window is refilled toward the *base* ceiling
    and the fast path spends the surplus — the schedule silently unenforced
    until the next admin fan-out. The plan assigns the fan-out's stamp to Task
    13 and leaves creation unassigned; §2.2 says "stamped at bucket creation,
    re-stamped by the set_limits fan-out".
    """

    NOW = 1_789_000_000_000
    BUSINESS = (ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),)
    NIGHTLY = (ScheduleEntry(cron="* 0-6 * * *", tz="America/New_York", scale=0.25),)

    def _state(self, limit):
        return BucketState.from_limit("sched-1", "gpt-4", limit, self.NOW)

    def _item(self, repo, limits):
        states = [self._state(limit) for limit in limits]
        return repo.build_composite_create("sched-1", "gpt-4", states, self.NOW)["Put"]["Item"]

    def test_stamps_the_item_level_schedule_and_timezone(self, repo):
        item = self._item(repo, [Limit.per_minute("rpm", 1000).with_schedule(self.BUSINESS)])
        assert item["sched"]["S"] == "1h9-17w1-5s500"
        assert item["sched_tz"]["S"] == "America/New_York"

    def test_omits_sched_when_nothing_is_scheduled(self, repo):
        """The overwhelming majority of buckets; they must not grow attributes."""
        item = self._item(repo, [Limit.per_minute("rpm", 1000)])
        assert "sched" not in item
        assert "sched_tz" not in item

    def test_writes_a_per_limit_override_only_where_a_limit_differs(self, repo):
        """§4.1: one item-level default plus overrides where they diverge. A
        limit sharing the default must not get a redundant copy."""
        item = self._item(
            repo,
            [
                Limit.per_minute("rpm", 1000).with_schedule(self.BUSINESS),
                Limit.per_minute("tpm", 5000).with_schedule(self.BUSINESS),
                Limit.per_minute("cpm", 20).with_schedule(self.NIGHTLY),
            ],
        )
        assert item["sched"]["S"] == "1h9-17w1-5s500"
        assert bucket_attr("rpm", "sched") not in item
        assert bucket_attr("tpm", "sched") not in item
        assert item[bucket_attr("cpm", "sched")]["S"] == "1h0-6s250"

    def test_an_unscheduled_limit_beside_a_scheduled_one_is_marked_unscheduled(self, repo):
        """#541. Absence means "use the item default", so an unscheduled limit
        on a scheduled item silently inherited the schedule until §4.1 grew an
        encoding for "explicitly unscheduled" (``BUCKET_SCHED_NONE``).

        The mix is reachable through ``set_limits``, contrary to the claim this
        test used to carry: ``models.hoisted_schedule_timezone`` documents that
        "limits without one do not vote", so an unscheduled limit clears the one
        write-time check a mixed list has to pass, and the config hierarchy
        resolves the whole list from one level."""
        item = self._item(
            repo,
            [
                Limit.per_minute("rpm", 1000).with_schedule(self.BUSINESS),
                Limit.per_minute("tpm", 5000),
            ],
        )
        assert item[bucket_attr("tpm", "sched")]["S"] == BUCKET_SCHED_NONE
        # ...and the scheduled limit that supplied the default still needs no
        # copy of its own, so this is not "an override for every limit".
        assert bucket_attr("rpm", "sched") not in item

    def test_rejects_limits_that_disagree_on_timezone(self, repo):
        """``sched_tz`` is one attribute per item, so keeping the first limit's
        zone would reinterpret the second limit's cron in the wrong one — the
        last-one-wins shape #222's config writer already rejects."""
        other_zone = (ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="Europe/Berlin", scale=0.5),)
        with pytest.raises(ValueError, match="share a timezone"):
            self._item(
                repo,
                [
                    Limit.per_minute("rpm", 1000).with_schedule(self.BUSINESS),
                    Limit.per_minute("tpm", 5000).with_schedule(other_zone),
                ],
            )

    def test_stored_params_stay_the_undivided_base(self, repo):
        """The schedule never rewrites cp/ra — it applies on top (§2.1). The
        base is the only copy from which the *next* window can be computed."""
        inside = int(
            datetime(2026, 9, 15, 14, 0, tzinfo=ZoneInfo("America/New_York")).timestamp() * 1000
        )
        state = BucketState.from_limit(
            "sched-1", "gpt-4", Limit.per_minute("rpm", 1000).with_schedule(self.BUSINESS), inside
        )
        item = repo.build_composite_create("sched-1", "gpt-4", [state], inside)["Put"]["Item"]
        assert item[bucket_attr("rpm", "cp")]["N"] == "1000000"
        assert item[bucket_attr("rpm", "ra")]["N"] == "1000000"
        # ...while the balance it starts at is the scheduled one.
        assert item[bucket_attr("rpm", BUCKET_FIELD_TK)]["N"] == "500000"

    def test_a_new_bucket_outside_the_window_starts_at_the_base_balance(self, repo):
        """The contrast case: same limit, instant outside the window."""
        outside = int(
            datetime(2026, 9, 15, 3, 0, tzinfo=ZoneInfo("America/New_York")).timestamp() * 1000
        )
        state = BucketState.from_limit(
            "sched-1", "gpt-4", Limit.per_minute("rpm", 1000).with_schedule(self.BUSINESS), outside
        )
        item = repo.build_composite_create("sched-1", "gpt-4", [state], outside)["Put"]["Item"]
        assert item[bucket_attr("rpm", BUCKET_FIELD_TK)]["N"] == "1000000"


class TestFanOutStampsSchedule:
    """The `set_limits` fan-out re-stamps the schedule and expires `vu` (#222 Task 13).

    Two independent jobs share one write:

    * **`sched`/`sched_tz`/`b_{name}_sched`** follow the config, because the
      aggregator reads the bucket item and nothing else. A bucket left holding
      a superseded schedule is refilled toward a ceiling the operator already
      changed.
    * **`vu = 0` on EVERY fan-out**, scheduled or not, forcing exactly one
      materialising pass. Since #496 `refill_bucket` clamps on every path, but
      the speculative fast path is a pure ADD with no cap maths, so after a
      capacity shrink nothing else trims the surplus before it is spent. This
      is what makes #222 subsume #469 completely rather than partially.
    """

    BUSINESS = (ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),)
    NIGHTLY = (ScheduleEntry(cron="* 0-6 * * *", tz="America/New_York", scale=0.25),)
    BERLIN = (ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="Europe/Berlin", scale=0.5),)

    @staticmethod
    async def _seed(repo, entity_id, resource, limits, shards=1):
        now_ms = int(time.time() * 1000)
        for shard_id in range(shards):
            states = [BucketState.from_limit(entity_id, resource, lim, now_ms) for lim in limits]
            await repo.transact_write(
                [
                    repo.build_composite_create(
                        entity_id,
                        resource,
                        states,
                        now_ms,
                        shard_id=shard_id,
                        shard_count=shards,
                    )
                ]
            )

    @staticmethod
    async def _raw(repo, entity_id, resource, shard=0):
        from zae_limiter import schema

        client = await repo._get_client()
        response = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(repo._namespace_id, entity_id, resource, shard)},
                "SK": {"S": schema.sk_state()},
            },
        )
        return response.get("Item") or {}

    # -- the plan's four ---------------------------------------------------

    @pytest.mark.asyncio
    async def test_set_limits_stamps_sched_and_expires_vu(self, repo):
        await repo.create_entity("fan-1")
        await self._seed(repo, "fan-1", "gpt-4", [Limit.per_minute("rpm", 1000)])

        await repo.set_limits(
            "fan-1",
            [Limit.per_minute("rpm", 1000).with_schedule(self.BUSINESS)],
            resource="gpt-4",
        )

        item = await self._raw(repo, "fan-1", "gpt-4")
        assert item["sched"]["S"] == "1h9-17w1-5s500"
        assert item["sched_tz"]["S"] == "America/New_York"
        assert item[BUCKET_FIELD_VU]["N"] == "0"

    @pytest.mark.asyncio
    async def test_removing_a_schedule_removes_sched_but_still_expires_vu(self, repo):
        """The two stamps are not removed together, and must not be conflated.

        `sched`/`sched_tz` go away with the schedule. `vu` does **not**: it is
        SET to 0 on every fan-out and self-clears on the next materialising
        pass, which has not run yet at this assertion.
        """
        await repo.create_entity("fan-2")
        await repo.set_limits(
            "fan-2",
            [Limit.per_minute("rpm", 1000).with_schedule(self.BUSINESS)],
            resource="gpt-4",
        )
        await self._seed(
            repo,
            "fan-2",
            "gpt-4",
            [Limit.per_minute("rpm", 1000).with_schedule(self.BUSINESS)],
        )

        await repo.set_limits("fan-2", [Limit.per_minute("rpm", 1000)], resource="gpt-4")

        item = await self._raw(repo, "fan-2", "gpt-4")
        assert "sched" not in item
        assert "sched_tz" not in item
        assert item[BUCKET_FIELD_VU]["N"] == "0"

    @pytest.mark.asyncio
    async def test_a_never_scheduled_fan_out_still_expires_vu(self, repo):
        """The case the unconditional write exists for, and the common one.

        This bucket never had a schedule at all and is shrinking a capacity —
        literally #469's scenario, and the shape most `set_limits` calls take.
        Nesting `vu = 0` back inside `if scheduled:` leaves it free to spend
        its surplus over the lowered ceiling before any refiller trims it.
        """
        await repo.create_entity("fan-4")
        await self._seed(repo, "fan-4", "gpt-4", [Limit.per_minute("rpm", 1000)])

        await repo.set_limits("fan-4", [Limit.per_minute("rpm", 10)], resource="gpt-4")

        item = await self._raw(repo, "fan-4", "gpt-4")
        assert "sched" not in item
        assert item[BUCKET_FIELD_VU]["N"] == "0"

    @pytest.mark.asyncio
    async def test_base_params_stay_undivided_and_unscaled(self, repo):
        """The schedule never rewrites cp/ra — it applies on top (§2.1)."""
        await repo.create_entity("fan-3")
        await self._seed(repo, "fan-3", "gpt-4", [Limit.per_minute("rpm", 1000)])

        await repo.set_limits(
            "fan-3",
            [Limit.per_minute("rpm", 1000).with_schedule(self.BUSINESS)],
            resource="gpt-4",
        )

        item = await self._raw(repo, "fan-3", "gpt-4")
        assert item[bucket_attr("rpm", "cp")]["N"] == "1000000"
        assert item[bucket_attr("rpm", "ra")]["N"] == "1000000"

    # -- the N-surfaces ----------------------------------------------------

    @pytest.mark.asyncio
    async def test_every_shard_is_stamped_and_expired(self, repo):
        """`vu` is per item, so a shard the fan-out skipped keeps a stale
        schedule *and* keeps fast-pathing against an unclamped surplus. The
        #468 fan-out exists because keying only shard 0 left 1..N-1 enforcing
        the limits they were born with."""
        await repo.create_entity("fan-shard")
        await self._seed(repo, "fan-shard", "gpt-4", [Limit.per_minute("rpm", 1000)], shards=4)

        await repo.set_limits(
            "fan-shard",
            [Limit.per_minute("rpm", 1000).with_schedule(self.BUSINESS)],
            resource="gpt-4",
        )

        for shard in range(4):
            item = await self._raw(repo, "fan-shard", "gpt-4", shard)
            assert item["sched"]["S"] == "1h9-17w1-5s500", f"shard {shard}"
            assert item[BUCKET_FIELD_VU]["N"] == "0", f"shard {shard}"

    @pytest.mark.asyncio
    async def test_the_entity_wide_scope_reaches_every_resource(self, repo):
        """`_default_` is the entity-WIDE scope (#487): one call, N resources,
        every one of which needs the forced pass."""
        await repo.create_entity("fan-wide")
        for resource in ("gpt-4", "claude-3"):
            await self._seed(repo, "fan-wide", resource, [Limit.per_minute("rpm", 1000)])

        await repo.set_limits("fan-wide", [Limit.per_minute("rpm", 10)])

        for resource in ("gpt-4", "claude-3"):
            item = await self._raw(repo, "fan-wide", resource)
            assert item[bucket_attr("rpm", "cp")]["N"] == "10000", resource
            assert item[BUCKET_FIELD_VU]["N"] == "0", resource

    @pytest.mark.asyncio
    async def test_a_resource_with_its_own_config_gets_its_own_schedule(self, repo):
        """Entity(resource) outranks Entity(`_default_`), so the unscoped
        fan-out must stamp each bucket from the limits resolved for ITS
        resource — schedule included. Stamping the caller's would push the
        `_default_` schedule onto a resource that overrode it."""
        await repo.create_entity("fan-mixed")
        await repo.set_limits(
            "fan-mixed",
            [Limit.per_minute("rpm", 500).with_schedule(self.NIGHTLY)],
            resource="gpt-4",
        )
        for resource in ("gpt-4", "claude-3"):
            await self._seed(repo, "fan-mixed", resource, [Limit.per_minute("rpm", 1000)])

        await repo.set_limits(
            "fan-mixed", [Limit.per_minute("rpm", 1000).with_schedule(self.BUSINESS)]
        )

        overridden = await self._raw(repo, "fan-mixed", "gpt-4")
        assert overridden["sched"]["S"] == "1h0-6s250", "gpt-4 keeps its own schedule"
        assert overridden[BUCKET_FIELD_VU]["N"] == "0"
        inherited = await self._raw(repo, "fan-mixed", "claude-3")
        assert inherited["sched"]["S"] == "1h9-17w1-5s500", "claude-3 takes `_default_`"
        assert inherited[BUCKET_FIELD_VU]["N"] == "0"

    @pytest.mark.asyncio
    async def test_a_narrowed_per_limit_override_is_removed(self, repo):
        """Two limits diverge, then converge. Absence means "inherit the item
        default", so the override left behind by the first write keeps `tpm`
        on the superseded schedule forever. The `if scheduled:` branch has to
        REMOVE as well as SET — this is the half the plan's snippet omitted.
        """
        await repo.create_entity("fan-narrow")
        await self._seed(
            repo,
            "fan-narrow",
            "gpt-4",
            [Limit.per_minute("rpm", 1000), Limit.per_minute("tpm", 5000)],
        )

        await repo.set_limits(
            "fan-narrow",
            [
                Limit.per_minute("rpm", 1000).with_schedule(self.BUSINESS),
                Limit.per_minute("tpm", 5000).with_schedule(self.NIGHTLY),
            ],
            resource="gpt-4",
        )
        assert (await self._raw(repo, "fan-narrow", "gpt-4"))[bucket_attr("tpm", "sched")][
            "S"
        ] == "1h0-6s250"

        await repo.set_limits(
            "fan-narrow",
            [
                Limit.per_minute("rpm", 1000).with_schedule(self.BUSINESS),
                Limit.per_minute("tpm", 5000).with_schedule(self.BUSINESS),
            ],
            resource="gpt-4",
        )

        item = await self._raw(repo, "fan-narrow", "gpt-4")
        assert item["sched"]["S"] == "1h9-17w1-5s500"
        assert bucket_attr("tpm", "sched") not in item

    @pytest.mark.asyncio
    async def test_a_limit_that_loses_its_schedule_beside_one_that_keeps_it(self, repo):
        """The other direction into the same trap: the item stays scheduled,
        so the `else` branch's blanket REMOVE never runs.

        The superseded `b_tpm_sched` is replaced rather than removed (#541):
        removal would put `tpm` back on the item default, which is exactly the
        schedule it just lost. `BUCKET_SCHED_NONE` is the only value that says
        "unscheduled" without saying "inherit"."""
        await repo.create_entity("fan-drop")
        await self._seed(
            repo,
            "fan-drop",
            "gpt-4",
            [Limit.per_minute("rpm", 1000), Limit.per_minute("tpm", 5000)],
        )
        await repo.set_limits(
            "fan-drop",
            [
                Limit.per_minute("rpm", 1000).with_schedule(self.BUSINESS),
                Limit.per_minute("tpm", 5000).with_schedule(self.NIGHTLY),
            ],
            resource="gpt-4",
        )

        await repo.set_limits(
            "fan-drop",
            [
                Limit.per_minute("rpm", 1000).with_schedule(self.BUSINESS),
                Limit.per_minute("tpm", 5000),
            ],
            resource="gpt-4",
        )

        item = await self._raw(repo, "fan-drop", "gpt-4")
        assert item["sched"]["S"] == "1h9-17w1-5s500"
        assert item[bucket_attr("tpm", "sched")]["S"] == BUCKET_SCHED_NONE
        buckets = {b.limit_name: b for b in repo._deserialize_composite_bucket(item)}
        assert buckets["tpm"].sched == ()

    @pytest.mark.asyncio
    async def test_a_stale_limits_schedule_override_is_removed_with_it(self, repo):
        """A dropped limit's `b_{name}_sched` is orphan state that re-attaches
        the moment a limit of that name is configured again."""
        await repo.create_entity("fan-stale")
        await self._seed(
            repo,
            "fan-stale",
            "gpt-4",
            [Limit.per_minute("rpm", 1000), Limit.per_minute("tpm", 5000)],
        )
        await repo.set_limits(
            "fan-stale",
            [
                Limit.per_minute("rpm", 1000).with_schedule(self.BUSINESS),
                Limit.per_minute("tpm", 5000).with_schedule(self.NIGHTLY),
            ],
            resource="gpt-4",
        )

        await repo.reconcile_bucket_to_defaults(
            "fan-stale",
            "gpt-4",
            [Limit.per_minute("rpm", 1000).with_schedule(self.BUSINESS)],
            stale_limit_names={"tpm"},
        )

        item = await self._raw(repo, "fan-stale", "gpt-4")
        assert bucket_attr("tpm", "sched") not in item
        assert bucket_attr("tpm", "cp") not in item

    @pytest.mark.asyncio
    async def test_rejects_limits_that_disagree_on_timezone(self, repo):
        """`sched_tz` is one attribute per item, exactly as at bucket create."""
        await repo.create_entity("fan-tz")
        await self._seed(
            repo,
            "fan-tz",
            "gpt-4",
            [Limit.per_minute("rpm", 1000), Limit.per_minute("tpm", 5000)],
        )
        with pytest.raises(ValueError, match="share a timezone"):
            repo._build_bucket_param_update(
                [
                    Limit.per_minute("rpm", 1000).with_schedule(self.BUSINESS),
                    Limit.per_minute("tpm", 5000).with_schedule(self.BERLIN),
                ],
                None,
                None,
            )

    @pytest.mark.asyncio
    async def test_vu_is_never_set_and_removed_in_one_expression(self, repo):
        """#488: SET and REMOVE on one attribute is a ValidationException, and
        the `else` branch below is a REMOVE list `vu` must stay out of. Both
        branches are checked — moto would reject it, but the expression is the
        thing under test."""
        for limits in (
            [Limit.per_minute("rpm", 1000)],
            [Limit.per_minute("rpm", 1000).with_schedule(self.BUSINESS)],
        ):
            expr, _names, _values = repo._build_bucket_param_update(limits, 0, {"old"})
            set_clause, remove_clause = expr.split(" REMOVE ")
            assert "#vu" in set_clause
            assert "#vu" not in remove_clause

    @pytest.mark.asyncio
    async def test_no_alias_is_both_set_and_removed(self, repo):
        """The general form of the check above, over the per-limit schedule
        aliases the scheduled branch now emits on both sides."""
        expr, _names, _values = repo._build_bucket_param_update(
            [
                Limit.per_minute("rpm", 1000).with_schedule(self.BUSINESS),
                Limit.per_minute("tpm", 5000).with_schedule(self.NIGHTLY),
                Limit.per_minute("cpm", 20).with_schedule(self.BUSINESS),
            ],
            0,
            {"old"},
        )
        set_clause, remove_clause = expr.split(" REMOVE ")
        set_aliases = {part.split(" = ")[0].strip() for part in set_clause[4:].split(", ")}
        remove_aliases = {part.strip() for part in remove_clause.split(", ")}
        assert not (set_aliases & remove_aliases)

    @pytest.mark.asyncio
    async def test_a_partial_fan_out_reports_how_many_buckets_were_stamped(self, repo):
        """The config item is committed before the fan-out, so a half-applied
        table needs a progress count. `vu = 0` does not change that contract:
        the buckets past the failure keep their old params AND their old `vu`,
        and re-running the same call reconciles them."""
        from zae_limiter.exceptions import FanoutIncomplete

        await repo.create_entity("fan-partial")
        await self._seed(repo, "fan-partial", "gpt-4", [Limit.per_minute("rpm", 1000)], shards=3)

        calls = {"n": 0}
        real = repo._sync_one_bucket_shard

        async def flaky(pk, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("throttled")
            return await real(pk, *args, **kwargs)

        with patch.object(repo, "_sync_one_bucket_shard", flaky):
            with pytest.raises(FanoutIncomplete) as exc:
                await repo.set_limits(
                    "fan-partial", [Limit.per_minute("rpm", 10)], resource="gpt-4"
                )

        assert exc.value.stamped == 2


class TestAggregatorCannotRefillPastAFanOut:
    """#508: the `rf` lock alone cannot see a `_sync_bucket_params` fan-out.

    The aggregator guards `try_refill_bucket` with an optimistic lock on the
    shared `rf` timestamp so a refill computed from a stale stream image
    cannot land after another writer moved the bucket on. But the fan-out
    rewrites `cp`/`ra`/`rp`/`sched` on every shard **without touching `rf`**,
    so the lock cannot tell a pre-fan-out image from a post-fan-out one — and
    an invocation holding a pre-shrink image passes the condition and refills
    toward the old, larger capacity.

    Resolved by pinning `vu` in the aggregator's condition rather than by
    bumping `rf` in the fan-out. Since Task 13 the fan-out SETs `vu = 0` on
    **every** call, so `vu` is the marker for "the operator changed something
    here" and covers every attribute that write touches — present and future —
    for one condition term, and forfeits none of the refill accrued since the
    last stamp the way moving `rf` would. See the module docstring on
    `try_refill_bucket`.

    These run the real fan-out against moto and then the real aggregator
    against the same table, so they test the interaction rather than a
    restatement of the condition string.
    """

    @staticmethod
    def _table(repo):
        import boto3

        return boto3.resource("dynamodb", region_name="us-east-1").Table(repo.table_name)

    @staticmethod
    async def _seed(repo, entity_id, limits):
        now_ms = int(time.time() * 1000)
        states = [BucketState.from_limit(entity_id, "gpt-4", lim, now_ms) for lim in limits]
        # Drain the bucket so a refill is worth attempting at all.
        for state in states:
            state.tokens_milli = 0
        await repo.transact_write([repo.build_composite_create(entity_id, "gpt-4", states, now_ms)])
        return now_ms

    @staticmethod
    async def _raw(repo, entity_id):
        from zae_limiter import schema

        client = await repo._get_client()
        response = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(repo._namespace_id, entity_id, "gpt-4", 0)},
                "SK": {"S": schema.sk_state()},
            },
        )
        return response["Item"]

    @staticmethod
    def _image(repo, entity_id, item, vu_ms):
        from zae_limiter_aggregator.processor import BucketRefillState, LimitRefillInfo

        return BucketRefillState(
            namespace_id=repo._namespace_id,
            entity_id=entity_id,
            resource="gpt-4",
            rf_ms=int(item["rf"]["N"]),
            limits={
                "rpm": LimitRefillInfo(
                    # Above the *old* capacity, so `try_refill_bucket` gets
                    # past its "projected tokens already cover the observed
                    # consumption" threshold and genuinely attempts the write
                    # in every case below — including the pre-shrink one,
                    # which must fail on the condition rather than be skipped
                    # before it. `test_an_untouched_bucket_refills_with_no_vu`
                    # is the same image against a bucket no fan-out touched,
                    # and it writes.
                    tc_delta=2_000_000,
                    tk_milli=int(item[bucket_attr("rpm", BUCKET_FIELD_TK)]["N"]),
                    cp_milli=int(item[bucket_attr("rpm", "cp")]["N"]),
                    ra_milli=int(item[bucket_attr("rpm", "ra")]["N"]),
                    rp_ms=int(item[bucket_attr("rpm", "rp")]["N"]),
                )
            },
            vu_ms=vu_ms,
        )

    @pytest.mark.asyncio
    async def test_a_pre_shrink_image_cannot_refill_toward_the_old_capacity(self, repo):
        from zae_limiter_aggregator.processor import try_refill_bucket

        await repo.create_entity("stale-img")
        now_ms = await self._seed(repo, "stale-img", [Limit.per_minute("rpm", 1000)])
        image = self._image(repo, "stale-img", await self._raw(repo, "stale-img"), vu_ms=None)

        await repo.set_limits("stale-img", [Limit.per_minute("rpm", 10)], resource="gpt-4")

        assert try_refill_bucket(self._table(repo), image, now_ms + 60_000) is False
        item = await self._raw(repo, "stale-img")
        assert item[bucket_attr("rpm", BUCKET_FIELD_TK)]["N"] == "0", "no tokens minted"
        assert item[BUCKET_FIELD_VU]["N"] == "0", "the forced pass is still owed"

    @pytest.mark.asyncio
    async def test_a_current_image_still_refills(self, repo):
        """Discriminates the test above: the pin must not block every refill,
        only one that raced a fan-out. Same bucket, same call, image read
        *after* the fan-out instead of before."""
        from zae_limiter_aggregator.processor import try_refill_bucket

        await repo.create_entity("fresh-img")
        now_ms = await self._seed(repo, "fresh-img", [Limit.per_minute("rpm", 1000)])
        await repo.set_limits("fresh-img", [Limit.per_minute("rpm", 10)], resource="gpt-4")

        image = self._image(repo, "fresh-img", await self._raw(repo, "fresh-img"), vu_ms=0)

        assert try_refill_bucket(self._table(repo), image, now_ms + 60_000) is True
        item = await self._raw(repo, "fresh-img")
        assert item[bucket_attr("rpm", BUCKET_FIELD_TK)]["N"] == "10000", "the NEW ceiling"

    @pytest.mark.asyncio
    async def test_an_untouched_bucket_refills_with_no_vu_at_all(self, repo):
        """The steady state for the unscheduled majority: no fan-out has run,
        `vu` is absent on both the image and the item, and
        `attribute_not_exists(#vu)` must hold rather than reject."""
        from zae_limiter_aggregator.processor import try_refill_bucket

        await repo.create_entity("quiet-img")
        now_ms = await self._seed(repo, "quiet-img", [Limit.per_minute("rpm", 1000)])
        image = self._image(repo, "quiet-img", await self._raw(repo, "quiet-img"), vu_ms=None)

        assert try_refill_bucket(self._table(repo), image, now_ms + 60_000) is True
        item = await self._raw(repo, "quiet-img")
        assert item[bucket_attr("rpm", BUCKET_FIELD_TK)]["N"] == "1000000"


class TestQuotaConfigRoundTrip:
    """A quota written through `set_limits` must come back (#538).

    `Limit.quota()` shipped in #531 with no storage leg, so
    `_serialize_composite_limits` dropped `reset_schedule` and the next read
    rebuilt a `Limit` with `refill_amount=0` and no reset — which is exactly
    what `Limit.__post_init__` rejects. The config item was poisoned by the
    write and every later read raised.
    """

    QUOTA = Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York")

    async def test_entity_quota_survives_set_and_get(self, repo):
        await repo.set_limits("quota-1", [self.QUOTA], resource="gpt-4")
        (stored,) = await repo.get_limits("quota-1", resource="gpt-4")
        assert stored == self.QUOTA

    async def test_resource_quota_survives_set_and_get(self, repo):
        await repo.set_resource_defaults("gpt-4", [self.QUOTA])
        (stored,) = await repo.get_resource_defaults("gpt-4")
        assert stored == self.QUOTA

    async def test_system_quota_survives_set_and_get(self, repo):
        await repo.set_system_defaults([self.QUOTA])
        stored_limits, _on_unavailable = await repo.get_system_defaults()
        assert stored_limits == [self.QUOTA]

    async def test_resolve_limits_returns_the_reset_schedule(self, repo):
        """The read `acquire()`'s slow path actually uses. Without this leg
        `resolve_limits()` raises, and before #531 it would have silently
        returned a quota with no reset for Task 3's reset to never fire on."""
        await repo.set_limits("quota-2", [self.QUOTA], resource="gpt-4")
        limits, _on_unavailable, source = await repo.resolve_limits("quota-2", "gpt-4")
        assert source == "entity"
        assert limits == [self.QUOTA]


class TestResetScheduleReachesStorage:
    """`rsched` on config and bucket items (#222 §3.6, §4.1, surface Task 4)."""

    RESET = (ScheduleEntry.reset(cron="0 0 * * *", tz="America/New_York"),)
    QUOTA = Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York")

    @staticmethod
    async def _raw_config(repo, entity_id, resource):
        from zae_limiter import schema

        client = await repo._get_client()
        response = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_entity(repo._namespace_id, entity_id)},
                "SK": {"S": schema.sk_config(resource)},
            },
        )
        return response.get("Item") or {}

    @staticmethod
    async def _raw_bucket(repo, entity_id, resource, shard=0):
        from zae_limiter import schema

        client = await repo._get_client()
        response = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(repo._namespace_id, entity_id, resource, shard)},
                "SK": {"S": schema.sk_state()},
            },
        )
        return response.get("Item") or {}

    @staticmethod
    async def _seed_bucket(repo, entity_id, resource, limits):
        now_ms = int(time.time() * 1000)
        states = [BucketState.from_limit(entity_id, resource, lim, now_ms) for lim in limits]
        await repo.transact_write(
            [repo.build_composite_create(entity_id, resource, states, now_ms)]
        )

    # -- config item -------------------------------------------------------

    async def test_config_round_trips_the_reset_schedule(self, repo):
        """Without this leg `resolve_limits()` cannot return a quota at all."""
        await repo.set_limits("rs-1", [self.QUOTA], resource="gpt-4")
        (stored,) = await repo.get_limits("rs-1", resource="gpt-4")
        assert stored.reset_schedule == self.RESET

    async def test_config_stores_the_compact_form_under_its_own_attribute(self, repo):
        """`rsched`, not a tag inside `sched` (§4.1), and four bytes."""
        await repo.set_limits("rs-2", [self.QUOTA], resource="gpt-4")
        item = await self._raw_config(repo, "rs-2", "gpt-4")

        assert item[limit_attr("rpd", LIMIT_FIELD_RSCHED)]["S"] == "1m0h0"
        assert limit_attr("rpd", "sched") not in item
        assert item[CONFIG_FIELD_SCHED_TZ]["S"] == "America/New_York"

    async def test_a_reset_only_limit_still_writes_the_timezone(self, repo):
        """The hoisting trap: a quota carries no parameter schedule, so a
        `sched_tz` derived from `schedule` alone would be absent and the reset
        would decode as UTC — a New York midnight quota resetting at 19:00."""
        await repo.set_limits("rs-2b", [self.QUOTA], resource="gpt-4")
        (stored,) = await repo.get_limits("rs-2b", resource="gpt-4")
        assert stored.reset_schedule[0].tz == "America/New_York"

    async def test_both_tuples_on_one_limit_round_trip(self, repo):
        """§1.7: a quota may carry a parameter schedule too. Two attributes,
        one shared `sched_tz`."""
        sched = (ScheduleEntry(cron="* 0-6 * * *", tz="America/New_York", scale=0.5),)
        await repo.set_limits("rs-2c", [self.QUOTA.with_schedule(sched)], resource="gpt-4")
        item = await self._raw_config(repo, "rs-2c", "gpt-4")

        assert item[limit_attr("rpd", "sched")]["S"] == "1h0-6s500"
        assert item[limit_attr("rpd", LIMIT_FIELD_RSCHED)]["S"] == "1m0h0"
        assert item[CONFIG_FIELD_SCHED_TZ]["S"] == "America/New_York"

        (stored,) = await repo.get_limits("rs-2c", resource="gpt-4")
        assert stored.schedule == sched
        assert stored.reset_schedule == self.RESET

    async def test_two_limits_one_quota_one_drip(self, repo):
        """Many limits per item: each gets its own `l_{name}_rsched`, and the
        drip gets none."""
        await repo.set_limits("rs-2d", [Limit.per_minute("rpm", 100), self.QUOTA], resource="gpt-4")
        item = await self._raw_config(repo, "rs-2d", "gpt-4")
        assert item[limit_attr("rpd", LIMIT_FIELD_RSCHED)]["S"] == "1m0h0"
        assert limit_attr("rpm", LIMIT_FIELD_RSCHED) not in item

        by_name = {lim.name: lim for lim in await repo.get_limits("rs-2d", resource="gpt-4")}
        assert by_name["rpd"].reset_schedule == self.RESET
        assert by_name["rpm"].reset_schedule == ()

    async def test_many_entries_in_one_reset_tuple_round_trip(self, repo):
        many = (
            ScheduleEntry.reset("0 0 1 * *", "America/New_York"),
            ScheduleEntry.reset("0 12 15 * *", "America/New_York"),
        )
        await repo.set_limits("rs-2e", [self.QUOTA.with_reset_schedule(many)], resource="gpt-4")
        item = await self._raw_config(repo, "rs-2e", "gpt-4")
        assert item[limit_attr("rpd", LIMIT_FIELD_RSCHED)]["S"] == "1m0h0D1;m0h12D15"

        (stored,) = await repo.get_limits("rs-2e", resource="gpt-4")
        assert stored.reset_schedule == many

    async def test_replacing_a_quota_with_a_drip_removes_it(self, repo):
        """All three setters are full-replace PutItems, so an omitted attribute
        disappears — confirmed, not assumed."""
        await repo.set_limits("rs-3", [self.QUOTA], resource="gpt-4")
        await repo.set_limits("rs-3", [Limit.per_day("rpd", 10_000)], resource="gpt-4")

        (stored,) = await repo.get_limits("rs-3", resource="gpt-4")
        assert stored.reset_schedule == ()
        assert LIMIT_FIELD_RSCHED not in str(await self._raw_config(repo, "rs-3", "gpt-4"))

    async def test_resource_and_system_levels_round_trip_too(self, repo):
        """All three config levels share one serialiser, but only one of them
        is exercised by the entity tests above."""
        await repo.set_resource_defaults("gpt-4", [self.QUOTA])
        await repo.set_system_defaults([self.QUOTA])

        (from_resource,) = await repo.get_resource_defaults("gpt-4")
        system_limits, _on_unavailable = await repo.get_system_defaults()
        assert from_resource.reset_schedule == self.RESET
        assert system_limits[0].reset_schedule == self.RESET

    # -- bucket items ------------------------------------------------------

    async def test_a_created_bucket_carries_the_stamp(self, repo):
        """Both refillers read the schedules off the item, so a quota bucket
        born without `rsched` has `refill_amount = 0` and nothing to restore
        it — the one shape that can never recover."""
        await self._seed_bucket(repo, "rs-6", "gpt-4", [self.QUOTA])
        item = await self._raw_bucket(repo, "rs-6", "gpt-4")

        assert item[BUCKET_FIELD_RSCHED]["S"] == "1m0h0"
        assert item[BUCKET_FIELD_SCHED_TZ]["S"] == "America/New_York"
        assert BUCKET_FIELD_SCHED not in item, "a quota carries no parameter schedule"

    async def test_a_created_bucket_carries_both_stamps(self, repo):
        sched = (ScheduleEntry(cron="* 0-6 * * *", tz="America/New_York", scale=0.5),)
        await self._seed_bucket(repo, "rs-6b", "gpt-4", [self.QUOTA.with_schedule(sched)])
        item = await self._raw_bucket(repo, "rs-6b", "gpt-4")

        assert item[BUCKET_FIELD_SCHED]["S"] == "1h0-6s500"
        assert item[BUCKET_FIELD_RSCHED]["S"] == "1m0h0"
        assert item[BUCKET_FIELD_SCHED_TZ]["S"] == "America/New_York"

    async def test_a_created_bucket_writes_a_per_limit_override(self, repo):
        """Two limits resetting on different calendars: the first becomes the
        item default and the second gets `b_{name}_rsched` (§4.1)."""
        weekly = Limit.quota("rpw", 50_000, cron="0 0 * * SUN", tz="America/New_York")
        await self._seed_bucket(repo, "rs-6c", "gpt-4", [self.QUOTA, weekly])
        item = await self._raw_bucket(repo, "rs-6c", "gpt-4")

        assert item[BUCKET_FIELD_RSCHED]["S"] == "1m0h0"
        assert item[bucket_attr("rpw", BUCKET_FIELD_RSCHED)]["S"] == "1m0h0w7"
        assert bucket_attr("rpd", BUCKET_FIELD_RSCHED) not in item

    async def test_an_unscheduled_bucket_carries_neither(self, repo):
        await self._seed_bucket(repo, "rs-6d", "gpt-4", [Limit.per_minute("rpm", 100)])
        item = await self._raw_bucket(repo, "rs-6d", "gpt-4")
        assert BUCKET_FIELD_RSCHED not in item
        assert BUCKET_FIELD_SCHED_TZ not in item

    async def test_the_fan_out_stamps_rsched_on_an_existing_bucket(self, repo):
        await repo.create_entity("rs-4")
        await repo.set_limits("rs-4", [Limit.per_day("rpd", 10_000)], resource="gpt-4")
        await self._seed_bucket(repo, "rs-4", "gpt-4", [Limit.per_day("rpd", 10_000)])

        await repo.set_limits("rs-4", [self.QUOTA], resource="gpt-4")

        item = await self._raw_bucket(repo, "rs-4", "gpt-4")
        assert item[BUCKET_FIELD_RSCHED]["S"] == "1m0h0"
        assert item[BUCKET_FIELD_SCHED_TZ]["S"] == "America/New_York"

    async def test_removing_a_reset_schedule_removes_the_stamp(self, repo):
        """Override, not merge (§1.6): dropping a reset must clear the item, or
        the bucket keeps resetting after the operator stopped asking it to."""
        await repo.create_entity("rs-5")
        await repo.set_limits("rs-5", [self.QUOTA], resource="gpt-4")
        await self._seed_bucket(repo, "rs-5", "gpt-4", [self.QUOTA])

        await repo.set_limits("rs-5", [Limit.per_day("rpd", 10_000)], resource="gpt-4")

        item = await self._raw_bucket(repo, "rs-5", "gpt-4")
        assert BUCKET_FIELD_RSCHED not in item
        assert BUCKET_FIELD_SCHED_TZ not in item

    async def test_a_reset_only_fan_out_still_writes_sched_tz(self, repo):
        """The #488 trap this task's restructure exists for. `sched_tz` is
        shared by both tuples, so deciding it from the parameter branch alone
        would REMOVE it here while `rsched` was SET — leaving the stored reset
        to decode as UTC, or raising a ValidationException outright."""
        await repo.create_entity("rs-5b")
        await repo.set_limits("rs-5b", [Limit.per_day("rpd", 10_000)], resource="gpt-4")
        await self._seed_bucket(repo, "rs-5b", "gpt-4", [Limit.per_day("rpd", 10_000)])

        expr, names, _values = repo._build_bucket_param_update([self.QUOTA], None, None)
        set_clause = expr.split("REMOVE")[0]
        remove_clause = expr.split("REMOVE")[1] if "REMOVE" in expr else ""
        tz_alias = next(alias for alias, attr in names.items() if attr == BUCKET_FIELD_SCHED_TZ)
        assert tz_alias in set_clause
        assert tz_alias not in remove_clause

        await repo.set_limits("rs-5b", [self.QUOTA], resource="gpt-4")
        item = await self._raw_bucket(repo, "rs-5b", "gpt-4")
        assert item[BUCKET_FIELD_SCHED_TZ]["S"] == "America/New_York"

    async def test_no_alias_is_both_set_and_removed(self, repo):
        """#488: SET and REMOVE on one attribute in one expression is a
        ValidationException. Checked across all four scheduled/unscheduled
        combinations of the two tuples, since the aliases are shared."""
        sched = (ScheduleEntry(cron="* 0-6 * * *", tz="America/New_York", scale=0.5),)
        for limits in (
            [Limit.per_minute("rpm", 100)],
            [Limit.per_minute("rpm", 100).with_schedule(sched)],
            [self.QUOTA],
            [self.QUOTA.with_schedule(sched)],
        ):
            expr, _names, _values = repo._build_bucket_param_update(limits, None, {"gone"})
            set_aliases = {
                part.split("=")[0].strip()
                for part in expr.split("REMOVE")[0].removeprefix("SET").split(",")
            }
            remove_aliases = {part.strip() for part in expr.split("REMOVE")[1].split(",")}
            assert not (set_aliases & remove_aliases), (
                f"{limits[0].name}: {set_aliases & remove_aliases}"
            )


class TestDurationWindowReachesConfigStorage:
    """`l_{name}_rsa` — a duration quota's window length (ADR-139, plan Task 4).

    The alternative spelling of the reset half: a window anchored to the
    entity's own first use rather than a calendar instant. Mirrors
    `TestResetScheduleReachesStorage`, scoped to config items only — bucket
    items are Task 3's concern.
    """

    WINDOW = Limit.quota("session", 10_000, reset_after=timedelta(hours=5))

    @staticmethod
    async def _raw_config(repo, entity_id, resource):
        from zae_limiter import schema

        client = await repo._get_client()
        response = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_entity(repo._namespace_id, entity_id)},
                "SK": {"S": schema.sk_config(resource)},
            },
        )
        return response.get("Item") or {}

    async def test_entity_limits_round_trip_a_duration_window(self, repo):
        await repo.set_limits("dw-1", [self.WINDOW], resource="gpt-4")
        await repo.invalidate_config_cache()

        stored = await repo.get_limits("dw-1", resource="gpt-4")
        assert stored == [self.WINDOW]
        assert stored[0].reset_after == timedelta(hours=5)

        resolved, _on_unavailable, source = await repo.resolve_limits("dw-1", "gpt-4")
        assert resolved is not None
        assert resolved[0].reset_after == timedelta(hours=5)
        assert source == "entity"

    async def test_the_window_is_stored_under_its_own_attribute(self, repo):
        """Seconds, not the `timedelta` — the field name carries no unit, so
        storage has to spell it (`l_{name}_rsa`), and it is a sibling of
        `rsched`, not a tag inside it (a quota has one or the other, ADR-139)."""
        await repo.set_limits("dw-2", [self.WINDOW], resource="gpt-4")
        item = await self._raw_config(repo, "dw-2", "gpt-4")

        assert item[limit_attr("session", LIMIT_FIELD_RSA)]["N"] == "18000"
        assert limit_attr("session", LIMIT_FIELD_RSCHED) not in item

    async def test_rewriting_a_limit_without_a_window_drops_it(self, repo):
        """Config storage is override-not-merge (full-replace PutItem), so this
        needs no explicit REMOVE — the same property `sched` relies on."""
        await repo.set_limits("dw-3", [self.WINDOW], resource="gpt-4")
        await repo.set_limits("dw-3", [Limit.per_minute("session", 100)], resource="gpt-4")
        await repo.invalidate_config_cache()

        stored = await repo.get_limits("dw-3", resource="gpt-4")
        assert stored[0].reset_after is None
        assert stored[0].is_quota is False

    async def test_resource_and_system_levels_round_trip_too(self, repo):
        """All three config levels share one serialiser, but only one of them
        is exercised by the entity tests above."""
        await repo.set_resource_defaults("gpt-4", [self.WINDOW])
        await repo.set_system_defaults([self.WINDOW])

        (from_resource,) = await repo.get_resource_defaults("gpt-4")
        system_limits, _on_unavailable = await repo.get_system_defaults()
        assert from_resource.reset_after == timedelta(hours=5)
        assert system_limits[0].reset_after == timedelta(hours=5)


class TestDeserialisedBucketsCarryBothSchedules:
    """`_deserialize_composite_bucket` reads `sched` / `rsched` off the item.

    Every `BucketState` the client builds from a stored item comes from here,
    including the `ALL_OLD` / `ALL_NEW` images behind the speculative path. An
    empty `sched` there makes every schedule-aware number computed from a
    `BucketState` — the refill ceiling, `Limit.from_bucket_state`, the
    rejection's `LimitStatus` — silently flat inside a window that has already
    changed the parameters.

    The slow path overwrites `state.sched` from the config it just resolved
    (`_do_acquire`), which is the fresher of the two; these tests are about the
    paths that have no config in hand.
    """

    NIGHT = (ScheduleEntry(cron="* 0-6 * * *", tz="America/New_York", scale=0.5),)
    RESET = (ScheduleEntry.reset(cron="0 0 * * *", tz="America/New_York"),)
    QUOTA = Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York")

    @staticmethod
    async def _seed(repo, entity_id, resource, limits):
        """Write a real bucket item through the real create path."""
        now_ms = int(time.time() * 1000)
        states = [BucketState.from_limit(entity_id, resource, lim, now_ms) for lim in limits]
        await repo.transact_write(
            [repo.build_composite_create(entity_id, resource, states, now_ms)]
        )

    async def test_the_parameter_schedule_reaches_bucket_state(self, repo):
        await repo.create_entity("ds-1")
        limit = Limit.per_minute("rpm", 1000).with_schedule(self.NIGHT)
        await self._seed(repo, "ds-1", "gpt-4", [limit])

        (bucket,) = [
            b for b in await repo.get_buckets("ds-1", resource="gpt-4") if b.limit_name == "rpm"
        ]
        assert bucket.sched == self.NIGHT

    async def test_the_reset_schedule_reaches_bucket_state(self, repo):
        """The second tuple decodes independently of the first, and a quota
        carries only this one."""
        await repo.create_entity("ds-2")
        await self._seed(repo, "ds-2", "gpt-4", [self.QUOTA])

        (bucket,) = [
            b for b in await repo.get_buckets("ds-2", resource="gpt-4") if b.limit_name == "rpd"
        ]
        assert bucket.reset_sched == self.RESET
        assert bucket.sched == ()

    async def test_both_tuples_on_one_limit(self, repo):
        await repo.create_entity("ds-3")
        await self._seed(repo, "ds-3", "gpt-4", [self.QUOTA.with_schedule(self.NIGHT)])

        (bucket,) = [
            b for b in await repo.get_buckets("ds-3", resource="gpt-4") if b.limit_name == "rpd"
        ]
        assert bucket.sched == self.NIGHT
        assert bucket.reset_sched == self.RESET

    async def test_a_per_limit_override_beats_the_item_default(self, repo):
        """Absence means "inherit"; `b_{name}_sched` is written only where a
        limit differs from the item default. Reading the default for every
        limit would refill the overridden one at the wrong rate — over-refilling
        whenever the override is the tighter of the two."""
        tighter = (ScheduleEntry(cron="* 0-6 * * *", tz="America/New_York", scale=0.25),)
        await repo.create_entity("ds-4")
        await self._seed(
            repo,
            "ds-4",
            "gpt-4",
            [
                Limit.per_minute("rpm", 1000).with_schedule(self.NIGHT),
                Limit.per_minute("tpm", 100_000).with_schedule(tighter),
            ],
        )

        by_name = {b.limit_name: b for b in await repo.get_buckets("ds-4", resource="gpt-4")}
        assert by_name["rpm"].sched == self.NIGHT
        assert by_name["tpm"].sched == tighter

    async def test_a_per_limit_reset_override_beats_the_item_default(self, repo):
        # `7`, not `SUN`: the compact form normalises names to numbers so that
        # re-encoding a decoded schedule is byte-identical (`schedule.encode`).
        weekly = (ScheduleEntry.reset(cron="0 0 * * 7", tz="America/New_York"),)
        await repo.create_entity("ds-5")
        await self._seed(
            repo,
            "ds-5",
            "gpt-4",
            [self.QUOTA, Limit.quota("rpw", 50_000, cron="0 0 * * SUN", tz="America/New_York")],
        )

        by_name = {b.limit_name: b for b in await repo.get_buckets("ds-5", resource="gpt-4")}
        assert by_name["rpd"].reset_sched == self.RESET
        assert by_name["rpw"].reset_sched == weekly

    async def test_an_unscheduled_item_yields_empty_tuples(self, repo):
        """Discriminates against "always return the item default": every bucket
        written before scheduling existed must still deserialise to `()`, not
        to a UTC schedule invented from a missing attribute."""
        await repo.create_entity("ds-6")
        await self._seed(repo, "ds-6", "gpt-4", [Limit.per_minute("rpm", 1000)])

        for bucket in await repo.get_buckets("ds-6", resource="gpt-4"):
            assert bucket.sched == ()
            assert bucket.reset_sched == ()

    async def test_wcu_never_carries_the_item_schedule(self, repo):
        """`wcu` tracks partition write pressure, not a user limit. Scaling it
        by a 0.5x window would halve the write ceiling on exactly the hot
        buckets sharding exists to protect (#519 is the aggregator's version of
        that bug). The user limit on the same item still gets it, so this
        discriminates against "never attach a schedule at all"."""
        await repo.create_entity("ds-7")
        await self._seed(
            repo, "ds-7", "gpt-4", [Limit.per_minute("rpm", 1000).with_schedule(self.NIGHT)]
        )

        buckets = repo._deserialize_composite_bucket(await self._raw_item(repo, "ds-7", "gpt-4"))
        by_name = {b.limit_name: b for b in buckets}
        assert by_name[WCU_LIMIT_NAME].sched == ()
        assert by_name[WCU_LIMIT_NAME].reset_sched == ()
        assert by_name["rpm"].sched == self.NIGHT

    async def test_an_unscheduled_limit_does_not_inherit_the_item_default(self, repo):
        """#541. "Unscheduled" and "same as the default" used to be the same
        byte pattern — absence — so this reader gave an unscheduled limit a
        window it never declared. The writer now spells the first case
        ``BUCKET_SCHED_NONE``; this reader honours it, and the aggregator's
        parser applies the identical rule."""
        await repo.create_entity("ds-8")
        await self._seed(
            repo,
            "ds-8",
            "gpt-4",
            [Limit.per_minute("rpm", 1000).with_schedule(self.NIGHT), Limit.per_minute("tpm", 10)],
        )

        by_name = {b.limit_name: b for b in await repo.get_buckets("ds-8", resource="gpt-4")}
        assert by_name["tpm"].sched == ()
        # The scheduled limit on the same item still gets it, so this does not
        # pass by never attaching a schedule at all.
        assert by_name["rpm"].sched == self.NIGHT

    async def test_the_speculative_failure_image_carries_the_schedule(self, repo):
        """The path that matters most: a fast rejection builds its statuses
        from these states and has no config in hand."""
        await repo.create_entity("ds-9")
        limit = Limit.per_minute("rpm", 10).with_schedule(self.NIGHT)
        await self._seed(repo, "ds-9", "gpt-4", [limit])

        result = await repo.speculative_consume("ds-9", "gpt-4", {"rpm": 5_000}, shard_id=0)
        assert result.success is False
        by_name = {b.limit_name: b for b in result.old_buckets}
        assert by_name["rpm"].sched == self.NIGHT

    async def test_the_speculative_success_image_carries_the_schedule(self, repo):
        await repo.create_entity("ds-10")
        limit = Limit.per_minute("rpm", 1000).with_schedule(self.NIGHT)
        await self._seed(repo, "ds-10", "gpt-4", [limit])

        result = await repo.speculative_consume("ds-10", "gpt-4", {"rpm": 1}, shard_id=0)
        assert result.success is True
        by_name = {b.limit_name: b for b in result.buckets}
        assert by_name["rpm"].sched == self.NIGHT

    async def test_the_timezone_comes_from_the_item_not_utc(self, repo):
        """`sched_tz` is hoisted once per item. Defaulting to UTC when it is
        present would shift every window by the offset — silent, and wrong by
        five hours here."""
        await repo.create_entity("ds-11")
        await self._seed(
            repo, "ds-11", "gpt-4", [Limit.per_minute("rpm", 1000).with_schedule(self.NIGHT)]
        )

        by_name = {b.limit_name: b for b in await repo.get_buckets("ds-11", resource="gpt-4")}
        assert by_name["rpm"].sched[0].tz == "America/New_York"

    @staticmethod
    async def _raw_item(repo, entity_id, resource, shard=0):
        from zae_limiter import schema

        client = await repo._get_client()
        response = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(repo._namespace_id, entity_id, resource, shard)},
                "SK": {"S": schema.sk_state()},
            },
        )
        return response.get("Item") or {}


async def _corrupt_config_sched(repo, entity_id, resource, limit_name, value, field="sched"):
    """Overwrite one stored schedule attribute with an undecodable string.

    No public API can produce one — `set_limits` encodes from a validated
    `Limit` — so the only way to reach the read path's failure branch is to
    write the attribute directly, exactly as a newer client or a corrupted
    write would leave it.
    """
    from zae_limiter import schema

    client = await repo._get_client()
    await client.update_item(
        TableName=repo.table_name,
        Key={
            "PK": {"S": schema.pk_entity(repo._namespace_id, entity_id)},
            "SK": {"S": schema.sk_config(resource)},
        },
        UpdateExpression="SET #a = :v",
        ExpressionAttributeNames={"#a": limit_attr(limit_name, field)},
        ExpressionAttributeValues={":v": {"S": value}},
    )


async def _corrupt_config_rsa(repo, entity_id, resource, limit_name, value):
    """Overwrite `l_{name}_rsa` with a value `Limit.__post_init__` rejects.

    Mirrors `_corrupt_config_sched`, but `rsa` has no grammar to fail
    decoding — a plain `int()` on a DynamoDB `N` cannot realistically fail —
    so the realistic corruption is a stored value the constructor itself
    rejects (zero, negative, or over `MAX_PERIOD_SECONDS`).
    """
    from zae_limiter import schema

    client = await repo._get_client()
    await client.update_item(
        TableName=repo.table_name,
        Key={
            "PK": {"S": schema.pk_entity(repo._namespace_id, entity_id)},
            "SK": {"S": schema.sk_config(resource)},
        },
        UpdateExpression="SET #a = :v",
        ExpressionAttributeNames={"#a": limit_attr(limit_name, LIMIT_FIELD_RSA)},
        ExpressionAttributeValues={":v": {"N": str(value)}},
    )


async def _corrupt_bucket_sched(
    repo, entity_id, resource, value, shard=0, field=BUCKET_FIELD_SCHED
):
    from zae_limiter import schema

    client = await repo._get_client()
    await client.update_item(
        TableName=repo.table_name,
        Key={
            "PK": {"S": schema.pk_bucket(repo._namespace_id, entity_id, resource, shard)},
            "SK": {"S": schema.sk_state()},
        },
        UpdateExpression="SET #a = :v",
        ExpressionAttributeNames={"#a": field},
        ExpressionAttributeValues={":v": {"S": value}},
    )


class TestUnreadableStoredSchedule:
    """A schedule the client cannot read makes the limiter unavailable (#222 §6).

    Not "no schedule": that runs at the *base* limit, so a parse error would
    double a customer's limit when the schedule said 0.5x, and with `vu` left
    expired it would pin the bucket to the slow path forever.
    """

    # `1-5`, not `MON-FRI`: the compact storage form normalises names to
    # numbers, so this is what a round trip returns (`schedule.encode`).
    BUSINESS = (ScheduleEntry(cron="* 9-17 * * 1-5", tz="America/New_York", scale=0.5),)
    QUOTA = Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York")

    async def _seed(self, repo, entity_id, limits=None):
        await repo.set_limits(
            entity_id,
            limits or [Limit.per_minute("rpm", 1000).with_schedule(self.BUSINESS)],
            resource="gpt-4",
        )

    async def _seed_bucket(self, repo, entity_id, limits=None):
        """Create the bucket item through the real create path.

        `speculative_consume` is a conditional UpdateItem on an item that must
        already exist, so a bucket-side test has to create one first.
        """
        limits = limits or [Limit.per_minute("rpm", 1000).with_schedule(self.BUSINESS)]
        await repo.create_entity(entity_id)
        await self._seed(repo, entity_id, limits)
        now_ms = int(time.time() * 1000)
        states = [BucketState.from_limit(entity_id, "gpt-4", lim, now_ms) for lim in limits]
        await repo.transact_write([repo.build_composite_create(entity_id, "gpt-4", states, now_ms)])

    async def test_get_limits_raises_unavailable(self, repo):
        await self._seed(repo, "corrupt-1")
        await _corrupt_config_sched(repo, "corrupt-1", "gpt-4", "rpm", "not-a-schedule")
        await repo.invalidate_config_cache()

        with pytest.raises(RateLimiterUnavailable, match="schedule"):
            await repo.get_limits("corrupt-1", resource="gpt-4")

    async def test_it_is_not_a_bare_value_error(self, repo):
        """The whole point of the conversion: `acquire()`'s handler re-raises
        `ValidationError` and friends but routes `RateLimiterUnavailable`
        through `on_unavailable`. A `ValueError` would escape uncontrolled."""
        await self._seed(repo, "corrupt-1b")
        await _corrupt_config_sched(repo, "corrupt-1b", "gpt-4", "rpm", "not-a-schedule")
        await repo.invalidate_config_cache()

        with pytest.raises(RateLimiterUnavailable) as excinfo:
            await repo.get_limits("corrupt-1b", resource="gpt-4")
        assert not isinstance(excinfo.value, ValueError)

    async def test_the_message_names_the_attribute_and_the_value(self, repo):
        """An operator debugging a mixed-version fleet has only this line, now
        that no version marker is being added (#515)."""
        await self._seed(repo, "corrupt-2")
        await _corrupt_config_sched(repo, "corrupt-2", "gpt-4", "rpm", "q42h9-17")
        await repo.invalidate_config_cache()

        with pytest.raises(RateLimiterUnavailable) as excinfo:
            await repo.get_limits("corrupt-2", resource="gpt-4")
        message = str(excinfo.value)
        assert "l_rpm_sched" in message
        assert "q42h9-17" in message
        assert "America/New_York" in message, "the zone the entries were decoded in"
        assert isinstance(excinfo.value.cause, ValueError)

    async def test_resolve_limits_raises_too(self, repo):
        """`resolve_limits` is what `acquire()`'s slow path calls; if only
        `get_limits` converted, the path that matters would still surface a
        bare ValueError. It reaches the item through `batch_get_configs`, a
        different call site from `get_limits`."""
        await self._seed(repo, "corrupt-3")
        await _corrupt_config_sched(repo, "corrupt-3", "gpt-4", "rpm", "not-a-schedule")
        await repo.invalidate_config_cache()

        with pytest.raises(RateLimiterUnavailable):
            await repo.resolve_limits("corrupt-3", "gpt-4")

    async def test_an_unreadable_reset_schedule_raises_the_same_way(self, repo):
        """The two tuples decode independently, so `rsched` needs its own
        coverage — and a quota has no `sched` at all to fail first."""
        await self._seed(repo, "corrupt-4", [self.QUOTA])
        await _corrupt_config_sched(repo, "corrupt-4", "gpt-4", "rpd", "zzz", field="rsched")
        await repo.invalidate_config_cache()

        with pytest.raises(RateLimiterUnavailable, match="l_rpd_rsched"):
            await repo.get_limits("corrupt-4", resource="gpt-4")

    async def test_a_modifier_smuggled_into_a_reset_attribute_raises(self, repo):
        """`decode_reset` rejects a modifier rather than ignoring it — an entry
        that silently reset a balance on a schedule meant only to scale it is
        the worst available reading. That rejection must reach the caller as
        unavailability, not as a `ValueError`."""
        await self._seed(repo, "corrupt-4b", [self.QUOTA])
        await _corrupt_config_sched(repo, "corrupt-4b", "gpt-4", "rpd", "1m0h0s500", field="rsched")
        await repo.invalidate_config_cache()

        with pytest.raises(RateLimiterUnavailable, match="overrides no parameters"):
            await repo.get_limits("corrupt-4b", resource="gpt-4")

    async def test_a_corrupt_timezone_raises(self, repo):
        """`sched_tz` is hoisted once per item, so one bad value takes every
        scheduled limit on it down. It fails inside `parse_cron`, which wraps
        `ZoneInfoNotFoundError` — a `KeyError` subclass — into a `ValueError`;
        without that wrapping this guard would not catch it at all."""
        from zae_limiter import schema

        await self._seed(repo, "corrupt-4c")
        client = await repo._get_client()
        await client.update_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_entity(repo._namespace_id, "corrupt-4c")},
                "SK": {"S": schema.sk_config("gpt-4")},
            },
            UpdateExpression="SET #a = :v",
            ExpressionAttributeNames={"#a": CONFIG_FIELD_SCHED_TZ},
            ExpressionAttributeValues={":v": {"S": "Mars/Olympus_Mons"}},
        )
        await repo.invalidate_config_cache()

        with pytest.raises(RateLimiterUnavailable, match="Mars/Olympus_Mons"):
            await repo.get_limits("corrupt-4c", resource="gpt-4")

    async def test_one_corrupt_limit_fails_the_whole_item(self, repo):
        """Item granularity, deliberately. Returning the readable limits would
        drop the unreadable one from the level, and precedence is per *level*:
        no lower level would supply it, so it would go unenforced — worse than
        the over-admission this guard exists to prevent. Both other readers
        already fail at item granularity."""
        await self._seed(
            repo,
            "corrupt-4d",
            [
                Limit.per_minute("rpm", 1000).with_schedule(self.BUSINESS),
                Limit.per_minute("tpm", 100_000),
            ],
        )
        await _corrupt_config_sched(repo, "corrupt-4d", "gpt-4", "rpm", "not-a-schedule")
        await repo.invalidate_config_cache()

        with pytest.raises(RateLimiterUnavailable):
            await repo.get_limits("corrupt-4d", resource="gpt-4")

    async def test_a_stored_reset_beside_a_positive_rate_raises_unavailable(self, repo):
        """The failure a decode guard alone would miss: both attributes parse,
        and `Limit.__post_init__` then rejects the combination (ADR-137, never
        both). Same class — the stored schedule leaves the limit
        undeterminable — so it converts the same way."""
        await self._seed(repo, "corrupt-4e", [Limit.per_minute("rpm", 1000)])
        await _corrupt_config_sched(repo, "corrupt-4e", "gpt-4", "rpm", "1m0h0", field="rsched")
        await repo.invalidate_config_cache()

        with pytest.raises(RateLimiterUnavailable, match="cannot be reconstructed"):
            await repo.get_limits("corrupt-4e", resource="gpt-4")

    async def test_a_corrupt_duration_window_raises_unavailable(self, repo):
        """`rsa` has no grammar of its own — a bare `N` — but a stored value
        `Limit.__post_init__` rejects outright (zero here) must still convert
        exactly like a schedule that fails to parse or a reset stored beside
        a positive rate, for the same reason: silently reading "no window"
        would run the limit as an unbounded drip at its base rate."""
        window = Limit.quota("session", 10_000, reset_after=timedelta(hours=5))
        await self._seed(repo, "corrupt-4g", [window])
        await _corrupt_config_rsa(repo, "corrupt-4g", "gpt-4", "session", 0)
        await repo.invalidate_config_cache()

        with pytest.raises(RateLimiterUnavailable) as excinfo:
            await repo.get_limits("corrupt-4g", resource="gpt-4")
        message = str(excinfo.value)
        assert "l_session_rsa" in message
        assert "positive whole number of seconds" in message
        assert isinstance(excinfo.value.cause, ValueError)

    async def test_a_negative_duration_window_raises_unavailable_too(self, repo):
        window = Limit.quota("session", 10_000, reset_after=timedelta(hours=5))
        await self._seed(repo, "corrupt-4h", [window])
        await _corrupt_config_rsa(repo, "corrupt-4h", "gpt-4", "session", -5)
        await repo.invalidate_config_cache()

        with pytest.raises(RateLimiterUnavailable, match="l_session_rsa"):
            await repo.get_limits("corrupt-4h", resource="gpt-4")

    async def test_a_non_integral_duration_window_raises_unavailable_too(self, repo):
        """A DynamoDB `N` legally holds `"1.5"` — `rsa` has no grammar to
        reject it before `int()` runs, so this is a distinct failure mode
        from the value-range checks above: the parse itself raises, inside
        the guarded region, before `Limit.__post_init__` is ever reached
        (#621 — a fix round found this escaping as a bare `ValueError`)."""
        window = Limit.quota("session", 10_000, reset_after=timedelta(hours=5))
        await self._seed(repo, "corrupt-4i", [window])
        await _corrupt_config_rsa(repo, "corrupt-4i", "gpt-4", "session", "1.5")
        await repo.invalidate_config_cache()

        with pytest.raises(RateLimiterUnavailable) as excinfo:
            await repo.get_limits("corrupt-4i", resource="gpt-4")
        assert not isinstance(excinfo.value, ValueError)
        message = str(excinfo.value)
        assert "l_session_rsa" in message
        assert "1.5" in message
        assert isinstance(excinfo.value.cause, ValueError)

    async def test_an_unscheduled_limit_that_will_not_reconstruct_still_raises_value_error(
        self, repo
    ):
        """The scoping decision, from the other side.

        `#538`'s shape — a stored zero rate with no reset — is a config item
        the client cannot turn into a `Limit` either, but no schedule decides
        it, so it keeps raising the `ValueError` it always has. Widening the
        conversion to every validation failure in the read path would make the
        exception type say less, not more.
        """
        from zae_limiter import schema

        await self._seed(repo, "corrupt-4f", [Limit.per_minute("rpm", 1000)])
        client = await repo._get_client()
        await client.update_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_entity(repo._namespace_id, "corrupt-4f")},
                "SK": {"S": schema.sk_config("gpt-4")},
            },
            UpdateExpression="SET #a = :v",
            ExpressionAttributeNames={"#a": limit_attr("rpm", "ra")},
            ExpressionAttributeValues={":v": {"N": "0"}},
        )
        await repo.invalidate_config_cache()

        with pytest.raises(ValueError, match="reset_schedule") as excinfo:
            await repo.get_limits("corrupt-4f", resource="gpt-4")
        assert not isinstance(excinfo.value, RateLimiterUnavailable)

    async def test_an_unreadable_bucket_schedule_raises(self, repo):
        """`_deserialize_composite_bucket` is the other decode site, and it is
        the one behind the speculative path's ALL_OLD / ALL_NEW states."""
        await self._seed_bucket(repo, "corrupt-5")
        await _corrupt_bucket_sched(repo, "corrupt-5", "gpt-4", "not-a-schedule")

        with pytest.raises(RateLimiterUnavailable, match="sched"):
            await repo.get_buckets("corrupt-5", resource="gpt-4")

    async def test_an_unreadable_bucket_reset_schedule_raises(self, repo):
        await self._seed_bucket(repo, "corrupt-5b", [self.QUOTA])
        await _corrupt_bucket_sched(repo, "corrupt-5b", "gpt-4", "zzz", field=BUCKET_FIELD_RSCHED)

        with pytest.raises(RateLimiterUnavailable, match="rsched"):
            await repo.get_buckets("corrupt-5b", resource="gpt-4")

    async def test_a_corrupt_per_limit_override_names_its_own_attribute(self, repo):
        """The item default and a per-limit override are different attributes
        to repair, so the message must distinguish them."""
        await self._seed_bucket(repo, "corrupt-5c")
        await _corrupt_bucket_sched(
            repo, "corrupt-5c", "gpt-4", "not-a-schedule", field=bucket_attr("rpm", "sched")
        )

        with pytest.raises(RateLimiterUnavailable, match=bucket_attr("rpm", "sched")):
            await repo.get_buckets("corrupt-5c", resource="gpt-4")

    async def test_the_speculative_failure_image_converts_too(self, repo):
        """A fast rejection deserialises the ALL_OLD image; an unconverted
        ValueError would escape `acquire()`'s handler from inside the fast
        path, where no config was ever read."""
        await self._seed_bucket(
            repo, "corrupt-6", [Limit.per_minute("rpm", 1).with_schedule(self.BUSINESS)]
        )
        await _corrupt_bucket_sched(repo, "corrupt-6", "gpt-4", "not-a-schedule")

        with pytest.raises(RateLimiterUnavailable):
            await repo.speculative_consume("corrupt-6", "gpt-4", {"rpm": 5_000}, shard_id=0)

    async def test_an_unscheduled_limit_still_reads_normally(self, repo):
        """The guard must not turn every ValueError in the read path into an
        infrastructure error — only a schedule decides this."""
        await repo.set_limits("plain-1", [Limit.per_minute("rpm", 1000)], resource="gpt-4")
        (stored,) = await repo.get_limits("plain-1", resource="gpt-4")
        assert stored.capacity == 1000

    async def test_an_unscheduled_bucket_still_reads_normally(self, repo):
        await self._seed_bucket(repo, "plain-2", [Limit.per_minute("rpm", 1000)])
        buckets = await repo.get_buckets("plain-2", resource="gpt-4")
        assert [b.limit_name for b in buckets] == ["rpm"]

    async def test_a_readable_schedule_still_reads_normally(self, repo):
        """Discriminates every test above against "raise whenever scheduled"."""
        await self._seed(repo, "plain-3")
        (stored,) = await repo.get_limits("plain-3", resource="gpt-4")
        assert stored.schedule == self.BUSINESS


class TestAMixedItemAttributesEachScheduleToItsOwnLimit:
    """#541: a limit with no schedule of its own must not inherit the item's.

    One bucket item carries one hoisted ``sched`` / ``rsched`` pair plus the
    per-limit ``b_{name}_*`` overrides, and absence of an override means
    "inherit the item default". That rule has no spelling for "this limit is
    explicitly unscheduled", so before #541 the two were the same byte pattern
    and every reader picked inheritance.

    The mix below is the shape that makes the defect bite in **both**
    directions at once:

    * ``rpm`` is scaled to ``0.5x`` during business hours and has no reset;
    * ``tpm`` is a plain rate limit with neither;
    * ``rpd`` is a daily quota (ADR-137) with a reset and no parameter schedule.

    Contaminated, ``rpm`` acquires ``rpd``'s midnight reset — its balance is
    hard-SET at the edge and its drip skipped that pass — while ``rpd``
    acquires ``rpm``'s ``0.5x`` window and silently serves half its allowance
    between 09:00 and 17:00. ``tpm`` gets both.

    Reached through ``set_limits``, deliberately: the deferral on record
    claimed "the config hierarchy makes the mix unreachable through
    ``set_limits``", and it is not so — ``models.hoisted_schedule_timezone``
    documents that "limits without one do not vote", so an unscheduled limit
    passes the one write-time check a mixed list has to clear.
    """

    ZONE = "America/New_York"
    BUSINESS = (ScheduleEntry(cron="* 9-17 * * 1-5", tz=ZONE, scale=0.5),)
    MIDNIGHT = (ScheduleEntry.reset(cron="0 0 * * *", tz=ZONE),)

    def _limits(self):
        return [
            Limit.per_minute("rpm", 1000).with_schedule(self.BUSINESS),
            Limit.per_minute("tpm", 100_000),
            Limit.quota("rpd", 10_000, cron="0 0 * * *", tz=self.ZONE),
        ]

    @staticmethod
    async def _seed(repo, entity_id, resource, limits):
        now_ms = int(time.time() * 1000)
        states = [BucketState.from_limit(entity_id, resource, lim, now_ms) for lim in limits]
        await repo.transact_write(
            [repo.build_composite_create(entity_id, resource, states, now_ms)]
        )

    @staticmethod
    async def _raw(repo, entity_id, resource, shard=0):
        client = await repo._get_client()
        response = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": pk_bucket(repo._namespace_id, entity_id, resource, shard)},
                "SK": {"S": sk_state()},
            },
        )
        return response.get("Item") or {}

    @pytest.mark.asyncio
    async def test_the_bucket_create_stamp_attributes_each_schedule(self, repo):
        await repo.create_entity("mix-1")
        await self._seed(repo, "mix-1", "gpt-4", self._limits())

        by_name = {b.limit_name: b for b in await repo.get_buckets("mix-1", resource="gpt-4")}
        assert by_name["rpm"].sched == self.BUSINESS
        assert by_name["rpm"].reset_sched == ()
        assert by_name["tpm"].sched == ()
        assert by_name["tpm"].reset_sched == ()
        assert by_name["rpd"].sched == ()
        assert by_name["rpd"].reset_sched == self.MIDNIGHT

    @pytest.mark.asyncio
    async def test_the_set_limits_fan_out_attributes_each_schedule(self, repo):
        """The other writer of these attributes. It must agree with the create
        stamp byte for byte, or which schedule a limit runs under would depend
        on whether an admin had touched the entity since the bucket was born."""
        await repo.create_entity("mix-2")
        # Seeded unscheduled, so the schedules on the item can only have come
        # from the fan-out. Every limit is present up front because the fan-out
        # rewrites parameters, never balances — it does not add a limit's `tk`.
        await self._seed(
            repo,
            "mix-2",
            "gpt-4",
            [
                Limit.per_minute("rpm", 1000),
                Limit.per_minute("tpm", 100_000),
                Limit.per_minute("rpd", 10_000),
            ],
        )

        await repo.set_limits("mix-2", self._limits(), resource="gpt-4")

        by_name = {b.limit_name: b for b in await repo.get_buckets("mix-2", resource="gpt-4")}
        assert by_name["rpm"].sched == self.BUSINESS
        assert by_name["rpm"].reset_sched == ()
        assert by_name["tpm"].sched == ()
        assert by_name["tpm"].reset_sched == ()
        assert by_name["rpd"].sched == ()
        assert by_name["rpd"].reset_sched == self.MIDNIGHT

    @pytest.mark.asyncio
    async def test_both_writers_produce_the_same_attributes(self, repo):
        """One encoder, two writers (#541 acceptance criteria). Compared as a
        set of schedule attributes rather than whole items, which differ in
        balances and timestamps by construction."""
        await repo.create_entity("mix-3")
        await repo.create_entity("mix-4")
        await self._seed(repo, "mix-3", "gpt-4", self._limits())
        await self._seed(repo, "mix-4", "gpt-4", [Limit.per_minute("rpm", 1000)])
        await repo.set_limits("mix-4", self._limits(), resource="gpt-4")

        def _sched_attrs(item):
            return {
                k: v["S"]
                for k, v in item.items()
                if k.endswith(("sched", "rsched", "sched_tz")) and "S" in v
            }

        created = _sched_attrs(await self._raw(repo, "mix-3", "gpt-4"))
        fanned = _sched_attrs(await self._raw(repo, "mix-4", "gpt-4"))
        assert created == fanned
        assert created["sched"] == "1h9-17w1-5s500"
        assert created["rsched"] == "1m0h0"
        assert created["sched_tz"] == self.ZONE
