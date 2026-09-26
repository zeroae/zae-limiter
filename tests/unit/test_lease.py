"""Tests for ``Lease._commit_initial`` carrying duration windows (ADR-139, #623).

Not a sync-generation source: these pin the async commit, and the sync twin is
generated from the same ``lease.py``.
"""

import logging
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from tests.fixtures.windows import FIVE_HOURS_MS, T0
from tests.fixtures.windows import SESSION_10 as SESSION
from zae_limiter import Limit, RateLimiter
from zae_limiter.lease import Lease, LeaseEntry
from zae_limiter.models import BucketState


def _session_state(**kwargs) -> BucketState:
    base = dict(
        entity_id="user-1",
        resource="gpt-4",
        limit_name="session",
        tokens_milli=0,
        last_refill_ms=T0,
        capacity_milli=10_000,
        refill_amount_milli=0,
        refill_period_ms=1_000,
        window_start_ms=T0,
        reset_after_seconds=18_000,
    )
    base.update(kwargs)
    return BucketState(**base)


def _mock_repo(now_ms: int) -> MagicMock:
    repo = MagicMock()
    repo._now_ms = MagicMock(return_value=now_ms)
    repo._bucket_ttl_refill_multiplier = 0
    repo.build_composite_normal = MagicMock(return_value={"Update": {}})
    repo.build_composite_create = MagicMock(return_value={"Put": {}})
    repo.build_composite_retry = MagicMock(return_value={"Update": {"retry": True}})
    repo.transact_write = AsyncMock(return_value=None)
    repo._propagate_window_start = AsyncMock(return_value=0)
    return repo


def _entry(limit: Limit, state: BucketState, **kwargs) -> LeaseEntry:
    base = dict(
        entity_id="user-1",
        resource="gpt-4",
        limit=limit,
        state=state,
        consumed=0,
        _original_tokens_milli=state.tokens_milli,
        _original_rf_ms=state.last_refill_ms,
        _has_custom_config=True,
    )
    base.update(kwargs)
    return LeaseEntry(**base)


class TestCommitStampsWindowStarts:
    async def test_an_opened_window_is_handed_to_the_normal_write(self):
        repo = _mock_repo(T0 + 1)
        entry = _entry(SESSION, _session_state(), _window_start_ms=T0 + 1)
        await Lease(repository=repo, entries=[entry])._commit_initial()
        kwargs = repo.build_composite_normal.call_args.kwargs
        assert kwargs["windows"] == {"session": (T0 + 1, 18_000)}

    async def test_an_undeclared_entry_sharing_the_item_is_stamped_too(self):
        """`ws` is per-limit but the write is one item, exactly as `vu` is. An
        undeclared quota sharing it must still have its window stamped or it
        will never roll."""
        repo = _mock_repo(T0 + 1)
        other = Limit.quota("daily", 10, reset_after=timedelta(hours=24))
        declared = _entry(SESSION, _session_state(), _window_start_ms=T0 + 1)
        undeclared = _entry(
            other,
            _session_state(limit_name="daily", reset_after_seconds=86_400),
            _window_start_ms=T0 + 1,
            _declared=False,
        )
        await Lease(repository=repo, entries=[declared, undeclared])._commit_initial()
        kwargs = repo.build_composite_normal.call_args.kwargs
        assert kwargs["windows"] == {"session": (T0 + 1, 18_000), "daily": (T0 + 1, 86_400)}

    async def test_no_window_opened_stamps_nothing(self):
        repo = _mock_repo(T0 + 1)
        entry = _entry(SESSION, _session_state(), _window_end_ms=T0 + FIVE_HOURS_MS)
        await Lease(repository=repo, entries=[entry])._commit_initial()
        assert repo.build_composite_normal.call_args.kwargs["windows"] == {}

    async def test_a_window_ending_between_the_readings_is_re_expressed(self):
        """The acquire path saw a live window; the commit's later reading is
        past its end. The commit anchors the next window at its own reading
        and restores the balance, as it does for a reset edge in the gap."""
        commit_now = T0 + FIVE_HOURS_MS + 1
        repo = _mock_repo(commit_now)
        entry = _entry(
            SESSION,
            _session_state(tokens_milli=0),
            consumed=1,
            _window_end_ms=T0 + FIVE_HOURS_MS,
        )
        await Lease(repository=repo, entries=[entry])._commit_initial()
        kwargs = repo.build_composite_normal.call_args.kwargs
        assert kwargs["windows"] == {"session": (commit_now, 18_000)}
        # eff_cp - stored_tk: the `ADD` then lands at 10 - 1 consumed.
        assert kwargs["refill_amounts"] == {"session": 10_000}
        assert entry._window_start_ms == commit_now

    async def test_a_window_still_open_at_the_commit_is_left_alone(self):
        repo = _mock_repo(T0 + FIVE_HOURS_MS - 1)
        entry = _entry(
            SESSION,
            _session_state(tokens_milli=4_000),
            consumed=1,
            _window_end_ms=T0 + FIVE_HOURS_MS,
        )
        await Lease(repository=repo, entries=[entry])._commit_initial()
        kwargs = repo.build_composite_normal.call_args.kwargs
        assert kwargs["windows"] == {}
        assert kwargs["refill_amounts"] == {"session": 1_000}


def _condition_failed() -> ClientError:
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "rf moved"}},
        "TransactWriteItems",
    )


class TestCommitFansTheRolloverOut:
    """`_commit_initial` hands a persisted rollover to `_propagate_window_start` (#624)."""

    async def test_a_rollover_on_a_sharded_entity_fans_out_after_the_write(self):
        repo = _mock_repo(T0 + 1)
        order: list[str] = []
        repo.transact_write.side_effect = lambda items: order.append("write")
        repo._propagate_window_start.side_effect = lambda *a: order.append("fanout") or 3
        entry = _entry(
            SESSION, _session_state(), _window_start_ms=T0 + 1, _shard_id=2, _shard_count=4
        )
        await Lease(repository=repo, entries=[entry])._commit_initial()
        repo._propagate_window_start.assert_awaited_once_with(
            "user-1", "gpt-4", 2, 4, {"session": (T0 + 1, 18_000)}
        )
        assert order == ["write", "fanout"], "never before the roll is durable"

    async def test_the_lease_is_recorded_committed_before_the_fan_out(self):
        """The fan-out is bookkeeping-neutral: by the time it runs, the lease
        already records the write, so nothing it does can leave it
        half-recorded."""
        repo = _mock_repo(T0 + 1)
        entry = _entry(
            SESSION, _session_state(), consumed=1, _window_start_ms=T0 + 1, _shard_count=4
        )
        lease = Lease(repository=repo, entries=[entry])
        seen: list[tuple[bool, int]] = []

        async def observe(*args):
            seen.append((lease._initial_committed, entry._initial_consumed))
            return 3

        repo._propagate_window_start.side_effect = observe
        await lease._commit_initial()
        assert seen == [(True, 1)]

    async def test_a_short_fan_out_is_not_logged_at_info(self, caplog):
        """A shortfall is routine (siblings not created yet, or not due), so
        it stays below INFO."""
        repo = _mock_repo(T0 + 1)
        repo._propagate_window_start.return_value = 1
        entry = _entry(SESSION, _session_state(), _window_start_ms=T0 + 1, _shard_count=4)
        with caplog.at_level(logging.INFO, logger="zae_limiter.lease"):
            await Lease(repository=repo, entries=[entry])._commit_initial()
        assert "wrote 1 of 3" not in caplog.text

    async def test_an_unsharded_entity_issues_no_fan_out(self):
        """(S-1) x L writes: nothing at all at S = 1."""
        repo = _mock_repo(T0 + 1)
        entry = _entry(SESSION, _session_state(), _window_start_ms=T0 + 1)
        await Lease(repository=repo, entries=[entry])._commit_initial()
        repo._propagate_window_start.assert_not_awaited()

    async def test_no_rollover_issues_no_fan_out(self):
        repo = _mock_repo(T0 + 1)
        entry = _entry(SESSION, _session_state(), _window_end_ms=T0 + FIVE_HOURS_MS, _shard_count=4)
        await Lease(repository=repo, entries=[entry])._commit_initial()
        repo._propagate_window_start.assert_not_awaited()

    async def test_the_item_shard_count_counts_when_the_cache_lags(self):
        """The entry's `_shard_count` is the cached count; the item read back
        may already know more shards. Fan out over the larger, or a sibling
        the cache has not learned yet keeps its old window."""
        repo = _mock_repo(T0 + 1)
        entry = _entry(
            SESSION,
            _session_state(shard_count=8),
            _window_start_ms=T0 + 1,
            _shard_count=2,
        )
        await Lease(repository=repo, entries=[entry])._commit_initial()
        repo._propagate_window_start.assert_awaited_once_with(
            "user-1", "gpt-4", 0, 8, {"session": (T0 + 1, 18_000)}
        )

    async def test_the_consumption_only_retry_fans_nothing_out(self):
        """The retry path stamps no `ws` on the writer's own shard, so the
        rollover was not persisted and there is nothing to propagate. The
        next pass on this shard re-opens the window and fans out then."""
        repo = _mock_repo(T0 + 1)
        repo.transact_write.side_effect = [_condition_failed(), None]
        entry = _entry(
            SESSION,
            _session_state(tokens_milli=5_000),
            consumed=1,
            _window_start_ms=T0 + 1,
            _shard_count=4,
        )
        await Lease(repository=repo, entries=[entry])._commit_initial()
        assert repo.transact_write.await_count == 2, "the retry path ran"
        repo._propagate_window_start.assert_not_awaited()

    async def test_a_re_expressed_window_whose_write_failed_fans_nothing_out(self):
        """The re-expression sets `_window_start_ms` in memory before the
        write. If that write is rejected, the in-memory anchor was never
        persisted and must not reach the siblings."""
        commit_now = T0 + FIVE_HOURS_MS + 1
        repo = _mock_repo(commit_now)
        repo.transact_write.side_effect = [_condition_failed(), None]
        entry = _entry(
            SESSION,
            _session_state(tokens_milli=5_000),
            consumed=1,
            _window_end_ms=T0 + FIVE_HOURS_MS,
            _shard_count=4,
        )
        await Lease(repository=repo, entries=[entry])._commit_initial()
        assert entry._window_start_ms == commit_now, "anchored in memory"
        repo._propagate_window_start.assert_not_awaited()

    async def test_a_failed_write_fans_nothing_out(self):
        repo = _mock_repo(T0 + 1)
        repo.transact_write.side_effect = RuntimeError("network")
        entry = _entry(SESSION, _session_state(), _window_start_ms=T0 + 1, _shard_count=4)
        with pytest.raises(RuntimeError):
            await Lease(repository=repo, entries=[entry])._commit_initial()
        repo._propagate_window_start.assert_not_awaited()

    async def test_a_created_shard_never_fans_its_anchor_out(self):
        """A create stamps `ws` from the state, but it is a new shard's
        anchor, not a rollover. Fanning its `now` out mid-window would drag
        every sibling's window forward -- a reset nobody earned. A new shard
        inherits its siblings' window instead (Task 8)."""
        repo = _mock_repo(T0 + 1)
        entry = _entry(
            SESSION,
            _session_state(window_start_ms=T0 + 1),
            _is_new=True,
            _shard_id=3,
            _shard_count=4,
        )
        await Lease(repository=repo, entries=[entry])._commit_initial()
        repo.build_composite_create.assert_called_once()
        repo._propagate_window_start.assert_not_awaited()

    async def test_cascade_groups_fan_out_over_their_own_shard_counts(self):
        """Child and parent are separate items with separate shard counts;
        each fans out over its own, which is what keeps their windows
        independent (ADR-139)."""
        repo = _mock_repo(T0 + 1)
        child = _entry(SESSION, _session_state(), _window_start_ms=T0 + 1, _shard_count=4)
        parent = _entry(
            SESSION,
            _session_state(entity_id="org-1"),
            entity_id="org-1",
            _window_start_ms=T0 + 1,
            _shard_id=1,
            _shard_count=2,
        )
        await Lease(repository=repo, entries=[child, parent])._commit_initial()
        calls = sorted(c.args for c in repo._propagate_window_start.await_args_list)
        assert calls == [
            ("org-1", "gpt-4", 1, 2, {"session": (T0 + 1, 18_000)}),
            ("user-1", "gpt-4", 0, 4, {"session": (T0 + 1, 18_000)}),
        ]

    async def test_a_failed_fan_out_is_logged_and_never_fails_the_acquire(self, caplog):
        """The caller was admitted and the write landed. A sibling left on
        the old `ws` anchors its own window later; the `ws < :new` rule
        converges the entity on the latest one. The entity id is never
        logged -- it is routinely an API key."""
        repo = _mock_repo(T0 + 1)
        repo._propagate_window_start.side_effect = RuntimeError("throttled")
        entry = _entry(
            SESSION,
            _session_state(entity_id="sk-secret-key"),
            entity_id="sk-secret-key",
            _window_start_ms=T0 + 1,
            _shard_count=4,
        )
        lease = Lease(repository=repo, entries=[entry])
        with caplog.at_level(logging.WARNING, logger="zae_limiter.lease"):
            await lease._commit_initial()
        assert lease._initial_committed
        assert "fan-out failed" in caplog.text
        assert "sk-secret-key" not in caplog.text

    async def test_a_short_fan_out_is_logged(self, caplog):
        repo = _mock_repo(T0 + 1)
        repo._propagate_window_start.return_value = 1
        entry = _entry(SESSION, _session_state(), _window_start_ms=T0 + 1, _shard_count=4)
        with caplog.at_level(logging.DEBUG, logger="zae_limiter.lease"):
            await Lease(repository=repo, entries=[entry])._commit_initial()
        assert "wrote 1 of 3" in caplog.text
        assert "user-1" not in caplog.text


class TestWindowEndingBetweenTheReadingsThroughAcquire:
    """End to end on moto: the acquire path must carry `_window_end_ms` to the
    commit, or the re-expression above never fires."""

    async def test_the_commit_anchors_a_window_that_ended_mid_pass(self, limiter):
        repo = limiter._repository
        slow = RateLimiter(repository=repo, speculative_writes=False)
        await repo.set_limits("race-1", [SESSION], resource="gpt-4")

        repo._now_ms = lambda: T0
        async with slow.acquire("race-1", "gpt-4", consume={"session": 10}):
            pass

        original = Lease._commit_initial
        commit_now = T0 + FIVE_HOURS_MS + 1

        async def commit_after_the_window(lease_self):
            repo._now_ms = lambda: commit_now
            return await original(lease_self)

        repo._now_ms = lambda: T0 + FIVE_HOURS_MS - 1
        with patch.object(Lease, "_commit_initial", commit_after_the_window):
            async with slow.acquire("race-1", "gpt-4", consume={"session": 0}):
                pass

        bucket = next(iter(await repo.get_buckets("race-1", resource="gpt-4")))
        assert bucket.window_start_ms == commit_now, "the window anchors at the commit"
        assert bucket.last_refill_ms == commit_now, "`ws == rf`, so nothing re-applies it"
        assert bucket.tokens_milli == 10_000, "the new window's allowance"
        assert bucket.total_consumed_milli == 10_000, "and `tc` is still monotonic"
