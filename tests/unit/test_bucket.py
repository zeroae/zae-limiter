"""Tests for token bucket calculations."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from zae_limiter.bucket import (
    calculate_available,
    calculate_retry_after,
    calculate_time_until_available,
    force_consume,
    refill_bucket,
    try_consume,
    would_refill_satisfy,
)
from zae_limiter.models import BucketState
from zae_limiter.schedule import ScheduleEntry


class TestRefillBucket:
    """Tests for refill_bucket function."""

    def test_no_time_elapsed(self):
        """No refill when no time has passed."""
        result = refill_bucket(
            tokens_milli=50_000_000,  # 50k tokens
            last_refill_ms=1000,
            now_ms=1000,
            capacity_milli=100_000_000,
            refill_amount_milli=100_000_000,
            refill_period_ms=60_000,
        )
        assert result.new_tokens_milli == 50_000_000
        assert result.new_last_refill_ms == 1000

    def test_partial_refill(self):
        """Partial refill based on elapsed time."""
        # 30 seconds elapsed, should refill 50% of 100k = 50k
        result = refill_bucket(
            tokens_milli=0,
            last_refill_ms=0,
            now_ms=30_000,  # 30 seconds
            capacity_milli=100_000_000,
            refill_amount_milli=100_000_000,
            refill_period_ms=60_000,
        )
        assert result.new_tokens_milli == 50_000_000  # 50k tokens

    def test_full_refill_capped_at_capacity(self):
        """Refill capped at capacity."""
        # 2 minutes elapsed, but capped at capacity
        result = refill_bucket(
            tokens_milli=0,
            last_refill_ms=0,
            now_ms=120_000,  # 2 minutes
            capacity_milli=100_000_000,
            refill_amount_milli=100_000_000,
            refill_period_ms=60_000,
        )
        assert result.new_tokens_milli == 100_000_000  # capped at capacity

    def test_negative_bucket_refills(self):
        """Negative bucket refills towards zero."""
        result = refill_bucket(
            tokens_milli=-50_000_000,  # -50k tokens (debt)
            last_refill_ms=0,
            now_ms=30_000,  # 30 seconds
            capacity_milli=100_000_000,
            refill_amount_milli=100_000_000,
            refill_period_ms=60_000,
        )
        # -50k + 50k = 0
        assert result.new_tokens_milli == 0


class TestUnconditionalClamp:
    """A surplus over a lowered cap must not survive a pass that adds no tokens (#469)."""

    def test_clamps_when_no_time_has_passed(self):
        r = refill_bucket(
            tokens_milli=900_000,
            last_refill_ms=1000,
            now_ms=1000,
            capacity_milli=500_000,
            refill_amount_milli=500_000,
            refill_period_ms=60_000,
        )
        assert r.new_tokens_milli == 500_000
        assert r.new_last_refill_ms == 1000

    def test_clamps_when_elapsed_is_negative(self):
        r = refill_bucket(
            tokens_milli=900_000,
            last_refill_ms=2000,
            now_ms=1000,
            capacity_milli=500_000,
            refill_amount_milli=500_000,
            refill_period_ms=60_000,
        )
        assert r.new_tokens_milli == 500_000

    def test_clamps_when_elapsed_is_too_short_to_add_a_millitoken(self):
        r = refill_bucket(
            tokens_milli=900_000,
            last_refill_ms=1000,
            now_ms=1001,
            capacity_milli=500_000,
            refill_amount_milli=1,
            refill_period_ms=60_000,
        )
        assert r.new_tokens_milli == 500_000

    def test_does_not_disturb_a_bucket_already_at_or_below_capacity(self):
        r = refill_bucket(
            tokens_milli=100_000,
            last_refill_ms=1000,
            now_ms=1000,
            capacity_milli=500_000,
            refill_amount_milli=500_000,
            refill_period_ms=60_000,
        )
        assert r.new_tokens_milli == 100_000

    def test_leaves_debt_alone(self):
        """Buckets go negative for post-hoc reconciliation; clamping is min(), not max()."""
        r = refill_bucket(
            tokens_milli=-50_000,
            last_refill_ms=1000,
            now_ms=1000,
            capacity_milli=500_000,
            refill_amount_milli=500_000,
            refill_period_ms=60_000,
        )
        assert r.new_tokens_milli == -50_000


class TestTryConsume:
    """Tests for try_consume function."""

    @pytest.fixture
    def bucket_state(self):
        """Create a bucket state for testing."""
        return BucketState(
            entity_id="test",
            resource="gpt-4",
            limit_name="tpm",
            tokens_milli=100_000_000,  # 100k tokens
            last_refill_ms=0,
            capacity_milli=100_000_000,
            refill_amount_milli=100_000_000,
            refill_period_ms=60_000,
        )

    def test_consume_success(self, bucket_state):
        """Successful consumption when capacity available."""
        result = try_consume(bucket_state, 50_000, now_ms=0)
        assert result.success is True
        assert result.new_tokens_milli == 50_000_000  # 50k remaining
        assert result.available == 100_000
        assert result.retry_after_seconds == 0.0

    def test_consume_exact_capacity(self, bucket_state):
        """Consume exactly available capacity."""
        result = try_consume(bucket_state, 100_000, now_ms=0)
        assert result.success is True
        assert result.new_tokens_milli == 0
        assert result.retry_after_seconds == 0.0

    def test_consume_insufficient_capacity(self, bucket_state):
        """Fail when insufficient capacity."""
        result = try_consume(bucket_state, 150_000, now_ms=0)
        assert result.success is False
        assert result.available == 100_000
        assert result.retry_after_seconds > 0

    def test_consume_with_refill(self, bucket_state):
        """Consume after partial refill."""
        bucket_state.tokens_milli = 0
        bucket_state.last_refill_ms = 0

        # 30 seconds later, should have 50k tokens
        result = try_consume(bucket_state, 30_000, now_ms=30_000)
        assert result.success is True
        assert result.available == 50_000


class TestCalculateRetryAfter:
    """Tests for calculate_retry_after function."""

    def test_no_deficit(self):
        """No wait when no deficit."""
        result = calculate_retry_after(
            deficit_milli=0,
            refill_amount_milli=100_000_000,
            refill_period_ms=60_000,
        )
        assert result == 0.0

    def test_small_deficit(self):
        """Calculate wait for small deficit."""
        # Need 10k tokens, refill rate is 100k/min = 1666.67/sec
        # 10k / 1666.67 = 6 seconds
        result = calculate_retry_after(
            deficit_milli=10_000_000,  # 10k tokens
            refill_amount_milli=100_000_000,
            refill_period_ms=60_000,
        )
        assert 5.9 < result < 6.1  # approximately 6 seconds

    def test_large_deficit(self):
        """Calculate wait for large deficit."""
        # Need 100k tokens, refill rate is 100k/min
        # Should take 1 minute
        result = calculate_retry_after(
            deficit_milli=100_000_000,  # 100k tokens
            refill_amount_milli=100_000_000,
            refill_period_ms=60_000,
        )
        assert 59.9 < result < 60.1  # approximately 60 seconds

    def test_zero_refill_rate_does_not_raise(self):
        """A stored rate of 0 must not divide by zero inside an error path."""
        assert (
            calculate_retry_after(
                deficit_milli=1_000,
                refill_amount_milli=0,
                refill_period_ms=60_000,
            )
            == 0.0
        )


class TestQuotaRetryAfterUsesTheResetEdge:
    """A quota has no drip, so its wait is the next reset instant (#530).

    ADR-137 makes ``refill_amount = 0`` valid only alongside a
    ``reset_schedule``, so the zero-rate branch is the *common* path for a
    quota rather than the corrupt-item path its comment used to describe.
    ``next_reset_ms`` is what turns it back into a truthful answer.
    """

    NOW = 1_800_000_000_000  # an arbitrary but fixed epoch-ms reading

    def test_a_future_reset_is_the_wait(self):
        """Two different offsets, so a hard-coded constant cannot pass."""
        assert (
            calculate_retry_after(
                deficit_milli=5_000_000,
                refill_amount_milli=0,
                refill_period_ms=86_400_000,
                next_reset_ms=self.NOW + 3_600_000,
                now_ms=self.NOW,
            )
            == 3600.001
        )
        assert (
            calculate_retry_after(
                deficit_milli=5_000_000,
                refill_amount_milli=0,
                refill_period_ms=86_400_000,
                next_reset_ms=self.NOW + 90_000,
                now_ms=self.NOW,
            )
            == 90.001
        )

    def test_the_wait_does_not_depend_on_the_deficit_or_the_period(self):
        """A reset restores the whole balance in one lump, so the size of the
        shortfall is irrelevant. Discriminates against an implementation that
        smuggles the deficit back into the reset branch."""
        big = calculate_retry_after(
            deficit_milli=999_000_000,
            refill_amount_milli=0,
            refill_period_ms=60_000,
            next_reset_ms=self.NOW + 3_600_000,
            now_ms=self.NOW,
        )
        small = calculate_retry_after(
            deficit_milli=1,
            refill_amount_milli=0,
            refill_period_ms=86_400_000,
            next_reset_ms=self.NOW + 3_600_000,
            now_ms=self.NOW,
        )
        assert big == small == 3600.001

    def test_a_reset_already_past_reports_no_wait(self):
        """Nothing has applied the edge yet, but a negative wait is worse than
        none: a client would `sleep()` on a nonsense value. Discriminates
        against returning the raw difference."""
        assert (
            calculate_retry_after(
                deficit_milli=5_000_000,
                refill_amount_milli=0,
                refill_period_ms=86_400_000,
                next_reset_ms=self.NOW - 60_000,
                now_ms=self.NOW,
            )
            == 0.0
        )

    def test_a_reset_exactly_now_reports_no_wait(self):
        """The boundary between the two branches above."""
        assert (
            calculate_retry_after(
                deficit_milli=5_000_000,
                refill_amount_milli=0,
                refill_period_ms=86_400_000,
                next_reset_ms=self.NOW,
                now_ms=self.NOW,
            )
            == 0.0
        )

    def test_a_zero_rate_with_no_reset_still_reports_no_wait(self):
        """The pre-#530 behaviour, preserved. Unreachable for a limit built
        through the public API — ADR-137 forbids a zero rate without a reset —
        so what is left of this branch guards a corrupt stored item."""
        assert (
            calculate_retry_after(
                deficit_milli=5_000_000,
                refill_amount_milli=0,
                refill_period_ms=86_400_000,
                next_reset_ms=None,
                now_ms=self.NOW,
            )
            == 0.0
        )

    def test_the_instant_and_the_clock_must_travel_together(self):
        """An absolute instant cannot become a wait without the reading it is
        measured against, so half the pair is treated as neither."""
        assert (
            calculate_retry_after(
                deficit_milli=5_000_000,
                refill_amount_milli=0,
                refill_period_ms=86_400_000,
                next_reset_ms=self.NOW + 3_600_000,
            )
            == 0.0
        )

    def test_a_cleared_deficit_wins_over_a_pending_reset(self):
        """The deficit guard runs first: nothing is owed, so nothing is waited
        for. Discriminates against 'a reset always decides the answer'."""
        assert (
            calculate_retry_after(
                deficit_milli=0,
                refill_amount_milli=0,
                refill_period_ms=86_400_000,
                next_reset_ms=self.NOW + 3_600_000,
                now_ms=self.NOW,
            )
            == 0.0
        )

    def test_a_positive_rate_ignores_the_reset_entirely(self):
        """The existing arithmetic is untouched: a limit that drips is answered
        by its rate even when a reset is also pending. Discriminates against
        checking the reset edge before the rate — which is exactly the ordering
        bug in surface-plan Task 5's walk that #530 also records."""
        with_reset = calculate_retry_after(
            deficit_milli=10_000_000,
            refill_amount_milli=100_000_000,
            refill_period_ms=60_000,
            next_reset_ms=self.NOW + 3_600_000,
            now_ms=self.NOW,
        )
        without_reset = calculate_retry_after(
            deficit_milli=10_000_000,
            refill_amount_milli=100_000_000,
            refill_period_ms=60_000,
        )
        assert with_reset == without_reset
        assert with_reset == pytest.approx(6.0, abs=0.01)
        assert with_reset != 3600.001


class TestShardedRetryEstimate:
    """A per-shard refill share that floors to zero still needs a finite wait.

    ``effective_refill_amount_milli`` is ``refill_amount_milli //
    shard_count``, which is 0 for a slow refill on a heavily sharded bucket.
    Dividing by it raised ``ZeroDivisionError`` from inside the rejection path
    (#475); the estimate now falls back to the undivided rate.
    """

    @staticmethod
    def _state(shard_count: int) -> BucketState:
        # Limit.custom("rpd", 5, refill_amount=1, refill_period_seconds=60)
        return BucketState(
            entity_id="e1",
            resource="gpt-4",
            limit_name="rpd",
            tokens_milli=0,
            last_refill_ms=1_000,
            capacity_milli=5_000,
            refill_amount_milli=1_000,
            refill_period_ms=60_000,
            shard_count=shard_count,
        )

    def test_share_that_floors_to_zero_falls_back_to_the_undivided_rate(self):
        state = self._state(1024)
        assert state.effective_refill_amount_milli(1_000) == 0
        assert state.retry_refill_amount_milli(1_000) == 1_000

    def test_nonzero_share_is_used_as_is(self):
        state = self._state(2)
        assert state.retry_refill_amount_milli(1_000) == 500

    def test_try_consume_rejects_without_dividing_by_zero(self):
        """The exact repro: 1 token/min at shard_count=1024."""
        result = try_consume(self._state(1024), 1, 1_000)
        assert result.success is False
        assert result.retry_after_seconds > 0


class TestForceConsume:
    """Tests for force_consume function."""

    @pytest.fixture
    def bucket_state(self):
        """Create a bucket state for testing."""
        return BucketState(
            entity_id="test",
            resource="gpt-4",
            limit_name="tpm",
            tokens_milli=100_000_000,
            last_refill_ms=0,
            capacity_milli=100_000_000,
            refill_amount_milli=100_000_000,
            refill_period_ms=60_000,
        )

    def test_force_consume_within_capacity(self, bucket_state):
        """Force consume within capacity."""
        new_tokens, _ = force_consume(bucket_state, 50_000, now_ms=0)
        assert new_tokens == 50_000_000  # 50k remaining

    def test_force_consume_beyond_capacity(self, bucket_state):
        """Force consume beyond capacity (goes negative)."""
        new_tokens, _ = force_consume(bucket_state, 150_000, now_ms=0)
        assert new_tokens == -50_000_000  # 50k debt

    def test_force_consume_negative_returns_tokens(self, bucket_state):
        """Negative amount returns tokens."""
        new_tokens, _ = force_consume(bucket_state, -50_000, now_ms=0)
        assert new_tokens == 150_000_000  # but capped on next refill


class TestCalculateAvailable:
    """Tests for calculate_available function."""

    def test_available_after_refill(self):
        """Calculate available includes refill."""
        state = BucketState(
            entity_id="test",
            resource="gpt-4",
            limit_name="tpm",
            tokens_milli=0,
            last_refill_ms=0,
            capacity_milli=100_000_000,
            refill_amount_milli=100_000_000,
            refill_period_ms=60_000,
        )
        # 30 seconds later
        available = calculate_available(state, now_ms=30_000)
        assert available == 50_000  # 50k tokens

    def test_available_negative_bucket(self):
        """Available can be negative."""
        state = BucketState(
            entity_id="test",
            resource="gpt-4",
            limit_name="tpm",
            tokens_milli=-100_000_000,  # 100k debt
            last_refill_ms=0,
            capacity_milli=100_000_000,
            refill_amount_milli=100_000_000,
            refill_period_ms=60_000,
        )
        # 30 seconds later, refill 50k
        available = calculate_available(state, now_ms=30_000)
        assert available == -50_000  # still 50k debt


class TestWouldRefillSatisfy:
    """Tests for would_refill_satisfy function."""

    def _make_bucket(
        self,
        limit_name: str = "rpm",
        tokens_milli: int = 0,
        last_refill_ms: int = 0,
        capacity_milli: int = 100_000,
        refill_amount_milli: int = 100_000,
        refill_period_ms: int = 60_000,
    ) -> BucketState:
        return BucketState(
            entity_id="entity-1",
            resource="gpt-4",
            limit_name=limit_name,
            tokens_milli=tokens_milli,
            last_refill_ms=last_refill_ms,
            capacity_milli=capacity_milli,
            refill_amount_milli=refill_amount_milli,
            refill_period_ms=refill_period_ms,
        )

    def test_refill_would_satisfy_single_limit(self):
        """After enough time, refill provides sufficient tokens."""
        # Bucket at 0 tokens, 30s has elapsed → 50 tokens refilled (100/min)
        bucket = self._make_bucket(tokens_milli=0, last_refill_ms=0)
        would_help, statuses = would_refill_satisfy([bucket], {"rpm": 10}, now_ms=30_000)
        assert would_help is True
        assert len(statuses) == 1
        assert not statuses[0].exceeded

    def test_refill_would_not_satisfy_exhausted_bucket(self):
        """Even after refill, not enough tokens for the request."""
        # Bucket at 0, 1s elapsed → ~1.67 tokens refilled, need 100
        bucket = self._make_bucket(tokens_milli=0, last_refill_ms=0)
        would_help, statuses = would_refill_satisfy([bucket], {"rpm": 100}, now_ms=1_000)
        assert would_help is False
        assert len(statuses) == 1
        assert statuses[0].exceeded
        assert statuses[0].retry_after_seconds > 0

    def test_multi_limit_all_pass(self):
        """Multiple limits all pass after refill."""
        rpm_bucket = self._make_bucket(limit_name="rpm", tokens_milli=0, last_refill_ms=0)
        tpm_bucket = self._make_bucket(
            limit_name="tpm",
            tokens_milli=0,
            last_refill_ms=0,
            capacity_milli=200_000_000,
            refill_amount_milli=200_000_000,
            refill_period_ms=60_000,
        )
        would_help, statuses = would_refill_satisfy(
            [rpm_bucket, tpm_bucket], {"rpm": 1, "tpm": 100}, now_ms=30_000
        )
        assert would_help is True
        assert len(statuses) == 2

    def test_multi_limit_one_fails(self):
        """One limit passes but another still fails."""
        rpm_bucket = self._make_bucket(limit_name="rpm", tokens_milli=50_000, last_refill_ms=0)
        tpm_bucket = self._make_bucket(
            limit_name="tpm",
            tokens_milli=0,
            last_refill_ms=0,
            capacity_milli=1_000,
            refill_amount_milli=1_000,
            refill_period_ms=60_000,
        )
        # rpm has plenty, tpm has 1/min capacity, need 100 → fails
        would_help, statuses = would_refill_satisfy(
            [rpm_bucket, tpm_bucket], {"rpm": 1, "tpm": 100}, now_ms=1_000
        )
        assert would_help is False

    def test_skips_limits_not_in_consume(self):
        """Limits not in consume dict are ignored."""
        bucket = self._make_bucket(limit_name="rpm", tokens_milli=50_000)
        would_help, statuses = would_refill_satisfy([bucket], {"tpm": 100}, now_ms=1_000)
        # No statuses for rpm since tpm isn't in the bucket
        assert would_help is True
        assert len(statuses) == 0

    def test_statuses_have_correct_retry_after(self):
        """LimitStatus objects include accurate retry_after_seconds."""
        bucket = self._make_bucket(tokens_milli=0, last_refill_ms=0)
        would_help, statuses = would_refill_satisfy([bucket], {"rpm": 100}, now_ms=0)
        assert would_help is False
        assert statuses[0].retry_after_seconds > 0
        assert statuses[0].limit.name == "rpm"


NY = ZoneInfo("America/New_York")
TUE_1400 = int(datetime(2026, 9, 15, 14, 0, tzinfo=NY).timestamp() * 1000)  # inside 9-17
TUE_0300 = int(datetime(2026, 9, 15, 3, 0, tzinfo=NY).timestamp() * 1000)  # outside


def _sched_state(**kwargs) -> BucketState:
    """A BucketState with the three required identity fields filled in."""
    base = dict(
        entity_id="user-1",
        resource="gpt-4",
        limit_name="rpm",
        tokens_milli=0,
        last_refill_ms=0,
        capacity_milli=1_000_000,
        refill_amount_milli=1_000_000,
        refill_period_ms=60_000,
    )
    base.update(kwargs)
    return BucketState(**base)  # type: ignore[arg-type]


BUSINESS = (ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),)


class TestScheduledEffectiveParams:
    """The effective params of a scheduled bucket are a function of time (#222 §3.5)."""

    def test_scale_applies_before_shard_division(self):
        """Scale then divide: the schedule applies to the whole limit, shards
        split the result. Dividing first would floor twice against a smaller
        numerator and drift."""
        state = _sched_state(shard_count=4, sched=BUSINESS)
        assert state.effective_capacity_milli(TUE_1400) == 125_000  # (1_000_000*0.5)//4
        assert state.effective_capacity_milli(TUE_0300) == 250_000  # 1_000_000//4, no match

    def test_scale_then_divide_is_not_divide_then_scale(self):
        """The ordering is observable, and this is a case where it shows.

        Both orders floor twice, and for most inputs the two floors land on the
        same integer — which makes an ordering bug invisible to a carelessly
        chosen example. 7 shards of a 0.99x window separates them:

        * scale-then-divide (correct): ``int(1_000 * 0.99) // 7`` == 141
        * divide-then-scale (wrong):   ``int((1_000 // 7) * 0.99)`` == 140
        """
        sched = (ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.99),)
        state = _sched_state(
            capacity_milli=1_000, refill_amount_milli=1_000, shard_count=7, sched=sched
        )
        assert state.effective_capacity_milli(TUE_1400) == 141
        assert state.effective_refill_amount_milli(TUE_1400) == 141

    def test_refill_scales_with_capacity(self):
        state = _sched_state(shard_count=1, sched=BUSINESS)
        assert state.effective_refill_amount_milli(TUE_1400) == 500_000
        assert state.effective_refill_amount_milli(TUE_0300) == 1_000_000

    def test_unscheduled_state_is_unchanged_at_any_instant(self):
        state = _sched_state(shard_count=1)
        assert state.effective_capacity_milli(TUE_1400) == 1_000_000
        assert state.effective_capacity_milli(TUE_0300) == 1_000_000

    def test_absolute_entry_overrides_capacity(self):
        night = (ScheduleEntry(cron="* 0-6 * * *", tz="America/New_York", capacity=2000),)
        state = _sched_state(shard_count=1, sched=night)
        assert state.effective_capacity_milli(TUE_0300) == 2_000_000
        assert state.effective_capacity_milli(TUE_1400) == 1_000_000

    def test_retry_rate_falls_back_to_the_undivided_scheduled_rate(self):
        """A share that floors to 0 has no finite wait. Fall back to the
        *scheduled* undivided rate — falling back to the base rate would quote
        a speed nothing in the system refills at during the window."""
        state = _sched_state(refill_amount_milli=1_000, shard_count=1024, sched=BUSINESS)
        assert state.effective_refill_amount_milli(TUE_1400) == 0
        assert state.retry_refill_amount_milli(TUE_1400) == 500  # 1_000*0.5, undivided

    def test_retry_rate_uses_the_share_when_it_is_non_zero(self):
        state = _sched_state(shard_count=2, sched=BUSINESS)
        assert state.retry_refill_amount_milli(TUE_1400) == 250_000


class TestScheduledRefillPeriod:
    """An absolute ``ScheduleEntry`` may replace ``refill_period_seconds`` too.

    ``effective_params`` has always returned a triple and the aggregator
    (``processor.py``) already feeds all three into ``refill_bucket``. The
    client must use the same denominator or the two refillers compute
    different token counts for the same bucket in the same window.
    """

    FASTER = (
        ScheduleEntry(
            cron="* 9-17 * * MON-FRI",
            tz="America/New_York",
            capacity=10_000,
            refill_amount=1_000,
            refill_period_seconds=6,  # 10x the base rate
        ),
    )

    def _state(self, **kwargs) -> BucketState:
        return _sched_state(
            capacity_milli=10_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
            **kwargs,
        )

    def test_period_follows_the_schedule(self):
        state = self._state(sched=self.FASTER)
        assert state.effective_refill_period_ms(TUE_1400) == 6_000
        assert state.effective_refill_period_ms(TUE_0300) == 60_000

    def test_period_is_never_divided_by_shard_count(self):
        """Shards split the numerator; they all refill on the same clock."""
        state = self._state(shard_count=4, sched=self.FASTER)
        assert state.effective_refill_period_ms(TUE_1400) == 6_000

    def test_refill_uses_the_scheduled_period(self):
        """The end-to-end check: one minute of elapsed time refills 10x as much
        inside the window. Reading the base period here yields 1_000."""
        state = self._state(sched=self.FASTER, last_refill_ms=TUE_1400 - 60_000)
        assert calculate_available(state, TUE_1400) == 10_000

        outside = self._state(sched=self.FASTER, last_refill_ms=TUE_0300 - 60_000)
        assert calculate_available(outside, TUE_0300) == 1_000

    def test_retry_estimate_uses_the_scheduled_period(self):
        """A wait quoted against the base period is 10x too long here."""
        state = self._state(sched=self.FASTER, last_refill_ms=TUE_1400)
        # 1_000 tokens short at 1_000 tokens / 6 s
        assert calculate_time_until_available(state, 1_000, TUE_1400) == pytest.approx(
            6.0, abs=0.01
        )
