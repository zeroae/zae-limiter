"""Limit names containing `.` or `-` work on every write path (#634).

`models.NAME_PATTERN` accepts `rpm.v2` and `req-min`. DynamoDB accepts neither
character in an `ExpressionAttributeNames` key or an `ExpressionAttributeValues`
placeholder, and reads `.` in expression text as a document-path separator.
Every write that built its tokens from the name therefore failed with
`ValidationException` — on LocalStack, as on DynamoDB — except the create `Put`,
which carries no expression. These tests go through the real `acquire()` so
each path is exercised exactly as an application reaches it.

The clock is frozen and advanced one second between acquires, which is what
tells the paths apart on the stored item: a fast-path success never touches
`rf`; the slow path's rf-locked write stamps it.
"""

import boto3
import pytest

from zae_limiter import Limit, RateLimiter
from zae_limiter.schema import BUCKET_FIELD_RF, BUCKET_FIELD_TK, bucket_attr, pk_bucket, sk_state
from zae_limiter_aggregator.processor import aggregate_bucket_states, try_refill_bucket

RESOURCE = "gpt-4"
T0 = 1_800_000_000_000
LIMITS = [Limit.per_minute("rpm.v2", 100), Limit.per_minute("req-min", 1000)]
CONSUME = {"rpm.v2": 1, "req-min": 10}


async def _raw(repo, entity_id: str) -> dict:
    client = await repo._get_client()
    response = await client.get_item(
        TableName=repo.table_name,
        Key={
            "PK": {"S": pk_bucket(repo._namespace_id, entity_id, RESOURCE, 0)},
            "SK": {"S": sk_state()},
        },
        ConsistentRead=True,
    )
    return response["Item"]


async def _tk(repo, entity_id: str, limit_name: str) -> int:
    item = await _raw(repo, entity_id)
    return int(item[bucket_attr(limit_name, BUCKET_FIELD_TK)]["N"])


async def _seed(limiter: RateLimiter, entity_id: str):
    """Configure both limits and create the bucket (the one write that never broke)."""
    repo = limiter._repository
    repo._now_ms = lambda: T0
    await repo.set_limits(entity_id, LIMITS, resource=RESOURCE)
    async with limiter.acquire(entity_id, RESOURCE, consume=CONSUME):
        pass
    repo._now_ms = lambda: T0 + 1_000
    return repo


@pytest.mark.integration
@pytest.mark.asyncio
class TestLimitNamesWithDotsAndHyphens:
    async def test_fast_path(self, localstack_limiter, unique_name):
        entity_id = f"fast-{unique_name}"
        repo = await _seed(localstack_limiter, entity_id)
        async with localstack_limiter.acquire(entity_id, RESOURCE, consume=CONSUME):
            pass

        item = await _raw(repo, entity_id)
        assert int(item[BUCKET_FIELD_RF]["N"]) == T0, "the fast path took the write"
        assert await _tk(repo, entity_id, "rpm.v2") == 98_000
        assert await _tk(repo, entity_id, "req-min") == 980_000

    async def test_slow_path(self, localstack_limiter, unique_name):
        entity_id = f"slow-{unique_name}"
        slow = RateLimiter(repository=localstack_limiter._repository, speculative_writes=False)
        repo = await _seed(slow, entity_id)
        async with slow.acquire(entity_id, RESOURCE, consume=CONSUME):
            pass

        item = await _raw(repo, entity_id)
        assert int(item[BUCKET_FIELD_RF]["N"]) == T0 + 1_000, "the rf-locked write landed"
        # One second of refill tops both back up to their ceilings first.
        assert await _tk(repo, entity_id, "rpm.v2") == 100_000 - 1_000
        assert await _tk(repo, entity_id, "req-min") == 1_000_000 - 10_000

    async def test_adjust(self, localstack_limiter, unique_name):
        entity_id = f"adjust-{unique_name}"
        repo = await _seed(localstack_limiter, entity_id)
        async with localstack_limiter.acquire(entity_id, RESOURCE, consume=CONSUME) as lease:
            await lease.adjust(**{"rpm.v2": 4, "req-min": -5})

        assert await _tk(repo, entity_id, "rpm.v2") == 100_000 - 2_000 - 4_000
        assert await _tk(repo, entity_id, "req-min") == 1_000_000 - 20_000 + 5_000

    async def test_rollback(self, localstack_limiter, unique_name):
        entity_id = f"rollback-{unique_name}"
        repo = await _seed(localstack_limiter, entity_id)
        with pytest.raises(RuntimeError, match="boom"):
            async with localstack_limiter.acquire(entity_id, RESOURCE, consume=CONSUME):
                raise RuntimeError("boom")

        # Only the seeding acquire's consumption remains.
        assert await _tk(repo, entity_id, "rpm.v2") == 99_000
        assert await _tk(repo, entity_id, "req-min") == 990_000

    async def test_aggregator_refill(self, localstack_limiter, unique_name, localstack_endpoint):
        """The aggregator's refill wrote `ADD b_rpm.v2_tk :rd_rpm.v2` inline."""
        entity_id = f"refill-{unique_name}"
        repo = await _seed(localstack_limiter, entity_id)
        image = await _raw(repo, entity_id)
        # A heavy consumer since the previous image: forces a top-up.
        old_image = {k: v for k, v in image.items() if not k.endswith("_tc")}
        for name in ("rpm.v2", "req-min"):
            old_image[f"b_{name}_tc"] = {"N": "-5000000000"}
        record = {"eventName": "MODIFY", "dynamodb": {"NewImage": image, "OldImage": old_image}}
        state = next(iter(aggregate_bucket_states([record]).values()))
        table = boto3.resource(
            "dynamodb", endpoint_url=localstack_endpoint, region_name="us-east-1"
        ).Table(repo.table_name)

        assert try_refill_bucket(table, state, T0 + 60_000) is True

        assert await _tk(repo, entity_id, "rpm.v2") == 100_000
        assert await _tk(repo, entity_id, "req-min") == 1_000_000
