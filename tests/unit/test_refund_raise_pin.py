"""A raise computed from a read must not stack on a refund that landed since (#730 R5).

Every write that raises a balance by a delta computed from what it read — the
client's rf-locked refill, reset, roll or window open, and the aggregator's
refill, reset or roll — used to be guarded only by ``rf`` (and ``vu``). A
refund or release landing between that read and the write moves neither, so
the write's delta stacked on top of it and the balance rose past the ceiling
by the refund. Each such write is now pinned on ``tk <= tk as read`` for every
limit it raises; speculative debits only lower ``tk``, so a balance above the
read means a credit landed. The client falls to the consumption-only retry and
the aggregator skips, both as on a lost ``rf`` lock.

Not a sync-generation source: both repros need two processes and a hook
between one's read and its write.
"""

from datetime import UTC, datetime

import pytest

from zae_limiter import Limit, RateLimiter, RateLimitExceeded
from zae_limiter.schema import BUCKET_FIELD_TK, bucket_attr, pk_bucket, sk_state

T0 = int(datetime(2026, 10, 9, 10, 0, 0, tzinfo=UTC).timestamp() * 1000)
EVE = int(datetime(2026, 10, 9, 23, 59, 59, tzinfo=UTC).timestamp() * 1000)
MIDNIGHT = int(datetime(2026, 10, 10, 0, 0, 0, tzinfo=UTC).timestamp() * 1000)
TK = bucket_attr("q", BUCKET_FIELD_TK)


async def _raw(repo, entity_id):
    client = await repo._get_client()
    response = await client.get_item(
        TableName=repo.table_name,
        Key={
            "PK": {"S": pk_bucket(repo._namespace_id, entity_id, "r", 0)},
            "SK": {"S": sk_state()},
        },
    )
    return response["Item"]


@pytest.fixture
async def two(limiter):
    """Two processes on one table and one clock, neither caching rejections."""
    from zae_limiter.repository import Repository

    now = [T0]
    r1 = limiter._repository
    r2 = await Repository.open(stack="test-rate-limits", rejection_cache_ttl=0)
    for repo in (r1, r2):
        repo._rejection_cache.ttl_seconds = 0
        repo._now_ms = lambda: now[0]
    yield limiter, r2, now
    await r2.close()


async def test_client_refill_does_not_stack_on_a_refund(two):
    """A slow pass reads a balance of 6 and refills it to its capacity of 10 (+4);
    a refund of 3 lands before its write. Unpinned, the +4 stacked on 9: 12 on a
    capacity of 10, and with the slow pass's own 1 that was 13 admitted against
    10. Pinned, the write falls to the consumption-only retry."""
    l1, r2, now = two
    r1 = l1._repository
    limit = Limit.custom("q", capacity=10, refill_amount=10, refill_period_seconds=3600)
    await r1.set_limits("c", [limit], resource="r")
    async with l1.acquire("c", "r", {"q": 1}):
        pass  # create: 9
    lease_cm = l1.acquire("c", "r", {"q": 3})
    await lease_cm.__aenter__()  # 6, refund pending

    now[0] = T0 + 3_600_000  # a full refill has accrued
    original = r2.transact_write
    raced: list[int] = []

    async def refund_first(items):
        if not raced:
            raced.append(1)
            await lease_cm.__aexit__(RuntimeError, RuntimeError("body failed"), None)
        return await original(items)

    r2.transact_write = refund_first
    try:
        async with RateLimiter(repository=r2, speculative_writes=False).acquire("c", "r", {"q": 1}):
            pass
    finally:
        r2.transact_write = original
    assert raced

    assert int((await _raw(r1, "c"))[TK]["N"]) <= 10_000
    admitted = 0
    while admitted <= 20:  # what the fast path would spend
        result = await r1.speculative_consume("c", "r", {"q": 1}, shard_id=0)
        if not result.success:
            break
        admitted += 1
    assert 1 + admitted <= 10


async def test_aggregator_reset_does_not_stack_on_a_refund(two):
    """The aggregator's image shows 4 of a daily quota of 10, and it resets the
    balance after midnight (+6); a refund of yesterday's 5 landed after the image
    (the item's `rf` was still before midnight, so the refund was in its period).
    Unpinned, the +6 stacked on 9: 15 admitted against 10 today."""
    import boto3

    from zae_limiter_aggregator.processor import aggregate_bucket_states, try_refill_bucket

    l1, _, now = two
    r1 = l1._repository
    now[0] = EVE
    await r1.set_limits("a", [Limit.quota("q", 10, cron="0 0 * * *")], resource="r")
    async with l1.acquire("a", "r", {"q": 1}):
        pass  # create: 9
    image: dict = {}
    with pytest.raises(RuntimeError):
        async with l1.acquire("a", "r", {"q": 5}):  # 4
            image = await _raw(r1, "a")  # the stream record the aggregator sees
            now[0] = MIDNIGHT + 1_000
            raise RuntimeError("body failed")  # the refund lands: 9

    assert image  # captured inside the lease
    record = {"eventName": "MODIFY", "dynamodb": {"NewImage": image, "OldImage": image}}
    (state,) = aggregate_bucket_states([record]).values()
    table = boto3.resource("dynamodb", region_name="us-east-1").Table(r1.table_name)
    try_refill_bucket(table, state, MIDNIGHT + 2_000)

    assert int((await _raw(r1, "a"))[TK]["N"]) <= 10_000
    today = 0
    while today <= 20:
        try:
            async with l1.acquire("a", "r", {"q": 1}):
                today += 1
        except RateLimitExceeded:
            break
    assert today <= 10
