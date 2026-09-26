"""A limit added to a config level whose bucket already exists (#633).

The first acquire creates the composite bucket item carrying only the limits
configured at the time. A limit added afterwards is absent from that item, and
every write guarded on it failed its condition on DynamoDB (and LocalStack): an
acquire consuming the new limit was rejected on every call with
``retry_after_seconds == 0.0``, and a slow-path acquire *not* consuming it
created ``b_{name}_tk = 0`` with no parameters. The fix seeds the missing limit
on the same write; these tests run it against LocalStack's condition
evaluation rather than moto's.
"""

import pytest

from zae_limiter import Limit, RateLimiter
from zae_limiter.schema import (
    BUCKET_FIELD_CP,
    BUCKET_FIELD_RF,
    BUCKET_FIELD_TK,
    bucket_attr,
    pk_bucket,
    sk_state,
)

T0 = 1_800_000_000_000
RPM = Limit.per_minute("rpm", 100)
TPM = Limit.per_minute("tpm", 1_000)


async def _raw(repo, entity_id: str, resource: str) -> dict:
    client = await repo._get_client()
    response = await client.get_item(
        TableName=repo.table_name,
        Key={
            "PK": {"S": pk_bucket(repo._namespace_id, entity_id, resource, 0)},
            "SK": {"S": sk_state()},
        },
        ConsistentRead=True,
    )
    return response["Item"]


async def _configure(repo, level: str, entity_id: str, resource: str, limits) -> None:
    if level == "resource":
        await repo.set_resource_defaults(resource, limits)
    else:
        await repo.set_limits(entity_id, limits, resource=resource)
    await repo.invalidate_config_cache()


async def _item_then_tpm(limiter: RateLimiter, level: str, entity_id: str, resource: str):
    """Create the item carrying ``rpm`` only, then add ``tpm`` at ``level``."""
    repo = limiter._repository
    repo._now_ms = lambda: T0
    await _configure(repo, level, entity_id, resource, [RPM])
    async with limiter.acquire(entity_id, resource, consume={"rpm": 1}):
        pass
    await _configure(repo, level, entity_id, resource, [RPM, TPM])
    repo._now_ms = lambda: T0 + 1_000
    return repo


@pytest.mark.integration
@pytest.mark.asyncio
class TestLimitAddedToExistingBucket:
    @pytest.mark.parametrize("level", ["resource", "entity"])
    @pytest.mark.parametrize("speculative", [True, False], ids=["fast", "slow"])
    async def test_consuming_the_new_limit_is_admitted(
        self, localstack_limiter, unique_name, level, speculative
    ):
        entity_id = f"add-{unique_name}"
        resource = f"res-{unique_name}"
        acq = RateLimiter(repository=localstack_limiter._repository, speculative_writes=speculative)
        repo = await _item_then_tpm(acq, level, entity_id, resource)

        async with acq.acquire(entity_id, resource, consume={"rpm": 1, "tpm": 1}):
            pass

        item = await _raw(repo, entity_id, resource)
        assert item[bucket_attr("tpm", BUCKET_FIELD_TK)]["N"] == "999000"
        assert item[bucket_attr("tpm", BUCKET_FIELD_CP)]["N"] == "1000000"

        # Seeded once: the next acquire takes the fast path and leaves `rf`.
        repo._now_ms = lambda: T0 + 2_000
        async with localstack_limiter.acquire(entity_id, resource, consume={"tpm": 1}):
            pass
        after = await _raw(repo, entity_id, resource)
        assert after[BUCKET_FIELD_RF] == item[BUCKET_FIELD_RF]
        assert after[bucket_attr("tpm", BUCKET_FIELD_TK)]["N"] == "998000"

    @pytest.mark.parametrize("level", ["resource", "entity"])
    async def test_slow_path_not_consuming_the_new_limit_seeds_it_full(
        self, localstack_limiter, unique_name, level
    ):
        entity_id = f"mode2-{unique_name}"
        resource = f"res-{unique_name}"
        slow = RateLimiter(repository=localstack_limiter._repository, speculative_writes=False)
        repo = await _item_then_tpm(slow, level, entity_id, resource)

        async with slow.acquire(entity_id, resource, consume={"rpm": 1}):
            pass

        item = await _raw(repo, entity_id, resource)
        assert item[bucket_attr("tpm", BUCKET_FIELD_TK)]["N"] == "1000000"
        assert item[bucket_attr("tpm", BUCKET_FIELD_CP)]["N"] == "1000000"
        # One second of refill (100/min) has topped `rpm` back up to 99.
        assert await slow.available(entity_id, resource) == {"rpm": 99, "tpm": 1000}
