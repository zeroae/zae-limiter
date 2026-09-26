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

from tests.fixtures.sharding import drain_wcu, materialise, spendable
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
