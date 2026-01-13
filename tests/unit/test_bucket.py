"""Tests for token bucket calculations."""

import pytest

from zae_limiter.bucket import (
    calculate_available,
    calculate_retry_after,
    force_consume,
    refill_bucket,
    try_consume,
)
from zae_limiter.models import BucketState


class TestRefillBucket:
    """Tests for refill_bucket function."""

    def test_no_time_elapsed(self):
        """No refill when no time has passed."""
        result = refill_bucket(
            tokens_milli=50_000_000,  # 50k tokens
            last_refill_ms=1000,
            now_ms=1000,
            burst_milli=100_000_000,
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
            burst_milli=100_000_000,
            refill_amount_milli=100_000_000,
            refill_period_ms=60_000,
        )
        assert result.new_tokens_milli == 50_000_000  # 50k tokens

    def test_full_refill_capped_at_burst(self):
        """Refill capped at burst capacity."""
        # 2 minutes elapsed, but capped at burst
        result = refill_bucket(
            tokens_milli=0,
            last_refill_ms=0,
            now_ms=120_000,  # 2 minutes
            burst_milli=100_000_000,
            refill_amount_milli=100_000_000,
            refill_period_ms=60_000,
        )
        assert result.new_tokens_milli == 100_000_000  # capped at burst

    def test_negative_bucket_refills(self):
        """Negative bucket refills towards zero."""
        result = refill_bucket(
            tokens_milli=-50_000_000,  # -50k tokens (debt)
            last_refill_ms=0,
            now_ms=30_000,  # 30 seconds
            burst_milli=100_000_000,
            refill_amount_milli=100_000_000,
            refill_period_ms=60_000,
        )
        # -50k + 50k = 0
        assert result.new_tokens_milli == 0


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
            burst_milli=100_000_000,
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
            burst_milli=100_000_000,
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
            burst_milli=100_000_000,
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
            burst_milli=100_000_000,
            refill_amount_milli=100_000_000,
            refill_period_ms=60_000,
        )
        # 30 seconds later, refill 50k
        available = calculate_available(state, now_ms=30_000)
        assert available == -50_000  # still 50k debt
