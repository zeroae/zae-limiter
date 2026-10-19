"""The consumption-only retry never spends a quota period nobody applied (#743).

The slow pass applies a reset, roll or window open in memory, and its
rf-locked write can lose to something that does not move ``rf`` — here a
limit-change fan-out, which stamps ``vu = 0`` and fails the write's #701 ``vu``
pin. The consumption-only retry then debited the *previous* period's balance
and left ``rf`` (and ``wa``) behind, so the period stayed pending and the next
pass granted a full allowance again: 15 admitted against a quota of 10.

The retry now requires the period it would have applied (calendar
``rf >= edge``, session ``wa = ws``); when nobody has applied it, the acquire
re-plans once from a fresh read, which applies it.

Not a sync-generation source: both cases need a hook between one process's
read and its write.
"""

from datetime import UTC, datetime, timedelta

import pytest

from zae_limiter import Limit, RateLimiter, RateLimitExceeded
from zae_limiter.lease import _unapplied_periods
from zae_limiter.schema import pk_bucket, sk_state

EVE = int(datetime(2026, 10, 9, 23, 59, 0, tzinfo=UTC).timestamp() * 1000)
MIDNIGHT = int(datetime(2026, 10, 10, 0, 0, 0, tzinfo=UTC).timestamp() * 1000)
T0 = int(datetime(2026, 10, 9, 10, 0, 0, tzinfo=UTC).timestamp() * 1000)


@pytest.fixture
async def two(limiter):
    """Two processes on one table and one clock, neither caching rejections."""
    from zae_limiter.repository import Repository

    now = [EVE]
    r1 = limiter._repository
    r2 = await Repository.open(stack="test-rate-limits", rejection_cache_ttl=0)
    for repo in (r1, r2):
        repo._rejection_cache.ttl_seconds = 0
        repo._now_ms = lambda: now[0]
    yield limiter, r2, now
    await r2.close()


async def _admit_with_a_fan_out_mid_pass(r1, r2, entity_id, limits, amount) -> list[int]:
    """Process 2 admits ``amount`` on its slow path; between its read and its
    write a limit change (same limits) fans out and stamps ``vu = 0``, so the
    rf-locked write loses its ``vu`` pin and falls to the consumption-only retry."""
    original = r2.transact_write
    fanned: list[int] = []

    async def fan_out_first(items):
        if not fanned:
            fanned.append(1)
            await r1.set_limits(entity_id, limits, resource="r")
        return await original(items)

    r2.transact_write = fan_out_first
    try:
        async with RateLimiter(repository=r2, speculative_writes=False).acquire(
            entity_id, "r", {"q": amount}
        ):
            pass
    finally:
        r2.transact_write = original
    return fanned


async def _drain(limiter, entity_id) -> int:
    admitted = 0
    while admitted <= 40:
        try:
            async with limiter.acquire(entity_id, "r", {"q": 1}):
                admitted += 1
        except RateLimitExceeded:
            break
    return admitted


async def test_calendar_reset_is_not_left_pending_by_the_retry(two):
    l1, r2, now = two
    r1 = l1._repository
    limits = [Limit.quota("q", 10, cron="0 0 * * *")]
    await r1.set_limits("e", limits, resource="r")
    async with l1.acquire("e", "r", {"q": 4}):
        pass  # day 1: 6 left

    now[0] = MIDNIGHT + 1_000
    assert await _admit_with_a_fan_out_mid_pass(r1, r2, "e", limits, 5)
    assert 5 + await _drain(l1, "e") == 10


async def test_session_roll_is_not_left_pending_by_the_retry(two):
    l1, r2, now = two
    now[0] = T0
    r1 = l1._repository
    limits = [Limit.quota("q", 10, reset_after=timedelta(hours=1))]
    await r1.set_limits("w", limits, resource="r")
    async with l1.acquire("w", "r", {"q": 4}):
        pass  # window 1: 6 left

    now[0] = T0 + 3_601_000  # window 1 has ended: the pass opens window 2
    assert await _admit_with_a_fan_out_mid_pass(r1, r2, "w", limits, 5)
    assert 5 + await _drain(l1, "w") == 10


async def test_a_second_loss_is_a_rejection_never_a_hot_retry(two):
    """When the redo loses the same way, the acquire is rejected — an
    under-admission — with the quota marked exceeded and a non-zero wait."""
    l1, r2, now = two
    r1 = l1._repository
    limits = [Limit.quota("q", 10, cron="0 0 * * *")]
    await r1.set_limits("x", limits, resource="r")
    async with l1.acquire("x", "r", {"q": 4}):
        pass

    now[0] = MIDNIGHT + 1_000
    original = r2.transact_write
    client = await r1._get_client()
    stamps: list[int] = []

    async def move_vu_every_time(items):
        # A fresh `vu` before every write: each rf-locked write loses its pin.
        stamps.append(len(stamps) + 1)
        await client.update_item(
            TableName=r1.table_name,
            Key={
                "PK": {"S": pk_bucket(r1._namespace_id, "x", "r", 0)},
                "SK": {"S": sk_state()},
            },
            UpdateExpression="SET vu = :v",
            ExpressionAttributeValues={":v": {"N": str(stamps[-1])}},
        )
        return await original(items)

    r2.transact_write = move_vu_every_time
    try:
        with pytest.raises(RateLimitExceeded) as caught:
            async with RateLimiter(repository=r2, speculative_writes=False).acquire(
                "x", "r", {"q": 5}
            ):
                pass
    finally:
        r2.transact_write = original
    (violation,) = caught.value.violations
    assert violation.limit_name == "q"
    assert violation.retry_after_seconds > 0
    assert await _drain(l1, "x") == 10  # nothing was spent from either period


def test_a_failure_with_no_image_cannot_be_told():
    """Without the failed write's image the rejection stands as before."""
    key: tuple[str, str, int] = ("e", "r", 0)
    assert _unapplied_periods({key: {"q": (MIDNIGHT, None)}}, {}) == set()
    image = {"rf": {"N": str(MIDNIGHT - 1)}, "b_s_wa": {"N": "7"}}
    guards: dict[tuple[str, str, int], dict[str, tuple[int | None, int | None]]] = {
        key: {"q": (MIDNIGHT, None), "s": (None, 7)}
    }
    assert _unapplied_periods(guards, {key: image}) == {(key, "q")}
