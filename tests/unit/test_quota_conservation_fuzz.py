"""I8 against the real repository on moto (ADR-145, Refs #637, Refs #642).

Seeded random operation sequences drive a real ``RateLimiter`` over moto, and
after every operation the quota's accounting is read back off the bucket items:

    admitted this period + held by current-period shards + still grantable == C

where *still grantable* is ``C // S`` for every slot no current-period shard
carrying the quota covers (a slot ``j`` is covered by shard ``i`` when
``j % gc_i == i % gc_i``, design §3). ``C = 1024`` so every share divides
exactly and the equality is exact rather than a bound.

The operations and how each is driven — every one through the real path:

- **spend** — ``acquire()`` of ``n`` quota tokens with every shard draw pinned
  to a random shard, through a speculative limiter or a slow-path-only one. A
  missing shard is created (a move or a fresh grant), a shard lacking the quota
  is seeded, a pending reset is applied — whatever the real path does. A
  speculative probe of another shard (#633 misclassifies a missing limit as
  exhausted) draws from the test's own seeded RNG.
- **double** — ``Repository.bump_shard_count``: the client's own doubling of
  shard 0, which propagates the new count to the existing shards.
- **late seed** — ``set_limits`` adding the quota to an entity whose shards
  already exist (the #633 case); only for seeds that start without it.
- **next period** — the frozen clock advanced past the next reset: midnight
  for the calendar quota, the window's end for the session quota.
- **top up** — ``Repository.top_up`` of the quota by a random amount (ADR-149):
  exactly that amount joins the period, so the right-hand side becomes
  ``C + top-ups this period``, and a shard's ceiling becomes ``C // gc + tu``.
- **reset** — ``Repository.reset_bucket`` of the quota (ADR-149): a new period,
  every existing shard re-granted at the count, nothing admitted yet.

Not covered here: the aggregator (its Path 2 clone and refill have their own
tests in the processor suite), concurrent writers (stepped race tests in
``test_quota_shard_creation.py``; moto is not thread-safe, #656), and v0.14
items without ``gc`` (tested directly there).

Ten seeds per quota kind: breadth comes from the 300-seed pure differential
test (``test_quota_grant_plan.TestPlannerMatchesModel``), which drives the same
operation mix through the shipped planner and the model; this file samples it
against the real repository.

``QuotaModel(rule="NEW")`` runs the same sequence alongside and must account
for ``C`` too. The two need not agree shard by shard: the model applies a
pending reset even on a rejected spend, and the real path writes nothing on a
rejection that carries no move, so the distribution — never the total — can
differ.
"""

from __future__ import annotations

import contextlib
import random
from datetime import timedelta
from typing import Any

from tests.fixtures.quota_model import QuotaModel
from tests.fixtures.sharding import RESOURCE
from tests.fixtures.windows import T0
from zae_limiter import Limit, RateLimiter, RateLimitExceeded, schema
from zae_limiter.models import quota_grant_is_current

C = 1024
OPS = 40
DAY_MS = 86_400_000
SESSION = timedelta(hours=5)
SESSION_MS = 5 * 3_600_000
RPM = Limit.per_minute("rpm", 10_000_000)

QUOTAS = {
    "calendar": (Limit.quota("q", C, cron="0 0 * * *"), DAY_MS),
    "session": (Limit.quota("q", C, reset_after=SESSION), SESSION_MS + 1),
}


@contextlib.contextmanager
def drawn(shard: int, rng: random.Random):
    """Every shard draw lands on ``shard``; any other draw comes from ``rng``."""
    real_randrange, real_choice = random.randrange, random.choice
    random.randrange = lambda a, b=None: shard if b is None else rng.randrange(a, b)
    random.choice = lambda seq: shard if shard in seq else seq[rng.randrange(len(seq))]
    try:
        yield
    finally:
        random.randrange, random.choice = real_randrange, real_choice


async def _items(repo, entity_id: str, count: int) -> dict[int, dict[str, Any]]:
    """Every existing shard item of the entity, keyed by shard id."""
    client = await repo._get_client()
    keys = [
        {
            "PK": {"S": schema.pk_bucket(repo._namespace_id, entity_id, RESOURCE, shard)},
            "SK": {"S": schema.sk_state()},
        }
        for shard in range(count)
    ]
    response = await client.batch_get_item(
        RequestItems={repo.table_name: {"Keys": keys, "ConsistentRead": True}}
    )
    assert not response.get("UnprocessedKeys"), "moto never throttles"
    out = {}
    for item in response["Responses"][repo.table_name]:
        _ns, _entity, _resource, shard = schema.parse_bucket_pk(item["PK"]["S"])
        out[shard] = item
    return out


def _cached_count(repo, entity_id: str) -> int:
    cached = repo._entity_cache.get((repo._namespace_id, entity_id))
    return cached[2].get(RESOURCE, 1) if cached else 1


def _num(item: dict[str, Any], attr: str) -> int | None:
    raw = item.get(attr)
    return None if raw is None else int(raw["N"])


async def real_accounting(repo, entity_id: str, quota: Limit, now_ms: int) -> tuple[int, int, int]:
    """``(held, still_grantable, shard_count)`` in millitokens, read off the items."""
    count = _cached_count(repo, entity_id)
    items = await _items(repo, entity_id, schema.MAX_SHARD_COUNT)
    count = max([count] + [_num(item, "shard_count") or 1 for item in items.values()])

    tk_attr = schema.bucket_attr(quota.name, schema.BUCKET_FIELD_TK)
    cp_attr = schema.bucket_attr(quota.name, schema.BUCKET_FIELD_CP)
    gc_attr = schema.bucket_attr(quota.name, schema.BUCKET_FIELD_GC)
    tu_attr = schema.bucket_attr(quota.name, schema.BUCKET_FIELD_TU)
    ws_attr = schema.bucket_attr(quota.name, schema.BUCKET_FIELD_WS)
    wa_attr = schema.bucket_attr(quota.name, schema.BUCKET_FIELD_WA)

    current: dict[int, int] = {}  # shard -> grant count, current-period carriers only
    held = 0
    for shard, item in items.items():
        tokens = _num(item, tk_attr)
        if tokens is None or cp_attr not in item:
            continue  # does not carry the quota (not yet seeded)
        assert shard < count, f"shard {shard} exists beyond the count {count}"
        is_current = quota_grant_is_current(
            quota, _num(item, "rf") or 0, _num(item, ws_attr), _num(item, wa_attr), now_ms
        )
        if not is_current:
            continue  # a pending reset: its slot is still grantable
        grant = _num(item, gc_attr)
        assert grant is not None, f"shard {shard}: a v0.15 writer always stamps gc"
        assert tokens >= 0, f"shard {shard} in debt {tokens}: nothing here adjusts"
        topped_up = _num(item, tu_attr) or 0
        assert tokens <= C * 1000 // grant + topped_up, (
            f"shard {shard} above its ceiling C // {grant} + {topped_up}"
        )
        current[shard] = grant
        held += tokens

    uncovered = [
        slot
        for slot in range(count)
        if slot not in current
        and not any(slot % grant == shard % grant for shard, grant in current.items())
    ]
    return held, len(uncovered) * (C * 1000 // count), count


def pytest_generate_tests(metafunc):
    """Each class below runs its own slice of the seeds."""
    if metafunc.cls is not None and "seed" in metafunc.fixturenames:
        metafunc.parametrize("seed", metafunc.cls.SEEDS)


async def run_fuzz(limiter, seed: int, kind: str) -> None:
    """One seeded sequence of ``OPS`` operations, checking I8 after each."""
    quota, period_ms = QUOTAS[kind]
    rng = random.Random(seed)
    repo = limiter._repository
    now = [T0]
    repo._now_ms = lambda: now[0]
    fast = limiter
    slow = RateLimiter(repository=repo, speculative_writes=False)
    model = QuotaModel(C, rule="NEW")
    eid = f"fuzz-{kind}-{seed}"

    quota_on = rng.random() < 0.5
    await limiter.set_limits(eid, [RPM, quota] if quota_on else [RPM], resource=RESOURCE)
    # Shard 0 must exist before anything can double it.
    async with slow.acquire(eid, RESOURCE, {"rpm": 1}):
        pass
    late_seed_at = None if quota_on else rng.randrange(1, 12)

    admitted = 0
    topped_up = 0  # tokens topped up this period (ADR-149)
    trace: list[str] = []
    for step in range(OPS):
        count = _cached_count(repo, eid)
        roll = rng.random()
        if step == late_seed_at:
            await limiter.set_limits(eid, [RPM, quota], resource=RESOURCE)
            quota_on = True
            trace.append("seed")
        elif roll < 0.2:
            await repo.bump_shard_count(eid, RESOURCE, count)
            model.double()
            trace.append("double")
        elif roll < 0.27 and quota_on:
            now[0] += period_ms
            admitted = 0
            topped_up = 0
            model.next_period()
            trace.append("next")
        elif roll < 0.33 and quota_on:
            amount = rng.choice([1, 7, 64, 300])
            # A quota configured after the shards existed and not yet seeded
            # on any of them is granted nothing (the result says so).
            granted = (await repo.top_up(eid, RESOURCE, {quota.name: amount})).amounts
            topped_up += granted[quota.name]
            trace.append(f"top_up({amount})")
        elif roll < 0.36 and quota_on:
            await repo.reset_bucket(eid, RESOURCE, limits=[quota.name])
            admitted = 0
            topped_up = 0
            trace.append("reset")
        else:
            shard = rng.randrange(count)
            amount = rng.choice([1, 3, 10, 40, 100, 300])
            lim = fast if rng.random() < 0.5 else slow
            consume = {quota.name: amount} if quota_on else {"rpm": 1}
            ok = True
            with drawn(shard, rng):
                try:
                    async with lim.acquire(eid, RESOURCE, consume):
                        pass
                except RateLimitExceeded:
                    ok = False
            if quota_on:
                admitted += amount if ok else 0
                model.spend(shard, amount)
            trace.append(f"spend({shard},{amount},{'fast' if lim is fast else 'slow'})={ok}")
        now[0] += 1  # every op at its own instant; never across a boundary

        assert model.accounted() == C, (seed, trace)
        if not quota_on:
            continue
        held, grantable, _count = await real_accounting(repo, eid, quota, now[0])
        assert admitted <= C + topped_up, (seed, trace)
        assert admitted * 1000 + held + grantable == (C + topped_up) * 1000, (
            f"seed {seed}: admitted {admitted} + held {held / 1000} + grantable "
            f"{grantable / 1000} != {C} + {topped_up} after {trace}"
        )


# `--dist loadscope` sends a module's plain functions to one xdist worker; a
# class is its own scope, so the seeds are split across classes to run in
# parallel. 10 seeds per quota kind (v0.15 CI-speed ruling): breadth comes from
# the 300-seed pure differential in test_quota_grant_plan.py; this file checks
# that the real repository keeps I8 on a smaller sample.
class _Fuzz:
    KIND = "calendar"
    SEEDS: range = range(0)

    async def test_admitted_never_exceeds_and_nothing_is_lost(self, limiter, seed):
        await run_fuzz(limiter, seed, self.KIND)


class TestCalendarSeeds0(_Fuzz):
    SEEDS = range(0, 5)


class TestCalendarSeeds5(_Fuzz):
    SEEDS = range(5, 10)


class TestSessionSeeds0(_Fuzz):
    KIND = "session"
    SEEDS = range(0, 5)


class TestSessionSeeds5(_Fuzz):
    KIND = "session"
    SEEDS = range(5, 10)
