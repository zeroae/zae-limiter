"""ADR-154's two writes on LocalStack (#819).

The not-full write (``ADD fi :span`` under ``fi BETWEEN :stamp AND :upper``),
the full write (``SET fi = :value`` under ``fi < :stamp``) and a retry chosen
from a failed write's ``ALL_OLD``, through the real ``acquire()``, against
LocalStack's DynamoDB rather than moto. Turns ``WRITE_FILL_INSTANTS`` on for the
test only: until #821 removes it, nothing else stores a fill instant.
"""

import pytest

from zae_limiter import Limit, schema
from zae_limiter import fill_instant as fi
from zae_limiter.schema import BUCKET_FIELD_FI, bucket_attr, pk_bucket, sk_state

RESOURCE = "gpt-4"
T0 = 1_800_000_000_000
RPM = Limit.per_minute("rpm.v2", 100)
PARAMS = fi.ShardParams(100_000, 100_000, 60_000, 1)


def _key(repo, entity_id: str) -> dict:
    return {
        "PK": {"S": pk_bucket(repo._namespace_id, entity_id, RESOURCE, 0)},
        "SK": {"S": sk_state()},
    }


async def _fi(repo, entity_id: str) -> int:
    client = await repo._get_client()
    item = (
        await client.get_item(
            TableName=repo.table_name, Key=_key(repo, entity_id), ConsistentRead=True
        )
    )["Item"]
    return int(item[bucket_attr(RPM.name, BUCKET_FIELD_FI)]["N"])


@pytest.mark.integration
@pytest.mark.asyncio
class TestFillInstantWrites:
    @pytest.fixture(autouse=True)
    def _fill_instants_on(self, monkeypatch):
        monkeypatch.setattr(schema, "WRITE_FILL_INSTANTS", True)

    async def test_both_writes_and_a_retry(self, localstack_limiter, unique_name):
        entity_id = f"fill-{unique_name}"
        repo = localstack_limiter._repository
        clock = {"now": T0}
        repo._now_ms = lambda: clock["now"]
        await repo.set_limits(entity_id, [RPM], resource=RESOURCE)

        # Created full by the slow path, then a probe and a not-full write.
        for _ in range(2):
            async with localstack_limiter.acquire(entity_id, RESOURCE, consume={RPM.name: 1}):
                pass
        z = await _fi(repo, entity_id)
        assert z == fi.stamp(T0, PARAMS) + fi.span(2_000, PARAMS)

        # An hour idle: the image shows not full, the item is full. The
        # not-full write fails, and the full write chosen from its ALL_OLD
        # lands with the same stamp.
        clock["now"] = T0 + 3_600_000
        fill_key = (repo._namespace_id, entity_id, RESOURCE, 0)
        repo._fill_images.store(
            fill_key,
            {
                **repo._fill_images.get(fill_key).item,
                bucket_attr(RPM.name, BUCKET_FIELD_FI): {
                    "N": str(fi.stamp(clock["now"], PARAMS) + 1)
                },
            },
        )
        async with localstack_limiter.acquire(entity_id, RESOURCE, consume={RPM.name: 3}):
            pass
        assert await _fi(repo, entity_id) == fi.stamp(clock["now"], PARAMS) + fi.span(3_000, PARAMS)

        # Not full now: the next request's not-full write lands first time.
        async with localstack_limiter.acquire(entity_id, RESOURCE, consume={RPM.name: 4}):
            pass
        assert await _fi(repo, entity_id) == fi.stamp(clock["now"], PARAMS) + fi.span(7_000, PARAMS)
