"""When a duration window's allowance returns, as every status reports it (ADR-139, #629).

``LimitStatus.resets_at_ms`` and ``retry_after_seconds`` for a ``reset_after``
quota are constants read off the bucket — ``ws + reset_after`` — rather than
scans, and they are wired at every ``LimitStatus`` construction site: the fast
path (``bucket.declared_statuses``), slow-path admission
(``RateLimiter._admit_limit``), the lease (``consume()`` and
``_build_retry_failure_statuses``) and ``RateLimiter.check_availability``.

Not a sync-generation source: these drive the async limiter and lease, and the
sync twins are generated from the same ``limiter.py`` / ``lease.py``.
"""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.fixtures.windows import FIVE_HOURS_MS, SESSION_10, T0
from zae_limiter import Limit, RateLimiter, RateLimitExceeded
from zae_limiter.bucket import (
    calculate_time_until_available,
    declared_statuses,
    retry_after_for_deficit,
    try_consume,
    window_end_in_force,
    would_refill_satisfy,
)
from zae_limiter.lease import Lease, LeaseEntry, _build_retry_failure_statuses
from zae_limiter.models import BucketState
from zae_limiter.schema import BUCKET_FIELD_RSA, bucket_attr, pk_bucket, sk_state

RESOURCE = "gpt-4"
ONE_HOUR_MS = 3_600_000


def _bucket_key(repo, entity_id, shard=0):
    return {
        "PK": {"S": pk_bucket(repo._namespace_id, entity_id, RESOURCE, shard)},
        "SK": {"S": sk_state()},
    }


def _window_shard(**kwargs) -> BucketState:
    """A spent shard of ``SESSION_10`` whose window opened at ``T0``."""
    base = dict(
        entity_id="user-1",
        resource=RESOURCE,
        limit_name="session",
        tokens_milli=0,
        last_refill_ms=T0,
        capacity_milli=10_000,
        # A quota does not drip (ADR-137): zero rate, inert period.
        refill_amount_milli=0,
        refill_period_ms=1_000,
        total_consumed_milli=10_000,
        window_start_ms=T0,
        reset_after_seconds=FIVE_HOURS_MS // 1000,
    )
    base.update(kwargs)
    return BucketState(**base)


class TestBucketReportsTheWindow:
    """``bucket.py``: the arithmetic every site shares."""

    def test_retry_after_for_a_duration_quota_is_the_wait_to_its_window_end(self):
        """A quota has no drip at all under ADR-137, so the window end is the
        only finite answer — the same call the calendar form's reset branch
        makes, but a constant rather than a scan."""
        result = try_consume(_window_shard(), 1, T0 + ONE_HOUR_MS)
        assert not result.success
        assert result.retry_after_seconds == pytest.approx(4 * 3600.0)

    def test_the_window_is_half_open_so_its_end_needs_no_extra_millisecond(self):
        """`[ws, ws + rsa)`: at `end - 1` there is exactly one ms to wait."""
        assert retry_after_for_deficit(_window_shard(), 1_000, T0 + FIVE_HOURS_MS - 1) == 0.001

    def test_an_ended_window_reports_no_wait(self):
        """The next acquire opens a fresh window and restores the balance."""
        assert retry_after_for_deficit(_window_shard(), 1_000, T0 + FIVE_HOURS_MS + 5) == 0.0

    def test_no_deficit_is_no_wait_even_inside_a_window(self):
        assert retry_after_for_deficit(_window_shard(), 0, T0 + ONE_HOUR_MS) == 0.0

    def test_a_windowless_state_still_walks_the_schedule(self):
        """A dripping limit is untouched: 1 token at 60/min is ~1 s away."""
        state = _window_shard(
            capacity_milli=60_000,
            refill_amount_milli=60_000,
            refill_period_ms=60_000,
            window_start_ms=None,
            reset_after_seconds=None,
        )
        assert retry_after_for_deficit(state, 1_000, T0) == pytest.approx(1.0, abs=0.01)

    def test_time_until_available_answers_from_the_window_too(self):
        wait = calculate_time_until_available(_window_shard(), 1, T0 + ONE_HOUR_MS)
        assert wait == pytest.approx(4 * 3600.0)

    def test_the_fast_path_status_carries_the_window_end(self):
        """`declared_statuses` builds the fast rejection from the ALL_OLD image."""
        (status,) = declared_statuses([_window_shard()], {"session": 1}, T0 + ONE_HOUR_MS)
        assert status.exceeded
        assert status.resets_at_ms == T0 + FIVE_HOURS_MS
        assert status.retry_after_seconds == pytest.approx(4 * 3600.0)
        assert status.limit.reset_after == timedelta(hours=5)

    def test_a_rate_limit_status_carries_no_instant(self):
        state = _window_shard(
            limit_name="rpm",
            capacity_milli=60_000,
            refill_amount_milli=60_000,
            refill_period_ms=60_000,
            window_start_ms=None,
            reset_after_seconds=None,
        )
        (status,) = declared_statuses([state], {"rpm": 1}, T0)
        assert status.resets_at_ms is None

    def test_only_the_resolved_limit_decides_a_window_is_in_force(self):
        """A stale `ws`/`rsa` left by a limit that lost its window does not vote."""
        rpm = Limit.per_minute("session", 10)
        assert window_end_in_force(rpm, _window_shard(), T0) is None
        assert window_end_in_force(SESSION_10, _window_shard(), T0) == T0 + FIVE_HOURS_MS

    @pytest.mark.parametrize("offset", [0, 1, ONE_HOUR_MS])
    def test_an_end_at_or_before_now_is_never_reported(self, offset):
        """Clamped in the one place every site reads it: "returns at <a moment
        already past>" reads as "retry now" (#574's hot loop)."""
        now = T0 + FIVE_HOURS_MS + offset
        assert window_end_in_force(SESSION_10, _window_shard(), now) is None

    def test_a_positive_rate_beside_a_stray_window_waits_for_the_drip(self):
        """A corrupt item: positive stored rate plus a leftover `ws`/`rsa`.
        `Limit.from_bucket_state` reads it as dripping, so the wait is the
        drip's (1 token at 60/min ~ 1 s), not the 4 h to the stray end."""
        state = _window_shard(
            capacity_milli=60_000, refill_amount_milli=60_000, refill_period_ms=60_000
        )
        wait = retry_after_for_deficit(state, 1_000, T0 + ONE_HOUR_MS)
        assert wait == pytest.approx(1.0, abs=0.01)


class TestFastPathReadsAnEndedWindowAsRestored:
    """Ruling (a): an image whose window has ended is not a fast rejection.

    Reachable when a resource- or system-level `reset_after` is lengthened:
    the item keeps the short `rsa`, the slow path stamped `vu` from the long
    config end, so the spent bucket fast-rejects between the two ends.
    """

    def test_the_reviewers_image_goes_to_the_slow_path(self):
        now = T0 + 2 * ONE_HOUR_MS
        stale = _window_shard(window_start_ms=now - 2 * ONE_HOUR_MS, reset_after_seconds=3600)
        would_help, statuses = would_refill_satisfy([stale], {"session": 1}, now)
        assert would_help, "an ended window is restored on the next pass: not a fast rejection"
        for status in statuses:
            assert not (
                status.retry_after_seconds == 0.0
                and status.resets_at_ms is not None
                and status.resets_at_ms <= now
            )
        (status,) = statuses
        assert status.available == 10
        assert status.resets_at_ms is None

    def test_a_live_window_on_the_image_still_fast_rejects(self):
        now = T0 + ONE_HOUR_MS
        would_help, (status,) = would_refill_satisfy([_window_shard()], {"session": 1}, now)
        assert not would_help
        assert status.resets_at_ms == T0 + FIVE_HOURS_MS
        assert status.retry_after_seconds == pytest.approx(4 * 3600.0)

    async def test_through_acquire_the_slow_path_rejects_at_the_config_end(self, limiter):
        """End to end: the item says 1 h, config says 5 h, the clock is 2 h in.
        The rejection quotes the config end, 3 h away — never "retry now"."""
        repo = limiter._repository
        await repo.set_limits("user-1", [SESSION_10], resource=RESOURCE)
        repo._now_ms = lambda: T0
        async with limiter.acquire("user-1", RESOURCE, consume={"session": 10}):
            pass
        # Simulate the non-fanned-out lengthening: the item keeps the short rsa.
        client = await repo._get_client()
        await client.update_item(
            TableName=repo.table_name,
            Key=_bucket_key(repo, "user-1"),
            UpdateExpression="SET #rsa = :short",
            ExpressionAttributeNames={"#rsa": bucket_attr("session", BUCKET_FIELD_RSA)},
            ExpressionAttributeValues={":short": {"N": "3600"}},
        )

        now = T0 + 2 * ONE_HOUR_MS
        repo._now_ms = lambda: now
        with pytest.raises(RateLimitExceeded) as exc:
            async with limiter.acquire("user-1", RESOURCE, consume={"session": 1}):
                pass
        status = next(s for s in exc.value.statuses if s.limit_name == "session")
        assert status.resets_at_ms == T0 + FIVE_HOURS_MS
        assert status.retry_after_seconds == pytest.approx(3 * 3600.0, abs=1.0)


def _entry(limit: Limit, state: BucketState, consumed: int = 0) -> LeaseEntry:
    return LeaseEntry(
        entity_id="user-1",
        resource=RESOURCE,
        limit=limit,
        state=state,
        consumed=consumed,
        _original_tokens_milli=state.tokens_milli,
        _original_rf_ms=state.last_refill_ms,
        _has_custom_config=True,
    )


class TestLeaseReportsTheWindow:
    """``lease.py``: the consumption-only retry failure and ``Lease.consume()``."""

    def test_retry_failure_statuses_wait_for_the_window_end(self):
        entry = _entry(SESSION_10, _window_shard(), consumed=1)
        (status,) = _build_retry_failure_statuses([entry], now_ms=T0 + ONE_HOUR_MS)
        assert status.exceeded
        assert status.resets_at_ms == T0 + FIVE_HOURS_MS
        assert status.retry_after_seconds == pytest.approx(4 * 3600.0)

    async def test_consume_reports_the_window_on_both_statuses(self):
        """The exceeded status and the passed one (declared, not consumed now)
        both carry their window's end."""
        turns = Limit.quota("turns", 10, reset_after=timedelta(hours=1))
        repo = MagicMock()
        repo._now_ms = MagicMock(return_value=T0 + ONE_HOUR_MS)
        lease = Lease(
            repository=repo,
            entries=[
                _entry(SESSION_10, _window_shard()),
                _entry(
                    turns,
                    _window_shard(
                        limit_name="turns",
                        tokens_milli=5_000,
                        window_start_ms=T0 + 30 * 60_000,
                        reset_after_seconds=3600,
                    ),
                ),
            ],
        )
        with pytest.raises(RateLimitExceeded) as exc:
            await lease.consume(session=1)
        by_name = {s.limit_name: s for s in exc.value.statuses}
        assert by_name["session"].resets_at_ms == T0 + FIVE_HOURS_MS
        assert by_name["session"].retry_after_seconds == pytest.approx(4 * 3600.0)
        assert not by_name["turns"].exceeded
        assert by_name["turns"].resets_at_ms == T0 + 90 * 60_000


async def _spend_the_session(limiter, now_ms=T0):
    repo = limiter._repository
    await repo.set_limits("user-1", [SESSION_10], resource=RESOURCE)
    repo._now_ms = lambda: now_ms
    async with limiter.acquire("user-1", RESOURCE, consume={"session": 10}):
        pass


class TestAcquireReportsTheWindow:
    """Through ``acquire()``: an exhausted quota inside a live window."""

    @pytest.mark.parametrize("speculative", [True, False], ids=["fast", "slow"])
    async def test_an_exhausted_session_reports_its_window_end(self, limiter, speculative):
        repo = limiter._repository
        await _spend_the_session(limiter)
        target = limiter if speculative else RateLimiter(repository=repo, speculative_writes=False)

        repo._now_ms = lambda: T0 + ONE_HOUR_MS
        with pytest.raises(RateLimitExceeded) as exc:
            async with target.acquire("user-1", RESOURCE, consume={"session": 1}):
                pass

        status = next(s for s in exc.value.statuses if s.limit_name == "session")
        assert status.resets_at_ms == T0 + FIVE_HOURS_MS
        assert status.retry_after_seconds == pytest.approx(4 * 3600.0, abs=1.0)
        assert exc.value.retry_after_seconds == pytest.approx(4 * 3600.0, abs=1.0)
        body = exc.value.as_dict()["limits"][0]
        assert body["kind"] == "quota"
        assert body["resets_at_ms"] == T0 + FIVE_HOURS_MS

    async def test_a_rejection_at_a_boundary_reports_the_new_window(self, limiter):
        """`_admit_limit` reads the end AFTER the roll: a caller asking for more
        than the whole allowance at the boundary is told about the window it
        just opened, not the one that elapsed."""
        repo = limiter._repository
        await _spend_the_session(limiter)

        now = T0 + FIVE_HOURS_MS + 1
        repo._now_ms = lambda: now
        with pytest.raises(RateLimitExceeded) as exc:
            async with limiter.acquire("user-1", RESOURCE, consume={"session": 999_999}):
                pass
        status = next(s for s in exc.value.statuses if s.limit_name == "session")
        assert status.available == 10
        assert status.resets_at_ms == now + FIVE_HOURS_MS
        assert status.retry_after_seconds == pytest.approx(5 * 3600.0, abs=1.0)


class TestCheckAvailabilityReportsOneWindow:
    """``check_availability`` across shards (rulings 3 and 4)."""

    @staticmethod
    async def _check(limiter, shards, now_ms, needed=None, limits=(SESSION_10,)):
        repo = limiter._repository
        await repo.set_limits("user-1", list(limits), resource=RESOURCE)
        repo._now_ms = lambda: now_ms
        with patch.object(repo, "get_buckets", AsyncMock(return_value=shards)):
            return await limiter.check_availability("user-1", RESOURCE, needed=needed)

    @staticmethod
    def _shard(tokens: int, ws: int | None, rf: int, shard_count: int = 2, **kwargs):
        return _window_shard(
            tokens_milli=tokens * 1000,
            window_start_ms=ws,
            last_refill_ms=rf,
            shard_count=shard_count,
            **kwargs,
        )

    async def test_staggered_live_windows_report_the_latest_end(self, limiter):
        """Two concurrent openers anchor a few ms apart: E1 < E2, both live.
        The latest is the conservative "whole quota back" instant."""
        now = T0 + ONE_HOUR_MS
        shards = [self._shard(0, T0, T0), self._shard(0, T0 + 7, T0 + 7)]
        check = await self._check(limiter, shards, now, needed={"session": 1})
        status = check.status("session")
        assert status.resets_at_ms == T0 + 7 + FIVE_HOURS_MS
        assert status.available == 0
        assert status.retry_after_seconds == pytest.approx((T0 + 7 + FIVE_HOURS_MS - now) / 1000)

    async def test_an_ended_shard_does_not_vote_and_reads_restored(self, limiter):
        """One shard missed the fan-out and its window is over; the live one's
        end is the answer, and the ended shard reads as its full share."""
        now = T0 + ONE_HOUR_MS
        shards = [
            self._shard(0, T0 - FIVE_HOURS_MS, T0 - FIVE_HOURS_MS),
            self._shard(0, T0, T0),
        ]
        check = await self._check(limiter, shards, now, needed={"session": 1})
        status = check.status("session")
        assert status.resets_at_ms == T0 + FIVE_HOURS_MS
        assert status.available == 5, "the ended shard's share is back on its next acquire"
        assert not status.exceeded
        assert status.retry_after_seconds == 0.0

    async def test_every_window_ended_reports_no_instant_and_full_availability(self, limiter):
        now = T0 + FIVE_HOURS_MS + 3  # the later end exactly: half-open, so over
        shards = [self._shard(0, T0, T0), self._shard(0, T0 + 3, T0 + 3)]
        check = await self._check(limiter, shards, now, needed={"session": 10})
        status = check.status("session")
        assert status.resets_at_ms is None
        assert status.available == 10
        assert status.retry_after_seconds == 0.0

    async def test_a_shard_that_received_the_fan_out_reads_restored(self, limiter):
        """`ws > rf` (`BucketState.window_rolled`): the fan-out moved `ws` and
        never `tk`, so the burnt balance on disk is not what the next acquire
        on that shard sees."""
        now = T0 + FIVE_HOURS_MS + ONE_HOUR_MS
        opened = T0 + FIVE_HOURS_MS + 10
        shards = [
            self._shard(4, opened, opened),  # the opener: rolled and spent 1
            self._shard(0, opened, T0 + 60_000),  # fan-out received, not drawn
        ]
        check = await self._check(limiter, shards, now)
        status = check.status("session")
        assert status.available == 9
        assert status.resets_at_ms == opened + FIVE_HOURS_MS

    async def test_a_windowless_shard_contributes_no_end_and_reads_restored(self, limiter):
        """A shard carrying `rsa` but no `ws` (stamped by the param sync, never
        used) opens its window on its next acquire, restoring its share."""
        now = T0 + ONE_HOUR_MS
        shards = [self._shard(0, None, T0 - 10), self._shard(2, T0, T0)]
        check = await self._check(limiter, shards, now, needed={"session": 1})
        status = check.status("session")
        assert status.resets_at_ms == T0 + FIVE_HOURS_MS
        assert status.available == 7

    async def test_a_shortened_config_waits_for_the_items_longer_end(self, limiter):
        """Ruling (e): config shortened to 1 h, item still carries 5 h, and the
        clock is between the two ends. The item's `vu` still gates the fast
        path to the long end, so the shard is NOT restored yet and the reset
        instant is the item end."""
        one_hour = Limit.quota("session", 10, reset_after=timedelta(hours=1))
        now = T0 + 2 * ONE_HOUR_MS
        shards = [self._shard(0, T0, T0, shard_count=1)]
        check = await self._check(limiter, shards, now, needed={"session": 1}, limits=(one_hour,))
        status = check.status("session")
        assert status.available == 0
        assert status.exceeded
        assert status.resets_at_ms == T0 + FIVE_HOURS_MS
        assert status.retry_after_seconds == pytest.approx(3 * 3600.0)

    async def test_a_lengthened_config_waits_for_the_configs_longer_end(self, limiter):
        """The mirror: item 1 h, config 5 h, 2 h in. The slow path pairs `ws`
        with the config length, so the window is still live until then."""
        now = T0 + 2 * ONE_HOUR_MS
        shards = [self._shard(0, T0, T0, shard_count=1, reset_after_seconds=3600)]
        check = await self._check(limiter, shards, now, needed={"session": 1})
        status = check.status("session")
        assert status.available == 0
        assert status.resets_at_ms == T0 + FIVE_HOURS_MS

    async def test_an_exhausted_live_window_waits_for_its_end(self, limiter):
        now = T0 + ONE_HOUR_MS
        shards = [self._shard(0, T0, T0, shard_count=1)]
        check = await self._check(limiter, shards, now, needed={"session": 1})
        assert check.exceeded == ["session"]
        assert check.retry_after_seconds == pytest.approx(4 * 3600.0)
        assert check.status("session").resets_at_ms == T0 + FIVE_HOURS_MS

    async def test_a_rate_limit_reports_no_instant(self, limiter):
        rpm = Limit.per_minute("rpm", 60)
        check = await self._check(limiter, [], T0, needed={"rpm": 1}, limits=(rpm,))
        assert check.status("rpm").resets_at_ms is None
