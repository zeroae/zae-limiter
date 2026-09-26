"""A shard created mid-window joins the window in progress (ADR-139, #625).

``BucketState.from_limit`` stamps ``ws = now``, which is right for an entity's
first shard and wrong for every later one: shards that each opened their own
window leave the entity with no single ``resets_at_ms``. The create path reads
shard 0's ``ws`` and inherits it while the window is still live.

Not a sync-generation source: these drive the async limiter through the async
helpers in ``tests/fixtures/sharding.py``, and the sync twin of the create path
is generated from the same ``limiter.py``.
"""

from unittest.mock import patch

import pytest

from tests.fixtures.sharding import (
    drain_wcu,
    materialise,
    pinned_shard,
    spendable,
    walk_doublings,
)
from tests.fixtures.windows import FIVE_HOURS_MS, SESSION_10, T0
from zae_limiter import RateLimiter, RateLimiterUnavailable
from zae_limiter.schema import (
    BUCKET_FIELD_RF,
    BUCKET_FIELD_TK,
    BUCKET_FIELD_VU,
    BUCKET_FIELD_WS,
    bucket_attr,
    pk_bucket,
    sk_state,
)

RESOURCE = "gpt-4"


def _key(repo, entity_id, shard):
    return {
        "PK": {"S": pk_bucket(repo._namespace_id, entity_id, RESOURCE, shard)},
        "SK": {"S": sk_state()},
    }


async def _raw(repo, entity_id, shard):
    client = await repo._get_client()
    response = await client.get_item(TableName=repo.table_name, Key=_key(repo, entity_id, shard))
    return response.get("Item") or {}


async def _stored_ws_on(repo, entity_id, limit_name, shard):
    attr = (await _raw(repo, entity_id, shard)).get(bucket_attr(limit_name, BUCKET_FIELD_WS))
    return None if attr is None else int(attr["N"])


async def _stored_tk_on(repo, entity_id, limit_name, shard):
    item = await _raw(repo, entity_id, shard)
    return int(item[bucket_attr(limit_name, BUCKET_FIELD_TK)]["N"])


async def _balances(repo, entity_id, shard_count):
    return [await _stored_tk_on(repo, entity_id, "session", s) for s in range(shard_count)]


async def _first_use(limiter, entity_id, now_ms, consume=1):
    """Configure ``SESSION_10`` for ``entity_id`` and anchor its window on shard 0."""
    repo = limiter._repository
    await repo.set_limits(entity_id, [SESSION_10], resource=RESOURCE)
    repo._now_ms = lambda: now_ms
    async with limiter.acquire(entity_id, RESOURCE, consume={"session": consume}):
        pass
    assert await _stored_ws_on(repo, entity_id, "session", 0) == now_ms


class TestNewShardJoinsTheWindow:
    """The created shard's ``ws`` is shard 0's while that window is live."""

    @pytest.mark.parametrize("offset_ms", [60_000, FIVE_HOURS_MS - 1])
    async def test_a_new_shard_inherits_the_window_in_progress(self, limiter, offset_ms):
        """A shard created mid-window joins it rather than opening its own.

        The created shard sets ``rf = now`` and inherits ``ws``, so ``ws > rf``
        is FALSE on the new item and it does not immediately re-roll itself.
        The doubling is forced the #480-permitted way: drain ``wcu`` only.
        """
        repo = limiter._repository
        await _first_use(limiter, "user-1", T0)
        await drain_wcu(repo, "user-1", 0, resource=RESOURCE)

        now = T0 + offset_ms
        repo._now_ms = lambda: now
        assert await materialise(limiter, "user-1", "session", 0, resource=RESOURCE) == 1
        assert repo._entity_cache[(repo._namespace_id, "user-1")][2][RESOURCE] == 2

        assert await _stored_ws_on(repo, "user-1", "session", 1) == T0
        item = await _raw(repo, "user-1", 1)
        assert int(item[BUCKET_FIELD_RF]["N"]) == now
        assert int(item[BUCKET_FIELD_VU]["N"]) == T0 + FIVE_HOURS_MS, "vu at the joined end"
        # Joining a live window keeps the #587 transfer: shard 0 held 9 against
        # a new share of 5, so 4 moved and the creating acquire spent 1.
        assert await _balances(repo, "user-1", 2) == [5_000, 3_000]
        assert await spendable(repo, "user-1", "session", 2, resource=RESOURCE) == 8
        # And a live-inherit create never fans out: shard 0 keeps its window.
        assert await _stored_ws_on(repo, "user-1", "session", 0) == T0

    async def test_a_new_shard_with_no_sibling_window_opens_one(self, limiter):
        """Shard 0 swept by TTL (possible only for resource- and system-level
        configs — ADR-136 gives entity-level buckets none). The degraded case,
        and the same durability asymmetry ADR-139 records.
        """
        repo = limiter._repository
        await _first_use(limiter, "user-1", T0)
        assert await repo.bump_shard_count("user-1", RESOURCE, 1) == 2
        client = await repo._get_client()
        await client.delete_item(TableName=repo.table_name, Key=_key(repo, "user-1", 0))

        t_now = T0 + 60_000
        repo._now_ms = lambda: t_now
        assert await materialise(limiter, "user-1", "session", 1, resource=RESOURCE) == 1
        assert await _stored_ws_on(repo, "user-1", "session", 1) == t_now
        # Nothing to transfer from, so the shard takes its full share of 5.
        assert await _stored_tk_on(repo, "user-1", "session", 1) == 4_000

    @pytest.mark.parametrize("offset_ms", [FIVE_HOURS_MS, FIVE_HOURS_MS + 3_600_000])
    async def test_a_new_shard_after_the_window_ended_opens_its_own(self, limiter, offset_ms):
        """Shard 0's window has ended (the end instant itself included: the
        window is half-open). Inheriting its ``ws`` would force an immediate
        re-roll, so the shard anchors at ``now`` — and starts at its full share,
        not the transfer: shard 0's spent balance belongs to the window that
        ended, and shard 0 restores its own share when it next rolls.
        """
        repo = limiter._repository
        await _first_use(limiter, "user-1", T0, consume=10)
        assert await repo.bump_shard_count("user-1", RESOURCE, 1) == 2

        now = T0 + offset_ms
        repo._now_ms = lambda: now
        assert await materialise(limiter, "user-1", "session", 1, resource=RESOURCE) == 1
        assert await _stored_ws_on(repo, "user-1", "session", 1) == now
        assert await _stored_tk_on(repo, "user-1", "session", 1) == 4_000
        item = await _raw(repo, "user-1", 1)
        assert int(item[BUCKET_FIELD_VU]["N"]) == now + FIVE_HOURS_MS
        # The new anchor fans out, so the entity runs one window phase, not
        # two: shard 0 moves onto it (its own window had ended) but keeps its
        # balance until it applies the roll under its own lock.
        assert await _stored_ws_on(repo, "user-1", "session", 0) == now
        assert await _stored_tk_on(repo, "user-1", "session", 0) == 0

        # Shard 0 then rolls on its own next pass, into the SAME window.
        repo._now_ms = lambda: now + 1
        assert await materialise(limiter, "user-1", "session", 0, resource=RESOURCE) == 1
        assert await _stored_ws_on(repo, "user-1", "session", 0) == now
        assert await spendable(repo, "user-1", "session", 2, resource=RESOURCE) == 8

    async def test_a_dead_windows_surplus_is_clamped_and_the_new_shard_gets_its_share(
        self, limiter
    ):
        """Shard 0 still holds 9 from the window that ended. The reclaim clamps
        it to the new share of 5 and the 4 it takes are thrown away: the new
        shard starts at exactly its share, never share plus the dead surplus.
        """
        repo = limiter._repository
        await _first_use(limiter, "user-1", T0)
        assert await repo.bump_shard_count("user-1", RESOURCE, 1) == 2

        now = T0 + FIVE_HOURS_MS + 3_600_000
        repo._now_ms = lambda: now
        assert await materialise(limiter, "user-1", "session", 1, resource=RESOURCE) == 1
        assert await _balances(repo, "user-1", 2) == [5_000, 4_000]
        assert await _stored_ws_on(repo, "user-1", "session", 1) == now

    async def test_a_cascade_parent_shard_inherits_the_parents_window(self, limiter):
        """ADR-139: parent and child anchor independently, so the parent shard's
        ``ws`` is resolved with the PARENT's own read — never reused from the
        child, whose window may be hours out of step.
        """
        repo = limiter._repository
        slow = RateLimiter(repository=repo, speculative_writes=False)
        await repo.create_entity("parent")
        await repo.create_entity("child", parent_id="parent", cascade=True)
        await repo.set_limits("parent", [SESSION_10], resource=RESOURCE)
        await repo.set_limits("child", [SESSION_10], resource=RESOURCE)

        t_parent = T0 - 3_600_000
        repo._now_ms = lambda: t_parent
        async with slow.acquire("parent", RESOURCE, consume={"session": 1}):
            pass
        repo._now_ms = lambda: T0
        async with slow.acquire("child", RESOURCE, consume={"session": 1}):
            pass
        assert await repo.bump_shard_count("child", RESOURCE, 1) == 2
        assert await repo.bump_shard_count("parent", RESOURCE, 1) == 2

        repo._now_ms = lambda: T0 + 60_000
        with patch("zae_limiter.repository.random.randrange", return_value=1):
            async with slow.acquire("child", RESOURCE, consume={"session": 1}):
                pass

        assert await _stored_ws_on(repo, "parent", "session", 1) == t_parent
        assert await _stored_ws_on(repo, "child", "session", 1) == T0

    async def test_a_new_quota_shard_is_still_filled_by_transfer(self, limiter):
        """A duration window is a quota, so PR #594's reclaim-then-grant must
        fire for it too — a mint here would be #587 again for this feature.
        Measured as #594 measures it: sum(max(0, tk)) across shards.

        Shard 0 holds 7 against a new share of 5, so exactly 2 transfer. A mint
        would create shard 1 at 5 and leave the entity with 9 spendable after
        one admission instead of 6.
        """
        repo = limiter._repository
        await _first_use(limiter, "user-1", T0, consume=3)
        await drain_wcu(repo, "user-1", 0, resource=RESOURCE)
        repo._now_ms = lambda: T0 + 60_000

        before = await spendable(repo, "user-1", "session", 1, resource=RESOURCE)
        assert before == 7
        assert await materialise(limiter, "user-1", "session", 0, resource=RESOURCE) == 1
        assert await _balances(repo, "user-1", 2) == [5_000, 1_000]
        after = await spendable(repo, "user-1", "session", 2, resource=RESOURCE)
        assert after == before - 1, "conserved, less the one token admitted"

    async def test_shard_zero_being_created_never_reads_a_sibling(self, limiter):
        """Shard 0 is the source of truth: its creation is an entity's first
        bucket or a TTL recreation, both of which rightly open a window."""
        repo = limiter._repository
        with patch.object(repo, "get_shard_window_starts") as read:
            await _first_use(limiter, "user-1", T0)
        read.assert_not_called()

    async def test_a_limit_with_no_window_is_not_read_for(self, limiter):
        """A dripping limit's new shard costs no sibling read at all."""
        from zae_limiter import Limit

        repo = limiter._repository
        await repo.set_limits("user-1", [Limit.per_minute("rpm", 100)], resource=RESOURCE)
        repo._now_ms = lambda: T0
        async with limiter.acquire("user-1", RESOURCE, consume={"rpm": 1}):
            pass
        assert await repo.bump_shard_count("user-1", RESOURCE, 1) == 2
        with patch.object(repo, "get_shard_window_starts") as read:
            assert await materialise(limiter, "user-1", "rpm", 1, resource=RESOURCE) == 1
        read.assert_not_called()


class TestSiblingReadIsConsistent:
    """The sibling ``ws`` read is strongly consistent (ADR-139).

    A shard is usually created just after shard 0 was written — often by the
    very acquire that rolled shard 0's window. An eventually consistent read can
    return the pre-roll ``ws``, which looks ended, and the create path would
    then grant a fresh full share on top of shard 0's new window.
    """

    async def test_the_read_asks_for_a_consistent_read(self, limiter):
        repo = limiter._repository
        await _first_use(limiter, "user-1", T0)
        client = await repo._get_client()
        real_get_item = client.get_item
        calls = []

        async def spy(**kwargs):
            calls.append(kwargs)
            return await real_get_item(**kwargs)

        with patch.object(client, "get_item", spy):
            await repo.get_shard_window_starts("user-1", RESOURCE, ["session"])
        assert [c.get("ConsistentRead") for c in calls] == [True]

    async def test_a_shard_created_right_after_a_roll_joins_the_new_window(self, limiter):
        """Reviewer's shape: shard 0 spent, its window ends, one acquire rolls
        it, and the next doubling creates shard 1. An eventually consistent
        replica still holding the pre-roll ``ws`` is simulated by answering any
        non-consistent window read with ``ws = T0``; the consistent read sees
        the roll, so the new shard takes the transfer and the window's total
        (admitted + still spendable) stays within the quota of 10.
        """
        repo = limiter._repository
        await _first_use(limiter, "user-1", T0, consume=10)

        rolled_at = T0 + FIVE_HOURS_MS + 3_600_000
        repo._now_ms = lambda: rolled_at
        assert await materialise(limiter, "user-1", "session", 0, resource=RESOURCE) == 1
        assert await _stored_ws_on(repo, "user-1", "session", 0) == rolled_at
        admitted = 1

        await drain_wcu(repo, "user-1", 0, resource=RESOURCE)
        now = rolled_at + 60_000
        repo._now_ms = lambda: now

        client = await repo._get_client()
        real_get_item = client.get_item
        ws_attr = bucket_attr("session", BUCKET_FIELD_WS)

        async def stale_replica(**kwargs):
            if "ProjectionExpression" in kwargs and not kwargs.get("ConsistentRead"):
                return {"Item": {ws_attr: {"N": str(T0)}}}
            return await real_get_item(**kwargs)

        with patch.object(client, "get_item", stale_replica):
            admitted += await materialise(limiter, "user-1", "session", 0, resource=RESOURCE)
        assert repo._entity_cache[(repo._namespace_id, "user-1")][2][RESOURCE] == 2

        assert await _stored_ws_on(repo, "user-1", "session", 1) == rolled_at
        left = await spendable(repo, "user-1", "session", 2, resource=RESOURCE)
        assert admitted + left <= 10, (admitted, left)


class TestGetShardWindowStarts:
    """``Repository.get_shard_window_starts`` — the projected sibling read."""

    async def test_no_limit_names_reads_nothing(self, limiter):
        repo = limiter._repository
        with patch.object(repo, "_get_client") as get_client:
            assert await repo.get_shard_window_starts("user-1", RESOURCE, []) == {}
        get_client.assert_not_called()

    async def test_reads_the_stored_start_of_each_named_window(self, limiter):
        repo = limiter._repository
        await _first_use(limiter, "user-1", T0)
        assert await repo.get_shard_window_starts("user-1", RESOURCE, ["session"]) == {
            "session": T0
        }

    async def test_a_missing_shard_or_limit_is_absent_from_the_result(self, limiter):
        repo = limiter._repository
        await _first_use(limiter, "user-1", T0)
        assert await repo.get_shard_window_starts("nobody", RESOURCE, ["session"]) == {}
        assert await repo.get_shard_window_starts("user-1", RESOURCE, ["session", "other"]) == {
            "session": T0
        }
        assert await repo.get_shard_window_starts("user-1", RESOURCE, ["session"], 1) == {}

    async def test_a_corrupt_stored_start_is_reported_as_unavailable(self, limiter):
        repo = limiter._repository
        await _first_use(limiter, "user-1", T0)
        client = await repo._get_client()
        await client.update_item(
            TableName=repo.table_name,
            Key=_key(repo, "user-1", 0),
            UpdateExpression="SET #ws = :bad",
            ExpressionAttributeNames={"#ws": bucket_attr("session", BUCKET_FIELD_WS)},
            ExpressionAttributeValues={":bad": {"N": "1.5"}},
        )
        with pytest.raises(RateLimiterUnavailable, match="b_session_ws"):
            await repo.get_shard_window_starts("user-1", RESOURCE, ["session"])


class TestDoublingWalkConservesAWindow:
    """PR #594's invariant, restated for a duration window and walked to
    ``MAX_SHARD_COUNT``.

    Measured as #594 measures it — ``sum(max(0, tk))`` across shards, debt
    excluded — because debt is dead weight for a quota: its rate is zero and
    its reset SETS the balance rather than adding to it. The single doubling is
    pinned above; these walk every generation, where a created shard that
    opened a fresh window instead of joining the live one would mint a share
    per doubling.
    """

    LIMIT_NAME = "session"

    async def _anchor(self, limiter, entity_id):
        from datetime import timedelta

        from zae_limiter import Limit

        repo = limiter._repository
        limit = Limit.quota(self.LIMIT_NAME, 1_000, reset_after=timedelta(hours=5))
        await repo.set_limits(entity_id, [limit], resource=RESOURCE)
        repo._now_ms = lambda: T0
        async with limiter.acquire(entity_id, RESOURCE, consume={self.LIMIT_NAME: 1}):
            pass

    async def test_a_spent_entity_walk_mints_nothing(self, limiter):
        """The issue's probe (`walk_doublings`): every shard drained, then
        `wcu` tripped, five generations. Nothing is left to transfer, so no
        created shard may start with anything."""
        from zae_limiter import schema

        repo = limiter._repository
        await self._anchor(limiter, "walker")

        admitted, shard_count, spends = await walk_doublings(
            limiter, "walker", self.LIMIT_NAME, resource=RESOURCE
        )

        assert shard_count == schema.MAX_SHARD_COUNT
        for index, (before, after) in enumerate(spends):
            assert after == before, f"doubling {index}: {before} -> {after}"
        left = await spendable(repo, "walker", self.LIMIT_NAME, shard_count, resource=RESOURCE)
        assert 1 + admitted + left <= 1_000

    async def test_a_full_entity_walk_joins_one_window_and_never_grows(self, limiter):
        """The other end: nothing drained but `wcu`, so every doubling has a
        surplus to transfer and every new shard is created (by a zero-token
        draw, which spends nothing). All 32 must join the window anchored at
        ``T0`` — a shard that opened its own would start at a fresh full share
        — and the spendable total may only fall (a clamp's excess beyond one
        share is discarded, #587), never rise."""
        from zae_limiter import schema

        repo = limiter._repository
        await self._anchor(limiter, "full-walker")
        nothing = {self.LIMIT_NAME: 0}
        shard_count = 1
        totals = [await spendable(repo, "full-walker", self.LIMIT_NAME, 1, resource=RESOURCE)]
        assert totals == [999]

        while shard_count < schema.MAX_SHARD_COUNT:
            for shard in range(shard_count):
                await drain_wcu(repo, "full-walker", shard, resource=RESOURCE)
            with pinned_shard(0):
                async with limiter.acquire("full-walker", RESOURCE, consume=nothing):
                    pass
            shard_count = repo._entity_cache[(repo._namespace_id, "full-walker")][2][RESOURCE]
            for shard in range(shard_count):
                if await _stored_ws_on(repo, "full-walker", self.LIMIT_NAME, shard) is None:
                    with pinned_shard(shard):
                        async with limiter.acquire("full-walker", RESOURCE, consume=nothing):
                            pass
            totals.append(
                await spendable(
                    repo, "full-walker", self.LIMIT_NAME, shard_count, resource=RESOURCE
                )
            )

        assert shard_count == schema.MAX_SHARD_COUNT
        assert totals[1] == 999, "the first doubling transfers the whole surplus"
        assert all(b >= a for a, b in zip(totals[1:], totals, strict=False)), totals
        assert totals[-1] > 0, totals
        starts = {
            await _stored_ws_on(repo, "full-walker", self.LIMIT_NAME, s) for s in range(shard_count)
        }
        assert starts == {T0}, "every created shard joined the live window"


class TestAggregatorCloneOfAnUnappliedShardZero:
    """Path 2 clones shard 0 while shard 0 carries a window it has not applied.

    Shard 0 received a fan-out (``ws > rf``, ``vu = 0``) and the aggregator's
    proactive sharding doubles it before any client draws it. The clone copies
    ``ws``/``rsa``/``rf``/``vu`` verbatim, so it is unapplied too, and whatever
    the #587 transfer granted it is overwritten when it rolls — a SET to the
    share, never an ADD. After both shards roll the entity holds exactly one
    allowance, whichever writer applies the roll.
    """

    WINDOW_S = FIVE_HOURS_MS // 1000
    T1 = T0 + FIVE_HOURS_MS + 1_000  # the fanned-out window's start

    @staticmethod
    def _table(repo):
        import boto3

        return boto3.resource("dynamodb", region_name="us-east-1").Table(repo.table_name)

    @staticmethod
    def _record(new_image, old_image):
        return {"eventName": "MODIFY", "dynamodb": {"NewImage": new_image, "OldImage": old_image}}

    @pytest.mark.parametrize("aggregator_first", [False, True], ids=["client", "aggregator"])
    @pytest.mark.parametrize("spent", [0, 8])
    async def test_both_shards_rolling_restore_one_allowance(
        self, limiter, spent, aggregator_first
    ):
        from zae_limiter_aggregator.processor import (
            aggregate_bucket_states,
            propagate_shard_count,
            try_refill_bucket,
        )

        repo = limiter._repository
        table = self._table(repo)
        if spent:
            await _first_use(limiter, "user-1", T0, consume=spent)
        else:
            # A zero-token first use anchors the window without spending.
            await _first_use(limiter, "user-1", T0, consume=0)

        # A sibling opened the next window and fanned it out: shard 0 now
        # carries it unapplied.
        written = await repo._propagate_window_start(
            "user-1", RESOURCE, 1, 2, {"session": (self.T1, self.WINDOW_S)}
        )
        assert written == 1
        old_image = await _raw(repo, "user-1", 0)
        assert int(old_image[BUCKET_FIELD_RF]["N"]) < self.T1
        assert int(old_image[BUCKET_FIELD_VU]["N"]) == 0

        # Proactive sharding doubles shard 0; its stream record reaches Path 2.
        assert await repo.bump_shard_count("user-1", RESOURCE, 1) == 2
        new_image = await _raw(repo, "user-1", 0)
        now = self.T1 + 60_000
        assert propagate_shard_count(table, self._record(new_image, old_image), now) == 1

        clone = await _raw(repo, "user-1", 1)
        assert await _stored_ws_on(repo, "user-1", "session", 1) == self.T1
        assert clone[BUCKET_FIELD_RF] == new_image[BUCKET_FIELD_RF], "rf copied: unapplied"
        assert int(clone[BUCKET_FIELD_VU]["N"]) == 0

        if aggregator_first:
            # The aggregator applies the window it sees on each shard's image.
            # A shard already holding exactly its share has nothing to restore
            # and is left for the client, which then applies it as a SET.
            rolled = []
            for shard in (0, 1):
                image = await _raw(repo, "user-1", shard)
                (state,) = aggregate_bucket_states([self._record(image, image)]).values()
                rolled.append(try_refill_bucket(table, state, now))
            # Spent: shard 0 holds 2 and the clone was granted nothing, so both
            # are below their share of 5 and both roll. Unspent: the transfer
            # left both at exactly 5, so neither has anything to restore.
            assert rolled == [bool(spent), bool(spent)]
            assert await spendable(repo, "user-1", "session", 2, resource=RESOURCE) == 10

        repo._now_ms = lambda: now
        admitted = 0
        for shard in (0, 1):
            admitted += await materialise(limiter, "user-1", "session", shard, RESOURCE)
        assert admitted == 2

        for shard in (0, 1):
            item = await _raw(repo, "user-1", shard)
            assert int(item[BUCKET_FIELD_RF]["N"]) >= self.T1, f"shard {shard} never rolled"
            assert await _stored_ws_on(repo, "user-1", "session", shard) == self.T1
        left = await spendable(repo, "user-1", "session", 2, resource=RESOURCE)
        assert admitted + left == 10, (admitted, left)
