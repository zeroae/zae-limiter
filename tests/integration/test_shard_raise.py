"""ADR-156's raise on LocalStack (#821).

The raise marker, the strongly consistent read it returns, and one
``TransactWriteItems`` per doubling with ADR-150's owner check, against
LocalStack's DynamoDB rather than moto: a bucket holding a dripping limit is
raised from 1 to 32 shards, and the total balance is exactly what it was.
"""

import pytest

from zae_limiter import Limit, schema
from zae_limiter.bucket import balance_milli_at
from zae_limiter.schema import pk_bucket, sk_state

RESOURCE = "gpt-4"
T0 = 1_800_000_000_000
RPM = Limit.per_minute("rpm", 32_000)


async def _items(repo, entity_id: str, count: int) -> list[dict]:
    client = await repo._get_client()
    out = []
    for shard in range(count):
        response = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": pk_bucket(repo._namespace_id, entity_id, RESOURCE, shard)},
                "SK": {"S": sk_state()},
            },
            ConsistentRead=True,
        )
        out.append(response["Item"])
    return out


def _balance(repo, item: dict, now_ms: int) -> int:
    (state,) = [s for s in repo._deserialize_composite_bucket(item) if s.limit_name == "rpm"]
    return balance_milli_at(state, now_ms)


@pytest.mark.integration
@pytest.mark.asyncio
class TestShardRaise:
    async def test_a_raise_to_32_shards_conserves_the_balance(
        self, localstack_limiter, unique_name
    ):
        entity_id = f"raise-{unique_name}"
        repo = localstack_limiter._repository
        clock = {"now": T0}
        repo._now_ms = lambda: clock["now"]
        await repo.set_limits(entity_id, [RPM], resource=RESOURCE)
        async with localstack_limiter.acquire(entity_id, RESOURCE, consume={"rpm": 5_000}):
            pass
        held = RPM.capacity * 1000 - 5_000_000

        count = 1
        while count < schema.MAX_SHARD_COUNT:  # the clock holds still: nothing refills
            raised = await repo.raise_shards(entity_id, RESOURCE, count)
            assert raised == 2 * count
            count = raised
            items = await _items(repo, entity_id, count)
            balances = [_balance(repo, item, clock["now"]) for item in items]
            share = RPM.capacity * 1000 // count
            assert all(b <= share for b in balances)
            assert all(int(item["shard_count"]["N"]) == count for item in items)
            assert all(schema.BUCKET_FIELD_RAISE not in item for item in items)
            assert sum(balances) == held  # exactly what the bucket held

        assert await repo.raise_shards(entity_id, RESOURCE, count) == count  # the cap
