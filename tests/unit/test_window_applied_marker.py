"""The per-limit window-applied marker ``b_{name}_wa`` (#640, ADR-140).

Once a client predating ADR-139 can write a bucket that carries a duration
window (option B of #638: the config is hidden from it, so it no longer fails
the whole level), ``rf`` stops being a record that a shard applied its window.
The old writer stamps ``rf = :now`` from its own clock and knows nothing of
``ws``:

- a **backward** stamp (its clock is behind the opener's) makes ``ws > rf``
  hold again, and the next pass rolls the window a second time;
- a **forward** stamp on a sibling after a rollover fan-out makes ``ws > rf``
  false, and the sibling never applies the window.

These tests emulate that writer with raw ``UpdateItem`` calls that move only
what it moves (``rf``, and ``vu`` from its own limits), and pin that every
shard rolls exactly once per window whichever way the stamp goes.

Not a sync-generation source: the async limiter drives everything, and the
sync twin of every path exercised here is generated from the same modules.
"""

from dataclasses import replace

import pytest

from tests.fixtures.sharding import materialise, pinned_shard
from tests.fixtures.windows import FIVE_HOURS_MS, SESSION_10, T0
from zae_limiter import Limit, RateLimiter, RateLimitExceeded
from zae_limiter.bucket import would_refill_satisfy
from zae_limiter.models import BucketState
from zae_limiter.schema import (
    BUCKET_FIELD_RF,
    BUCKET_FIELD_TK,
    BUCKET_FIELD_VU,
    BUCKET_FIELD_WA,
    BUCKET_FIELD_WS,
    BUCKET_FIELD_WTC,
    bucket_attr,
    pk_bucket,
    sk_state,
)

RESOURCE = "gpt-4"
WA = bucket_attr("session", BUCKET_FIELD_WA)
WS = bucket_attr("session", BUCKET_FIELD_WS)
TK = bucket_attr("session", BUCKET_FIELD_TK)
WTC = bucket_attr("session", BUCKET_FIELD_WTC)
T1 = T0 + FIVE_HOURS_MS + 1_000  # the next window's start, after T0's has ended


def _key(repo, entity_id, shard):
    return {
        "PK": {"S": pk_bucket(repo._namespace_id, entity_id, RESOURCE, shard)},
        "SK": {"S": sk_state()},
    }


async def _raw(repo, entity_id, shard):
    client = await repo._get_client()
    response = await client.get_item(TableName=repo.table_name, Key=_key(repo, entity_id, shard))
    return response.get("Item") or {}


async def _num(repo, entity_id, shard, attr):
    raw = (await _raw(repo, entity_id, shard)).get(attr)
    return None if raw is None else int(raw["N"])


async def _old_writer(repo, entity_id, shard, rf):
    """What a pre-ADR-139 client's normal-path write does to the shared attributes.

    It stamps ``rf`` from its own clock under its own ``rf`` lock and, at entity
    level where none of *its* limits is scheduled, REMOVEs ``vu`` (verified
    against v0.14.0). It never touches ``ws``, ``wa`` or the session balance.
    """
    client = await repo._get_client()
    await client.update_item(
        TableName=repo.table_name,
        Key=_key(repo, entity_id, shard),
        UpdateExpression="SET #rf = :rf REMOVE #vu",
        ExpressionAttributeNames={"#rf": BUCKET_FIELD_RF, "#vu": BUCKET_FIELD_VU},
        ExpressionAttributeValues={":rf": {"N": str(rf)}},
    )


async def _unmark(repo, entity_id, shard):
    """An item written before the marker existed."""
    client = await repo._get_client()
    await client.update_item(
        TableName=repo.table_name,
        Key=_key(repo, entity_id, shard),
        UpdateExpression="REMOVE #wa",
        ExpressionAttributeNames={"#wa": WA},
    )


async def _set_tk(repo, entity_id, shard, milli):
    client = await repo._get_client()
    await client.update_item(
        TableName=repo.table_name,
        Key=_key(repo, entity_id, shard),
        UpdateExpression="SET #tk = :tk",
        ExpressionAttributeNames={"#tk": TK},
        ExpressionAttributeValues={":tk": {"N": str(milli)}},
    )


async def _slow_acquire(limiter, entity_id, shard, now_ms, amount=1):
    """One slow-path acquire pinned to ``shard``. Returns 1 if admitted, else 0."""
    repo = limiter._repository
    repo._now_ms = lambda: now_ms
    slow = RateLimiter(repository=repo, speculative_writes=False)
    with pinned_shard(shard):
        try:
            async with slow.acquire(entity_id, RESOURCE, consume={"session": amount}):
                return 1
        except RateLimitExceeded:
            return 0


async def _first_use(limiter, entity_id, now_ms, consume=1):
    repo = limiter._repository
    await repo.set_limits(entity_id, [SESSION_10], resource=RESOURCE)
    repo._now_ms = lambda: now_ms
    async with limiter.acquire(entity_id, RESOURCE, consume={"session": consume}):
        pass


async def _two_shards_then_rollover(limiter, entity_id, leftover=0):
    """Two shards on window T0; shard 1 down to ``leftover``; shard 0 opens T1 and fans out.

    Returns with shard 1 carrying ``ws = T1`` beside the old window's balance
    (burnt at 0 by default), unapplied — exactly the state a forward ``rf``
    stamp would hide, and a ``vu`` REMOVE would expose to the fast path.
    """
    repo = limiter._repository
    await _first_use(limiter, entity_id, T0, consume=0)
    assert await repo.bump_shard_count(entity_id, RESOURCE, 1) == 2
    repo._now_ms = lambda: T0 + 1_000
    assert await materialise(limiter, entity_id, "session", 1, resource=RESOURCE) == 1
    await _set_tk(repo, entity_id, 1, leftover * 1_000)

    assert await _slow_acquire(limiter, entity_id, 0, T1) == 1  # opens T1, fans out
    assert await _num(repo, entity_id, 0, WS) == T1
    assert await _num(repo, entity_id, 1, WS) == T1
    assert await _num(repo, entity_id, 1, WA) == T0, "the fan-out never marks a window applied"
    assert await _num(repo, entity_id, 1, TK) == leftover * 1_000


async def _drain_shard_1(limiter, entity_id, start_ms, fast=True, limit=20):
    """Acquire 1 on shard 1 until rejected. Returns how many were admitted.

    Shard 0 is emptied first: an exhausted shard's fast-path retry probes the
    others, and what shard 0 admits is not what is being counted.
    """
    repo = limiter._repository
    await _set_tk(repo, entity_id, 0, 0)
    lim = limiter if fast else RateLimiter(repository=repo, speculative_writes=False)
    admitted = 0
    for i in range(limit):
        repo._now_ms = lambda i=i: start_ms + i * 1_000
        with pinned_shard(1):
            try:
                async with lim.acquire(entity_id, RESOURCE, consume={"session": 1}):
                    admitted += 1
            except RateLimitExceeded:
                return admitted
    return admitted


def _image_record(item):
    return {"eventName": "MODIFY", "dynamodb": {"NewImage": item, "OldImage": item}}


def _table(repo):
    import boto3

    return boto3.resource("dynamodb", region_name="us-east-1").Table(repo.table_name)


class TestWindowRolledRule:
    """``BucketState.window_rolled``: ``ws > wa``, falling back to ``ws > rf``."""

    @staticmethod
    def _state(ws, rf, wa=None):
        state = BucketState.from_limit("e", RESOURCE, SESSION_10, rf)
        return replace(state, window_start_ms=ws, last_refill_ms=rf, window_applied_ms=wa)

    def test_an_unmarked_state_compares_against_rf(self):
        assert self._state(ws=200, rf=100).window_rolled
        assert not self._state(ws=100, rf=100).window_rolled

    def test_a_marked_state_ignores_a_backward_rf(self):
        """The double roll: `rf` stamped below `ws` by a slow clock."""
        assert not self._state(ws=200, rf=100, wa=200).window_rolled

    def test_a_marked_state_ignores_a_forward_rf(self):
        """The skipped roll: `rf` stamped past a fanned-out `ws`."""
        assert self._state(ws=300, rf=400, wa=200).window_rolled

    def test_no_window_never_rolls(self):
        assert not self._state(ws=None, rf=100, wa=50).window_rolled


class TestEveryWriterMarks:
    """Each v0.15 writer that brings a window's balance into being stamps ``wa``."""

    async def test_the_create_marks_the_window_it_opens(self, limiter):
        repo = limiter._repository
        await _first_use(limiter, "u", T0)
        assert await _num(repo, "u", 0, WA) == T0

    async def test_a_shard_joining_a_live_window_marks_it(self, limiter):
        repo = limiter._repository
        await _first_use(limiter, "u", T0)
        assert await repo.bump_shard_count("u", RESOURCE, 1) == 2
        repo._now_ms = lambda: T0 + 60_000
        assert await materialise(limiter, "u", "session", 1, resource=RESOURCE) == 1
        assert await _num(repo, "u", 1, WS) == T0
        assert await _num(repo, "u", 1, WA) == T0

    async def test_the_opener_marks_the_window_it_opens(self, limiter):
        repo = limiter._repository
        await _first_use(limiter, "u", T0)
        assert await _slow_acquire(limiter, "u", 0, T1) == 1
        assert await _num(repo, "u", 0, WS) == T1
        assert await _num(repo, "u", 0, WA) == T1

    async def test_a_pass_marks_an_unmarked_item_without_rolling_it(self, limiter):
        """The migration: an item written before the marker is marked by its
        next materialising pass, with the window the `rf` rule says it applied."""
        repo = limiter._repository
        await _first_use(limiter, "u", T0, consume=3)
        await _unmark(repo, "u", 0)
        assert await _slow_acquire(limiter, "u", 0, T0 + 1_000) == 1
        assert await _num(repo, "u", 0, WA) == T0
        assert await _num(repo, "u", 0, TK) == 6_000, "no second roll"

    async def test_a_pass_rolls_an_unmarked_item_by_rf_once_and_marks_it(self, limiter):
        """An unmarked shard holding a fanned-out window still rolls by the
        pre-#640 rule, exactly once, and the marker then records it."""
        repo = limiter._repository
        await _two_shards_then_rollover(limiter, "u")
        await _unmark(repo, "u", 1)
        assert await _slow_acquire(limiter, "u", 1, T1 + 1_000) == 1
        assert await _num(repo, "u", 1, WA) == T1
        assert await _num(repo, "u", 1, TK) == 4_000
        assert await _slow_acquire(limiter, "u", 1, T1 + 2_000) == 1
        assert await _num(repo, "u", 1, TK) == 3_000

    async def test_a_seed_joining_a_live_window_marks_it(self, limiter):
        """A shard that exists without the session quota seeds it by joining
        shard 0's live window (and a move off shard 0, ADR-145), and marks it."""
        repo = limiter._repository
        await _first_use(limiter, "u", T0)
        assert await repo.bump_shard_count("u", RESOURCE, 1) == 2
        rpm = Limit.per_minute("rpm", 100)
        states = [BucketState.from_limit("u", RESOURCE, rpm, T0, shard_count=2)]
        await repo.transact_write(
            [repo.build_composite_create("u", RESOURCE, states, T0, shard_id=1, shard_count=2)]
        )
        assert await _slow_acquire(limiter, "u", 1, T0 + 60_000) == 1
        assert await _num(repo, "u", 1, WS) == T0
        assert await _num(repo, "u", 1, WA) == T0
        assert await _num(repo, "u", 1, TK) == 4_000


class TestOldWriterClockSkew:
    """A shard rolls exactly once per window, whichever way an old writer stamps ``rf``."""

    async def test_a_backward_rf_does_not_roll_the_window_again(self, limiter):
        """The double roll (over-admission). Before the marker the next slow
        pass read `ws > rf`, restored 10 and admitted: 3 + 1 spent from a
        window whose balance said 7 had been left, then 9 more available."""
        repo = limiter._repository
        await _first_use(limiter, "u", T0, consume=3)
        await _old_writer(repo, "u", 0, rf=T0 - 5_000)
        assert await _slow_acquire(limiter, "u", 0, T0 + 1_000) == 1
        assert await _num(repo, "u", 0, TK) == 6_000

    async def test_a_forward_rf_after_a_fan_out_still_rolls_the_sibling(self, limiter):
        """The skipped roll (under-admission for a whole window). Before the
        marker the sibling read `ws > rf` as false and kept its burnt 0."""
        repo = limiter._repository
        await _two_shards_then_rollover(limiter, "u")
        await _old_writer(repo, "u", 1, rf=T1 + 60_000)
        assert await _slow_acquire(limiter, "u", 1, T1 + 120_000) == 1
        assert await _num(repo, "u", 1, TK) == 4_000
        assert await _num(repo, "u", 1, WA) == T1

    async def test_the_sibling_then_rolls_no_more_under_either_stamp(self, limiter):
        repo = limiter._repository
        await _two_shards_then_rollover(limiter, "u")
        await _old_writer(repo, "u", 1, rf=T1 + 60_000)
        admitted = await _slow_acquire(limiter, "u", 1, T1 + 120_000)
        for stamp, now in ((T1 - 10_000, T1 + 180_000), (T1 + 999_000, T1 + 240_000)):
            await _old_writer(repo, "u", 1, rf=stamp)
            admitted += await _slow_acquire(limiter, "u", 1, now)
        # One share of 5 for window T1 on shard 1, never restored twice.
        assert admitted == 3
        assert await _num(repo, "u", 1, TK) == 2_000

    async def test_a_forward_rf_before_the_fan_out_no_longer_blocks_it(self, limiter):
        """An old write that lands after the sibling's window ended but before
        the fan-out left `rf` past the new start. Unmarked, the fan-out had to
        skip it (it would read the window as applied) and the entity ran two
        window phases; marked, it moves and rolls into the same window."""
        repo = limiter._repository
        await _first_use(limiter, "u", T0, consume=0)
        assert await repo.bump_shard_count("u", RESOURCE, 1) == 2
        repo._now_ms = lambda: T0 + 1_000
        assert await materialise(limiter, "u", "session", 1, resource=RESOURCE) == 1
        await _old_writer(repo, "u", 1, rf=T1 + 30_000)

        assert await _slow_acquire(limiter, "u", 0, T1) == 1  # opens T1, fans out
        assert await _num(repo, "u", 1, WS) == T1
        assert await _slow_acquire(limiter, "u", 1, T1 + 60_000) == 1
        assert await _num(repo, "u", 1, WS) == T1, "one window phase, not two"
        assert await _num(repo, "u", 1, TK) == 4_000


class TestAggregatorUnderOldWriters:
    """The aggregator asks the same question (``processor._window_applied``)."""

    async def test_a_backward_rf_is_not_rolled_again(self, limiter):
        from zae_limiter_aggregator.processor import aggregate_bucket_states, try_refill_bucket

        repo = limiter._repository
        await _first_use(limiter, "u", T0, consume=3)
        await _old_writer(repo, "u", 0, rf=T0 - 5_000)
        item = await _raw(repo, "u", 0)
        (state,) = aggregate_bucket_states([_image_record(item)]).values()
        try_refill_bucket(_table(repo), state, T0 + 1_000)
        assert await _num(repo, "u", 0, TK) == 7_000

    async def test_a_forward_rf_after_a_fan_out_is_rolled_and_marked(self, limiter):
        from zae_limiter_aggregator.processor import aggregate_bucket_states, try_refill_bucket

        repo = limiter._repository
        await _two_shards_then_rollover(limiter, "u")
        await _old_writer(repo, "u", 1, rf=T1 + 60_000)
        item = await _raw(repo, "u", 1)
        (state,) = aggregate_bucket_states([_image_record(item)]).values()
        assert try_refill_bucket(_table(repo), state, T1 + 120_000) is True
        assert await _num(repo, "u", 1, TK) == 5_000
        assert await _num(repo, "u", 1, WA) == T1
        # And a client pass afterwards does not roll it again.
        assert await _slow_acquire(limiter, "u", 1, T1 + 180_000) == 1
        assert await _num(repo, "u", 1, TK) == 4_000

    async def test_a_roll_marks_an_unmarked_item(self, limiter):
        from zae_limiter_aggregator.processor import aggregate_bucket_states, try_refill_bucket

        repo = limiter._repository
        await _two_shards_then_rollover(limiter, "u")
        await _unmark(repo, "u", 1)
        item = await _raw(repo, "u", 1)
        (state,) = aggregate_bucket_states([_image_record(item)]).values()
        assert try_refill_bucket(_table(repo), state, T1 + 120_000) is True
        assert await _num(repo, "u", 1, WA) == T1


class TestFastPathReadsAPendingRollAsRestored:
    """An old writer REMOVEs ``vu``, so the fast path can see a fanned-out,
    unapplied window beside the old window's burnt balance. Judged as it
    stood, every request on that shard was fast-rejected until the window
    ended; it now goes to the slow path, which rolls it."""

    @staticmethod
    def _image(wa):
        state = BucketState.from_limit("e", RESOURCE, SESSION_10, T0, shard_count=2)
        return replace(
            state,
            tokens_milli=0,
            window_start_ms=T1,
            window_applied_ms=wa,
            last_refill_ms=T1 + 60_000,
        )

    def test_an_unapplied_live_window_is_worth_a_slow_pass(self):
        would_help, statuses = would_refill_satisfy([self._image(wa=T0)], {"session": 1}, T1 + 1)
        assert would_help
        assert statuses[0].available == 5

    def test_an_applied_live_window_is_still_judged_as_it_stands(self):
        would_help, _ = would_refill_satisfy([self._image(wa=T1)], {"session": 1}, T1 + 1)
        assert not would_help

    @pytest.mark.parametrize("leftover", [0, 3])
    async def test_end_to_end_the_shard_admits_one_share_in_the_window(self, limiter, leftover):
        """Burnt (0): the first request is admitted by the roll rather than
        fast-rejected. Leftover (3): the fast path spends the old window's
        leftover first, and the roll charges those debits (#640 review: it
        used to SET the full share on top — 8 admitted against a share of 5)."""
        repo = limiter._repository
        await _two_shards_then_rollover(limiter, "u", leftover=leftover)
        await _set_tk(repo, "u", 0, 0)  # no other shard can absorb the request
        await _old_writer(repo, "u", 1, rf=T1 + 60_000)
        assert await _drain_shard_1(limiter, "u", T1 + 120_000) == 5
        assert await _num(repo, "u", 1, WA) == T1


class TestAPendingRollChargesWhatWasSpentMeanwhile:
    """#640 review: a roll that SETs the share forgave every debit made while it
    was pending. The fan-out snapshots ``tc`` as ``wtc``; the roll targets
    ``eff_cp - max(0, tc - wtc)`` on the client and in the aggregator."""

    async def test_the_reviewers_repro(self, limiter):
        """Leftover 3 on shard 1, then a v0.14-style ``REMOVE vu``: shard 1 used
        to admit 3 from the leftover and then a fresh 5 (13 against 10 across
        the entity). It now admits its share of 5 in window T1, no more."""
        repo = limiter._repository
        await _two_shards_then_rollover(limiter, "u", leftover=3)
        assert await _num(repo, "u", 1, WTC) == 1_000, "tc snapshot at the fan-out"
        await _old_writer(repo, "u", 1, rf=T1 + 10_000)
        assert await _drain_shard_1(limiter, "u", T1 + 20_000) == 5

    async def test_the_aggregator_roll_charges_debits_in_its_image(self, limiter):
        from zae_limiter_aggregator.processor import aggregate_bucket_states, try_refill_bucket

        repo = limiter._repository
        await _two_shards_then_rollover(limiter, "u", leftover=3)
        await _old_writer(repo, "u", 1, rf=T1 + 10_000)
        # The fast path spends 2 of the leftover inside window T1, roll pending.
        repo._now_ms = lambda: T1 + 20_000
        for _ in range(2):
            with pinned_shard(1):
                async with limiter.acquire("u", RESOURCE, consume={"session": 1}):
                    pass
        assert await _num(repo, "u", 1, WA) == T0, "still pending"
        item = await _raw(repo, "u", 1)
        (state,) = aggregate_bucket_states([_image_record(item)]).values()
        assert try_refill_bucket(_table(repo), state, T1 + 30_000) is True
        assert await _num(repo, "u", 1, TK) == 3_000  # 5 less the 2 spent
        assert await _num(repo, "u", 1, WA) == T1
        assert await _drain_shard_1(limiter, "u", T1 + 40_000) == 3

    async def test_a_retry_that_lost_the_lock_to_an_old_write_is_charged(self, limiter):
        """The slow pass computes the roll, a v0.14 write takes its ``rf`` lock,
        and the consumption-only retry (which applies no roll) debits the
        leftover. The next pass rolls to the share less that debit."""
        repo = limiter._repository
        await _two_shards_then_rollover(limiter, "u", leftover=3)
        real = repo.transact_write
        calls = []

        async def lose_the_first_lock(items):
            if not calls:
                calls.append(1)
                await _old_writer(repo, "u", 1, rf=T1 + 15_000)
            return await real(items)

        repo.transact_write = lose_the_first_lock
        try:
            assert await _slow_acquire(limiter, "u", 1, T1 + 20_000) == 1
        finally:
            repo.transact_write = real
        assert await _num(repo, "u", 1, WA) == T0, "the retry applies no roll"
        assert await _num(repo, "u", 1, TK) == 2_000
        assert 1 + await _drain_shard_1(limiter, "u", T1 + 30_000, fast=False) == 5

    async def test_adjust_debt_on_a_pending_shard_is_charged(self, limiter):
        """Why zeroing the leftover in the fan-out would not do: a zero-estimate
        lease passes ``tk >= 0`` on an empty shard and ``adjust()`` puts it in
        debt, which a SET-to-share roll forgives. The snapshot charges it."""
        repo = limiter._repository
        await _two_shards_then_rollover(limiter, "u", leftover=0)
        await _old_writer(repo, "u", 1, rf=T1 + 10_000)
        # Shard 1 would otherwise be fast-rejected at 0 and rolled; a zero
        # estimate is admitted by the fast path on the empty balance.
        await _set_tk(repo, "u", 1, 0)
        repo._now_ms = lambda: T1 + 20_000
        with pinned_shard(1):
            async with limiter.acquire("u", RESOURCE, consume={"session": 0}) as lease:
                await lease.adjust(session=4)
        assert await _num(repo, "u", 1, WA) == T0, "the zero-estimate took the fast path"
        assert await _drain_shard_1(limiter, "u", T1 + 30_000) == 1  # 5 - 4

    async def test_a_net_credit_never_lifts_the_roll_above_the_share(self):
        state = BucketState.from_limit("e", RESOURCE, SESSION_10, T1, shard_count=2)
        state = replace(
            state,
            window_start_ms=T1,
            window_applied_ms=T0,
            total_consumed_milli=1_000,
            window_consumed_mark_milli=4_000,  # an old-window lease rolled back 3
        )
        assert state.window_roll_target_milli(T1 + 1) == 5_000

    async def test_a_clone_of_a_pending_shard_restarts_its_snapshot(self, limiter):
        from zae_limiter_aggregator.processor import propagate_shard_count

        repo = limiter._repository
        await _two_shards_then_rollover(limiter, "u", leftover=3)
        old_image = await _raw(repo, "u", 1)
        assert await repo.bump_shard_count("u", RESOURCE, 2) == 4
        new_image = await _raw(repo, "u", 0)
        record = {"eventName": "MODIFY", "dynamodb": {"NewImage": new_image, "OldImage": old_image}}
        # Shard 0 is the source of truth for Path 2; fake its image as pending.
        for image in (record["dynamodb"]["NewImage"], record["dynamodb"]["OldImage"]):
            image[WA] = {"N": str(T0)}
            image[WTC] = {"N": "7000"}
        assert propagate_shard_count(_table(repo), record, T1 + 60_000) >= 1
        clone = await _raw(repo, "u", 2)
        assert clone[WTC] == {"N": "0"}
        assert clone[bucket_attr("session", "tc")] == {"N": "0"}
