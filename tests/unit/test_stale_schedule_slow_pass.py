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
from botocore.exceptions import ClientError

from tests.fixtures.balances import balance_milli
from tests.unit.test_admission_bound import ENTITY, RESOURCE, T0, Model, World, window_excess
from tests.unit.test_repository import _as_legacy_item
from zae_limiter import (
    Limit,
    RateLimiter,
    RateLimitExceeded,
    Repository,
    ScheduleEntry,
    schema,
)

#: 1000 tokens an hour: a few seconds refill well under one token.
BASE = Limit.custom("rpm", 1000, refill_amount=1000, refill_period_seconds=3600)


def _window(cron: str, scale: float) -> Limit:
    return BASE.with_schedule((ScheduleEntry(cron=cron, tz="UTC", scale=scale),))


#: T0 is 2027-01-15 08:00 UTC: in force from now, or from 08:01.
NOW_DOUBLED = _window("* 8-9 * * *", 2.0)
NOW_HALVED = _window("* 8-9 * * *", 0.5)
SOON_DOUBLED = _window("1-59 8 * * *", 2.0)
SOON_HALVED = _window("1-59 8 * * *", 0.5)
#: The doubled window beside a daily quota the bucket does not carry yet.
NOW_DOUBLED_WITH_QUOTA = [NOW_DOUBLED, Limit.quota("rpd", 100, cron="0 0 * * *")]


@pytest.fixture
async def fresh_repo(limiter):
    repo = await Repository.open(stack="test-rate-limits", config_cache_ttl=0)
    yield repo
    await repo.close()


async def _item(repo: Repository, entity_id: str = ENTITY, shard: int = 0) -> dict[str, Any]:
    client = await repo._get_client()
    response = await client.get_item(
        TableName=repo.table_name,
        Key={
            "PK": {"S": schema.pk_bucket(repo._namespace_id, entity_id, RESOURCE, shard)},
            "SK": {"S": schema.sk_state()},
        },
        ConsistentRead=True,
    )
    return response["Item"]


async def _put_back(repo: Repository, item: dict[str, Any]) -> None:
    """Put ``item`` back exactly as read: a bucket the change's fan-out missed."""
    client = await repo._get_client()
    await client.put_item(TableName=repo.table_name, Item=item)


async def _balance(world: World, entity_id: str = ENTITY) -> float:
    """The bucket's balance in tokens, read off the item under its own schedule."""
    item = await _item(world.admin, entity_id)
    return balance_milli(world.admin, item, "rpm", world.now) / 1000


async def _set_default(world: World, level: str, limit: Limit | list[Limit]) -> None:
    """The change, through the public setter, from ``fresh``: each fans out."""
    limits = limit if isinstance(limit, list) else [limit]
    if level == "resource":
        await world.admin.set_resource_defaults(RESOURCE, limits)
    elif level == "system":
        await world.admin.set_system_defaults(limits)
    else:  # the entity-level sync (#468); its bucket carries no TTL (ADR-136)
        await world.admin.set_limits(ENTITY, limits, resource=RESOURCE)


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


class TestWhatThePassDerives:
    """What a stale slow pass derives from the state follows the schedule its
    write is computed under: the ``vu`` it votes, and a rejection's statuses."""

    async def test_the_vu_vote_follows_the_items_schedule(self, limiter, fresh_repo):
        world = _world(limiter, fresh_repo, speculative_writes=False)
        await _set_default(world, "resource", BASE)
        assert await world.acquire(1, 500)
        world.now += 1_000
        await _set_default(world, "resource", NOW_DOUBLED)
        world.now += 1_000
        assert await world.acquire(1, 1)
        item = await _item(fresh_repo)
        # The window `* 8-9 * * *` closes at 10:00: the next parameter change.
        assert item.get(schema.BUCKET_FIELD_VU) == {"N": str(T0 + 2 * 3_600_000)}

    async def test_a_rejection_reports_the_items_schedule(self, limiter, fresh_repo):
        world = _world(limiter, fresh_repo, speculative_writes=False)
        await _set_default(world, "resource", BASE)
        assert await world.acquire(1, 500)
        world.now += 1_000
        await _set_default(world, "resource", NOW_DOUBLED)
        world.now += 1_000
        with pytest.raises(RateLimitExceeded) as caught:
            async with world.limiters[1].acquire(ENTITY, RESOURCE, {"rpm": 600}):
                pass
        status = caught.value.statuses[0]
        assert status.limit.capacity == 2000  # the 2x window the item is under
        assert status.available == 500
        # ~99.5 short at the 2x rate, 2000 an hour: ~179 s (not ~358 at 1x).
        assert 170 < status.retry_after_seconds <= 180

    async def test_a_contended_planning_rejection_reports_the_items_schedule(
        self, limiter, fresh_repo, monkeypatch
    ):
        """A conflict while planning a quota seed is the contended 429 (#729),
        built before any lease entry: its statuses come straight from the
        resolved limits and the states read. On a bucket the fan-out missed,
        the dripping limit's status is under its item's schedule (1x), not
        config's (2x): 1000 // 2 shards, not 2000 // 2."""
        world = _world(limiter, fresh_repo)
        await _set_default(world, "resource", BASE)
        assert await world.acquire(0, 1)
        await world.raise_shards()  # two shards: a quota seed must be planned
        missed = [await _item(fresh_repo, shard=shard) for shard in (0, 1)]
        world.now += 1_000
        await _set_default(world, "resource", NOW_DOUBLED_WITH_QUOTA)
        for item in missed:
            await _put_back(fresh_repo, item)  # neither change reached them

        async def busy(*args: Any, **kwargs: Any) -> Any:
            raise ClientError(
                {"Error": {"Code": "TransactionConflictException", "Message": "busy"}},
                "TransactWriteItems",
            )

        monkeypatch.setattr(fresh_repo, "plan_quota_shard", busy)
        with pytest.raises(RateLimitExceeded) as caught:
            async with world.limiters[0].acquire(ENTITY, RESOURCE, {"rpm": 1, "rpd": 1}):
                pass
        assert caught.value.contended
        rpm = next(s for s in caught.value.statuses if s.limit_name == "rpm")
        assert rpm.limit.capacity == 500


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
        await _put_back(fresh_repo, missed)  # not reached
        before = await _balance(world)  # under the item's own (old) schedule
        world.now += 1_000
        assert await world.acquire(0, 1)
        after_pass = await _balance(world)
        assert before - 2 < after_pass < before, (
            f"missed bucket: before={before}, after fresh slow pass={after_pass}"
        )


# ---------------------------------------------------------------------------
# The other call sites of the pattern
# ---------------------------------------------------------------------------


def _count_carriers(monkeypatch) -> list[int]:
    """Count ``RateLimiter._item_limit_carriers`` calls, so a test proves its path."""
    calls: list[int] = []
    real = RateLimiter._item_limit_carriers

    def spy(cls: type, *args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(RateLimiter, "_item_limit_carriers", classmethod(spy))
    return calls


def _count_parent_only(monkeypatch) -> list[int]:
    """Count ``RateLimiter._try_parent_only_acquire`` calls."""
    calls: list[int] = []
    real = RateLimiter._try_parent_only_acquire

    async def spy(self: RateLimiter, *args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(RateLimiter, "_try_parent_only_acquire", spy)
    return calls


class TestTheOverrideCarriers:
    """``acquire(limits=...)`` carries the item's other configured limits
    through its rf-locked write (#744), from config read through the cache."""

    async def test_a_carried_fill_instant_keeps_its_items_schedule(
        self, limiter, fresh_repo, monkeypatch
    ):
        world = _world(limiter, fresh_repo)
        await _set_default(world, "resource", BASE)
        assert await world.acquire(1, 500)  # stale: bucket created, cache warmed
        world.now += 1_000
        world.model.set(world.now, NOW_DOUBLED)
        await _set_default(world, "resource", NOW_DOUBLED)  # fans out (#823)
        before = await _balance(world)
        world.now += 1_000
        carriers = _count_carriers(monkeypatch)
        override = Limit.per_minute("x", 100)
        async with world.limiters[1].acquire(ENTITY, RESOURCE, {"x": 1}, limits=[override]):
            pass
        assert carriers, "the override pass carried no configured limit"
        after = await _balance(world)
        assert before <= after < before + 1, f"carried rpm: before={before}, after={after}"


class TestTheParentOnlyPath:
    """A cascading child admitted on the fast path, whose parent's write failed
    where refill would admit it, takes ``_try_parent_only_acquire``. A parent
    still stored as ``tk`` (written before v0.18) is converted there, and its
    schedule can predate a change that never fanned out (#823 is new in v0.18)."""

    async def test_a_parent_is_converted_under_its_items_schedule(
        self, limiter, fresh_repo, monkeypatch
    ):
        world = _world(limiter, fresh_repo)
        await _set_default(world, "resource", BASE)
        await fresh_repo.create_entity("org")
        await fresh_repo.create_entity(ENTITY, parent_id="org", cascade=True)
        assert await world.acquire(0, 1)  # creates the child's and the parent's buckets
        assert await world.acquire(0, 1)  # the entity cache now knows the cascade
        await _as_legacy_item(fresh_repo, "org", RESOURCE, tokens={"rpm": 0})
        legacy_parent = await _item(fresh_repo, "org")
        world.now += 1_000
        world.model.set(world.now, NOW_DOUBLED)
        await _set_default(world, "resource", NOW_DOUBLED)
        await _put_back(fresh_repo, legacy_parent)  # never fanned out
        world.now = T0 + 30_000  # 30 s of the parent's 1x drip: ~8.3 tokens
        before = await _balance(world, "org")
        parent_only = _count_parent_only(monkeypatch)
        assert await world.acquire(0, 1)
        assert parent_only, "the parent-only path did not run"
        after = await _balance(world, "org")
        assert before - 2 < after < before, f"parent: before={before}, after={after}"


class TestTheResetAndTopUpPlanner:
    """``reset_bucket`` / ``top_up`` read config uncached (ADR-149) and write
    each fill instant re-expressed at the operation's instant. On a bucket the
    fan-out missed, config's schedule is not the one the item is read under."""

    async def _missed(self, limiter, fresh_repo) -> World:
        world = _world(limiter, fresh_repo)
        await _set_default(world, "resource", BASE)
        assert await world.acquire(0, 500)
        world.now += 1_000
        missed = await _item(fresh_repo)
        await _set_default(world, "resource", NOW_DOUBLED)
        await _put_back(fresh_repo, missed)
        world.now += 1_000
        return world

    async def test_a_reset_fills_the_bucket_under_its_items_schedule(self, limiter, fresh_repo):
        world = await self._missed(limiter, fresh_repo)
        await fresh_repo.reset_bucket(ENTITY, RESOURCE)
        after = await _balance(world)
        assert after == 1000, f"after reset: {after}"

    async def test_a_top_up_adds_under_its_items_schedule(self, limiter, fresh_repo):
        world = await self._missed(limiter, fresh_repo)
        before = await _balance(world)
        await fresh_repo.top_up(ENTITY, RESOURCE, {"rpm": 100})
        after = await _balance(world)
        assert before + 99 < after <= before + 100, f"top-up: before={before}, after={after}"


class TestAPreV018Conversion:
    """A dripping limit still stored as ``tk`` is converted to a fill instant on
    its next slow pass (#818), under whichever schedule that pass reads it with."""

    async def test_a_tk_limit_is_converted_under_its_items_schedule(self, limiter, fresh_repo):
        world = _world(limiter, fresh_repo)
        await _set_default(world, "resource", BASE)
        assert await world.acquire(0, 1)
        await _as_legacy_item(fresh_repo, ENTITY, RESOURCE, tokens={"rpm": 0})
        legacy = await _item(fresh_repo)
        world.now += 1_000
        world.model.set(world.now, NOW_DOUBLED)
        await _set_default(world, "resource", NOW_DOUBLED)
        await _put_back(fresh_repo, legacy)  # never fanned out
        world.now = T0 + 30_000
        before = await _balance(world)  # ~8.3: 30 s of 1x drip on an empty bucket
        assert await world.acquire(0, 1)  # `tk >= 1` fails, refill helps: the slow path
        assert schema.BUCKET_FIELD_XI in await _item(fresh_repo)  # converted
        after = await _balance(world)
        assert before - 2 < after < before, f"converted: before={before}, after={after}"
