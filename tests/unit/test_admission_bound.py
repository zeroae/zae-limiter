"""ADR-153's bound, checked on the real limiter over every window (#825).

For every dripping limit (soft and bypassed limits, ``on_unavailable=ALLOW``
and the reserved ``wcu`` aside), the tokens debited minus the tokens returned
on one (entity, resource), summed over its shards, from the start of any
window to its last admission, is at most ``C + R(w) + 2·r·s`` plus what
operator resets and top-ups grant in it, plus the debt positive adjustments
leave on shards other than the one that last admitted (ADR-153, the proofs
page's Theorem 5). Each shard is its own bucket, so an adjustment's debt holds
back only its own shard; with one shard the term is zero. ``C`` is the
largest ceiling in force from ``s`` before the window's start to its start,
``R(w)`` the refill the configured schedule grants across it, ``r`` the
largest refill rate in force, ``s`` the largest gap between a writer's clock
and the instant its write lands.

The harness drives the real ``RateLimiter`` and ``Repository`` on moto with
several consumers, each a repository of its own (its own caches, as another
process) on its own clock, offset by up to ``s`` from the true one. Every
operation runs at one true instant. Its net debit is read off the items'
consumption counters (``tc``, summed over the shards), so a return the library
dropped is not counted as returned, and every grant off their balances.
``window_excess`` is the window checker of the 2026-11-07 review's ``sim.py``
(branch ``docs/808-fill-instant-review``), in exact integers.

``TestTheContextCases`` reruns each over-admission ADR-153's Context records;
``TestTheFuzz`` is the dripping-limit conservation fuzz, which also checks
after every step that no shard holds more than its share and that a raise
conserves the total balance exactly.
"""

from __future__ import annotations

import math
import random
from bisect import bisect_right
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

import pytest

from tests.fixtures.sharding import pinned_shard
from zae_limiter import Limit, RateLimiter, RateLimitExceeded, Repository, ScheduleEntry, schema
from zae_limiter import fill_instant as fi
from zae_limiter.bucket import balance_milli_at, fill_instant_at, fill_params
from zae_limiter.models import BucketState
from zae_limiter.schedule import effective_params

RESOURCE = "gpt-4"
ENTITY = "user-1"
T0 = 1_800_000_000_000  # 2027-01-15 08:00 UTC: on a minute, and on an hour
MINUTE_MS = 60_000


# ---------------------------------------------------------------------------
# The bound
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    """One operation: its true instant, net millitokens debited, whether it admitted.

    ``debt`` is, for an admission, the debt held right after it on every shard
    but the one it landed on: what positive adjustments left there, which holds
    back those shards only (ADR-153's last term). Never the admitting shard's
    own: an admission that over-admits leaves its shard below zero, and that
    must still count.
    """

    t: int
    delta: int
    admission: bool
    debt: Fraction = Fraction(0)


class Model:
    """The limit in force for the bucket over time, and what the bound allows."""

    def __init__(self, t0: int, limit: Limit) -> None:
        self.changes: list[tuple[int, Limit]] = [(t0, limit)]
        self._cum: list[tuple[int, Fraction]] | None = None

    def set(self, t: int, limit: Limit) -> None:
        # The model is indexed by time: two changes at one instant would both
        # read as the later one.
        assert t > self.changes[-1][0], "one configuration change per instant"
        self.changes.append((t, limit))
        self._cum = None

    def limit_at(self, t: int) -> Limit:
        index = bisect_right([c for c, _ in self.changes], t) - 1
        return self.changes[max(index, 0)][1]

    def params_at(self, t: int) -> tuple[int, int, int]:
        limit = self.limit_at(t)
        return effective_params(
            limit.capacity * 1000,
            limit.refill_amount * 1000,
            limit.refill_period_seconds * 1000,
            limit.schedule,
            t,
        )

    def capacity_at(self, t: int) -> int:
        return self.params_at(t)[0]

    def rate_at(self, t: int) -> Fraction:
        _cp, ra, rp = self.params_at(t)
        return Fraction(ra, rp)

    def _breakpoints(self, t1: int, t2: int) -> list[int]:
        points = {t1, t2, *(c for c, _ in self.changes if t1 < c < t2)}
        if any(limit.schedule for _, limit in self.changes):
            # Cron is minute-grained: every parameter change lands on a minute.
            first = (t1 // MINUTE_MS + 1) * MINUTE_MS
            points.update(range(first, t2, MINUTE_MS))
        return sorted(points)

    def build(self, t_end: int) -> None:
        """The cumulative refill at every breakpoint up to ``t_end``."""
        points = self._breakpoints(self.changes[0][0], t_end)
        cum = [(points[0], Fraction(0))]
        for a, b in zip(points, points[1:], strict=False):
            cum.append((b, cum[-1][1] + self.rate_at(a) * (b - a)))
        self._cum = cum

    def refilled(self, t: int) -> Fraction:
        assert self._cum is not None
        index = bisect_right([p for p, _ in self._cum], t) - 1
        point, total = self._cum[index]
        return total + self.rate_at(point) * (t - point)

    def max_capacity(self, t1: int, t2: int) -> int:
        return max(self.capacity_at(p) for p in self._breakpoints(t1, t2))

    def max_rate(self) -> Fraction:
        assert self._cum is not None
        return max(self.rate_at(p) for p, _ in self._cum)


def window_excess(events: list[Event], model: Model, skew_ms: int) -> tuple[Fraction, Any]:
    """The largest excess over ADR-153's bound of any window, and where.

    Every window from a debit (``i``) to an admission (``j``): the net debit
    from ``i`` through ``j``, less ``C`` (the largest ceiling over the ``s``
    before ``T_i``), the refill across ``[T_i, T_j]``, ``2·r·s`` and the debt
    other shards hold at ``j`` (``Event.debt``). A value above zero is an
    over-admission.
    """
    if not events:
        return Fraction(0), None
    model.build(max(e.t for e in events))
    slack = 2 * model.max_rate() * skew_ms
    worst: tuple[Fraction, Any] = (Fraction(-(10**30)), None)
    for i, start in enumerate(events):
        if start.delta <= 0:
            continue
        base = model.max_capacity(start.t - skew_ms, start.t) + slack - model.refilled(start.t)
        net = 0
        for end in events[i:]:
            net += end.delta
            if end.admission:
                excess = net - base - model.refilled(end.t) - end.debt
                if excess > worst[0]:
                    worst = (excess, (start.t - T0, end.t - T0, net))
    return worst


def test_the_checker_has_teeth() -> None:
    """Two full capacities in one instant is an excess of one capacity."""
    model = Model(T0, Limit.per_minute("rpm", 10))
    events = [Event(T0, 10_000, True), Event(T0, 10_000, True)]
    assert window_excess(events, model, 0)[0] == 10_000
    assert window_excess([Event(T0, 10_000, True)], model, 0)[0] == 0
    later = [Event(T0, 10_000, True), Event(T0 + 30_000, 5_000, True)]
    assert window_excess(later, model, 0)[0] == 0  # half a minute refilled 5
    # Debt another shard holds is allowed for; nothing else is.
    debt_elsewhere = [Event(T0, 15_000, False), Event(T0, 1_000, True, Fraction(6_000))]
    assert window_excess(debt_elsewhere, model, 0)[0] == 0
    assert window_excess([Event(T0, 16_000, True, Fraction(5_000))], model, 0)[0] == 1_000


# ---------------------------------------------------------------------------
# The world: consumers on skewed clocks, measured off the items
# ---------------------------------------------------------------------------


class World:
    """Consumers sharing one table, each with its own repository and clock."""

    def __init__(self, repos: list[Repository], offsets: list[int], model: Model) -> None:
        self.now = T0
        self.repos = repos
        self.limiters = [RateLimiter(repository=repo) for repo in repos]
        self.offsets = offsets
        self.model = model
        self.events: list[Event] = []
        self.max_shards = 1
        for repo, offset in zip(repos, offsets, strict=True):
            repo._now_ms = lambda offset=offset: self.now + offset

    @property
    def admin(self) -> Repository:
        return self.repos[0]

    @property
    def skew(self) -> int:
        return max(abs(offset) for offset in self.offsets)

    def _pk(self, shard: int) -> str:
        return schema.pk_bucket(self.admin._namespace_id, ENTITY, RESOURCE, shard)

    async def _items(self) -> list[dict[str, Any]]:
        client = await self.admin._get_client()
        items = []
        for shard in range(self.max_shards):
            response = await client.get_item(
                TableName=self.admin.table_name,
                Key={"PK": {"S": self._pk(shard)}, "SK": {"S": schema.sk_state()}},
                ConsistentRead=True,
            )
            if "Item" in response:
                items.append(response["Item"])
        return items

    async def consumed(self) -> int:
        """``tc`` over every shard, millitokens: the net debit so far."""
        return sum(_consumed_by_shard(await self._items()).values())

    def instant(self, *snapshots: list[dict[str, Any]]) -> int:
        """The true instant, or the latest ``xi`` a write stamped ahead of it.

        A reset stamps ``rf = max(now, rf + 1)`` and every later write
        expresses its fill instants at that ``xi``. Two snapshots are compared
        at one instant at or after every shard's ``xi``, or the refill between
        two readings would count as a change.
        """
        xis = [
            int(item[schema.BUCKET_FIELD_XI]["N"])
            for snapshot in snapshots
            for item in snapshot
            if schema.BUCKET_FIELD_XI in item
        ]
        return max([self.now, *xis])

    def balances_of(
        self, items: list[dict[str, Any]], at: int | None = None
    ) -> list[tuple[Fraction, int]]:
        """Each shard's ``(balance, shard_count)``, exact millitokens, at ``at``."""
        at = self.instant(items) if at is None else at
        out = []
        for item in items:
            for state in self.admin._deserialize_composite_bucket(item):
                if state.limit_name == "rpm":
                    state.sched = self.model.limit_at(self.now).schedule
                    out.append((_exact(state, at), state.shard_count))
        return out

    def balances_by_shard(self, items: list[dict[str, Any]]) -> dict[int, Fraction]:
        """Each shard's exact balance in millitokens, by shard id."""
        return {
            _shard_of(item): balance
            for item in items
            for balance, _count in self.balances_of([item], self.instant(items))
        }

    async def balances(self) -> list[tuple[Fraction, int]]:
        return self.balances_of(await self._items())

    async def check_shares(self) -> None:
        """No shard holds more than its share of the capacity in force."""
        cap = self.model.capacity_at(self.now)
        for balance, count in await self.balances():
            assert balance <= Fraction(cap, count), (float(balance), cap, count, self.now - T0)

    async def acquire(
        self,
        k: int,
        amount: int,
        *,
        more: int = 0,
        adjust: int = 0,
        release: int = 0,
        rollback: bool = False,
    ) -> bool:
        """One lease by consumer ``k``; records its debits. Returns whether it admitted.

        Each step is its own event, in order: the admission, a ``consume()``
        (an admission of its own), then what the lease does after it (an
        adjustment, a release, the exit's write or a rollback). The bound runs
        to a window's last admission, so debt an adjustment takes on after it
        belongs to later windows only.
        """
        mark = _consumed_by_shard(await self._items())

        async def record(admission: bool) -> None:
            nonlocal mark
            items = await self._items()
            now = _consumed_by_shard(items)
            delta = sum(now.values()) - sum(mark.values())
            debt = Fraction(0)
            if admission:
                landed = {shard for shard, tc in now.items() if tc > mark.get(shard, 0)}
                debt = sum(
                    (
                        -balance
                        for shard, balance in self.balances_by_shard(items).items()
                        if shard not in landed and balance < 0
                    ),
                    Fraction(0),
                )
            if delta or admission:
                self.events.append(Event(self.now, delta, admission, debt))
            mark = now

        admitted = False
        try:
            async with self.limiters[k].acquire(ENTITY, RESOURCE, {"rpm": amount}) as lease:
                admitted = True
                await record(True)
                if more:
                    try:
                        await lease.consume(rpm=more)
                    except RateLimitExceeded:
                        await record(False)
                    else:
                        await record(True)
                if adjust:
                    await lease.adjust(rpm=adjust)
                if release:
                    await lease.release(rpm=release)
                if rollback:
                    raise _RollbackError
        except RateLimitExceeded:
            admitted = False
        except _RollbackError:
            admitted = False
        await record(False)
        return admitted

    async def burst(self, k: int, amount: int, limit: int = 2_000, *, tries: int = 1) -> int:
        """Acquire ``amount`` round-robin from ``k`` until ``tries`` rejections in a
        row (a shard drawn at random may be empty while others hold tokens);
        returns the tokens admitted."""
        admitted, missed = 0, 0
        for n in range(limit):
            if await self.acquire((k + n) % len(self.repos), amount):
                admitted += amount
                missed = 0
                continue
            missed += 1
            if missed >= tries:
                break
        return admitted

    async def grant(self, operation) -> None:
        """An operator reset or top-up: its grant is a return, read off the balances."""
        before_items = await self._items()
        await operation
        after_items = await self._items()
        at = self.instant(before_items, after_items)
        before = sum(b for b, _ in self.balances_of(before_items, at))
        after = sum(b for b, _ in self.balances_of(after_items, at))
        if after != before:
            # Rounded toward the grant: the bound only loosens.
            self.events.append(Event(self.now, -math.ceil(after - before), False))

    async def raise_shards(self) -> None:
        """ADR-156's raise: every shard split, the total balance kept exactly."""
        before_items = await self._items()
        if not before_items:
            return  # nothing to raise yet
        consumed = await self.consumed()
        count = max(c for _, c in self.balances_of(before_items))
        raised = await self.admin.raise_shards(ENTITY, RESOURCE, count)
        self.max_shards = max(self.max_shards, raised)
        for repo in self.repos:  # every consumer learns the count, as from an image
            cached = repo._entity_cache.get((repo._namespace_id, ENTITY))
            if cached is not None:
                cached[2][RESOURCE] = raised
        after_items = await self._items()
        at = self.instant(before_items, after_items)
        before = sum(b for b, _ in self.balances_of(before_items, at))
        assert sum(b for b, _ in self.balances_of(after_items, at)) == before
        assert await self.consumed() == consumed

    def assert_bound(self) -> None:
        excess, where = window_excess(self.events, self.model, self.skew)
        assert excess <= 0, f"over-admitted {float(excess) / 1000:.3f} tokens at {where}"


def _shard_of(item: dict[str, Any]) -> int:
    return schema.parse_bucket_pk(item["PK"]["S"])[3]


def _consumed_by_shard(items: list[dict[str, Any]]) -> dict[int, int]:
    """Each shard's ``tc``, millitokens."""
    attr = schema.bucket_attr("rpm", schema.BUCKET_FIELD_TC)
    return {_shard_of(item): int(item[attr]["N"]) for item in items if attr in item}


class _RollbackError(Exception):
    """Raised inside a lease to roll it back."""


def _exact(state: BucketState, t_ms: int) -> Fraction:
    """A shard's balance at ``t_ms`` in millitokens, without rounding.

    ``balance_milli_at`` rounds each shard down, so a sum over more shards can
    read lower; a fill instant is exact in units of ``1 / (S × rp)``.
    """
    if state.fill_instant is None:
        return Fraction(balance_milli_at(state, t_ms))
    z, expressed = fill_instant_at(state, t_ms)
    at = max(t_ms, expressed)
    params = fill_params(state, at)
    return Fraction(fi.balance_units(z, at, params), params.units_per_milli)


_OPENED: list[Repository] = []


@pytest.fixture(autouse=True)
async def _close_consumers():
    """Close the consumers' repositories ``_world`` opened (not the fixture's)."""
    yield
    while _OPENED:
        await _OPENED.pop().close()


async def _world(limiter, limit: Limit, offsets: list[int]) -> World:
    """``len(offsets)`` consumers on the fixture's table, the limit at entity level.

    Every consumer but the fixture's own reads configuration uncached, so a
    change is in force for it at once and the model's ``C(t)`` holds.
    """
    repos = [limiter._repository]
    for _ in offsets[1:]:
        repo = await Repository.open(stack="test-rate-limits", config_cache_ttl=0)
        _OPENED.append(repo)
        repos.append(repo)
    world = World(repos, offsets, Model(T0, limit))
    await world.admin.set_limits(ENTITY, [limit], resource=RESOURCE)
    return world


async def test_the_harness_sees_a_balance_minted_behind_the_library(limiter) -> None:
    """End to end, the measure has teeth: a raw write that refills a spent
    bucket, which no operation records, shows as exactly the minted excess."""
    limit = Limit.custom("rpm", 100, refill_amount=100, refill_period_seconds=86_400)
    world = await _world(limiter, limit, [0])
    assert await world.burst(0, 10) == 100
    client = await world.admin._get_client()
    await client.update_item(
        TableName=world.admin.table_name,
        Key={"PK": {"S": world._pk(0)}, "SK": {"S": schema.sk_state()}},
        UpdateExpression="SET #fi = :full",
        ExpressionAttributeNames={"#fi": schema.bucket_attr("rpm", schema.BUCKET_FIELD_FI)},
        ExpressionAttributeValues={":full": {"N": str(world.now * 100_000)}},  # z = now
    )
    # As if another process had written it: this one's caches have not seen it.
    await world.admin.invalidate_config_cache()  # clears the rejection cache too
    world.admin._fill_images.forget((world.admin._namespace_id, ENTITY, RESOURCE, 0))
    assert await world.burst(0, 10) == 100
    assert window_excess(world.events, world.model, world.skew)[0] == 100_000


# ---------------------------------------------------------------------------
# The over-admissions ADR-153's Context records
# ---------------------------------------------------------------------------


class TestTheContextCases:
    async def test_808_a_bucket_idled_to_full_admits_one_burst(self, limiter):
        """#808: today's fast path admitted near 2x capacity after idling to full."""
        limit = Limit.custom("rpm", 300, refill_amount=300, refill_period_seconds=14_400)
        world = await _world(limiter, limit, [0, 150, -150])
        for _round in range(3):
            world.now += 4 * 3_600_000  # idle to full
            assert await world.burst(0, 7) <= 300
            world.now += 600_000  # ten minutes: 12.5 tokens back
            await world.burst(1, 1)
        world.assert_bound()

    @pytest.mark.parametrize("raises", [1, 2, 3])
    async def test_doublings_admit_no_more_than_capacity(self, limiter, raises):
        """1252, 1402 and 1902 against 1000 across one, two and three doublings."""
        limit = Limit.custom("rpm", 1000, refill_amount=1, refill_period_seconds=86_400)
        world = await _world(limiter, limit, [0, 40])
        admitted = await world.burst(0, 125, limit=4)  # half the allowance, on shard 0
        for _ in range(raises):
            await world.raise_shards()
            await world.check_shares()
            admitted += await world.burst(1, 5, limit=400, tries=40)
        assert admitted <= 1000
        world.assert_bound()

    async def test_a_schedule_boundary_that_raises_the_rate(self, limiter):
        """1000 against 117: a boundary that raises the rate refilled the window
        at the new rate. Refill is integrated across windows (ADR-153)."""
        slow_half = (ScheduleEntry(cron="0-29 * * * *", tz="UTC", scale=0.1),)
        limit = Limit.custom("rpm", 1000, refill_amount=1000, refill_period_seconds=3600)
        world = await _world(limiter, limit.with_schedule(slow_half), [0, 100])
        assert await world.burst(0, 10) <= 100  # minute 0: capacity 100
        world.now += 31 * MINUTE_MS  # one minute past the boundary at :30
        await world.burst(1, 10)
        world.assert_bound()

    async def test_two_leases_calling_consume(self, limiter):
        """18 against 10: two leases each passed consume() against stale state."""
        limit = Limit.custom("rpm", 10, refill_amount=10, refill_period_seconds=86_400)
        world = await _world(limiter, limit, [0, 0])
        before = await world.consumed()
        async with world.limiters[0].acquire(ENTITY, RESOURCE, {"rpm": 1}) as a:
            async with world.limiters[1].acquire(ENTITY, RESOURCE, {"rpm": 1}) as b:
                for lease in (a, b):
                    try:
                        await lease.consume(rpm=8)
                    except RateLimitExceeded:
                        pass
        world.events.append(Event(world.now, await world.consumed() - before, True))
        assert world.events[-1].delta <= 10_000
        world.assert_bound()

    async def test_an_adjustments_debt_holds_back_its_own_shard_only(self, limiter):
        """Each shard is its own bucket: a positive adjustment's debt on one does
        not stop another admitting. Summed over shards that exceeds ``C``, by
        exactly the debt the other shard holds, ADR-153's last term."""
        limit = Limit.custom("rpm", 100, refill_amount=100, refill_period_seconds=86_400)
        world = await _world(limiter, limit, [0])
        await world.acquire(0, 1)  # create the bucket
        await world.raise_shards()  # 99 split: 50 on shard 0, 49 on shard 1
        with pinned_shard(0):
            await world.acquire(0, 1, adjust=60)  # shard 0: 50 - 1 - 60 = -11
        with pinned_shard(1):
            assert await world.acquire(0, 49)  # shard 1 still holds 49
        assert world.events[-1].debt == 11_000
        assert window_excess(world.events, world.model, 0)[0] <= 0
        bare = [Event(e.t, e.delta, e.admission) for e in world.events]
        assert window_excess(bare, world.model, 0)[0] == 11_000

    async def test_a_lowered_resource_level_limit(self, limiter):
        """A resource-level change reaches an active bucket (#823)."""
        world = await _world(limiter, Limit.per_minute("rpm", 1000), [0, 50])
        await world.admin.set_resource_defaults(RESOURCE, [Limit.per_minute("rpm", 1000)])
        await world.admin.delete_limits(ENTITY, RESOURCE)  # the bucket now on the resource's
        for _ in range(3):
            await world.burst(0, 50, limit=3)
            world.now += 1_000
        world.model.set(world.now, Limit.per_minute("rpm", 100))
        await world.admin.set_resource_defaults(RESOURCE, [Limit.per_minute("rpm", 100)])
        await world.check_shares()
        world.now += MINUTE_MS
        assert await world.burst(1, 10) <= 100
        world.assert_bound()


# ---------------------------------------------------------------------------
# The dripping-limit conservation fuzz
# ---------------------------------------------------------------------------

OPS = 60
LIMITS = [
    Limit.custom("rpm", 1000, refill_amount=1000, refill_period_seconds=600),
    Limit.custom("rpm", 400, refill_amount=400, refill_period_seconds=600),
    Limit.custom("rpm", 2000, refill_amount=500, refill_period_seconds=600),
    Limit.custom("rpm", 1000, refill_amount=1000, refill_period_seconds=600).with_schedule(
        (ScheduleEntry(cron="0-14 * * * *", tz="UTC", scale=0.25),)
    ),
]


async def run_fuzz(limiter, seed: int) -> None:
    """One seeded sequence: leases (with consume(), adjustments, releases and
    rollbacks), idle time, parameter changes, resets, top-ups and raises."""
    rng = random.Random(seed)
    world = await _world(limiter, LIMITS[0], [0, rng.randint(0, 200), -rng.randint(0, 200)])
    trace: list[str] = []
    for _step in range(OPS):
        roll = rng.random()
        k = rng.randrange(len(world.repos))
        if roll < 0.45:
            amount = rng.choice([1, 5, 25, 100, 300])
            kwargs: dict[str, Any] = {}
            if rng.random() < 0.3:
                kwargs["more"] = rng.choice([1, 10, 100])
            # Returns never exceed what the lease took: one returned to a full
            # bucket is capped, and counted as returned it would hide an excess.
            if rng.random() < 0.2:
                kwargs["adjust"] = rng.choice([-(amount // 2), 5, 50])
            elif rng.random() < 0.15:
                kwargs["release"] = rng.randint(0, amount)
            if rng.random() < 0.1:
                kwargs["rollback"] = True
            ok = await world.acquire(k, amount, **kwargs)
            trace.append(f"acquire({k},{amount},{kwargs})={ok}")
        elif roll < 0.6:
            world.now += rng.choice([1, 500, 30_000, 120_000, 900_000])
            trace.append(f"now+={world.now - T0}")
        elif roll < 0.7:
            limit = rng.choice(LIMITS)
            world.model.set(world.now, limit)
            await world.admin.set_limits(ENTITY, [limit], resource=RESOURCE)
            trace.append(f"set({limit.capacity},{bool(limit.schedule)})")
        elif roll < 0.78 and world.max_shards < 16:
            await world.raise_shards()
            trace.append(f"raise->{world.max_shards}")
        elif roll < 0.84:
            await world.grant(world.admin.reset_bucket(ENTITY, RESOURCE))
            trace.append("reset")
        elif roll < 0.9:
            n = rng.choice([1, 50, 500])
            await world.grant(world.admin.top_up(ENTITY, RESOURCE, {"rpm": n}))
            trace.append(f"top_up({n})")
        else:
            await world.burst(k, rng.choice([10, 50]))
            trace.append(f"burst({k})")
        await world.check_shares()
        world.now += 1  # every operation at its own instant
    excess, where = window_excess(world.events, world.model, world.skew)
    assert excess <= 0, (seed, float(excess) / 1000, where, trace)


class TestTheFuzz:
    @pytest.mark.parametrize("seed", range(8))
    async def test_no_window_over_admits(self, limiter, seed):
        await run_fuzz(limiter, seed)
