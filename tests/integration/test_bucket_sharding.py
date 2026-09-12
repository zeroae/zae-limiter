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
    WCU_LIMIT_CAPACITY,
    WCU_LIMIT_NAME,
    WCU_LIMIT_REFILL_AMOUNT,
    WCU_LIMIT_REFILL_PERIOD_SECONDS,
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


@pytest.mark.integration
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


@pytest.mark.integration
@pytest.mark.asyncio
class TestClientShardCreation:
    """Write sharding must engage with the aggregator DISABLED (issue #439).

    ``localstack_limiter`` runs on the shared minimal stack, which is deployed
    with ``enable_aggregator=False``, so nothing but the client can create a
    shard N>0 item here. Before the fix the client bumped ``shard_count``,
    selected shard 1, got ``BUCKET_MISSING``, and the slow path re-read and
    re-wrote shard 0 — every write stayed on the hot partition.

    See GHSA-76rv-2r9v-c5m6.
    """

    CAPACITY = 100_000

    @staticmethod
    async def _raw_item(repo, entity_id: str, shard_id: int) -> dict | None:
        client = await repo._get_client()
        resp = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": pk_bucket(repo._namespace_id, entity_id, "gpt-4", shard_id)},
                "SK": {"S": sk_state()},
            },
        )
        return resp.get("Item")

    async def test_wcu_exhaustion_creates_shard_one_without_aggregator(
        self, localstack_limiter, monkeypatch, unique_name
    ):
        from zae_limiter import repository as _repo_mod
        from zae_limiter.models import Limit

        limiter = localstack_limiter
        repo = limiter._repository
        ns = repo._namespace_id
        entity_id = f"shard-create-{unique_name}"
        cp_milli = self.CAPACITY * 1000
        # 1 token/hour keeps refill out of the token assertions below
        limit = Limit.custom("rpm", self.CAPACITY, refill_amount=1, refill_period_seconds=3600)

        await limiter.create_entity(entity_id)
        await limiter.set_system_defaults([limit])

        # First acquire creates shard 0 (slow path); the second is a
        # speculative hit whose ALL_NEW teaches the cache shard_count=1.
        for _ in range(2):
            async with limiter.acquire(entity_id, "gpt-4", {"rpm": 1}):
                pass
        assert repo._entity_cache[(ns, entity_id)][2]["gpt-4"] == 1

        # Exhaust wcu on shard 0 directly (equivalent to 1000 speculative writes)
        client = await repo._get_client()
        await client.update_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": pk_bucket(ns, entity_id, "gpt-4", 0)},
                "SK": {"S": sk_state()},
            },
            # wcu drained AND refilling 1 milli/hour: a hot partition. With the
            # default 1000/s refill any elapsed millisecond would restore a
            # write and the client correctly takes the slow path instead.
            UpdateExpression="SET #wcu = :zero, #rf = :rf, #ra = :one, #rp = :hour",
            ExpressionAttributeNames={
                "#wcu": bucket_attr(WCU_LIMIT_NAME, BUCKET_FIELD_TK),
                "#rf": "rf",
                "#ra": bucket_attr(WCU_LIMIT_NAME, BUCKET_FIELD_RA),
                "#rp": bucket_attr(WCU_LIMIT_NAME, BUCKET_FIELD_RP),
            },
            # rf pinned to now: an exhausted wcu whose refill would help is not
            # a hot partition and takes the slow path instead of doubling
            ExpressionAttributeValues={
                ":zero": {"N": "0"},
                ":rf": {"N": str(int(time.time() * 1000))},
                ":one": {"N": "1"},
                ":hour": {"N": "3600000"},
            },
        )
        shard0_before = int(
            (await self._raw_item(repo, entity_id, 0))[bucket_attr("rpm", BUCKET_FIELD_TK)]["N"]
        )

        slow_path_shards: list[int | None] = []
        original_do_acquire = limiter._do_acquire

        async def spy(*args, **kwargs):
            slow_path_shards.append(kwargs.get("shard_id"))
            return await original_do_acquire(*args, **kwargs)

        monkeypatch.setattr(limiter, "_do_acquire", spy)

        # wcu exhausted -> shard_count 1 -> 2 -> slow path targets the new shard 1
        async with limiter.acquire(entity_id, "gpt-4", {"rpm": 1}) as lease:
            assert {e._shard_id for e in lease.entries} == {1}
        assert slow_path_shards == [1]

        shard1 = await self._raw_item(repo, entity_id, 1)
        assert shard1 is not None, "shard 1 must exist without any aggregator"
        assert shard1["shard_count"]["N"] == "2"
        assert int(shard1[bucket_attr("rpm", BUCKET_FIELD_TK)]["N"]) == cp_milli // 2 - 1000
        assert int(shard1[bucket_attr("rpm", BUCKET_FIELD_CP)]["N"]) == cp_milli
        assert int(shard1[bucket_attr(WCU_LIMIT_NAME, BUCKET_FIELD_TK)]["N"]) == 1_000_000
        shard0_after = int(
            (await self._raw_item(repo, entity_id, 0))[bucket_attr("rpm", BUCKET_FIELD_TK)]["N"]
        )
        assert shard0_after == shard0_before, "the hot shard must not absorb the write"

        # Subsequent acquires on shard 1 are fast-path hits: no BUCKET_MISSING fallback
        slow_path_shards.clear()
        # randrange(n) -> 1 (pin the fast-path draw); randrange(old, new) -> old
        # (the post-bump draw from the newly added range, used further below)
        monkeypatch.setattr(_repo_mod.random, "randrange", lambda *a: a[0] if len(a) == 2 else 1)
        async with limiter.acquire(entity_id, "gpt-4", {"rpm": 1}) as lease:
            assert {e._shard_id for e in lease.entries} == {1}
        assert slow_path_shards == []
        shard1 = await self._raw_item(repo, entity_id, 1)
        assert int(shard1[bucket_attr("rpm", BUCKET_FIELD_TK)]["N"]) == cp_milli // 2 - 2000

        # Second doubling: exhaust shard 1 the same way -> 2 -> 4, and the
        # slow path creates one of the two new shards (2 or 3).
        await client.update_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": pk_bucket(ns, entity_id, "gpt-4", 1)},
                "SK": {"S": sk_state()},
            },
            # wcu drained AND refilling 1 milli/hour: a hot partition. With the
            # default 1000/s refill any elapsed millisecond would restore a
            # write and the client correctly takes the slow path instead.
            UpdateExpression="SET #wcu = :zero, #rf = :rf, #ra = :one, #rp = :hour",
            ExpressionAttributeNames={
                "#wcu": bucket_attr(WCU_LIMIT_NAME, BUCKET_FIELD_TK),
                "#rf": "rf",
                "#ra": bucket_attr(WCU_LIMIT_NAME, BUCKET_FIELD_RA),
                "#rp": bucket_attr(WCU_LIMIT_NAME, BUCKET_FIELD_RP),
            },
            ExpressionAttributeValues={
                ":zero": {"N": "0"},
                ":rf": {"N": str(int(time.time() * 1000))},
                ":one": {"N": "1"},
                ":hour": {"N": "3600000"},
            },
        )
        slow_path_shards.clear()
        async with limiter.acquire(entity_id, "gpt-4", {"rpm": 1}) as lease:
            (new_shard,) = {e._shard_id for e in lease.entries}
        assert new_shard == 2
        assert slow_path_shards == [new_shard]
        assert repo._entity_cache[(ns, entity_id)][2]["gpt-4"] == 4
        created = await self._raw_item(repo, entity_id, new_shard)
        assert created["shard_count"]["N"] == "4"
        assert int(created[bucket_attr("rpm", BUCKET_FIELD_TK)]["N"]) == cp_milli // 4 - 1000

    async def test_real_writes_double_and_stop_at_the_cap(self, localstack_limiter, unique_name):
        """Real speculative writes exhaust a shard whose wcu does not refill
        (its refill period is set to an hour to model sustained pressure):
        shard_count doubles, every existing shard learns the new count, and
        it never exceeds MAX_SHARD_COUNT. No hand-zeroing, no randrange pin."""
        from zae_limiter.models import Limit
        from zae_limiter.schema import BUCKET_FIELD_RP, MAX_SHARD_COUNT, WCU_LIMIT_CAPACITY

        limiter = localstack_limiter
        repo = limiter._repository
        ns = repo._namespace_id
        entity_id = f"shard-cap-{unique_name}"
        limit = Limit.custom("rpm", 10_000_000, refill_amount=1, refill_period_seconds=3600)
        await limiter.create_entity(entity_id)
        await limiter.set_system_defaults([limit])
        async with limiter.acquire(entity_id, "gpt-4", {"rpm": 1}):
            pass  # creates shard 0
        client = await repo._get_client()
        await client.update_item(  # shard 0's wcu refills once an hour: sustained pressure
            TableName=repo.table_name,
            Key={"PK": {"S": pk_bucket(ns, entity_id, "gpt-4", 0)}, "SK": {"S": sk_state()}},
            UpdateExpression="SET #rp = :hour, #ra = :one",
            ExpressionAttributeNames={
                "#rp": bucket_attr(WCU_LIMIT_NAME, BUCKET_FIELD_RP),
                "#ra": bucket_attr(WCU_LIMIT_NAME, BUCKET_FIELD_RA),
            },
            ExpressionAttributeValues={":hour": {"N": str(3_600_000)}, ":one": {"N": "1"}},
        )

        counts_seen = set()
        for _ in range(WCU_LIMIT_CAPACITY + 400):
            async with limiter.acquire(entity_id, "gpt-4", {"rpm": 1}):
                pass
            count = repo._entity_cache[(ns, entity_id)][2]["gpt-4"]
            counts_seen.add(count)
            assert count <= MAX_SHARD_COUNT
            if count == MAX_SHARD_COUNT:
                break

        assert {1, 2, 4} <= counts_seen, f"expected at least two doublings, saw {counts_seen}"
        final = repo._entity_cache[(ns, entity_id)][2]["gpt-4"]
        existing = []
        for shard_id in range(MAX_SHARD_COUNT):
            item = await self._raw_item(repo, entity_id, shard_id)
            if item is not None:
                existing.append(shard_id)
        assert 0 in existing and len(existing) >= 3
        assert final == MAX_SHARD_COUNT

    async def test_every_existing_shard_learns_the_final_count(
        self, localstack_limiter, unique_name
    ):
        """After two real wcu-driven doublings, every shard that exists carries
        the final ``shard_count`` and the per-shard shares still sum to at most
        the configured limit.

        Each shard refills toward ``cp // shard_count`` and by ``ra //
        shard_count`` (``BucketState.effective_*``), so a shard left on a stale
        lower count claims a *larger* share and the shares sum to more than the
        configured limit — the entity would be admitted above its rate. With
        ``--no-aggregator`` (this stack) the aggregator's Path 1 propagation is
        not there to fix it, so the client that wins the bump must propagate
        (issue #439).
        """
        from zae_limiter.models import Limit
        from zae_limiter.schema import BUCKET_FIELD_RP, MAX_SHARD_COUNT

        limiter = localstack_limiter
        repo = limiter._repository
        ns = repo._namespace_id
        entity_id = f"shard-propagate-{unique_name}"
        cp, ra = 10_000_000, 1_000_000
        limit = Limit.custom("rpm", cp, refill_amount=ra, refill_period_seconds=3600)
        await limiter.create_entity(entity_id)
        await limiter.set_system_defaults([limit])
        async with limiter.acquire(entity_id, "gpt-4", {"rpm": 1}):
            pass  # creates shard 0

        # Shard 0's wcu refills once an hour: it stays a hot partition, so every
        # acquire that draws it drives a real doubling. Shards created later keep
        # the default fast wcu refill and never exhaust, exactly as in production.
        client = await repo._get_client()
        await client.update_item(
            TableName=repo.table_name,
            Key={"PK": {"S": pk_bucket(ns, entity_id, "gpt-4", 0)}, "SK": {"S": sk_state()}},
            UpdateExpression="SET #rp = :hour, #ra = :one, #tk = :zero",
            ExpressionAttributeNames={
                "#rp": bucket_attr(WCU_LIMIT_NAME, BUCKET_FIELD_RP),
                "#ra": bucket_attr(WCU_LIMIT_NAME, BUCKET_FIELD_RA),
                "#tk": bucket_attr(WCU_LIMIT_NAME, BUCKET_FIELD_TK),
            },
            ExpressionAttributeValues={
                ":hour": {"N": str(3_600_000)},
                ":one": {"N": "1"},
                ":zero": {"N": "0"},
            },
        )

        # Real acquires only. Shard selection is random, so keep going until two
        # doublings have landed (1 -> 2 -> 4); bounded so a regression fails fast.
        for _ in range(200):
            async with limiter.acquire(entity_id, "gpt-4", {"rpm": 1}):
                pass
            if repo._entity_cache[(ns, entity_id)][2]["gpt-4"] >= 4:
                break
        final = repo._entity_cache[(ns, entity_id)][2]["gpt-4"]
        assert final >= 4, f"expected two doublings, got shard_count={final}"

        existing = {}
        for shard_id in range(MAX_SHARD_COUNT):
            item = await self._raw_item(repo, entity_id, shard_id)
            if item is not None:
                existing[shard_id] = int(item["shard_count"]["N"])
        assert len(existing) >= 2, f"expected several shards, found {sorted(existing)}"
        assert set(existing.values()) == {final}, (
            f"shards disagree on shard_count: {existing} (final={final})"
        )

        # The shares are what the count actually buys: sum(cp // count) over the
        # shards that exist must not exceed the configured limit. Token balances
        # may transiently exceed their share for one refill window (shard 0 keeps
        # its balance across a doubling), but the shares themselves must not.
        assert sum(cp * 1000 // c for c in existing.values()) <= cp * 1000
        assert sum(ra * 1000 // c for c in existing.values()) <= ra * 1000

    async def test_idle_sharded_entity_refills_to_capacity_not_shard_count_x_capacity(
        self, localstack_limiter, monkeypatch, unique_name
    ):
        """Two drained shards, one idle refill window: the slow path must admit
        at most the configured capacity in total, not capacity per shard."""
        from zae_limiter import repository as _repo_mod
        from zae_limiter.exceptions import RateLimitExceeded
        from zae_limiter.models import BucketState, Limit

        limiter = localstack_limiter
        repo = limiter._repository
        ns = repo._namespace_id
        entity_id = f"shard-refill-{unique_name}"
        limit = Limit.custom("rpm", 1000, refill_amount=1000, refill_period_seconds=60)

        await limiter.create_entity(entity_id)
        await limiter.set_system_defaults([limit])
        past_ms = int(time.time() * 1000) - 120_000  # two windows ago
        for shard_id in (0, 1):
            state = BucketState.from_limit(entity_id, "gpt-4", limit, past_ms)
            state.tokens_milli = 0
            await repo.transact_write(
                [
                    repo.build_composite_create(
                        entity_id, "gpt-4", [state], past_ms, shard_id=shard_id, shard_count=2
                    )
                ]
            )
        repo._entity_cache[(ns, entity_id)] = (False, None, {"gpt-4": 2})
        limiter._speculative_writes = False

        monkeypatch.setattr(_repo_mod.random, "randrange", lambda _n: 0)
        with pytest.raises(RateLimitExceeded):
            async with limiter.acquire(entity_id, "gpt-4", {"rpm": 501}):
                pass
        async with limiter.acquire(entity_id, "gpt-4", {"rpm": 500}):
            pass
        monkeypatch.setattr(_repo_mod.random, "randrange", lambda _n: 1)
        async with limiter.acquire(entity_id, "gpt-4", {"rpm": 500}):
            pass

        for shard_id in (0, 1):
            item = await self._raw_item(repo, entity_id, shard_id)
            assert int(item[bucket_attr("rpm", BUCKET_FIELD_TK)]["N"]) == 0


@pytest.mark.integration
@pytest.mark.asyncio
class TestLimitChangeFansOutToAllShards:
    """A limit change must reach every shard, not only shard 0 (issue #468).

    ``_sync_bucket_params`` used to key on ``pk_bucket(..., 0)``, so shards
    1..N-1 — created by the aggregator's Path 2 propagation or by the client
    slow path with ``--no-aggregator`` — kept the limits they were born with
    forever. Nothing detects the drift: the speculative success branch never
    compares the stored ``cp``/``ra`` against config, and the slow path refills
    from the stored values.

    Named residual of GHSA-w6c2-33wf-qfwf.
    """

    RESOURCE = "gpt-4"
    OLD_CAPACITY = 1000
    NEW_CAPACITY = 100

    @staticmethod
    async def _raw(repo, entity_id: str, shard_id: int) -> dict:
        client = await repo._get_client()
        resp = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": pk_bucket(repo._namespace_id, entity_id, "gpt-4", shard_id)},
                "SK": {"S": sk_state()},
            },
        )
        return resp.get("Item") or {}

    async def _seed(self, limiter, entity_id: str):
        """Two shards on the undivided old limits, drained and stale.

        Entity-level limits mean the buckets carry no TTL, so they persist and
        have to be reconciled in place. ``rf`` two windows back with ``tk`` at
        zero forces the next acquire through the slow path, where the refill is
        capped at the shard's *effective* capacity — which is what makes a stale
        ``cp`` on shard 1 observable.

        Returns the lowered limit the test then applies.
        """
        from zae_limiter.models import BucketState, Limit

        repo = limiter._repository
        old = Limit.custom(
            "rpm", self.OLD_CAPACITY, refill_amount=self.OLD_CAPACITY, refill_period_seconds=60
        )
        await limiter.create_entity(entity_id)
        await limiter.set_limits(entity_id, [old], resource=self.RESOURCE)

        past_ms = int(time.time() * 1000) - 120_000
        for shard_id in (0, 1):
            state = BucketState.from_limit(entity_id, self.RESOURCE, old, past_ms)
            state.tokens_milli = 0
            await repo.transact_write(
                [
                    repo.build_composite_create(
                        entity_id,
                        self.RESOURCE,
                        [state],
                        past_ms,
                        shard_id=shard_id,
                        shard_count=2,
                    )
                ]
            )
        repo._entity_cache[(repo._namespace_id, entity_id)] = (False, None, {self.RESOURCE: 2})
        return Limit.custom(
            "rpm", self.NEW_CAPACITY, refill_amount=self.NEW_CAPACITY, refill_period_seconds=60
        )

    async def test_set_limits_rewrites_every_shard(self, localstack_limiter, unique_name):
        """The new cp/ra/rp land on shard 1 too, stored undivided."""
        limiter = localstack_limiter
        repo = limiter._repository
        entity_id = f"fanout-store-{unique_name}"
        new = await self._seed(limiter, entity_id)

        await limiter.set_limits(entity_id, [new], resource=self.RESOURCE)

        for shard_id in (0, 1):
            item = await self._raw(repo, entity_id, shard_id)
            # Stored values stay undivided on every shard: the per-shard share
            # is derived at read time by BucketState.effective_*.
            assert int(item[bucket_attr("rpm", BUCKET_FIELD_CP)]["N"]) == self.NEW_CAPACITY * 1000
            assert int(item[bucket_attr("rpm", BUCKET_FIELD_RA)]["N"]) == self.NEW_CAPACITY * 1000
            assert int(item[bucket_attr("rpm", BUCKET_FIELD_RP)]["N"]) == 60_000

    async def test_shard_one_enforces_the_lowered_limit(
        self, localstack_limiter, monkeypatch, unique_name
    ):
        """An acquire that draws shard 1 is gated by the NEW capacity.

        Before the fix shard 1 still held cp=1000 (effective 500), so a
        200-token request kept being admitted there indefinitely.
        """
        from zae_limiter import repository as _repo_mod
        from zae_limiter.exceptions import RateLimitExceeded

        limiter = localstack_limiter
        entity_id = f"fanout-enforce-{unique_name}"
        new = await self._seed(limiter, entity_id)

        await limiter.set_limits(entity_id, [new], resource=self.RESOURCE)

        # Pin the draw to shard 1: randrange(2) -> 1
        monkeypatch.setattr(_repo_mod.random, "randrange", lambda *a: 1)
        # 200 > the new effective capacity (100 // 2 = 50), < the old one (500)
        with pytest.raises(RateLimitExceeded):
            async with limiter.acquire(entity_id, self.RESOURCE, {"rpm": 200}):
                pass
        # ...and the shard still admits inside its new effective share
        async with limiter.acquire(entity_id, self.RESOURCE, {"rpm": 50}) as lease:
            assert {e._shard_id for e in lease.entries} == {1}


@pytest.mark.integration
@pytest.mark.asyncio
class TestCascadeParentSharding:
    """A hot cascade *parent* must spread off shard 0 too (issue #474).

    ``localstack_limiter`` runs on the shared minimal stack, deployed with
    ``enable_aggregator=False``, so only the client can double or create a
    parent shard here. Before the fix the warm-cache parallel cascade path
    debited the parent on shard 0 unconditionally, so the one partition every
    child in a cascade hierarchy writes to — the case GHSA-76rv exists to
    protect — never benefited from write sharding at all.
    """

    CAPACITY = 100_000

    @staticmethod
    async def _raw_item(repo, entity_id: str, shard_id: int) -> dict | None:
        client = await repo._get_client()
        resp = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": pk_bucket(repo._namespace_id, entity_id, "gpt-4", shard_id)},
                "SK": {"S": sk_state()},
            },
        )
        return resp.get("Item")

    async def _consumed(self, repo, entity_id: str, shard_id: int) -> int:
        item = await self._raw_item(repo, entity_id, shard_id)
        return int(item[bucket_attr("rpm", BUCKET_FIELD_TC)]["N"])

    @staticmethod
    async def _set_wcu(repo, entity_id: str, shard_id: int, *, tk: int, ra: int, rp: int) -> None:
        """Rewrite one shard's wcu balance and refill rate.

        A drained balance plus an hourly refill reads as a hot partition; the
        stock 1000/s rate would restore a write on any elapsed millisecond, and
        the client would then (correctly) take the slow path instead of doubling.
        """
        client = await repo._get_client()
        await client.update_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": pk_bucket(repo._namespace_id, entity_id, "gpt-4", shard_id)},
                "SK": {"S": sk_state()},
            },
            UpdateExpression="SET #wcu = :tk, #rf = :rf, #ra = :ra, #rp = :rp",
            ExpressionAttributeNames={
                "#wcu": bucket_attr(WCU_LIMIT_NAME, BUCKET_FIELD_TK),
                "#rf": "rf",
                "#ra": bucket_attr(WCU_LIMIT_NAME, BUCKET_FIELD_RA),
                "#rp": bucket_attr(WCU_LIMIT_NAME, BUCKET_FIELD_RP),
            },
            ExpressionAttributeValues={
                ":tk": {"N": str(tk)},
                ":rf": {"N": str(int(time.time() * 1000))},
                ":ra": {"N": str(ra)},
                ":rp": {"N": str(rp)},
            },
        )

    async def test_hot_cascade_parent_spreads_across_its_shards(
        self, localstack_limiter, unique_name
    ):
        from zae_limiter.models import Limit

        limiter = localstack_limiter
        repo = limiter._repository
        ns = repo._namespace_id
        parent_id = f"cascade-parent-{unique_name}"
        child_id = f"cascade-child-{unique_name}"
        # 1 token/hour keeps refill out of the assertions below
        limit = Limit.custom("rpm", self.CAPACITY, refill_amount=1, refill_period_seconds=3600)

        await limiter.create_entity(parent_id)
        await limiter.create_entity(child_id, parent_id=parent_id, cascade=True)
        await limiter.set_system_defaults([limit])

        # The first acquire creates both shard 0 items (slow path); the second
        # is the parallel cascade path, which teaches both caches shard_count=1.
        for _ in range(2):
            async with limiter.acquire(child_id, "gpt-4", {"rpm": 1}):
                pass
        assert repo._entity_cache[(ns, parent_id)][2]["gpt-4"] == 1

        # The parent's shard 0 is now the hot partition; the child's is fine.
        await self._set_wcu(repo, parent_id, 0, tk=0, ra=1, rp=3_600_000)

        async with limiter.acquire(child_id, "gpt-4", {"rpm": 1}) as lease:
            parent_entry = next(e for e in lease.entries if e.entity_id == parent_id)
            assert parent_entry._shard_id == 1, "the parent must move off its hot shard"
        assert repo._entity_cache[(ns, parent_id)][2]["gpt-4"] == 2
        shard1 = await self._raw_item(repo, parent_id, 1)
        assert shard1 is not None, "parent shard 1 must exist without any aggregator"
        assert shard1["shard_count"]["N"] == "2"

        # Heal shard 0's wcu so both parent shards are writable, then watch the
        # fast path spread the parent's debits over them.
        await self._set_wcu(
            repo,
            parent_id,
            0,
            tk=WCU_LIMIT_CAPACITY * 1000,
            ra=WCU_LIMIT_REFILL_AMOUNT * 1000,
            rp=WCU_LIMIT_REFILL_PERIOD_SECONDS * 1000,
        )
        before = {shard: await self._consumed(repo, parent_id, shard) for shard in (0, 1)}
        for _ in range(20):
            async with limiter.acquire(child_id, "gpt-4", {"rpm": 1}):
                pass
        after = {shard: await self._consumed(repo, parent_id, shard) for shard in (0, 1)}

        assert after[0] > before[0] and after[1] > before[1], (
            f"parent writes must reach both shards: {before} -> {after}"
        )
