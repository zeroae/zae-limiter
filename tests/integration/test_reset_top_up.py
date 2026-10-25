"""Reset and top-up against LocalStack (ADR-149, #470).

The unit tests run these operations on moto. Here they run on LocalStack's
transaction and condition evaluation: a reset or top-up is one
``TransactWriteItems`` over every shard, plus its operation record and audit
event, and the next ``acquire()`` must see the result.
"""

import pytest

from zae_limiter import Limit, RateLimiter, RateLimitExceeded, __version__
from zae_limiter.models import BucketState
from zae_limiter.schema import BUCKET_FIELD_TK, bucket_attr, pk_bucket, sk_state
from zae_limiter.version import get_schema_version

T0 = 1_767_268_800_000  # 2026-01-01T12:00:00Z, far from the daily reset edge
CAL = Limit.quota("cal", 10, cron="0 0 * * *")


async def _admitted(limiter: RateLimiter, entity_id: str, resource: str, cap: int = 50) -> int:
    n = 0
    while n < cap:
        try:
            async with limiter.acquire(entity_id, resource, consume={"cal": 1}):
                pass
        except RateLimitExceeded:
            return n
        n += 1
    return n


async def _tokens(repo, entity_id: str, resource: str, shard_id: int) -> int:
    client = await repo._get_client()
    response = await client.get_item(
        TableName=repo.table_name,
        Key={
            "PK": {"S": pk_bucket(repo._namespace_id, entity_id, resource, shard_id)},
            "SK": {"S": sk_state()},
        },
        ConsistentRead=True,
    )
    return int(response["Item"][bucket_attr("cal", BUCKET_FIELD_TK)]["N"])


async def _quota(limiter: RateLimiter, entity_id: str, resource: str):
    repo = limiter._repository
    repo._now_ms = lambda: T0
    await repo.set_limits(entity_id, [CAL], resource=resource)
    return repo


@pytest.mark.integration
@pytest.mark.asyncio
class TestResetAndTopUpOnLocalStack:
    @pytest.mark.parametrize("speculative", [True, False], ids=["fast", "slow"])
    async def test_acquire_after_a_reset_sees_full_capacity(
        self, localstack_limiter, unique_name, speculative
    ):
        entity_id, resource = f"rst-{unique_name}", f"res-{unique_name}"
        acq = RateLimiter(repository=localstack_limiter._repository, speculative_writes=speculative)
        repo = await _quota(acq, entity_id, resource)
        assert await _admitted(acq, entity_id, resource) == 10

        result = await repo.reset_bucket(entity_id, resource)

        assert result.shards == 1
        assert await _admitted(acq, entity_id, resource) == 10

    async def test_a_top_up_admits_exactly_its_amount_more(self, localstack_limiter, unique_name):
        entity_id, resource = f"top-{unique_name}", f"res-{unique_name}"
        repo = await _quota(localstack_limiter, entity_id, resource)
        # A top-up above the plan needs 0.17.0 readers (ADR-149's gate).
        await repo.set_version_record(
            schema_version=get_schema_version(), lambda_version=__version__
        )
        assert await _admitted(localstack_limiter, entity_id, resource) == 10

        result = await repo.top_up(entity_id, resource, {"cal": 5})

        assert result.amounts == {"cal": 5}
        assert await _admitted(localstack_limiter, entity_id, resource) == 5

    async def test_a_sharded_reset_restores_every_shard(self, localstack_limiter, unique_name):
        """Both spent shards are re-granted in the one transaction; each holds
        its share. Read off the items: a draw can land on either shard."""
        entity_id, resource = f"shd-{unique_name}", f"res-{unique_name}"
        repo = await _quota(localstack_limiter, entity_id, resource)
        for shard_id in range(2):
            state = BucketState.from_limit(entity_id, resource, CAL, T0, shard_count=2)
            state.tokens_milli = 0
            state.grant_count = 2
            create = repo.build_composite_create(
                entity_id=entity_id,
                resource=resource,
                states=[state],
                now_ms=T0,
                ttl_seconds=None,
                shard_id=shard_id,
                shard_count=2,
                rf_ms=T0,
            )
            await repo.transact_write([create])
        repo._now_ms = lambda: T0 + 1

        result = await repo.reset_bucket(entity_id, resource)

        assert result.shards == 2
        assert [await _tokens(repo, entity_id, resource, s) for s in range(2)] == [5_000, 5_000]
