"""A slow pass from config cached before a schedule change (suspected defect).

Since v0.18.0 a dripping limit is stored as a fill instant scaled by the refill
amount its schedule put in force (``b_{n}_fi = z_ms × ra``, ADR-154), and a
resource- or system-level change fans out to every active bucket (#823),
rewriting ``sched`` / ``b_{n}_sched`` and re-expressing the fill instant.

The slow path re-expresses each fill instant into the schedule of the config it
resolved (``bucket.adopt_schedule``, ``RateLimiter._do_acquire``), and another
process may hold that config in its cache (60 s TTL) from before the change.
``build_composite_normal`` writes the fill instant but not the schedule it was
expressed against, so such a pass stores a fill instant expressed under the old
schedule on an item carrying the new one, and every later reader interprets it
under the item's schedule: a raised window reads the bucket as full, a lowered
one as a debt of ~2.5e8 tokens. Pinning the item's schedule strings as read
would not catch it: the pass read them after the fan-out, and they still hold.
``TestABucketTheFanOutMissed`` shows the same mismatch with config read fresh,
on a bucket the fan-out did not reach.

Two processes share one table: ``fresh`` reads config uncached (it also makes
the admin change), ``stale`` is a default repository (``config_cache_ttl`` 60 s)
whose cache was warmed before the change. Everything happens within 60 s of the
warm (the config cache runs on the real clock, so it is warm throughout).
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.fixtures.balances import balance_milli
from tests.unit.test_admission_bound import ENTITY, RESOURCE, T0, Model, World, window_excess
from zae_limiter import Limit, RateLimiter, Repository, ScheduleEntry, schema

#: 1000 tokens an hour: a few seconds refill well under one token.
BASE = Limit.custom("rpm", 1000, refill_amount=1000, refill_period_seconds=3600)


def _window(cron: str, scale: float) -> Limit:
    return BASE.with_schedule((ScheduleEntry(cron=cron, tz="UTC", scale=scale),))


#: T0 is 2027-01-15 08:00 UTC: in force from now, or from 08:01.
NOW_DOUBLED = _window("* 8-9 * * *", 2.0)
NOW_HALVED = _window("* 8-9 * * *", 0.5)
SOON_DOUBLED = _window("1-59 8 * * *", 2.0)
SOON_HALVED = _window("1-59 8 * * *", 0.5)


@pytest.fixture
async def fresh_repo(limiter):
    repo = await Repository.open(stack="test-rate-limits", config_cache_ttl=0)
    yield repo
    await repo.close()


async def _item(repo: Repository) -> dict[str, Any]:
    client = await repo._get_client()
    response = await client.get_item(
        TableName=repo.table_name,
        Key={
            "PK": {"S": schema.pk_bucket(repo._namespace_id, ENTITY, RESOURCE, 0)},
            "SK": {"S": schema.sk_state()},
        },
        ConsistentRead=True,
    )
    return response["Item"]


async def _balance(world: World) -> float:
    """The bucket's balance in tokens, read off the item under its own schedule."""
    item = await _item(world.admin)
    return balance_milli(world.admin, item, "rpm", world.now) / 1000


async def _set_default(world: World, level: str, limit: Limit) -> None:
    """The change, through the public setter, from ``fresh``: each fans out."""
    if level == "resource":
        await world.admin.set_resource_defaults(RESOURCE, [limit])
    elif level == "system":
        await world.admin.set_system_defaults([limit])
    else:  # the entity-level sync (#468); its bucket carries no TTL (ADR-136)
        await world.admin.set_limits(ENTITY, [limit], resource=RESOURCE)


async def _drain(world: World, amount: int = 100, limit: int = 40) -> int:
    """``fresh`` alone acquires ``amount`` until rejected; the tokens admitted."""
    admitted = 0
    for _ in range(limit):
        if not await world.acquire(0, amount):
            break
        admitted += amount
    return admitted


def _world(limiter, fresh_repo, *, speculative_writes: bool = True) -> World:
    stale_repo = limiter._repository
    assert stale_repo._config_cache_ttl == 60  # the default
    world = World([fresh_repo, stale_repo], [0, 0], Model(T0, BASE))
    if not speculative_writes:
        world.limiters[1] = RateLimiter(repository=stale_repo, speculative_writes=False)
    return world


def _report(what: str, world: World, **numbers: Any) -> str:
    excess, where = window_excess(world.events, world.model, world.skew)
    shown = ", ".join(f"{k}={v}" for k, v in numbers.items())
    return f"{what}: {shown}; ADR-153 excess {float(excess) / 1000:.3f} tokens at {where}"


@pytest.mark.parametrize("level", ["resource", "system", "entity"])
class TestSpeculativeWritesOff:
    """``stale`` runs ``speculative_writes=False``: its next acquire is a slow pass."""

    async def test_a_raised_window_does_not_refill_the_bucket(self, limiter, fresh_repo, level):
        world = _world(limiter, fresh_repo, speculative_writes=False)
        await _set_default(world, level, BASE)
        assert await world.acquire(1, 500)  # stale: creates the bucket, warms its cache
        world.now += 1_000
        world.model.set(world.now, NOW_DOUBLED)
        await _set_default(world, level, NOW_DOUBLED)  # fans out (#823)
        after_fanout = await _balance(world)
        world.now += 1_000
        assert await world.acquire(1, 1)  # stale's slow pass, config cached before the change
        after_stale = await _balance(world)
        drained = await _drain(world)
        excess, _ = window_excess(world.events, world.model, world.skew)
        msg = _report(
            "1x -> 2x",
            world,
            after_fanout=after_fanout,
            after_stale_pass=after_stale,
            fresh_then_admitted=drained,
        )
        assert after_stale < 501, msg
        assert excess <= 0, msg

    async def test_a_lowered_window_does_not_lock_the_bucket(self, limiter, fresh_repo, level):
        world = _world(limiter, fresh_repo, speculative_writes=False)
        await _set_default(world, level, BASE)
        assert await world.acquire(1, 200)
        world.now += 1_000
        world.model.set(world.now, NOW_HALVED)
        await _set_default(world, level, NOW_HALVED)  # fans out, trims to 500
        after_fanout = await _balance(world)
        world.now += 1_000
        assert await world.acquire(1, 1)
        after_stale = await _balance(world)
        drained = await _drain(world)
        msg = _report(
            "1x -> 0.5x",
            world,
            after_fanout=after_fanout,
            after_stale_pass=after_stale,
            fresh_then_admitted=drained,
        )
        assert after_stale > 498, msg
        assert drained >= 400, msg


class TestDefaultSettings:
    """No option changed: a change made within the cache TTL before its window
    opens. The first acquire after the boundary is a slow pass (a boundary since
    ``xi``), and ``stale``'s config still predates the change."""

    async def test_a_window_opening_at_2x(self, limiter, fresh_repo):
        world = _world(limiter, fresh_repo)
        await _set_default(world, "resource", BASE)
        world.now = T0 + 20_000
        assert await world.acquire(1, 500)  # stale: bucket created, cache warmed
        world.now = T0 + 30_000
        world.model.set(world.now, SOON_DOUBLED)
        await _set_default(world, "resource", SOON_DOUBLED)  # 2x from 08:01
        world.now = T0 + 61_000  # 08:01:01, 41 s after stale warmed its cache
        before_stale = await _balance(world)
        assert await world.acquire(1, 1)
        after_stale = await _balance(world)
        drained = await _drain(world)
        excess, _ = window_excess(world.events, world.model, world.skew)
        msg = _report(
            "1x -> 2x at 08:01",
            world,
            before_stale_pass=before_stale,
            after_stale_pass=after_stale,
            fresh_then_admitted=drained,
        )
        assert after_stale < before_stale, msg
        assert excess <= 0, msg

    async def test_a_window_opening_at_half(self, limiter, fresh_repo):
        world = _world(limiter, fresh_repo)
        await _set_default(world, "resource", BASE)
        world.now = T0 + 20_000
        assert await world.acquire(1, 200)
        world.now = T0 + 30_000
        world.model.set(world.now, SOON_HALVED)
        await _set_default(world, "resource", SOON_HALVED)  # 0.5x from 08:01
        world.now = T0 + 61_000
        before_stale = await _balance(world)
        assert await world.acquire(1, 1)
        after_stale = await _balance(world)
        drained = await _drain(world)
        msg = _report(
            "1x -> 0.5x at 08:01",
            world,
            before_stale_pass=before_stale,
            after_stale_pass=after_stale,
            fresh_then_admitted=drained,
        )
        assert after_stale > before_stale - 2, msg
        assert drained >= 400, msg


class TestABucketTheFanOutMissed:
    """The same mismatch from the other side, no stale cache involved: a bucket
    the fan-out did not reach (ADR-153's accepted residual) keeps the old
    schedule, and a process reading config fresh expresses the fill instant
    under the new one without writing it. Simulated by putting the pre-change
    item back after the fan-out, as if both discovery passes had missed it."""

    @pytest.mark.parametrize(
        ("after", "first"), [(NOW_DOUBLED, 500), (NOW_HALVED, 200)], ids=["2x", "half"]
    )
    async def test_a_fresh_slow_pass_keeps_the_items_balance(
        self, limiter, fresh_repo, after, first
    ):
        world = _world(limiter, fresh_repo)
        world.limiters[0] = RateLimiter(repository=fresh_repo, speculative_writes=False)
        await _set_default(world, "resource", BASE)
        assert await world.acquire(0, first)
        world.now += 1_000
        missed = await _item(fresh_repo)
        await _set_default(world, "resource", after)
        client = await fresh_repo._get_client()
        await client.put_item(TableName=fresh_repo.table_name, Item=missed)  # not reached
        before = await _balance(world)  # under the item's own (old) schedule
        world.now += 1_000
        assert await world.acquire(0, 1)
        after_pass = await _balance(world)
        assert before - 2 < after_pass < before, (
            f"missed bucket: before={before}, after fresh slow pass={after_pass}"
        )
