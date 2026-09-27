"""A ``reset_after`` limit's config under ``w_``, and the window marker (#640).

Against LocalStack's DynamoDB rather than moto: the storage mapping round-trips
through real ``PutItem`` / ``GetItem`` / ``BatchGetItem``, and the old-writer
sequence exercises the relaxed rollover fan-out condition
(``attribute_exists(wa) OR rf < :new``) and the ``wa`` SETs on the rf-locked
write under a real condition evaluator.

The pre-v0.15 writer is emulated with a raw ``UpdateItem`` that moves exactly
what one moves on a bucket carrying a window it cannot see: ``rf`` from its own
clock, and ``vu`` REMOVEd (entity level, none of its limits scheduled).
"""

from datetime import timedelta

import pytest

from zae_limiter import Limit, RateLimiter, RateLimitExceeded
from zae_limiter.schema import (
    BUCKET_FIELD_RF,
    BUCKET_FIELD_TK,
    BUCKET_FIELD_VU,
    BUCKET_FIELD_WA,
    BUCKET_FIELD_WS,
    bucket_attr,
    pk_bucket,
    pk_entity,
    sk_config,
    sk_state,
)

T0 = 1_800_000_000_000
FIVE_HOURS_MS = 5 * 3_600_000
T1 = T0 + FIVE_HOURS_MS + 1_000
RPM = Limit.per_minute("rpm", 100)
SESSION = Limit.quota("session", 10, reset_after=timedelta(hours=5))


async def _get(repo, pk: str, sk: str) -> dict:
    client = await repo._get_client()
    response = await client.get_item(
        TableName=repo.table_name,
        Key={"PK": {"S": pk}, "SK": {"S": sk}},
        ConsistentRead=True,
    )
    return response.get("Item") or {}


async def _bucket(repo, entity_id: str, resource: str, shard: int) -> dict:
    return await _get(repo, pk_bucket(repo._namespace_id, entity_id, resource, shard), sk_state())


def _n(item: dict, attr: str) -> int | None:
    raw = item.get(attr)
    return None if raw is None else int(raw["N"])


async def _old_writer(repo, entity_id: str, resource: str, shard: int, rf: int) -> None:
    client = await repo._get_client()
    await client.update_item(
        TableName=repo.table_name,
        Key={
            "PK": {"S": pk_bucket(repo._namespace_id, entity_id, resource, shard)},
            "SK": {"S": sk_state()},
        },
        UpdateExpression="SET #rf = :rf REMOVE #vu",
        ExpressionAttributeNames={"#rf": BUCKET_FIELD_RF, "#vu": BUCKET_FIELD_VU},
        ExpressionAttributeValues={":rf": {"N": str(rf)}},
    )


async def _slow(repo, entity_id: str, resource: str, shard: int, now_ms: int, amount=1) -> int:
    """One slow-path acquire forced onto ``shard``; 1 if admitted, else 0."""
    import random

    repo._now_ms = lambda: now_ms
    limiter = RateLimiter(repository=repo, speculative_writes=False)
    real = random.randrange
    random.randrange = lambda a, b=None: shard if b is None else real(a, b)
    try:
        async with limiter.acquire(entity_id, resource, consume={"session": amount}):
            return 1
    except RateLimitExceeded:
        return 0
    finally:
        random.randrange = real


@pytest.mark.integration
@pytest.mark.asyncio
class TestHiddenConfigOnLocalStack:
    async def test_the_session_limit_is_stored_under_w_and_reads_back(
        self, localstack_limiter, unique_name
    ):
        repo = localstack_limiter._repository
        entity_id, resource = f"hid-{unique_name}", f"res-{unique_name}"
        await repo.set_limits(entity_id, [RPM, SESSION], resource=resource)

        item = await _get(repo, pk_entity(repo._namespace_id, entity_id), sk_config(resource))
        assert {"w_session_cp", "w_session_ra", "w_session_rp", "w_session_rsa"} <= item.keys()
        assert not any(a.startswith("l_session") for a in item)
        assert {"l_rpm_cp", "l_rpm_ra", "l_rpm_rp"} <= item.keys()

        await repo.invalidate_config_cache()
        stored = await repo.get_limits(entity_id, resource=resource)
        assert sorted(stored, key=lambda limit: limit.name) == [RPM, SESSION]
        resolved, _unavailable, _source = await repo.resolve_limits(entity_id, resource)
        assert {limit.name for limit in resolved} == {"rpm", "session"}

    async def test_the_first_acquire_marks_the_window_it_opens(
        self, localstack_limiter, unique_name
    ):
        repo = localstack_limiter._repository
        entity_id, resource = f"mark-{unique_name}", f"res-{unique_name}"
        await repo.set_limits(entity_id, [SESSION], resource=resource)
        repo._now_ms = lambda: T0
        async with localstack_limiter.acquire(entity_id, resource, consume={"session": 3}):
            pass
        item = await _bucket(repo, entity_id, resource, 0)
        assert _n(item, bucket_attr("session", BUCKET_FIELD_WS)) == T0
        assert _n(item, bucket_attr("session", BUCKET_FIELD_WA)) == T0


@pytest.mark.integration
@pytest.mark.asyncio
class TestOldWriterSequenceOnLocalStack:
    """A mixed-fleet sequence: every shard rolls exactly once per window."""

    async def test_backward_and_forward_rf_stamps(self, localstack_limiter, unique_name):
        repo = localstack_limiter._repository
        entity_id, resource = f"mix-{unique_name}", f"res-{unique_name}"
        tk = bucket_attr("session", BUCKET_FIELD_TK)
        await repo.set_limits(entity_id, [SESSION], resource=resource)

        # Window T0 on shard 0, then a second shard joining it.
        assert await _slow(repo, entity_id, resource, 0, T0, amount=0) == 1
        assert await repo.bump_shard_count(entity_id, resource, 1) == 2
        assert await _slow(repo, entity_id, resource, 1, T0 + 1_000) == 1
        assert _n(await _bucket(repo, entity_id, resource, 1), tk) == 4_000

        # Backward stamp on shard 1: the next pass must not roll T0 again.
        await _old_writer(repo, entity_id, resource, 1, rf=T0 - 5_000)
        assert await _slow(repo, entity_id, resource, 1, T0 + 2_000) == 1
        assert _n(await _bucket(repo, entity_id, resource, 1), tk) == 3_000

        # Shard 0 opens window T1 and fans it out onto shard 1; then an old
        # write stamps shard 1's rf past T1.
        assert await _slow(repo, entity_id, resource, 0, T1) == 1
        shard1 = await _bucket(repo, entity_id, resource, 1)
        assert _n(shard1, bucket_attr("session", BUCKET_FIELD_WS)) == T1
        assert _n(shard1, bucket_attr("session", BUCKET_FIELD_WA)) == T0
        await _old_writer(repo, entity_id, resource, 1, rf=T1 + 60_000)

        # Shard 1 still rolls into T1 — once.
        admitted = 0
        for now in (T1 + 120_000, T1 + 180_000):
            admitted += await _slow(repo, entity_id, resource, 1, now)
        await _old_writer(repo, entity_id, resource, 1, rf=T1 - 10_000)
        admitted += await _slow(repo, entity_id, resource, 1, T1 + 240_000)
        assert admitted == 3
        shard1 = await _bucket(repo, entity_id, resource, 1)
        assert _n(shard1, tk) == 2_000
        assert _n(shard1, bucket_attr("session", BUCKET_FIELD_WA)) == T1
