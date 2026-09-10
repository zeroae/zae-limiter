"""Integration tests for bucket sharding against real DynamoDB (LocalStack).

Tests verify that the new bucket PK scheme (PK={ns}/BUCKET#{id}#{resource}#{shard})
and associated sharding behaviors work correctly with actual DynamoDB, including:
- Bucket creation at new partition keys
- Shard count propagation
- WCU infrastructure limit presence
- GSI3-based bucket discovery

See: GHSA-76rv-2r9v-c5m6
"""

import os
import time
import uuid
from decimal import Decimal

import boto3
import pytest

from zae_limiter.schema import (
    BUCKET_FIELD_CP,
    BUCKET_FIELD_RA,
    BUCKET_FIELD_RP,
    BUCKET_FIELD_TC,
    BUCKET_FIELD_TK,
    WCU_LIMIT_NAME,
    bucket_attr,
    get_table_definition,
    pk_bucket,
    sk_state,
)
from zae_limiter_aggregator.processor import (
    BucketRefillState,
    LimitRefillInfo,
    propagate_shard_count,
    try_proactive_shard,
)


@pytest.fixture(scope="module")
def dynamodb_table():
    """Create a DynamoDB table for testing."""
    endpoint_url = os.getenv("AWS_ENDPOINT_URL")
    if not endpoint_url:
        pytest.skip("AWS_ENDPOINT_URL not set - LocalStack not available")

    table_name = f"test-sharding-{uuid.uuid4().hex[:8]}"

    dynamodb = boto3.resource(
        "dynamodb",
        endpoint_url=endpoint_url,
        region_name="us-east-1",
    )

    table_def = get_table_definition(table_name)
    table = dynamodb.create_table(**table_def)
    table.wait_until_exists()

    yield table

    table.delete()


def _seed_sharded_bucket(
    table,
    entity_id: str,
    resource: str,
    shard_id: int,
    limits: dict,
    rf_ms: int,
    shard_count: int = 1,
    cascade: bool = False,
    parent_id: str | None = None,
):
    """Seed a composite bucket item at the new PK scheme.

    Args:
        table: boto3 Table resource
        entity_id: Entity ID
        resource: Resource name
        shard_id: Shard index
        limits: Dict of limit_name -> {tk, cp, ra, rp, tc} in millitokens/ms
        rf_ms: Shared refill timestamp
        shard_count: Number of shards for this bucket
        cascade: Whether entity cascades
        parent_id: Parent entity ID if cascading
    """
    item = {
        "PK": pk_bucket("default", entity_id, resource, shard_id),
        "SK": sk_state(),
        "entity_id": entity_id,
        "resource": resource,
        "rf": rf_ms,
        "shard_count": shard_count,
        "cascade": cascade,
        "GSI3PK": f"default/ENTITY#{entity_id}",
        "GSI3SK": f"BUCKET#{resource}#{shard_id}",
    }
    if parent_id is not None:
        item["parent_id"] = parent_id

    for limit_name, fields in limits.items():
        item[bucket_attr(limit_name, BUCKET_FIELD_TK)] = fields["tk"]
        item[bucket_attr(limit_name, BUCKET_FIELD_CP)] = fields["cp"]
        item[bucket_attr(limit_name, BUCKET_FIELD_RA)] = fields["ra"]
        item[bucket_attr(limit_name, BUCKET_FIELD_RP)] = fields["rp"]
        item[bucket_attr(limit_name, BUCKET_FIELD_TC)] = fields["tc"]

    table.put_item(Item=item)


def _get_sharded_bucket(table, entity_id: str, resource: str, shard_id: int) -> dict:
    """Read a bucket item from DynamoDB at the new PK."""
    response = table.get_item(
        Key={
            "PK": pk_bucket("default", entity_id, resource, shard_id),
            "SK": sk_state(),
        }
    )
    return response.get("Item", {})


@pytest.mark.integration
class TestBucketShardingIntegration:
    """Integration tests for bucket sharding against real DynamoDB."""

    def test_bucket_created_at_new_pk(self, dynamodb_table) -> None:
        """Bucket items are stored at PK=BUCKET#{id}#{resource}#{shard}."""
        entity_id = f"entity-{uuid.uuid4().hex[:8]}"
        resource = "gpt-4"
        now_ms = int(time.time() * 1000)

        _seed_sharded_bucket(
            dynamodb_table,
            entity_id,
            resource,
            shard_id=0,
            limits={
                "rpm": {"tk": 100_000, "cp": 100_000, "ra": 100_000, "rp": 60_000, "tc": 0},
                WCU_LIMIT_NAME: {
                    "tk": 1_000_000,
                    "cp": 1_000_000,
                    "ra": 1_000_000,
                    "rp": 60_000,
                    "tc": 0,
                },
            },
            rf_ms=now_ms,
        )

        # Verify bucket is readable at the new PK
        item = _get_sharded_bucket(dynamodb_table, entity_id, resource, 0)
        assert item is not None
        assert item["entity_id"] == entity_id
        assert item["resource"] == resource
        assert item["shard_count"] == Decimal("1")

        # Verify both rpm and wcu limits are present
        assert bucket_attr("rpm", BUCKET_FIELD_TK) in item
        assert bucket_attr(WCU_LIMIT_NAME, BUCKET_FIELD_TK) in item

    def test_multiple_shards_have_independent_pks(self, dynamodb_table) -> None:
        """Each shard gets its own DynamoDB partition key."""
        entity_id = f"entity-{uuid.uuid4().hex[:8]}"
        resource = "gpt-4"
        now_ms = int(time.time() * 1000)

        limits = {
            "rpm": {"tk": 50_000, "cp": 100_000, "ra": 100_000, "rp": 60_000, "tc": 0},
        }

        # Create 2 shards
        for shard_id in range(2):
            _seed_sharded_bucket(
                dynamodb_table,
                entity_id,
                resource,
                shard_id=shard_id,
                limits=limits,
                rf_ms=now_ms,
                shard_count=2,
            )

        # Verify both shards exist independently
        shard_0 = _get_sharded_bucket(dynamodb_table, entity_id, resource, 0)
        shard_1 = _get_sharded_bucket(dynamodb_table, entity_id, resource, 1)
        assert shard_0["PK"] != shard_1["PK"]
        assert shard_0["shard_count"] == Decimal("2")
        assert shard_1["shard_count"] == Decimal("2")

    def test_gsi3_discovers_all_shards(self, dynamodb_table) -> None:
        """GSI3 query returns all shards for an entity."""
        entity_id = f"entity-{uuid.uuid4().hex[:8]}"
        now_ms = int(time.time() * 1000)

        limits = {"rpm": {"tk": 100_000, "cp": 100_000, "ra": 100_000, "rp": 60_000, "tc": 0}}

        # Create 2 resources, 2 shards each
        for resource in ["gpt-4", "gpt-3.5"]:
            for shard_id in range(2):
                _seed_sharded_bucket(
                    dynamodb_table,
                    entity_id,
                    resource,
                    shard_id=shard_id,
                    limits=limits,
                    rf_ms=now_ms,
                    shard_count=2,
                )

        # Query GSI3 for this entity
        response = dynamodb_table.query(
            IndexName="GSI3",
            KeyConditionExpression="GSI3PK = :pk",
            ExpressionAttributeValues={":pk": f"default/ENTITY#{entity_id}"},
        )

        items = response.get("Items", [])
        # 2 resources × 2 shards = 4 items
        assert len(items) == 4

        # Verify all shards and resources are present
        gsi3_sks = {item["GSI3SK"] for item in items}
        assert "BUCKET#gpt-4#0" in gsi3_sks
        assert "BUCKET#gpt-4#1" in gsi3_sks
        assert "BUCKET#gpt-3.5#0" in gsi3_sks
        assert "BUCKET#gpt-3.5#1" in gsi3_sks


@pytest.mark.integration
class TestProactiveShardingIntegration:
    """Integration tests for aggregator proactive sharding."""

    def test_proactive_shard_doubles_count(self, dynamodb_table) -> None:
        """try_proactive_shard doubles shard_count when wcu is high."""
        entity_id = f"entity-{uuid.uuid4().hex[:8]}"
        resource = "gpt-4"
        now_ms = int(time.time() * 1000)

        # Seed a bucket with wcu at 90% consumption (above 80% threshold)
        _seed_sharded_bucket(
            dynamodb_table,
            entity_id,
            resource,
            shard_id=0,
            limits={
                "rpm": {"tk": 100_000, "cp": 100_000, "ra": 100_000, "rp": 60_000, "tc": 0},
                WCU_LIMIT_NAME: {
                    "tk": 100_000,  # 10% remaining
                    "cp": 1_000_000,
                    "ra": 1_000_000,
                    "rp": 60_000,
                    "tc": 900_000,  # 90% consumed
                },
            },
            rf_ms=now_ms,
            shard_count=1,
        )

        state = BucketRefillState(
            namespace_id="default",
            entity_id=entity_id,
            resource=resource,
            shard_id=0,
            shard_count=1,
            rf_ms=now_ms,
            limits={
                WCU_LIMIT_NAME: LimitRefillInfo(
                    tk_milli=100_000,
                    cp_milli=1_000_000,
                    ra_milli=1_000_000,
                    rp_ms=60_000,
                    tc_delta=900_000,
                ),
            },
        )

        result = try_proactive_shard(
            dynamodb_table,
            state,
            wcu_tk_milli=100_000,  # 10% remaining < 20% threshold
            wcu_capacity_milli=1_000_000,
        )
        assert result is True

        # Verify shard_count was doubled
        item = _get_sharded_bucket(dynamodb_table, entity_id, resource, 0)
        assert item["shard_count"] == Decimal("2")


@pytest.mark.integration
class TestShardCountPropagationIntegration:
    """Integration tests for shard_count propagation."""

    def test_propagate_creates_new_shard(self, dynamodb_table) -> None:
        """propagate_shard_count creates shard 1 when shard_count goes from 1 to 2."""
        entity_id = f"entity-{uuid.uuid4().hex[:8]}"
        resource = "gpt-4"
        now_ms = int(time.time() * 1000)
        pk = pk_bucket("default", entity_id, resource, 0)

        # Seed shard 0 with shard_count=2 (already doubled)
        _seed_sharded_bucket(
            dynamodb_table,
            entity_id,
            resource,
            shard_id=0,
            limits={
                "rpm": {"tk": 100_000, "cp": 100_000, "ra": 100_000, "rp": 60_000, "tc": 0},
            },
            rf_ms=now_ms,
            shard_count=2,
        )

        # Build a MODIFY stream record showing shard_count change 1 -> 2
        record = {
            "eventName": "MODIFY",
            "dynamodb": {
                "OldImage": {
                    "PK": {"S": pk},
                    "SK": {"S": sk_state()},
                    "shard_count": {"N": "1"},
                },
                "NewImage": {
                    "PK": {"S": pk},
                    "SK": {"S": sk_state()},
                    "shard_count": {"N": "2"},
                    "entity_id": {"S": entity_id},
                    "resource": {"S": resource},
                    "rf": {"N": str(now_ms)},
                    bucket_attr("rpm", BUCKET_FIELD_TK): {"N": "100000"},
                    bucket_attr("rpm", BUCKET_FIELD_CP): {"N": "100000"},
                    bucket_attr("rpm", BUCKET_FIELD_RA): {"N": "100000"},
                    bucket_attr("rpm", BUCKET_FIELD_RP): {"N": "60000"},
                    bucket_attr("rpm", BUCKET_FIELD_TC): {"N": "0"},
                },
            },
        }

        propagate_shard_count(dynamodb_table, record)

        # Verify shard 1 was created
        shard_1 = _get_sharded_bucket(dynamodb_table, entity_id, resource, 1)
        assert shard_1 is not None
        assert len(shard_1) > 0, "Shard 1 should have been created by propagation"
        assert shard_1["shard_count"] == Decimal("2")


@pytest.mark.asyncio
class TestShardedAdjustmentRouting:
    """Adjustments and rollbacks must land on the shard that was consumed.

    Covered by unit tests against moto too, but repeated here against real
    DynamoDB deliberately: the whole speculative path turns on
    ``ReturnValuesOnConditionCheckFailure="ALL_OLD"``, which is exactly the
    kind of API surface an emulator approximates. If moto's version of it
    diverged, the unit tests could pass while the shard-retry path was never
    reached at all and the routing was never exercised.

    See GHSA-76rv-2r9v-c5m6 and issue #453.
    """

    LIMIT_CAPACITY = 10

    @classmethod
    async def _two_shards_first_exhausted(cls, limiter, monkeypatch, entity_id):
        """Two shards, rpm drained on shard 0, selection pinned to shard 0."""
        import random as _random

        from zae_limiter import repository as _repo_mod
        from zae_limiter.models import BucketState, Limit

        repo = limiter._repository
        ns = repo._namespace_id
        # refill 1/hour keeps refill out of the assertions
        limit = Limit.custom("rpm", cls.LIMIT_CAPACITY, refill_amount=1, refill_period_seconds=3600)

        await limiter.create_entity(entity_id)
        await limiter.set_system_defaults([limit])

        now_ms = int(time.time() * 1000)
        for shard_id in (0, 1):
            states = [BucketState.from_limit(entity_id, "gpt-4", limit, now_ms)]
            await repo.transact_write(
                [
                    repo.build_composite_create(
                        entity_id, "gpt-4", states, now_ms, shard_id=shard_id, shard_count=2
                    )
                ]
            )
        repo._entity_cache[(ns, entity_id)] = (False, None, {"gpt-4": 2})

        await repo._speculative_consume_single(
            entity_id, "gpt-4", {"rpm": cls.LIMIT_CAPACITY}, shard_id=0
        )

        # Pin selection to the drained shard; the retry then has exactly one
        # untried shard, making the whole path deterministic.
        monkeypatch.setattr(_repo_mod.random, "randrange", lambda _n: 0)
        monkeypatch.setattr(_random, "choice", lambda seq: seq[0])
        limiter._speculative_writes = True
        return repo

    @staticmethod
    async def _rpm(repo, entity_id, shard_id):
        buckets = await repo.get_buckets(entity_id, resource="gpt-4", shard_id=shard_id)
        return next(b for b in buckets if b.limit_name == "rpm").tokens_milli

    async def test_adjust_lands_on_the_retried_shard(
        self, localstack_limiter, monkeypatch, unique_name
    ):
        entity_id = f"shard-adj-{unique_name}"
        repo = await self._two_shards_first_exhausted(localstack_limiter, monkeypatch, entity_id)

        async with localstack_limiter.acquire(entity_id, "gpt-4", {"rpm": 10}) as lease:
            await lease.adjust(rpm=5)

        assert await self._rpm(repo, entity_id, 1) == -5_000, (
            "shard 1 served the request; the adjustment belongs on shard 1"
        )
        assert await self._rpm(repo, entity_id, 0) == 0, (
            "shard 0 was already drained and served nothing — it must not be debited"
        )

    async def test_rollback_compensates_the_retried_shard(
        self, localstack_limiter, monkeypatch, unique_name
    ):
        entity_id = f"shard-rb-{unique_name}"
        repo = await self._two_shards_first_exhausted(localstack_limiter, monkeypatch, entity_id)

        with pytest.raises(RuntimeError):
            async with localstack_limiter.acquire(entity_id, "gpt-4", {"rpm": 10}):
                raise RuntimeError("caller blew up")

        assert await self._rpm(repo, entity_id, 1) == 10_000, "shard 1 must be made whole"
        assert await self._rpm(repo, entity_id, 0) == 0, (
            "compensating shard 0 would mint capacity it never lost"
        )
