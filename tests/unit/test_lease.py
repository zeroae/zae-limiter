"""Tests for ``Lease._commit_initial`` carrying duration windows (ADR-139, #623).

Not a sync-generation source: these pin the async commit, and the sync twin is
generated from the same ``lease.py``.
"""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from zae_limiter import Limit, RateLimiter
from zae_limiter.lease import Lease, LeaseEntry
from zae_limiter.models import BucketState

FIVE_HOURS_MS = 5 * 3_600_000
SESSION = Limit.quota("session", 10, reset_after=timedelta(hours=5))
T0 = 1_757_000_000_000


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
    repo.transact_write = AsyncMock(return_value=None)
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
        assert kwargs["window_starts"] == {"session": T0 + 1}

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
        assert kwargs["window_starts"] == {"session": T0 + 1, "daily": T0 + 1}

    async def test_no_window_opened_stamps_nothing(self):
        repo = _mock_repo(T0 + 1)
        entry = _entry(SESSION, _session_state(), _window_end_ms=T0 + FIVE_HOURS_MS)
        await Lease(repository=repo, entries=[entry])._commit_initial()
        assert repo.build_composite_normal.call_args.kwargs["window_starts"] == {}

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
        assert kwargs["window_starts"] == {"session": commit_now}
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
        assert kwargs["window_starts"] == {}
        assert kwargs["refill_amounts"] == {"session": 1_000}


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
