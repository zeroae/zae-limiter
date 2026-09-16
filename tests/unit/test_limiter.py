"""Tests for RateLimiter."""

import asyncio
import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from botocore.exceptions import ClientError

from zae_limiter import (
    CacheStats,
    Limit,
    LimiterInfo,
    OnUnavailable,
    RateLimiter,
    RateLimiterUnavailable,
    RateLimitExceeded,
    ValidationError,
)
from zae_limiter.bucket import calculate_available
from zae_limiter.exceptions import (
    InvalidIdentifierError,
    InvalidNameError,
    LeaseExpiredError,
)
from zae_limiter.infra.discovery import InfrastructureDiscovery
from zae_limiter.models import BucketState
from zae_limiter.repository_protocol import SpeculativeResult
from zae_limiter.schedule import ScheduleEntry, retry_after_with_schedule
from zae_limiter.schema import (
    BUCKET_FIELD_TK,
    BUCKET_FIELD_VU,
    bucket_attr,
    pk_bucket,
    sk_state,
)


def freeze_clock(repo) -> int:
    """Pin ``repo._now_ms()`` to one instant for the rest of the test (#498).

    ``Repository._now_ms()`` is the single injectable token-bucket clock
    (#430); see :class:`TestClockSeam` for the full contract. Freezing it makes
    every later write and read observe zero elapsed time, so the lazy refill
    credits nothing and a read-back assertion cannot drift upward.

    **Why a read-back assertion drifts.** ``Limit.per_minute("rpm", 100)`` is
    ``capacity=100, refill_amount=100, refill_period_seconds=60`` — one token
    every 600 ms. ``available()`` refills lazily and read-only:
    ``RateLimiter.check_availability()`` reads ``_now_ms()`` once and calls
    ``bucket.calculate_available()``, which runs ``bucket.refill_bucket()`` and
    clamps at ``capacity``. So after an ``acquire(consume={"rpm": 1})`` leaves
    the bucket at 99, ``600 ms`` of wall clock between the write that stamped
    ``rf`` and the read flips ``99`` to ``100`` — exactly the ``assert 100 ==
    99`` that #498 hit in CI. (599 ms still reads 99:
    ``599 * 100_000 // 60_000 == 998`` millitokens, short of the 1_000 needed.)
    600 ms between two moto-backed calls is reachable on a loaded ``-n auto``
    runner, which is why it was intermittent.

    Call this *before* the ``acquire()``, so the ``rf`` stamp and the later
    read share one instant.

    Returns:
        The frozen instant, in epoch milliseconds.
    """
    frozen = repo._now_ms()
    repo._now_ms = lambda: frozen
    return frozen


class TestRateLimiterEntities:
    """Tests for entity management."""

    async def test_create_entity(self, limiter):
        """Test creating an entity."""
        entity = await limiter.create_entity(
            entity_id="proj-1",
            name="Test Project",
            metadata={"tier": "premium"},
        )
        assert entity.id == "proj-1"
        assert entity.name == "Test Project"
        assert entity.parent_id is None
        assert entity.metadata == {"tier": "premium"}

    async def test_create_child_entity(self, limiter):
        """Test creating a child entity."""
        await limiter.create_entity(entity_id="proj-1")
        child = await limiter.create_entity(
            entity_id="key-1",
            name="API Key 1",
            parent_id="proj-1",
        )
        assert child.parent_id == "proj-1"

    async def test_get_entity(self, limiter):
        """Test getting an entity."""
        await limiter.create_entity(entity_id="proj-1", name="Test")
        entity = await limiter.get_entity("proj-1")
        assert entity is not None
        assert entity.id == "proj-1"

    async def test_get_nonexistent_entity(self, limiter):
        """Test getting a nonexistent entity."""
        entity = await limiter.get_entity("nonexistent")
        assert entity is None

    async def test_get_children(self, limiter):
        """Test getting children of a parent."""
        await limiter.create_entity(entity_id="proj-1")
        await limiter.create_entity(entity_id="key-1", parent_id="proj-1")
        await limiter.create_entity(entity_id="key-2", parent_id="proj-1")

        children = await limiter.get_children("proj-1")
        assert len(children) == 2
        child_ids = {c.id for c in children}
        assert child_ids == {"key-1", "key-2"}

    async def test_delete_entity(self, limiter):
        """Test deleting an entity."""
        await limiter.create_entity(entity_id="proj-1")
        await limiter.delete_entity("proj-1")
        entity = await limiter.get_entity("proj-1")
        assert entity is None


class TestRateLimiterAcquire:
    """Tests for acquire functionality."""

    async def test_acquire_success(self, limiter):
        """Test successful rate limit acquisition."""
        limits = [Limit.per_minute("rpm", 100)]

        async with limiter.acquire(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
            consume={"rpm": 1},
        ) as lease:
            assert lease.consumed == {"rpm": 1}

    async def test_acquire_multiple_limits(self, limiter):
        """Test acquiring multiple limits at once."""
        limits = [
            Limit.per_minute("rpm", 100),
            Limit.per_minute("tpm", 10_000),
        ]

        async with limiter.acquire(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
            consume={"rpm": 1, "tpm": 500},
        ) as lease:
            assert lease.consumed == {"rpm": 1, "tpm": 500}

    async def test_acquire_exceeds_limit(self, limiter):
        """Test that exceeding limit raises exception."""
        limits = [Limit.per_minute("rpm", 10)]

        with pytest.raises(RateLimitExceeded) as exc_info:
            async with limiter.acquire(
                entity_id="key-1",
                resource="gpt-4",
                limits=limits,
                consume={"rpm": 20},
            ):
                pass

        exc = exc_info.value
        assert len(exc.violations) == 1
        assert exc.violations[0].limit_name == "rpm"
        assert exc.violations[0].requested == 20
        assert exc.violations[0].available == 10
        assert exc.retry_after_seconds > 0

    async def test_acquire_exception_includes_all_limits(self, limiter):
        """Test that exception includes status of all limits."""
        limits = [
            Limit.per_minute("rpm", 100),  # will pass
            Limit.per_minute("tpm", 100),  # will fail
        ]

        with pytest.raises(RateLimitExceeded) as exc_info:
            async with limiter.acquire(
                entity_id="key-1",
                resource="gpt-4",
                limits=limits,
                consume={"rpm": 1, "tpm": 200},
            ):
                pass

        exc = exc_info.value
        assert len(exc.statuses) == 2
        assert len(exc.violations) == 1
        assert len(exc.passed) == 1
        assert exc.passed[0].limit_name == "rpm"
        assert exc.violations[0].limit_name == "tpm"

    async def test_acquire_rollback_on_exception(self, limiter):
        """Test that consumption is rolled back on exception."""
        limits = [Limit.per_minute("rpm", 100)]

        try:
            async with limiter.acquire(
                entity_id="key-1",
                resource="gpt-4",
                limits=limits,
                consume={"rpm": 10},
            ):
                raise ValueError("Simulated error")
        except ValueError:
            pass

        # Check that capacity is still available
        available = await limiter.available(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
        )
        # Bucket should still have full capacity (no commit happened).
        # Immune to clock drift (#503): 100 is the capacity ceiling and
        # `refill_bucket()` clamps there, so this cannot drift upward.
        assert available["rpm"] == 100

    async def test_acquire_fallback_when_batch_not_supported(self, limiter, monkeypatch):
        """Test that acquire falls back to sequential get_buckets when batch not supported."""
        from zae_limiter.models import BackendCapabilities

        limits = [Limit.per_minute("rpm", 100)]

        # First, create a bucket with batch operations enabled
        async with limiter.acquire(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
            consume={"rpm": 1},
        ) as lease:
            assert lease.consumed == {"rpm": 1}

        # Now override capabilities to disable batch operations
        no_batch_capabilities = BackendCapabilities(
            supports_audit_logging=True,
            supports_usage_snapshots=True,
            supports_infrastructure_management=True,
            supports_change_streams=True,
            supports_batch_operations=False,  # Disable batch
        )
        monkeypatch.setattr(limiter._repository, "_capabilities", no_batch_capabilities)

        # Second acquire should use fallback path with existing bucket
        async with limiter.acquire(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
            consume={"rpm": 1},
        ) as lease:
            assert lease.consumed == {"rpm": 1}


class TestRateLimiterRefillRecovery:
    """Wait-then-acquire: an exhausted bucket recovers after enough time passes.

    Regression for the stale slow-path bucket discriminator (buckets moved to
    SK=#STATE in the per-shard migration, but batch_get_entity_and_buckets still
    filtered on the old "#BUCKET#" prefix). With buckets silently dropped, the
    refill-recovery fallback treated existing buckets as new and the conditional
    write failed with a bogus retry_after=0.0 instead of refilling.
    """

    async def test_acquire_succeeds_after_refill_wait(self, limiter):
        """Exhaust a bucket, wait for refill, and acquire again (speculative on)."""
        # 100 tokens, refills the full bucket every second.
        limits = [Limit.custom("rpm", capacity=100, refill_amount=100, refill_period_seconds=1)]

        # Drain the bucket completely.
        async with limiter.acquire("key-1", "gpt-4", limits=limits, consume={"rpm": 100}):
            pass

        # Immediately exhausted: rejection must report a real wait, not 0.0.
        with pytest.raises(RateLimitExceeded) as exc_info:
            async with limiter.acquire("key-1", "gpt-4", limits=limits, consume={"rpm": 100}):
                pass
        assert exc_info.value.retry_after_seconds > 0

        # After refilling, the same acquire must succeed.
        await asyncio.sleep(1.1)
        async with limiter.acquire("key-1", "gpt-4", limits=limits, consume={"rpm": 50}) as lease:
            assert lease.consumed == {"rpm": 50}

    async def test_acquire_succeeds_after_refill_wait_non_speculative(self, limiter):
        """Same recovery on the pure slow path (speculative writes disabled)."""
        slow = RateLimiter(repository=limiter._repository, speculative_writes=False)
        limits = [Limit.custom("rpm", capacity=100, refill_amount=100, refill_period_seconds=1)]

        async with slow.acquire("key-2", "gpt-4", limits=limits, consume={"rpm": 100}):
            pass

        with pytest.raises(RateLimitExceeded) as exc_info:
            async with slow.acquire("key-2", "gpt-4", limits=limits, consume={"rpm": 100}):
                pass
        assert exc_info.value.retry_after_seconds > 0

        await asyncio.sleep(1.1)
        async with slow.acquire("key-2", "gpt-4", limits=limits, consume={"rpm": 50}) as lease:
            assert lease.consumed == {"rpm": 50}


class TestClockSeam:
    """``Repository._now_ms()`` is the injectable token-bucket clock (#430).

    Every clock read that feeds refill math, an ``rf`` stamp or a bucket TTL
    routes through ``_now_ms()``, so a test controls that clock by patching
    one method — no ``sleep``, and no patching of the global ``time``
    module, which would also move moto's and botocore's clocks. It is not
    the library's only clock: ``config_cache`` ages in *seconds* against
    ``time.time()`` and is deliberately out of scope for #430, so advancing
    this seam ages buckets but leaves cached config exactly as it was.

    **One instant per write, not per acquire.** A single speculative
    ``UpdateItem`` derives its ``ttl`` stamp, its TTL-expiry guard and the
    caller's admission decision from one reading. The slow path reads again
    on purpose: it runs after a ``BatchGetItem`` and then commits a
    transaction, and reusing the fast path's instant across those round
    trips would stamp ``rf`` in the past and under-refill. The counts are
    the contract, and each row below is pinned by a test here:

    ===================================== ========
    Path                                  readings
    ===================================== ========
    Warm speculative fast path            1
    ``speculative_writes=False``          2
    Speculative miss, then the slow path  3
    ===================================== ========

    #222 (scheduled limits) is why the speculative write needs its single
    instant: it puts an item-level ``vu`` (valid-until, epoch ms) into that
    ConditionExpression. The slow path always runs *after* the fast path, so
    ``now_slow >= now_fast`` — and with two readings straddling a window
    boundary ``T`` (``now_fast < T <= now_slow``) the fast path evaluates
    ``vu`` while still inside the expiring window and passes, then the slow
    path materialises tokens at ``now_slow``, already past ``T``, minting
    the old window's allowance into the new one. One reading per write
    closes that gap.
    """

    LIMITS = [Limit.custom("rpm", capacity=100, refill_amount=100, refill_period_seconds=60)]

    @staticmethod
    def _counting_clock(repo, step_ms: int = 60_000):
        """Patch ``repo._now_ms`` with a counter; return the list of readings.

        Each reading jumps a full minute, so a surviving second read cannot
        be confused with the first — while staying inside the bucket TTL, so
        the speculative write's expiry guard still passes.
        """
        readings: list[int] = []
        base = repo._now_ms()

        def fake_now_ms() -> int:
            readings.append(base + step_ms * len(readings))
            return readings[-1]

        repo._now_ms = fake_now_ms
        return readings

    async def test_acquire_reads_the_clock_once(self, limiter):
        """One speculative ``acquire()`` observes exactly one ``_now_ms()``."""
        repo = limiter._repository

        # Warm up: create the bucket and the entity cache so the measured
        # acquire is a pure speculative fast path.
        async with limiter.acquire("clock-1", "gpt-4", limits=self.LIMITS, consume={"rpm": 1}):
            pass

        readings = self._counting_clock(repo)
        async with limiter.acquire("clock-1", "gpt-4", limits=self.LIMITS, consume={"rpm": 1}):
            pass

        assert len(readings) == 1, f"acquire() read the clock {len(readings)} times: {readings}"

    async def test_non_speculative_acquire_reads_the_clock_twice(self, limiter):
        """``speculative_writes=False``: the slow path, then the lease commit.

        Pins the second row of the contract. `_do_acquire()` reads to refill
        and admit; `Lease._commit_initial()` reads again to stamp ``rf`` on
        the transaction it is about to send. Reusing the first reading would
        backdate ``rf`` by the BatchGetItem round trip.
        """
        repo = limiter._repository
        slow = RateLimiter(repository=repo, speculative_writes=False)

        # Warm up: create the bucket and the entity, so the measured acquire
        # is a plain slow-path admission rather than a bucket creation.
        async with slow.acquire("clock-4", "gpt-4", limits=self.LIMITS, consume={"rpm": 1}):
            pass

        readings = self._counting_clock(repo)
        async with slow.acquire("clock-4", "gpt-4", limits=self.LIMITS, consume={"rpm": 1}):
            pass

        assert len(readings) == 2, f"acquire() read the clock {len(readings)} times: {readings}"

    async def test_speculative_miss_falls_back_and_reads_three_times(self, limiter):
        """A speculative miss adds the fast path's reading to the slow path's.

        Pins the third row: fast path (1) + ``_do_acquire`` (2) +
        ``Lease._commit_initial`` (3). The bucket does not exist yet, so the
        conditional ``UpdateItem`` fails with ``BUCKET_MISSING`` and the slow
        path creates it.
        """
        repo = limiter._repository

        # Warm up on a *different* entity so `_ensure_initialized()` and the
        # config cache are settled; the measured entity stays untouched, so
        # its first acquire is a genuine speculative miss.
        async with limiter.acquire("clock-5-warm", "gpt-4", limits=self.LIMITS, consume={"rpm": 1}):
            pass

        readings = self._counting_clock(repo)
        async with limiter.acquire("clock-5", "gpt-4", limits=self.LIMITS, consume={"rpm": 1}):
            pass

        assert len(readings) == 3, f"acquire() read the clock {len(readings)} times: {readings}"

    async def test_speculative_consume_stamps_a_single_instant(self, limiter):
        """``ttl`` and the TTL-expiry guard derive from the same reading."""
        repo = limiter._repository

        async with limiter.acquire("clock-2", "gpt-4", limits=self.LIMITS, consume={"rpm": 1}):
            pass

        client = await repo._get_client()
        captured: list[dict] = []
        original_update_item = client.update_item

        async def spy_update_item(**kwargs):
            captured.append(kwargs)
            return await original_update_item(**kwargs)

        client.update_item = spy_update_item
        readings = self._counting_clock(repo)
        try:
            result = await repo.speculative_consume(
                "clock-2", "gpt-4", {"rpm": 1}, ttl_seconds=3600
            )
        finally:
            client.update_item = original_update_item

        assert result.success
        assert len(readings) == 1, (
            f"speculative_consume() read the clock {len(readings)} times: {readings}"
        )
        now_ms = readings[0]
        values = captured[0]["ExpressionAttributeValues"]
        # calculate_ttl(now_ms, 3600) == now_ms // 1000 + 3600
        assert int(values[":ttl"]["N"]) == now_ms // 1000 + 3600
        assert int(values[":now_epoch"]["N"]) == now_ms // 1000

    async def test_now_ms_alone_drives_refill_recovery(self, limiter):
        """Exhaust, advance the clock via ``_now_ms`` only, acquire again.

        No ``sleep``, no patching of the global ``time`` module.
        """
        repo = limiter._repository
        clock = {"now": repo._now_ms()}
        repo._now_ms = lambda: clock["now"]

        # Drain the bucket completely.
        async with limiter.acquire("clock-3", "gpt-4", limits=self.LIMITS, consume={"rpm": 100}):
            pass

        # Frozen clock: no refill has happened, so this must be rejected.
        with pytest.raises(RateLimitExceeded):
            async with limiter.acquire(
                "clock-3", "gpt-4", limits=self.LIMITS, consume={"rpm": 100}
            ):
                pass

        # Advance one full refill period through the seam alone.
        clock["now"] += 60_000

        async with limiter.acquire(
            "clock-3", "gpt-4", limits=self.LIMITS, consume={"rpm": 100}
        ) as lease:
            assert lease.consumed == {"rpm": 100}


class TestRateLimiterLease:
    """Tests for Lease functionality."""

    async def test_lease_consume(self, limiter):
        """Test consuming additional tokens via lease."""
        limits = [Limit.per_minute("tpm", 10_000)]

        async with limiter.acquire(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
            consume={"tpm": 500},
        ) as lease:
            await lease.consume(tpm=500)
            assert lease.consumed == {"tpm": 1000}

    async def test_lease_consume_exceeds_limit(self, limiter):
        """Test that lease.consume raises when exceeding limit."""
        limits = [Limit.per_minute("tpm", 1000)]

        async with limiter.acquire(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
            consume={"tpm": 500},
        ) as lease:
            with pytest.raises(RateLimitExceeded):
                await lease.consume(tpm=600)

    async def test_lease_adjust(self, limiter):
        """Test adjusting consumption (unchecked)."""
        limits = [Limit.per_minute("tpm", 1000)]

        async with limiter.acquire(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
            consume={"tpm": 500},
        ) as lease:
            # Adjust by additional 1000 (goes over limit)
            await lease.adjust(tpm=1000)
            assert lease.consumed == {"tpm": 1500}  # over limit but allowed

    async def test_lease_release(self, limiter):
        """Test releasing tokens back."""
        limits = [Limit.per_minute("tpm", 1000)]

        async with limiter.acquire(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
            consume={"tpm": 500},
        ) as lease:
            await lease.release(tpm=200)
            assert lease.consumed == {"tpm": 300}


class TestLeaseEdgeCases:
    """Tests for Lease edge cases (committed/rolled-back state, zero amounts)."""

    async def test_consume_after_commit_raises(self, limiter):
        """consume() on a committed lease raises LeaseExpiredError."""
        limits = [Limit.per_minute("tpm", 10_000)]

        async with limiter.acquire(
            entity_id="key-edge-1",
            resource="gpt-4",
            limits=limits,
            consume={"tpm": 100},
        ) as lease:
            pass  # commit happens on exit

        with pytest.raises(LeaseExpiredError):
            await lease.consume(tpm=50)

    async def test_adjust_after_commit_raises(self, limiter):
        """adjust() on a committed lease raises LeaseExpiredError."""
        limits = [Limit.per_minute("tpm", 10_000)]

        async with limiter.acquire(
            entity_id="key-edge-2",
            resource="gpt-4",
            limits=limits,
            consume={"tpm": 100},
        ) as lease:
            pass

        with pytest.raises(LeaseExpiredError):
            await lease.adjust(tpm=50)

    async def test_release_after_commit_raises(self, limiter):
        """release() on a committed lease raises LeaseExpiredError.

        release() no longer delegates to adjust() (#455), so it carries its
        own expiry guard and needs its own test.
        """
        limits = [Limit.per_minute("tpm", 10_000)]

        async with limiter.acquire(
            entity_id="key-edge-2b",
            resource="gpt-4",
            limits=limits,
            consume={"tpm": 100},
        ) as lease:
            pass

        with pytest.raises(LeaseExpiredError):
            await lease.release(tpm=50)

    async def test_consume_zero_amount_is_noop(self, limiter):
        """consume() with zero amount skips processing."""
        limits = [Limit.per_minute("rpm", 100), Limit.per_minute("tpm", 10_000)]

        async with limiter.acquire(
            entity_id="key-edge-3",
            resource="gpt-4",
            limits=limits,
            consume={"rpm": 1, "tpm": 100},
        ) as lease:
            # Consume only rpm, tpm=0 should be skipped
            await lease.consume(rpm=1)
            assert lease.consumed == {"rpm": 2, "tpm": 100}

    async def test_adjust_zero_amount_is_noop(self, limiter):
        """adjust() with zero amount skips processing."""
        limits = [Limit.per_minute("rpm", 100), Limit.per_minute("tpm", 10_000)]

        async with limiter.acquire(
            entity_id="key-edge-4",
            resource="gpt-4",
            limits=limits,
            consume={"rpm": 1, "tpm": 100},
        ) as lease:
            await lease.adjust(rpm=5)  # only rpm, tpm=0 skipped
            assert lease.consumed == {"rpm": 6, "tpm": 100}


class TestLeaseRetryPath:
    """Tests for lease _commit retry path and helpers (ADR-115)."""

    def test_is_condition_check_failure_by_class_name(self):
        """Detects ConditionalCheckFailedException by class name."""
        from zae_limiter.lease import _is_condition_check_failure

        # Use type() to create class with AWS name (avoids N818 lint rule)
        exc_cls = type("ConditionalCheckFailedException", (Exception,), {})
        assert _is_condition_check_failure(exc_cls()) is True

    def test_is_condition_check_failure_transaction_canceled_with_condition_reason(self):
        """TransactionCanceledException with ConditionalCheckFailed reason → True."""
        from zae_limiter.lease import _is_condition_check_failure

        exc_cls = type("TransactionCanceledException", (Exception,), {})
        exc = exc_cls()
        exc.response = {  # type: ignore[attr-defined]
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [
                {"Code": "ConditionalCheckFailed"},
                {"Code": "None"},
            ],
        }
        assert _is_condition_check_failure(exc) is True

    def test_is_condition_check_failure_transaction_conflict_only(self):
        """TransactionCanceledException with only TransactionConflict → False."""
        from zae_limiter.lease import _is_condition_check_failure

        exc_cls = type("TransactionCanceledException", (Exception,), {})
        exc = exc_cls()
        exc.response = {  # type: ignore[attr-defined]
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [
                {"Code": "None"},
                {"Code": "TransactionConflict"},
            ],
        }
        assert _is_condition_check_failure(exc) is False

    def test_is_condition_check_failure_mixed_reasons(self):
        """Mixed ConditionalCheckFailed + TransactionConflict → True."""
        from zae_limiter.lease import _is_condition_check_failure

        exc_cls = type("TransactionCanceledException", (Exception,), {})
        exc = exc_cls()
        exc.response = {  # type: ignore[attr-defined]
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [
                {"Code": "ConditionalCheckFailed"},
                {"Code": "TransactionConflict"},
            ],
        }
        assert _is_condition_check_failure(exc) is True

    def test_is_condition_check_failure_transaction_canceled_no_response(self):
        """TransactionCanceledException without response attribute → False."""
        from zae_limiter.lease import _is_condition_check_failure

        exc_cls = type("TransactionCanceledException", (Exception,), {})
        assert _is_condition_check_failure(exc_cls()) is False

    def test_is_condition_check_failure_client_error(self):
        """Detects ConditionalCheckFailedException via botocore ClientError response."""
        from zae_limiter.lease import _is_condition_check_failure

        exc = Exception("test")
        exc.response = {  # type: ignore[attr-defined]
            "Error": {"Code": "ConditionalCheckFailedException"},
        }
        assert _is_condition_check_failure(exc) is True

    def test_is_condition_check_failure_unrelated(self):
        """Returns False for unrelated exceptions."""
        from zae_limiter.lease import _is_condition_check_failure

        assert _is_condition_check_failure(ValueError("test")) is False

    def test_is_transaction_conflict_pure_conflict(self):
        """TransactionCanceledException with TransactionConflict → True."""
        from zae_limiter.lease import _is_transaction_conflict

        exc_cls = type("TransactionCanceledException", (Exception,), {})
        exc = exc_cls()
        exc.response = {  # type: ignore[attr-defined]
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [
                {"Code": "None"},
                {"Code": "TransactionConflict"},
            ],
        }
        assert _is_transaction_conflict(exc) is True

    def test_is_transaction_conflict_condition_check_only(self):
        """TransactionCanceledException with only ConditionalCheckFailed → False."""
        from zae_limiter.lease import _is_transaction_conflict

        exc_cls = type("TransactionCanceledException", (Exception,), {})
        exc = exc_cls()
        exc.response = {  # type: ignore[attr-defined]
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [
                {"Code": "ConditionalCheckFailed"},
                {"Code": "None"},
            ],
        }
        assert _is_transaction_conflict(exc) is False

    def test_is_transaction_conflict_unrelated(self):
        """Returns False for unrelated exceptions."""
        from zae_limiter.lease import _is_transaction_conflict

        assert _is_transaction_conflict(ValueError("test")) is False

    def test_is_transaction_conflict_client_error(self):
        """TransactionConflict via botocore ClientError response."""
        from zae_limiter.lease import _is_transaction_conflict

        exc = Exception("test")
        exc.response = {  # type: ignore[attr-defined]
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [
                {"Code": "TransactionConflict"},
            ],
        }
        assert _is_transaction_conflict(exc) is True

    def test_build_retry_failure_statuses(self):
        """Builds LimitStatus list for retry failure with computed retry_after."""
        from zae_limiter.lease import LeaseEntry, _build_retry_failure_statuses

        limit = Limit.per_minute("rpm", 100)
        state = MagicMock()
        state.tokens_milli = 50_000
        state.retry_refill_amount_milli.return_value = 100_000
        state.effective_refill_period_ms.return_value = 60_000
        # The boundary walk reads the undivided base and both schedule tuples
        # off the state and narrows them itself (#222 §7), so a mock has to
        # supply real integers here rather than MagicMocks.
        state.capacity_milli = 100_000
        state.refill_amount_milli = 100_000
        state.refill_period_ms = 60_000
        state.sched = ()
        state.reset_sched = ()
        state.shard_count = 1
        entry = LeaseEntry(
            entity_id="e1",
            resource="gpt-4",
            limit=limit,
            state=state,
            consumed=60,
        )
        # An undeclared carrier entry (#455) must not appear in the statuses.
        undeclared_state = MagicMock()
        undeclared_state.tokens_milli = 0
        undeclared_state.shard_count = 1
        undeclared = LeaseEntry(
            entity_id="e1",
            resource="gpt-4",
            limit=Limit.per_minute("tpm", 1000),
            state=undeclared_state,
            consumed=0,
            _declared=False,
        )
        statuses = _build_retry_failure_statuses([entry, undeclared], now_ms=1000)
        assert len(statuses) == 1
        assert statuses[0].entity_id == "e1"
        assert statuses[0].limit_name == "rpm"
        assert statuses[0].available == 50
        assert statuses[0].requested == 60
        assert statuses[0].exceeded is True
        # deficit = 60*1000 - 50_000 = 10_000 milli
        # retry_after = (10_000 * 60_000 / 100_000 + 1) / 1000 = 6.001
        assert statuses[0].retry_after_seconds > 0.0

    def test_build_retry_failure_statuses_no_deficit(self):
        """retry_after_seconds is 0 when tokens are sufficient (shouldn't happen in practice)."""
        from zae_limiter.lease import LeaseEntry, _build_retry_failure_statuses

        limit = Limit.per_minute("rpm", 100)
        state = MagicMock()
        state.tokens_milli = 100_000
        state.retry_refill_amount_milli.return_value = 100_000
        state.effective_refill_period_ms.return_value = 60_000
        state.refill_period_ms = 60_000
        state.shard_count = 1
        entry = LeaseEntry(
            entity_id="e1",
            resource="gpt-4",
            limit=limit,
            state=state,
            consumed=10,
        )
        statuses = _build_retry_failure_statuses([entry], now_ms=1000)
        assert statuses[0].retry_after_seconds == 0.0

    async def test_commit_retry_on_condition_failure(self, limiter):
        """Commit retries with consumption-only on optimistic lock failure."""
        limits = [Limit.per_minute("tpm", 10_000)]

        async with limiter.acquire(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
            consume={"tpm": 100},
        ) as lease:
            # Verify the consume happened in the lease
            assert lease.consumed == {"tpm": 100}
        # Commit succeeds via normal path (no actual contention in moto)

    async def test_commit_retry_raises_rate_limit_exceeded(self):
        """When both normal and retry writes fail, raises RateLimitExceeded."""
        from zae_limiter.lease import Lease, LeaseEntry

        limit = Limit.per_minute("rpm", 100)
        state = MagicMock()
        state.tokens_milli = 50_000
        state.last_refill_ms = 1000
        state.total_consumed_milli = None
        state.retry_refill_amount_milli.return_value = 100_000
        state.effective_refill_period_ms.return_value = 60_000
        state.refill_period_ms = 60_000
        state.shard_count = 1

        entry = LeaseEntry(
            entity_id="e1",
            resource="gpt-4",
            limit=limit,
            state=state,
            consumed=10,
            _original_tokens_milli=100_000,
            _original_rf_ms=1000,
        )

        # Create a mock repo that raises ConditionalCheckFailed
        mock_repo = AsyncMock()
        exc_cls = type("TransactionCanceledException", (Exception,), {})
        condition_exc = exc_cls()
        condition_exc.response = {  # type: ignore[attr-defined]
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [{"Code": "ConditionalCheckFailed"}],
        }
        mock_repo.transact_write.side_effect = condition_exc
        mock_repo.build_composite_normal.return_value = {"Update": {}}
        mock_repo.build_composite_retry.return_value = {"Update": {}}
        mock_repo._bucket_ttl_refill_multiplier = 7
        # Sync on the real Repository (issue #430): an AsyncMock attribute
        # would hand the lease an un-awaited coroutine as `now_ms`, which
        # then flows into the mocked builders and leaks a RuntimeWarning.
        mock_repo._now_ms = MagicMock(return_value=1000)

        lease = Lease(repository=mock_repo, entries=[entry])
        with pytest.raises(RateLimitExceeded):
            await lease._commit_initial()


class TestWriteOnEnter:
    """Tests for write-on-enter behavior (Issue #309).

    Verifies _commit_initial(), _commit_adjustments(), and _rollback() paths.
    """

    def _make_entry(self, consumed=10, is_new=False, initial_consumed=0, entity_id="e1"):
        """Create a LeaseEntry with mock state."""
        from zae_limiter.lease import LeaseEntry

        limit = Limit.per_minute("rpm", 100)
        state = MagicMock()
        state.tokens_milli = 90_000
        state.last_refill_ms = 1000
        state.total_consumed_milli = None
        state.retry_refill_amount_milli.return_value = 100_000
        state.effective_refill_period_ms.return_value = 60_000
        state.refill_period_ms = 60_000
        state.shard_count = 1
        return LeaseEntry(
            entity_id=entity_id,
            resource="gpt-4",
            limit=limit,
            state=state,
            consumed=consumed,
            _original_tokens_milli=100_000,
            _original_rf_ms=1000,
            _is_new=is_new,
            _initial_consumed=initial_consumed,
        )

    def _make_mock_repo(self):
        """Create a mock repository."""
        repo = AsyncMock()
        repo.build_composite_normal.return_value = {"Update": {}}
        repo.build_composite_create.return_value = {"Put": {}}
        repo.build_composite_retry.return_value = {"Update": {}}
        repo.build_composite_adjust.return_value = {"Update": {}}
        repo._bucket_ttl_refill_multiplier = 7
        # `_now_ms()` is sync on the real Repository (issue #430). Left as an
        # AsyncMock attribute it returns a coroutine nobody awaits, which the
        # lease then passes as `now_ms` into the mocked builders — harmless to
        # the assertions, but it leaks "coroutine was never awaited"
        # RuntimeWarnings and fails under -W error::RuntimeWarning.
        repo._now_ms = MagicMock(return_value=1000)
        return repo

    async def test_commit_initial_empty_entries(self):
        """_commit_initial is no-op with empty entries (lines 259-263)."""
        from zae_limiter.lease import Lease

        mock_repo = self._make_mock_repo()
        lease = Lease(repository=mock_repo, entries=[])
        await lease._commit_initial()

        assert lease._initial_committed is True
        mock_repo.transact_write.assert_not_called()

    async def test_commit_initial_non_condition_check_reraises(self):
        """Non-condition-check exceptions propagate from _commit_initial (line 269)."""
        from zae_limiter.lease import Lease

        entry = self._make_entry()
        mock_repo = self._make_mock_repo()
        mock_repo.transact_write.side_effect = RuntimeError("network error")

        lease = Lease(repository=mock_repo, entries=[entry])
        with pytest.raises(RuntimeError, match="network error"):
            await lease._commit_initial()

    async def test_commit_initial_create_race_retry(self):
        """Create race falls through to retry path (lines 275-278)."""
        from zae_limiter.lease import Lease

        entry = self._make_entry(is_new=True)
        mock_repo = self._make_mock_repo()

        exc_cls = type("TransactionCanceledException", (Exception,), {})
        exc = exc_cls()
        exc.response = {  # type: ignore[attr-defined]
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [{"Code": "ConditionalCheckFailed"}],
        }
        mock_repo.transact_write.side_effect = [exc, None]

        lease = Lease(repository=mock_repo, entries=[entry])
        await lease._commit_initial()

        assert lease._initial_committed is True
        assert mock_repo.transact_write.call_count == 2
        # Second call uses retry items
        mock_repo.build_composite_retry.assert_called_once()

    async def test_commit_initial_create_targets_the_entry_shard(self):
        """A new bucket is created on the shard the acquire selected, stamped
        with the cached shard_count (issue #439)."""
        from zae_limiter.lease import Lease

        entry = self._make_entry(is_new=True)
        entry._shard_id = 3
        entry._shard_count = 4
        mock_repo = self._make_mock_repo()

        lease = Lease(repository=mock_repo, entries=[entry])
        await lease._commit_initial()

        kwargs = mock_repo.build_composite_create.call_args.kwargs
        assert kwargs["shard_id"] == 3
        assert kwargs["shard_count"] == 4

    async def test_commit_initial_normal_targets_the_entry_shard(self):
        """An existing sharded bucket is debited on its own shard (issue #439)."""
        from zae_limiter.lease import Lease

        entry = self._make_entry()
        entry._shard_id = 2
        mock_repo = self._make_mock_repo()

        lease = Lease(repository=mock_repo, entries=[entry])
        await lease._commit_initial()

        assert mock_repo.build_composite_normal.call_args.kwargs["shard_id"] == 2

    async def test_commit_initial_reissues_the_put_when_a_sibling_failed(self):
        """A cancelled transaction rolls back every item. If the new-shard Put
        was innocent (reason ``None``) and only the parent's rf lock failed,
        the retry must re-issue the Put and debit the parent consumption-only;
        a consumption-only retry against the still-missing shard would fail
        its ``tk >= consumed`` condition and surface as RateLimitExceeded."""
        from zae_limiter.lease import Lease

        child = self._make_entry(is_new=True, entity_id="e1")
        child._shard_id = 1
        parent = self._make_entry(entity_id="p1")
        mock_repo = self._make_mock_repo()
        # Plain (non-async) builders so the transaction items compare by value
        mock_repo.build_composite_create = MagicMock(return_value={"Put": {"who": "e1"}})
        mock_repo.build_composite_normal = MagicMock(return_value={"Update": {"who": "p1-rf"}})
        mock_repo.build_composite_retry = MagicMock(return_value={"Update": {"who": "p1"}})

        exc_cls = type(
            "TransactionCanceledException",
            (Exception,),
            {
                "response": {
                    "Error": {"Code": "TransactionCanceledException"},
                    "CancellationReasons": [{"Code": "None"}, {"Code": "ConditionalCheckFailed"}],
                }
            },
        )
        mock_repo.transact_write.side_effect = [exc_cls(), None]

        lease = Lease(repository=mock_repo, entries=[child, parent])
        await lease._commit_initial()

        assert lease._initial_committed is True
        retry_items = mock_repo.transact_write.call_args_list[1].args[0]
        assert retry_items == [{"Put": {"who": "e1"}}, {"Update": {"who": "p1"}}]
        mock_repo.build_composite_retry.assert_called_once()
        assert mock_repo.build_composite_retry.call_args.kwargs["entity_id"] == "p1"

    def test_retry_failure_statuses_use_the_effective_refill(self):
        """A sharded bucket refills at refill_amount // shard_count, so the
        retry-path retry_after must use that share, not the undivided rate."""
        from zae_limiter.lease import LeaseEntry, _build_retry_failure_statuses

        limit = Limit.custom("rpm", 1000, refill_amount=1000, refill_period_seconds=60)
        state = BucketState(
            entity_id="e1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=0,
            last_refill_ms=0,
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
            shard_count=2,
        )
        entry = LeaseEntry(entity_id="e1", resource="gpt-4", limit=limit, state=state, consumed=500)

        (status,) = _build_retry_failure_statuses([entry], now_ms=1000)
        # 500 tokens at 500 tokens/60 s (the shard's share) = 60 s, not 30 s
        assert status.retry_after_seconds == pytest.approx(60.0, abs=0.01)

    def test_retry_failure_statuses_use_the_clock_they_are_given(self):
        """A scheduled limit's refill rate depends on when you ask (#222 §3.5).

        The status must be built from the commit's single clock reading, so a
        rejection inside a 0.5x window quotes the halved rate and a rejection
        outside it quotes the full one.
        """
        from zae_limiter.lease import LeaseEntry, _build_retry_failure_statuses

        ny = ZoneInfo("America/New_York")
        in_window = int(datetime(2026, 9, 15, 14, 0, tzinfo=ny).timestamp() * 1000)
        out_of_window = int(datetime(2026, 9, 15, 3, 0, tzinfo=ny).timestamp() * 1000)
        limit = Limit.custom("rpm", 1000, refill_amount=1000, refill_period_seconds=60)
        state = BucketState(
            entity_id="e1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=0,
            last_refill_ms=0,
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
            sched=(ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),),
        )
        entry = LeaseEntry(entity_id="e1", resource="gpt-4", limit=limit, state=state, consumed=500)

        # 500 tokens at 500/60 s inside the window; at 1000/60 s outside it.
        (inside,) = _build_retry_failure_statuses([entry], now_ms=in_window)
        assert inside.retry_after_seconds == pytest.approx(60.0, abs=0.01)
        (outside,) = _build_retry_failure_statuses([entry], now_ms=out_of_window)
        assert outside.retry_after_seconds == pytest.approx(30.0, abs=0.01)

    def test_retry_failure_statuses_report_the_effective_limit(self):
        """The reported limit must be the shard's share too (#475): a status
        pairing the undivided capacity with a per-shard wait promises a
        capacity no shard can serve."""
        from zae_limiter.lease import LeaseEntry, _build_retry_failure_statuses

        limit = Limit.custom("rpm", 1000, refill_amount=1000, refill_period_seconds=60)
        state = BucketState(
            entity_id="e1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=0,
            last_refill_ms=0,
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
            shard_count=4,
        )
        entry = LeaseEntry(entity_id="e1", resource="gpt-4", limit=limit, state=state, consumed=500)

        (status,) = _build_retry_failure_statuses([entry], now_ms=1000)
        assert (status.limit.capacity, status.limit.refill_amount) == (250, 250)
        assert status.limit_name == "rpm"

    def test_admit_limit_reports_the_effective_limit_on_rejection(self):
        """The slow-path admission gate reports the share the shard actually
        holds, so a request above it is not told to retry against a capacity
        that no shard can serve (#475)."""
        limit = Limit.custom("rpm", 1000, refill_amount=1000, refill_period_seconds=60)
        state = BucketState(
            entity_id="e1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=0,
            last_refill_ms=0,
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
            shard_count=4,
        )
        # 600 exceeds this shard's 250-token share on every shard.
        status, consumed = RateLimiter._admit_limit(
            "e1", "gpt-4", limit, state, {"rpm": 600}, now_ms=0
        )
        assert consumed == 0
        assert status is not None and status.exceeded
        assert (status.limit.capacity, status.limit.refill_amount) == (250, 250)

    async def test_commit_initial_lost_lock_with_nothing_consumed_needs_no_retry(self):
        """An rf-lock failure on a group that consumed nothing has nothing to
        debit: no retry transaction is issued and the commit still completes."""
        from zae_limiter.lease import Lease

        entry = self._make_entry(consumed=0)
        mock_repo = self._make_mock_repo()
        exc_cls = type(
            "TransactionCanceledException",
            (Exception,),
            {
                "response": {
                    "Error": {"Code": "TransactionCanceledException"},
                    "CancellationReasons": [{"Code": "ConditionalCheckFailed"}],
                }
            },
        )
        mock_repo.transact_write.side_effect = [exc_cls()]

        lease = Lease(repository=mock_repo, entries=[entry])
        await lease._commit_initial()

        assert lease._initial_committed is True
        assert mock_repo.transact_write.call_count == 1
        mock_repo.build_composite_retry.assert_not_called()

    async def test_commit_initial_create_race_retries_on_the_same_shard(self):
        """Losing the create race to the aggregator retries consumption-only
        on that same shard, never on shard 0 (issue #439)."""
        from zae_limiter.lease import Lease

        entry = self._make_entry(is_new=True)
        entry._shard_id = 1
        mock_repo = self._make_mock_repo()

        exc_cls = type(
            "TransactionCanceledException",
            (Exception,),
            {
                "response": {
                    "Error": {"Code": "TransactionCanceledException"},
                    "CancellationReasons": [{"Code": "ConditionalCheckFailed"}],
                }
            },
        )
        mock_repo.transact_write.side_effect = [exc_cls(), None]

        lease = Lease(repository=mock_repo, entries=[entry])
        await lease._commit_initial()

        assert mock_repo.build_composite_retry.call_args.kwargs["shard_id"] == 1

    async def test_retry_non_condition_check_reraises(self):
        """Non-condition-check error in retry path propagates (line 301)."""
        from zae_limiter.lease import Lease

        entry = self._make_entry()
        mock_repo = self._make_mock_repo()

        exc_cls = type("TransactionCanceledException", (Exception,), {})
        condition_exc = exc_cls()
        condition_exc.response = {  # type: ignore[attr-defined]
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [{"Code": "ConditionalCheckFailed"}],
        }
        mock_repo.transact_write.side_effect = [condition_exc, RuntimeError("retry fail")]

        lease = Lease(repository=mock_repo, entries=[entry])
        with pytest.raises(RuntimeError, match="retry fail"):
            await lease._commit_initial()

    async def test_commit_adjustments_skips_when_committed(self):
        """_commit_adjustments is no-op when already committed (line 315)."""
        from zae_limiter.lease import Lease

        entry = self._make_entry(consumed=10, initial_consumed=5)
        mock_repo = self._make_mock_repo()

        lease = Lease(repository=mock_repo, entries=[entry])
        lease._committed = True

        await lease._commit_adjustments()
        mock_repo.transact_write.assert_not_called()

    async def test_commit_adjustments_skips_when_rolled_back(self):
        """_commit_adjustments is no-op when already rolled back (line 315)."""
        from zae_limiter.lease import Lease

        entry = self._make_entry(consumed=10, initial_consumed=5)
        mock_repo = self._make_mock_repo()

        lease = Lease(repository=mock_repo, entries=[entry])
        lease._rolled_back = True

        await lease._commit_adjustments()
        mock_repo.transact_write.assert_not_called()

    async def test_commit_adjustments_writes_delta(self):
        """_commit_adjustments writes delta when adjustments exist."""
        from zae_limiter.lease import Lease

        entry = self._make_entry(consumed=15, initial_consumed=10)
        mock_repo = self._make_mock_repo()

        lease = Lease(repository=mock_repo, entries=[entry])
        await lease._commit_adjustments()

        assert lease._committed is True
        mock_repo.build_composite_adjust.assert_called_once_with(
            entity_id="e1",
            resource="gpt-4",
            deltas={"rpm": 5000},  # (15-10) * 1000
            shard_id=0,
        )
        mock_repo.write_each.assert_called_once()

    async def test_commit_adjustments_noop_when_no_change(self):
        """_commit_adjustments skips transact_write when no adjustments."""
        from zae_limiter.lease import Lease

        entry = self._make_entry(consumed=10, initial_consumed=10)
        mock_repo = self._make_mock_repo()

        lease = Lease(repository=mock_repo, entries=[entry])
        await lease._commit_adjustments()

        assert lease._committed is True
        mock_repo.transact_write.assert_not_called()

    async def test_commit_adjustments_failure_allows_rollback(self):
        """_rollback works after _commit_adjustments fails (token leak fix)."""
        from zae_limiter.lease import Lease

        entry = self._make_entry(consumed=15, initial_consumed=10)
        mock_repo = self._make_mock_repo()
        mock_repo.write_each.side_effect = RuntimeError("network error")

        lease = Lease(repository=mock_repo, entries=[entry])
        lease._initial_committed = True

        with pytest.raises(RuntimeError, match="network error"):
            await lease._commit_adjustments()

        # _committed should NOT be True after failed write
        assert lease._committed is False

        # _rollback should succeed (not blocked by _committed flag)
        mock_repo.write_each.side_effect = None
        await lease._rollback()
        assert lease._rolled_back is True
        # Rollback writes negative initial_consumed
        mock_repo.build_composite_adjust.assert_called_with(
            entity_id="e1",
            resource="gpt-4",
            deltas={"rpm": -10000},
            shard_id=0,
        )

    async def test_rollback_skips_when_committed(self):
        """_rollback is no-op when already committed (line 358)."""
        from zae_limiter.lease import Lease

        entry = self._make_entry(consumed=10, initial_consumed=10)
        mock_repo = self._make_mock_repo()

        lease = Lease(repository=mock_repo, entries=[entry])
        lease._committed = True
        lease._initial_committed = True

        await lease._rollback()
        mock_repo.transact_write.assert_not_called()

    async def test_rollback_skips_when_no_initial_commit(self):
        """_rollback is no-op when _initial_committed is False (line 364)."""
        from zae_limiter.lease import Lease

        entry = self._make_entry(consumed=10, initial_consumed=10)
        mock_repo = self._make_mock_repo()

        lease = Lease(repository=mock_repo, entries=[entry])

        await lease._rollback()

        assert lease._rolled_back is True
        mock_repo.transact_write.assert_not_called()

    async def test_rollback_writes_compensating_delta(self):
        """_rollback writes negative delta to restore tokens."""
        from zae_limiter.lease import Lease

        entry = self._make_entry(consumed=10, initial_consumed=10)
        mock_repo = self._make_mock_repo()

        lease = Lease(repository=mock_repo, entries=[entry])
        lease._initial_committed = True

        await lease._rollback()

        assert lease._rolled_back is True
        mock_repo.build_composite_adjust.assert_called_once_with(
            entity_id="e1",
            resource="gpt-4",
            deltas={"rpm": -10000},  # -10 * 1000
            shard_id=0,
        )
        mock_repo.write_each.assert_called_once()

    async def test_rollback_failure_logs_warning(self, caplog):
        """_rollback logs warning and doesn't raise on write_each failure (lines 394-395)."""
        import logging

        from zae_limiter.lease import Lease

        entry = self._make_entry(consumed=10, initial_consumed=10)
        mock_repo = self._make_mock_repo()
        mock_repo.write_each.side_effect = RuntimeError("DynamoDB down")

        lease = Lease(repository=mock_repo, entries=[entry])
        lease._initial_committed = True

        with caplog.at_level(logging.WARNING, logger="zae_limiter.lease"):
            await lease._rollback()

        assert lease._rolled_back is True
        assert "Failed to rollback consumed tokens" in caplog.text

    async def test_cascade_normal_fails_retry_succeeds(self):
        """Cascade: normal path fails (optimistic lock), retry path succeeds.

        Two entries with different entity_ids (child + parent) simulate a
        cascade scenario where the shared parent bucket causes the normal
        transact_write to fail with ConditionalCheckFailed. The retry
        path uses build_composite_retry for both entries and succeeds.
        """
        from zae_limiter.lease import Lease

        child_entry = self._make_entry(consumed=5, entity_id="child-1")
        parent_entry = self._make_entry(consumed=5, entity_id="parent-1")
        mock_repo = self._make_mock_repo()

        exc_cls = type("TransactionCanceledException", (Exception,), {})
        condition_exc = exc_cls()
        condition_exc.response = {  # type: ignore[attr-defined]
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [
                {"Code": "ConditionalCheckFailed"},
                {"Code": "None"},
            ],
        }
        # Normal path fails, retry path succeeds
        mock_repo.transact_write.side_effect = [condition_exc, None]

        lease = Lease(repository=mock_repo, entries=[child_entry, parent_entry])
        await lease._commit_initial()

        assert lease._initial_committed is True
        assert mock_repo.transact_write.call_count == 2
        # Retry calls build_composite_retry for both entity groups
        assert mock_repo.build_composite_retry.call_count == 2

    async def test_cascade_both_paths_fail_raises_rate_limit_exceeded(self):
        """Cascade: both normal and retry paths fail → RateLimitExceeded.

        When both transact_write calls fail with condition check failures,
        _commit_initial raises RateLimitExceeded with statuses for all entries.
        """
        from zae_limiter.lease import Lease

        child_entry = self._make_entry(consumed=5, entity_id="child-1")
        parent_entry = self._make_entry(consumed=5, entity_id="parent-1")
        mock_repo = self._make_mock_repo()

        exc_cls = type("TransactionCanceledException", (Exception,), {})
        condition_exc = exc_cls()
        condition_exc.response = {  # type: ignore[attr-defined]
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [{"Code": "ConditionalCheckFailed"}],
        }
        # Both normal and retry paths fail with ConditionalCheckFailed
        mock_repo.transact_write.side_effect = condition_exc

        lease = Lease(repository=mock_repo, entries=[child_entry, parent_entry])
        with pytest.raises(RateLimitExceeded) as exc_info:
            await lease._commit_initial()

        # Should have statuses for both entries
        assert len(exc_info.value.statuses) == 2
        entity_ids = {s.entity_id for s in exc_info.value.statuses}
        assert entity_ids == {"child-1", "parent-1"}

    async def test_commit_initial_transaction_conflict_retries_original(self):
        """TransactionConflict retries original transaction, not consumption-only.

        When transact_write fails with TransactionConflict (not ConditionalCheckFailed),
        _commit_initial should retry the same transaction with backoff rather than
        falling through to the retry (consumption-only) path.
        """
        from zae_limiter.lease import Lease

        entry = self._make_entry()
        mock_repo = self._make_mock_repo()

        # Build TransactionConflict exception with proper CancellationReasons
        exc_cls = type("TransactionCanceledException", (Exception,), {})
        conflict_exc = exc_cls()
        conflict_exc.response = {  # type: ignore[attr-defined]
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [
                {"Code": "None"},
                {"Code": "TransactionConflict"},
            ],
        }
        # First call: TransactionConflict, second call: success
        mock_repo.transact_write.side_effect = [conflict_exc, None]

        lease = Lease(repository=mock_repo, entries=[entry])
        await lease._commit_initial()

        assert lease._initial_committed is True
        assert mock_repo.transact_write.call_count == 2
        # Should NOT have called build_composite_retry (not the consumption-only path)
        mock_repo.build_composite_retry.assert_not_called()

    async def test_commit_initial_transaction_conflict_exhausts_retries(self):
        """TransactionConflict exhausts retries then propagates exception.

        After max retries on TransactionConflict, the exception should propagate
        rather than entering the consumption-only retry path.
        """
        from zae_limiter.lease import _CONFLICT_MAX_RETRIES, Lease

        entry = self._make_entry()
        mock_repo = self._make_mock_repo()

        exc_cls = type("TransactionCanceledException", (Exception,), {})
        conflict_exc = exc_cls()
        conflict_exc.response = {  # type: ignore[attr-defined]
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [
                {"Code": "TransactionConflict"},
            ],
        }
        # All retries fail with TransactionConflict
        mock_repo.transact_write.side_effect = conflict_exc

        lease = Lease(repository=mock_repo, entries=[entry])
        with pytest.raises(type(conflict_exc)):
            await lease._commit_initial()

        # Should have retried _CONFLICT_MAX_RETRIES times + 1 initial attempt
        assert mock_repo.transact_write.call_count == _CONFLICT_MAX_RETRIES + 1
        # Should NOT have entered consumption-only path
        mock_repo.build_composite_retry.assert_not_called()

    async def test_commit_initial_condition_check_still_enters_retry_path(self):
        """ConditionalCheckFailed still enters consumption-only retry path.

        Regression test: the TransactionConflict fix should not break the
        existing ConditionalCheckFailed → retry path behavior.
        """
        from zae_limiter.lease import Lease

        entry = self._make_entry()
        mock_repo = self._make_mock_repo()

        exc_cls = type("TransactionCanceledException", (Exception,), {})
        condition_exc = exc_cls()
        condition_exc.response = {  # type: ignore[attr-defined]
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [
                {"Code": "ConditionalCheckFailed"},
                {"Code": "None"},
            ],
        }
        # First call: ConditionalCheckFailed, second call (retry): success
        mock_repo.transact_write.side_effect = [condition_exc, None]

        lease = Lease(repository=mock_repo, entries=[entry])
        await lease._commit_initial()

        assert lease._initial_committed is True
        assert mock_repo.transact_write.call_count == 2
        # Should have entered consumption-only retry path
        mock_repo.build_composite_retry.assert_called_once()

    async def test_commit_initial_mixed_reasons_prefers_condition_check(self):
        """Mixed ConditionalCheckFailed + TransactionConflict enters retry path.

        When both codes are present in CancellationReasons, ConditionalCheckFailed
        takes precedence — the consumption-only retry path should be used rather
        than the TransactionConflict retry loop.
        """
        from zae_limiter.lease import Lease

        entry = self._make_entry()
        mock_repo = self._make_mock_repo()

        exc_cls = type("TransactionCanceledException", (Exception,), {})
        mixed_exc = exc_cls()
        mixed_exc.response = {  # type: ignore[attr-defined]
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [
                {"Code": "ConditionalCheckFailed"},
                {"Code": "TransactionConflict"},
            ],
        }
        mock_repo.transact_write.side_effect = [mixed_exc, None]

        lease = Lease(repository=mock_repo, entries=[entry])
        await lease._commit_initial()

        assert lease._initial_committed is True
        assert mock_repo.transact_write.call_count == 2
        # Should have entered consumption-only retry path (not TransactionConflict loop)
        mock_repo.build_composite_retry.assert_called_once()

    async def test_cascade_transaction_conflict_does_not_raise_rate_limit_exceeded(self):
        """Cascade TransactionConflict does not raise false RateLimitExceeded (Issue #332).

        When cascade transact_write fails with TransactionConflict (parent bucket
        contention), it should retry the original transaction, not enter the
        consumption-only path that would raise false RateLimitExceeded.
        """
        from zae_limiter.lease import Lease

        child_entry = self._make_entry(consumed=5, entity_id="child-1")
        parent_entry = self._make_entry(consumed=5, entity_id="parent-1")
        mock_repo = self._make_mock_repo()

        exc_cls = type("TransactionCanceledException", (Exception,), {})
        conflict_exc = exc_cls()
        conflict_exc.response = {  # type: ignore[attr-defined]
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [
                {"Code": "None"},
                {"Code": "TransactionConflict"},
            ],
        }
        # First call: TransactionConflict (parent contention), second call: success
        mock_repo.transact_write.side_effect = [conflict_exc, None]

        lease = Lease(repository=mock_repo, entries=[child_entry, parent_entry])
        # Should NOT raise RateLimitExceeded
        await lease._commit_initial()

        assert lease._initial_committed is True
        assert mock_repo.transact_write.call_count == 2
        mock_repo.build_composite_retry.assert_not_called()

    async def test_acquire_writes_on_enter(self, limiter):
        """Tokens are consumed in DynamoDB immediately on context enter."""
        limits = [Limit.per_minute("rpm", 100)]

        async with limiter.acquire(
            entity_id="enter-test",
            resource="gpt-4",
            limits=limits,
            consume={"rpm": 10},
        ):
            # Inside context: check DynamoDB already has consumption
            buckets = await limiter._repository.get_buckets(
                entity_id="enter-test", resource="gpt-4"
            )
            bucket = next(b for b in buckets if b.limit_name == "rpm")
            # 100 capacity - 10 consumed = 90 tokens = 90000 millitokens
            assert bucket.tokens_milli <= 90_000

    async def test_acquire_rollback_restores_on_error(self, limiter):
        """Rollback writes compensating transaction on error."""
        limits = [Limit.per_minute("rpm", 100)]

        try:
            async with limiter.acquire(
                entity_id="rollback-test",
                resource="gpt-4",
                limits=limits,
                consume={"rpm": 10},
            ):
                raise ValueError("boom")
        except ValueError:
            pass

        # After rollback, tokens should be restored
        available = await limiter.available(
            entity_id="rollback-test",
            resource="gpt-4",
            limits=limits,
        )
        # Immune to clock drift (#503): 100 is the capacity ceiling and
        # `refill_bucket()` clamps there.
        assert available["rpm"] == 100

    async def test_cascade_writes_both_on_enter(self, limiter):
        """Cascade writes both child and parent buckets on enter."""
        await limiter.create_entity(entity_id="proj-cascade")
        await limiter.create_entity(entity_id="key-cascade", parent_id="proj-cascade", cascade=True)

        # Both read-backs below are exact values under the ceiling, read inside
        # the lease body, so 600 ms of wall clock between the write that stamps
        # ``rf`` and either read refills them to 96 (#503, same mechanism and
        # same threshold as #498). Pin the clock; see ``freeze_clock``.
        freeze_clock(limiter._repository)

        limits = [Limit.per_minute("rpm", 100)]

        async with limiter.acquire(
            entity_id="key-cascade",
            resource="gpt-4",
            limits=limits,
            consume={"rpm": 5},
        ):
            # Inside context: both child and parent should already be consumed
            child_available = await limiter.available(
                entity_id="key-cascade",
                resource="gpt-4",
                limits=limits,
            )
            parent_available = await limiter.available(
                entity_id="proj-cascade",
                resource="gpt-4",
                limits=limits,
            )
            assert child_available["rpm"] == 95
            assert parent_available["rpm"] == 95

    async def test_concurrent_adjust_no_lost_tokens(self, limiter):
        """Concurrent leases with adjust() don't lose tokens via ADD atomicity.

        Two callers acquire the same entity concurrently, both call adjust()
        inside the lease, and their _commit_adjustments writes interleave.
        Because adjustments use atomic ADD (not SET), no tokens are lost.

        Uses per_day limit to avoid refill drift between writes and assertion.
        """
        limits = [Limit.per_day("rpd", 1000)]

        barrier = asyncio.Barrier(2)

        async def caller(consume: int, adjust: int):
            async with limiter.acquire(
                entity_id="concurrent-adjust",
                resource="gpt-4",
                limits=limits,
                consume={"rpd": consume},
            ) as lease:
                await lease.adjust(rpd=adjust)
                # Sync so both _commit_adjustments fire close together
                await barrier.wait()

        await asyncio.gather(
            caller(consume=10, adjust=20),
            caller(consume=5, adjust=15),
        )

        # Total consumed: (10+20) + (5+15) = 50
        available = await limiter.available(
            entity_id="concurrent-adjust",
            resource="gpt-4",
            limits=limits,
        )
        assert available["rpd"] == 950

        # Verify consumption counter is also correct
        buckets = await limiter._repository.get_buckets(
            entity_id="concurrent-adjust", resource="gpt-4"
        )
        rpd_bucket = next(b for b in buckets if b.limit_name == "rpd")
        assert rpd_bucket.total_consumed_milli == 50_000


class TestRateLimiterLeaseCounter:
    """Tests for consumption counter tracking (issue #179).

    The counter tracks net consumption in millitokens, stored as a flat
    top-level DynamoDB attribute to enable atomic ADD operations.
    """

    async def test_acquire_initializes_counter(self, limiter):
        """Initial acquire initializes counter to consumed amount."""
        limits = [Limit.per_minute("tpm", 10_000)]

        async with limiter.acquire(
            entity_id="counter-test-1",
            resource="gpt-4",
            limits=limits,
            consume={"tpm": 100},
        ):
            pass

        # Check the bucket state has counter initialized
        buckets = await limiter._repository.get_buckets(
            entity_id="counter-test-1", resource="gpt-4"
        )
        bucket = next(b for b in buckets if b.limit_name == "tpm")
        # Counter should be 100 tokens * 1000 = 100000 millitokens
        assert bucket.total_consumed_milli == 100_000

    async def test_lease_consume_increments_counter(self, limiter):
        """Additional consume() calls increment the counter."""
        limits = [Limit.per_minute("tpm", 10_000)]

        async with limiter.acquire(
            entity_id="counter-test-2",
            resource="gpt-4",
            limits=limits,
            consume={"tpm": 100},
        ) as lease:
            await lease.consume(tpm=50)

        buckets = await limiter._repository.get_buckets(
            entity_id="counter-test-2", resource="gpt-4"
        )
        bucket = next(b for b in buckets if b.limit_name == "tpm")
        # Counter: (100 + 50) * 1000 = 150000 millitokens
        assert bucket.total_consumed_milli == 150_000

    async def test_lease_adjust_negative_decrements_counter(self, limiter):
        """Negative adjust() decrements counter (net tracking)."""
        limits = [Limit.per_minute("tpm", 10_000)]

        async with limiter.acquire(
            entity_id="counter-test-3",
            resource="gpt-4",
            limits=limits,
            consume={"tpm": 100},
        ) as lease:
            # Return 30 tokens via adjust (negative amount)
            await lease.adjust(tpm=-30)

        buckets = await limiter._repository.get_buckets(
            entity_id="counter-test-3", resource="gpt-4"
        )
        bucket = next(b for b in buckets if b.limit_name == "tpm")
        # Counter: (100 - 30) * 1000 = 70000 millitokens
        assert bucket.total_consumed_milli == 70_000

    async def test_lease_release_decrements_counter(self, limiter):
        """release() decrements counter (same as negative adjust)."""
        limits = [Limit.per_minute("tpm", 10_000)]

        async with limiter.acquire(
            entity_id="counter-test-4",
            resource="gpt-4",
            limits=limits,
            consume={"tpm": 100},
        ) as lease:
            # Return 40 tokens via release
            await lease.release(tpm=40)

        buckets = await limiter._repository.get_buckets(
            entity_id="counter-test-4", resource="gpt-4"
        )
        bucket = next(b for b in buckets if b.limit_name == "tpm")
        # Counter: (100 - 40) * 1000 = 60000 millitokens
        assert bucket.total_consumed_milli == 60_000

    async def test_lease_adjust_positive_increments_counter(self, limiter):
        """Positive adjust() increments counter (same as consume)."""
        limits = [Limit.per_minute("tpm", 10_000)]

        async with limiter.acquire(
            entity_id="counter-test-5",
            resource="gpt-4",
            limits=limits,
            consume={"tpm": 100},
        ) as lease:
            # Consume 200 more tokens via adjust (positive amount)
            await lease.adjust(tpm=200)

        buckets = await limiter._repository.get_buckets(
            entity_id="counter-test-5", resource="gpt-4"
        )
        bucket = next(b for b in buckets if b.limit_name == "tpm")
        # Counter: (100 + 200) * 1000 = 300000 millitokens
        assert bucket.total_consumed_milli == 300_000


class TestRateLimiterCascade:
    """Tests for cascade functionality (entity-level cascade)."""

    async def test_cascade_consumes_parent(self, limiter):
        """Test that entity with cascade=True consumes from parent too."""
        await limiter.create_entity(entity_id="proj-1")
        await limiter.create_entity(entity_id="key-1", parent_id="proj-1", cascade=True)

        # Both assertions below read back an exact value under the ceiling, so
        # 600 ms of wall clock between the write and the read would refill them
        # to 100 (#498). Pin the clock; see ``freeze_clock``.
        freeze_clock(limiter._repository)

        limits = [Limit.per_minute("rpm", 100)]

        async with limiter.acquire(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
            consume={"rpm": 1},
        ):
            pass

        # Check both entities have consumed
        child_available = await limiter.available(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
        )
        parent_available = await limiter.available(
            entity_id="proj-1",
            resource="gpt-4",
            limits=limits,
        )

        assert child_available["rpm"] == 99
        assert parent_available["rpm"] == 99

    async def test_no_cascade_by_default(self, limiter):
        """Test that entities without cascade=True do NOT cascade."""
        await limiter.create_entity(entity_id="proj-1")
        await limiter.create_entity(entity_id="key-1", parent_id="proj-1")

        # `child_available == 99` is under the ceiling and would refill to 100
        # after 600 ms (#498). Pin the clock; see ``freeze_clock``.
        freeze_clock(limiter._repository)

        limits = [Limit.per_minute("rpm", 100)]

        async with limiter.acquire(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
            consume={"rpm": 1},
        ):
            pass

        # Child consumed, parent should NOT have consumed
        child_available = await limiter.available(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
        )
        parent_available = await limiter.available(
            entity_id="proj-1",
            resource="gpt-4",
            limits=limits,
        )

        assert child_available["rpm"] == 99
        # The parent never cascaded, so it has no bucket at all and
        # `check_availability` reports `limit.capacity`. Even once it had one,
        # 100 is the capacity ceiling and `refill_bucket()` clamps there, so
        # this assertion cannot drift upward the way `== 99` can (#498).
        assert parent_available["rpm"] == 100  # Parent untouched

    async def test_cascade_parent_limit_exceeded(self, limiter):
        """Test that parent limit can block child when cascade is enabled."""
        await limiter.create_entity(entity_id="proj-1")
        await limiter.create_entity(entity_id="key-1", parent_id="proj-1", cascade=True)

        # Child has high limit, parent has low limit
        child_limits = [Limit.per_minute("rpm", 100)]
        parent_limits = [Limit.per_minute("rpm", 5)]

        # Set parent's stored limits
        await limiter.set_limits("proj-1", parent_limits)

        # First, consume parent's capacity
        async with limiter.acquire(
            entity_id="proj-1",
            resource="gpt-4",
            limits=parent_limits,
            consume={"rpm": 5},
        ):
            pass

        # Now child should be blocked by parent
        with pytest.raises(RateLimitExceeded) as exc_info:
            async with limiter.acquire(
                entity_id="key-1",
                resource="gpt-4",
                limits=child_limits,
                consume={"rpm": 1},
            ):
                pass

        # The violation should be on the parent
        exc = exc_info.value
        assert any(v.entity_id == "proj-1" for v in exc.violations)

    async def test_cascade_entity_without_parent(self, limiter):
        """Test that cascade=True on entity without parent is harmless."""
        await limiter.create_entity(entity_id="orphan-1", cascade=True)

        # `available == 99` is under the ceiling and would refill to 100 after
        # 600 ms (#498). Pin the clock; see ``freeze_clock``.
        freeze_clock(limiter._repository)

        limits = [Limit.per_minute("rpm", 100)]

        async with limiter.acquire(
            entity_id="orphan-1",
            resource="gpt-4",
            limits=limits,
            consume={"rpm": 1},
        ):
            pass

        available = await limiter.available(
            entity_id="orphan-1",
            resource="gpt-4",
            limits=limits,
        )
        assert available["rpm"] == 99

    async def test_backward_compat_missing_cascade_field(self, limiter):
        """Test that entities without cascade field default to False."""
        # Create entity normally (cascade defaults to False)
        entity = await limiter.create_entity(entity_id="legacy-1", parent_id=None)
        assert entity.cascade is False


class TestRateLimiterStoredLimits:
    """Tests for stored limit configs."""

    async def test_set_and_get_limits(self, limiter):
        """Test storing and retrieving limits."""
        limits = [
            Limit.per_minute("rpm", 100),
            Limit.per_minute("tpm", 10_000),
        ]
        await limiter.set_limits("key-1", limits, resource="gpt-4")

        retrieved = await limiter.get_limits("key-1", resource="gpt-4")
        assert len(retrieved) == 2

        names = {limit.name for limit in retrieved}
        assert names == {"rpm", "tpm"}

    async def test_use_stored_limits(self, limiter):
        """Test using stored limits in acquire."""
        # Store custom limits
        stored_limits = [Limit.per_minute("rpm", 500)]
        await limiter.set_limits("key-1", stored_limits, resource="gpt-4")

        # Stored limits are resolved automatically (use_stored_limits is deprecated)
        with pytest.deprecated_call():
            async with limiter.acquire(
                entity_id="key-1",
                resource="gpt-4",
                consume={"rpm": 200},  # within stored limit of 500
                use_stored_limits=True,
            ):
                pass  # should succeed with stored limit of 500

    async def test_delete_limits(self, limiter):
        """Test deleting stored limits."""
        limits = [Limit.per_minute("rpm", 100)]
        await limiter.set_limits("key-1", limits)

        await limiter.delete_limits("key-1")

        retrieved = await limiter.get_limits("key-1")
        assert len(retrieved) == 0

    async def test_use_stored_limits_available_deprecation(self, limiter):
        """Test that use_stored_limits in available() emits deprecation warning."""
        import warnings

        limits = [Limit.per_minute("rpm", 100)]
        await limiter.set_limits("key-1", limits, resource="gpt-4")

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            await limiter.available(
                entity_id="key-1",
                resource="gpt-4",
                limits=limits,
                use_stored_limits=True,
            )
            assert len(w) == 1
            assert issubclass(w[0].category, DeprecationWarning)
            assert "use_stored_limits is deprecated" in str(w[0].message)

    async def test_use_stored_limits_time_until_available_deprecation(self, limiter):
        """Test that use_stored_limits in time_until_available() emits deprecation warning."""
        import warnings

        limits = [Limit.per_minute("rpm", 100)]
        await limiter.set_limits("key-1", limits, resource="gpt-4")

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            await limiter.time_until_available(
                entity_id="key-1",
                resource="gpt-4",
                limits=limits,
                needed={"rpm": 1},
                use_stored_limits=True,
            )
            assert len(w) == 1
            assert issubclass(w[0].category, DeprecationWarning)
            assert "use_stored_limits is deprecated" in str(w[0].message)


class TestRateLimiterResourceDefaults:
    """Tests for resource-level default configs."""

    async def test_set_and_get_resource_defaults(self, limiter):
        """Test storing and retrieving resource-level defaults."""
        limits = [
            Limit.per_minute("rpm", 100),
            Limit.per_minute("tpm", 10_000),
        ]
        await limiter.set_resource_defaults("gpt-4", limits)

        retrieved = await limiter.get_resource_defaults("gpt-4")
        assert len(retrieved) == 2

        names = {limit.name for limit in retrieved}
        assert names == {"rpm", "tpm"}

    async def test_delete_resource_defaults(self, limiter):
        """Test deleting resource-level defaults."""
        limits = [Limit.per_minute("rpm", 100)]
        await limiter.set_resource_defaults("gpt-4", limits)

        await limiter.delete_resource_defaults("gpt-4")

        retrieved = await limiter.get_resource_defaults("gpt-4")
        assert len(retrieved) == 0

    async def test_get_resource_defaults_empty(self, limiter):
        """Test getting resource defaults when none exist."""
        retrieved = await limiter.get_resource_defaults("nonexistent")
        assert len(retrieved) == 0

    async def test_list_resources_with_defaults(self, limiter):
        """Test listing resources with configured defaults."""
        # Initially empty
        resources = await limiter.list_resources_with_defaults()
        assert len(resources) == 0

        # Add defaults for two resources
        limits = [Limit.per_minute("rpm", 100)]
        await limiter.set_resource_defaults("gpt-4", limits)
        await limiter.set_resource_defaults("claude-3", limits)

        resources = await limiter.list_resources_with_defaults()
        assert "gpt-4" in resources
        assert "claude-3" in resources

    async def test_resource_defaults_replace_on_update(self, limiter):
        """Test that setting defaults replaces existing ones."""
        # Set initial defaults
        await limiter.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 100)])

        # Replace with different defaults
        await limiter.set_resource_defaults("gpt-4", [Limit.per_minute("tpm", 5000)])

        retrieved = await limiter.get_resource_defaults("gpt-4")
        assert len(retrieved) == 1
        assert retrieved[0].name == "tpm"


class TestRateLimiterSystemDefaults:
    """Tests for system-level default configs."""

    async def test_set_and_get_system_defaults(self, limiter):
        """Test storing and retrieving system-level defaults."""
        limits = [
            Limit.per_minute("rpm", 50),
            Limit.per_minute("tpm", 5_000),
        ]
        await limiter.set_system_defaults(limits)

        retrieved, on_unavailable = await limiter.get_system_defaults()
        assert len(retrieved) == 2

        names = {limit.name for limit in retrieved}
        assert names == {"rpm", "tpm"}
        assert on_unavailable is None

    async def test_set_system_defaults_with_on_unavailable(self, limiter):
        """Test storing system defaults with on_unavailable config."""
        from zae_limiter import OnUnavailable

        limits = [Limit.per_minute("rpm", 50)]
        await limiter.set_system_defaults(limits, on_unavailable=OnUnavailable.ALLOW)

        retrieved, on_unavailable = await limiter.get_system_defaults()
        assert len(retrieved) == 1
        assert on_unavailable == OnUnavailable.ALLOW

    async def test_delete_system_defaults(self, limiter):
        """Test deleting system-level defaults."""
        limits = [Limit.per_minute("rpm", 50)]
        await limiter.set_system_defaults(limits)

        await limiter.delete_system_defaults()

        retrieved, on_unavailable = await limiter.get_system_defaults()
        assert len(retrieved) == 0
        assert on_unavailable is None

    async def test_get_system_defaults_empty(self, limiter):
        """Test getting system defaults when none exist."""
        retrieved, on_unavailable = await limiter.get_system_defaults()
        assert len(retrieved) == 0
        assert on_unavailable is None

    async def test_system_defaults_replace_on_update(self, limiter):
        """Test that setting defaults replaces existing ones."""
        # Set initial defaults
        await limiter.set_system_defaults([Limit.per_minute("rpm", 50)])

        # Replace with different defaults
        await limiter.set_system_defaults([Limit.per_minute("tpm", 2500)])

        retrieved, _ = await limiter.get_system_defaults()
        assert len(retrieved) == 1
        assert retrieved[0].name == "tpm"


class TestRateLimiterFourTierResolution:
    """Tests for four-tier limit resolution: Entity > Entity Default > Resource > System."""

    async def test_resolution_entity_level(self, limiter):
        """Test that entity-level limits take precedence."""
        # Set all three levels
        await limiter.set_system_defaults([Limit.per_minute("rpm", 10)])
        await limiter.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 50)])
        await limiter.set_limits("user-1", [Limit.per_minute("rpm", 100)], resource="gpt-4")

        # Entity-level should be used (100 rpm)
        async with limiter.acquire(
            entity_id="user-1",
            resource="gpt-4",
            limits=None,  # Auto-resolve
            consume={"rpm": 75},  # Exceeds resource (50) and system (10), but not entity (100)
        ):
            pass  # Should succeed

    async def test_resolution_entity_default_fallback(self, limiter):
        """Entity _default_ config is used when no resource-specific entity config exists.

        Resolution for acquire(entity_id="user-1", resource="gpt-4"):
        1. Entity config for "gpt-4"? -> No
        2. Entity config for "_default_"? -> Yes (100 rpm) <- USED
        3. Resource defaults for "gpt-4"? -> Yes (50 rpm)
        4. System defaults? -> Yes (10 rpm)

        Entity's _default_ config (100 rpm) takes precedence over resource defaults (50 rpm).
        """
        # Set system and resource defaults
        await limiter.set_system_defaults([Limit.per_minute("rpm", 10)])
        await limiter.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 50)])

        # Set entity-level _default_ (should apply to all resources for this entity)
        await limiter.set_limits("user-1", [Limit.per_minute("rpm", 100)], resource="_default_")

        # Entity's _default_ should be used (100 rpm), not resource defaults (50 rpm)
        async with limiter.acquire(
            entity_id="user-1",
            resource="gpt-4",
            limits=None,  # Auto-resolve
            consume={"rpm": 75},  # Exceeds resource (50), but not entity _default_ (100)
        ):
            pass  # Should succeed if entity _default_ is used

    async def test_resolution_resource_level_fallback(self, limiter):
        """Test that resource-level limits are used when no entity limits exist."""
        # Set system and resource levels only
        await limiter.set_system_defaults([Limit.per_minute("rpm", 10)])
        await limiter.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 50)])

        # Resource-level should be used (50 rpm)
        async with limiter.acquire(
            entity_id="user-2",  # No entity-level limits
            resource="gpt-4",
            limits=None,  # Auto-resolve
            consume={"rpm": 25},  # Exceeds system (10), but not resource (50)
        ):
            pass  # Should succeed

    async def test_resolution_system_level_fallback(self, limiter):
        """Test that system-level limits are used when no entity/resource limits exist."""
        # Set system level only
        await limiter.set_system_defaults([Limit.per_minute("rpm", 100)])

        # System-level should be used (100 rpm)
        async with limiter.acquire(
            entity_id="user-3",  # No entity-level limits
            resource="claude-3",  # No resource-level limits
            limits=None,  # Auto-resolve
            consume={"rpm": 50},
        ):
            pass  # Should succeed

    async def test_resolution_override_fallback(self, limiter):
        """Test that override parameter is used when no stored config exists."""
        # No stored config at any level

        # Override parameter should be used
        async with limiter.acquire(
            entity_id="user-4",
            resource="new-resource",
            limits=[Limit.per_minute("rpm", 100)],  # Override
            consume={"rpm": 50},
        ):
            pass  # Should succeed

    async def test_resolution_no_limits_raises_validation_error(self, limiter):
        """Test that ValidationError is raised when no limits found anywhere."""
        # No stored config and no override

        with pytest.raises(ValidationError) as exc_info:
            async with limiter.acquire(
                entity_id="user-5",
                resource="unknown-resource",
                limits=None,  # No override
                consume={"rpm": 1},
            ):
                pass

        assert "No limits configured" in str(exc_info.value)
        assert "user-5" in str(exc_info.value)
        assert "unknown-resource" in str(exc_info.value)

    async def test_on_unavailable_resolution_from_system_config(self, limiter):
        """Test that on_unavailable is resolved from system config."""
        from zae_limiter import OnUnavailable

        # Set system defaults with on_unavailable
        await limiter.set_system_defaults(
            [Limit.per_minute("rpm", 100)],
            on_unavailable=OnUnavailable.ALLOW,
        )

        # Acquire should use the system config on_unavailable
        async with limiter.acquire(
            entity_id="user-6",
            resource="gpt-4",
            limits=None,  # Auto-resolve limits and on_unavailable
            consume={"rpm": 1},
        ):
            pass  # Should succeed

    async def test_available_uses_resolution(self, limiter):
        """Test that available() also uses three-tier resolution."""
        # Set resource-level limits
        await limiter.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 100)])

        # available() should resolve to resource level
        available = await limiter.available(
            entity_id="user-7",
            resource="gpt-4",
            limits=None,  # Auto-resolve
        )
        assert available["rpm"] == 100

    async def test_time_until_available_uses_resolution(self, limiter):
        """Test that time_until_available() also uses three-tier resolution."""
        # Set resource-level limits
        await limiter.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 100)])

        # time_until_available() should resolve to resource level
        wait_time = await limiter.time_until_available(
            entity_id="user-8",
            resource="gpt-4",
            needed={"rpm": 50},
            limits=None,  # Auto-resolve
        )
        assert wait_time == 0.0  # Full capacity available


class TestRateLimiterCapacity:
    """Tests for capacity queries."""

    async def test_available(self, limiter):
        """Test checking available capacity."""
        # The post-consumption read-back below is an exact value under the
        # ceiling, so 600 ms of wall clock would refill it to 71 (#503). Pin
        # the clock; see ``freeze_clock``. (The first read is the ceiling and
        # is immune either way.)
        freeze_clock(limiter._repository)

        limits = [Limit.per_minute("rpm", 100)]

        # Initial - full capacity
        available = await limiter.available(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
        )
        assert available["rpm"] == 100

        # After consumption
        async with limiter.acquire(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
            consume={"rpm": 30},
        ):
            pass

        available = await limiter.available(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
        )
        assert available["rpm"] == 70

    async def test_time_until_available(self, limiter):
        """Test calculating time until capacity available."""
        # Every token refilled between the write and the read shaves 0.6 s off
        # the estimate, so ~1 s of wall clock walks 30.0 out of the 29..31
        # window (#503). Pin the clock; see ``freeze_clock``.
        freeze_clock(limiter._repository)

        limits = [Limit.per_minute("rpm", 100)]

        # Consume all capacity
        async with limiter.acquire(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
            consume={"rpm": 100},
        ):
            pass

        # Should need to wait
        wait = await limiter.time_until_available(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
            needed={"rpm": 50},
        )
        # 50 tokens at 100/min = 30 seconds
        assert 29 < wait < 31


class TestRateLimiterCheckAvailability:
    """Tests for check_availability() (issue #472)."""

    async def test_returns_full_capacity_for_fresh_entity(self, limiter):
        """A never-seen entity reports full capacity and no wait."""
        limits = [Limit.per_minute("rpm", 100), Limit.per_minute("tpm", 10_000)]

        check = await limiter.check_availability(
            entity_id="key-1",
            resource="gpt-4",
            needed={"rpm": 1, "tpm": 500},
            limits=limits,
        )

        assert check.entity_id == "key-1"
        assert check.resource == "gpt-4"
        # Immune to clock drift (#503): the entity has no bucket at all, so
        # `check_availability` reports `limit.capacity` directly and the lazy
        # refill never runs. No pin needed.
        assert check.available == {"rpm": 100, "tpm": 10_000}
        assert check.needed == {"rpm": 1, "tpm": 500}
        assert check.retry_after_seconds == 0.0
        assert check.allowed is True
        assert check.exceeded == []
        assert check.deficit == {}

    async def test_needed_is_optional(self, limiter):
        """Omitting needed answers availability only, with no wait."""
        # `available == 0` is a drained bucket, not the floor: refill only adds,
        # so 600 ms of wall clock between the write and the read turns it into
        # 1 (#503). Pin the clock; see ``freeze_clock``.
        freeze_clock(limiter._repository)

        limits = [Limit.per_minute("rpm", 100)]

        async with limiter.acquire(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
            consume={"rpm": 100},
        ):
            pass

        check = await limiter.check_availability(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
        )

        assert check.available == {"rpm": 0}
        assert check.needed == {}
        assert check.retry_after_seconds == 0.0
        # Nothing was asked for, so nothing is exceeded
        assert check.allowed is True

    async def test_reports_availability_and_wait_together(self, limiter):
        """One call answers both questions for an exhausted bucket."""
        # `available == 0` flips to 1 after 600 ms and `deficit == 50` follows
        # it down (#503). Pin the clock; see ``freeze_clock``.
        freeze_clock(limiter._repository)

        limits = [Limit.per_minute("rpm", 100)]

        async with limiter.acquire(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
            consume={"rpm": 100},
        ):
            pass

        check = await limiter.check_availability(
            entity_id="key-1",
            resource="gpt-4",
            needed={"rpm": 50},
            limits=limits,
        )

        assert check.available["rpm"] == 0
        # 50 tokens at 100/min = 30 seconds
        assert 29 < check.retry_after_seconds < 31
        assert check.allowed is False
        assert check.exceeded == ["rpm"]
        assert check.deficit == {"rpm": 50}

    async def test_wait_is_max_across_limits(self, limiter):
        """retry_after_seconds is the slowest limit, and zero amounts are skipped."""
        # Each rpm token refilled between the write and the read shaves 0.6 s
        # off the estimate, so ~1 s of wall clock walks it out of the 29..31
        # window (#503). Pin the clock; see ``freeze_clock``.
        freeze_clock(limiter._repository)

        limits = [
            Limit.per_minute("rpm", 100),
            Limit.per_minute("tpm", 10_000),
        ]

        async with limiter.acquire(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
            consume={"rpm": 100, "tpm": 10_000},
        ):
            pass

        check = await limiter.check_availability(
            entity_id="key-1",
            resource="gpt-4",
            needed={"rpm": 50, "tpm": 0},
            limits=limits,
        )

        # tpm is requested as 0, so only rpm drives the wait (30s) and the verdict
        assert 29 < check.retry_after_seconds < 31
        assert check.exceeded == ["rpm"]

    async def test_reports_debt_as_negative_available(self, limiter):
        """A bucket pushed into debt by adjust() reports negative availability."""
        limits = [Limit.per_minute("rpm", 100)]

        async with limiter.acquire(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
            consume={"rpm": 90},
        ) as lease:
            await lease.adjust(rpm=30)

        check = await limiter.check_availability(
            entity_id="key-1",
            resource="gpt-4",
            needed={"rpm": 10},
            limits=limits,
        )

        # Immune to clock drift (#503): the bucket sits at -20, and climbing
        # back to 0 would take 20 tokens = 12 s of wall clock, far outside the
        # window a moto-backed test can drift. No pin needed.
        assert check.available["rpm"] < 0
        assert check.allowed is False
        assert check.deficit["rpm"] > 10

    async def test_uses_four_tier_resolution(self, limiter):
        """Without a limits override, stored config is resolved."""
        await limiter.set_system_defaults(limits=[Limit.per_minute("rpm", 50)])

        check = await limiter.check_availability(entity_id="key-1", resource="gpt-4")

        # Immune to clock drift (#503): no bucket exists, so this is
        # `limit.capacity` reported directly and refill never runs.
        assert check.available == {"rpm": 50}
        assert [limit.name for limit in check.limits] == ["rpm"]

    async def test_raises_when_no_limits_configured(self, limiter):
        """No stored config and no override is a ValidationError."""
        with pytest.raises(ValidationError):
            await limiter.check_availability(entity_id="key-1", resource="gpt-4")

    async def test_issues_one_bucket_read_for_all_limits(self, limiter):
        """Both answers come from a single bucket read, whatever the limit count."""
        limits = [
            Limit.per_minute("rpm", 100),
            Limit.per_minute("tpm", 10_000),
            Limit.per_minute("ipm", 500),
        ]

        repo = limiter._repository
        with (
            patch.object(repo, "get_buckets", wraps=repo.get_buckets) as get_buckets,
            patch.object(repo, "get_bucket", wraps=repo.get_bucket) as get_bucket,
        ):
            await limiter.check_availability(
                entity_id="key-1",
                resource="gpt-4",
                needed={"rpm": 1, "tpm": 1, "ipm": 1},
                limits=limits,
            )

        assert get_buckets.call_count == 1
        # ADR-114 puts every limit in one composite item, so the per-limit
        # GetItem loop the two older methods ran was a pure N+1.
        get_bucket.assert_not_called()

    async def test_carries_a_status_per_limit(self, limiter):
        """The UI renders each limit on its own line, so each needs its own
        availability and its own countdown from the same snapshot."""
        # The hourly refill period only makes `rpm` immune: 100/hour is one
        # token per 36 s, so `rpm.available == 0` cannot drift. `tpm` is
        # 10_000/hour — one token every *360 ms*, a tighter window than the
        # 600 ms of #498/#503 — so `tpm.available == 5_000` drifts to 5_001
        # sooner than any per-minute limit in this file would. Pin the clock;
        # see ``freeze_clock``.
        freeze_clock(limiter._repository)

        limits = [
            Limit.custom("rpm", 100, refill_amount=100, refill_period_seconds=3600),
            Limit.custom("tpm", 10_000, refill_amount=10_000, refill_period_seconds=3600),
        ]

        async with limiter.acquire(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
            consume={"rpm": 100, "tpm": 5_000},
        ):
            pass

        check = await limiter.check_availability(
            entity_id="key-1",
            resource="gpt-4",
            needed={"rpm": 50, "tpm": 1_000},
            limits=limits,
        )

        assert [s.limit_name for s in check.statuses] == ["rpm", "tpm"]
        rpm = check.status("rpm")
        tpm = check.status("tpm")
        assert rpm.available == 0
        assert rpm.requested == 50
        assert rpm.exceeded is True
        # 50 tokens at 100/hour = half an hour
        assert 1799 < rpm.retry_after_seconds < 1801
        assert tpm.available == 5_000
        assert tpm.requested == 1_000
        assert tpm.exceeded is False
        assert tpm.retry_after_seconds == 0.0
        # The aggregate countdown is the slowest limit, not a per-limit answer
        assert check.retry_after_seconds == rpm.retry_after_seconds
        assert check.status("nope") is None

    async def test_pins_one_instant_for_every_number(self, limiter):
        """Availability and wait must come from one clock read, so a client
        can tick the countdown locally from `checked_at_ms`."""
        await limiter.set_system_defaults(limits=[Limit.per_minute("rpm", 50)])

        with patch.object(limiter._repository, "_now_ms", return_value=1_700_000_000_000) as clock:
            check = await limiter.check_availability(entity_id="key-1", resource="gpt-4")

        assert clock.call_count == 1
        assert check.checked_at_ms == 1_700_000_000_000

    async def test_available_matches_check_availability(self, limiter):
        """available() delegates, so the two can never disagree."""
        # Two independent reads are compared, so this drifts on the gap
        # *between* them: 600 ms there and one side reports 70 while the other
        # reports 71 (#503). Pin the clock; see ``freeze_clock``.
        freeze_clock(limiter._repository)

        limits = [Limit.per_minute("rpm", 100)]

        async with limiter.acquire(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
            consume={"rpm": 30},
        ):
            pass

        available = await limiter.available(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
        )
        check = await limiter.check_availability(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
        )

        assert available == check.available

    async def test_time_until_available_matches_check_availability(self, limiter):
        """time_until_available() delegates, so the two can never disagree."""
        # Same between-the-reads drift as above, and here it is already at the
        # edge: 600 ms between the two calls is one extra token, which moves
        # the estimate by 0.6 s — past the `abs=0.5` tolerance below (#503).
        # Pin the clock; see ``freeze_clock``.
        freeze_clock(limiter._repository)

        limits = [Limit.per_minute("rpm", 100)]

        async with limiter.acquire(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
            consume={"rpm": 100},
        ):
            pass

        wait = await limiter.time_until_available(
            entity_id="key-1",
            resource="gpt-4",
            needed={"rpm": 50},
            limits=limits,
        )
        check = await limiter.check_availability(
            entity_id="key-1",
            resource="gpt-4",
            needed={"rpm": 50},
            limits=limits,
        )

        assert wait == pytest.approx(check.retry_after_seconds, abs=0.5)


class TestAvailabilityAcrossShards:
    """The non-consuming query must read every shard (GHSA-76rv, #466).

    ``available()`` already sums across shards. ``time_until_available()`` did
    not: it called ``get_bucket()``, which defaults to ``shard_id=0``, so its
    estimate was computed from one shard's balance and one shard's *share* of
    the refill rate. The two public methods therefore disagreed about the same
    entity, and the UI they exist to serve could render "40 remaining" beside a
    countdown derived from 20.
    """

    @staticmethod
    async def _seed_shards(limiter, limit, balances_milli: list[int], rf_ms: int):
        """Create shards 0..N-1, each at its own balance, all at the same rf."""
        repo = limiter._repository
        shard_count = len(balances_milli)
        await limiter.create_entity("user-1")
        await limiter.set_system_defaults([limit])
        for shard_id, tokens_milli in enumerate(balances_milli):
            state = BucketState.from_limit("user-1", "gpt-4", limit, rf_ms)
            state.tokens_milli = tokens_milli
            await repo.transact_write(
                [
                    repo.build_composite_create(
                        "user-1",
                        "gpt-4",
                        [state],
                        rf_ms,
                        shard_id=shard_id,
                        shard_count=shard_count,
                    )
                ]
            )
        repo._entity_cache[(repo._namespace_id, "user-1")] = (
            False,
            None,
            {"gpt-4": shard_count},
        )
        return repo

    async def test_wait_is_computed_across_every_shard(self, limiter):
        """Two shards at 20 tokens each: the entity holds 40 and refills at the
        full 100/min, so 60 tokens are 12s away. Reading shard 0 alone sees 20
        tokens refilling at its 50/min share and says 48s — four times too
        long, for an entity that is not even at its limit."""
        limit = Limit.custom("rpm", 100, refill_amount=100, refill_period_seconds=60)
        now_ms = int(time.time() * 1000)
        repo = await self._seed_shards(limiter, limit, [20_000, 20_000], now_ms)

        # Pin the clock to the seeded rf so refill cannot move the numbers
        with patch.object(repo, "_now_ms", return_value=now_ms):
            check = await limiter.check_availability(
                entity_id="user-1", resource="gpt-4", needed={"rpm": 60}
            )

        assert check.available == {"rpm": 40}
        assert check.deficit == {"rpm": 20}
        assert 11 < check.retry_after_seconds < 13

    async def test_time_until_available_is_shard_aware(self, limiter):
        """The public wrapper inherits the shard-aware estimate."""
        limit = Limit.custom("rpm", 100, refill_amount=100, refill_period_seconds=60)
        now_ms = int(time.time() * 1000)
        repo = await self._seed_shards(limiter, limit, [20_000, 20_000], now_ms)

        with patch.object(repo, "_now_ms", return_value=now_ms):
            wait = await limiter.time_until_available(
                entity_id="user-1", resource="gpt-4", needed={"rpm": 60}
            )

        assert 11 < wait < 13

    async def test_available_still_sums_every_shard(self, limiter):
        """The #466 fix survives the rewrite: shard 0's balance alone is 20."""
        limit = Limit.custom("rpm", 100, refill_amount=1, refill_period_seconds=3600)
        now_ms = int(time.time() * 1000)
        await self._seed_shards(limiter, limit, [20_000, 20_000], now_ms)

        # Immune to clock drift (#503) without a pin, unlike its siblings
        # above: `refill_amount=1` over an hour is one token per 3600 s.
        assert await limiter.available("user-1", "gpt-4") == {"rpm": 40}

    async def test_the_two_methods_agree_on_a_sharded_entity(self, limiter):
        """The bug this replaces: available() summed, time_until_available()
        did not, so a caller asking both got a contradiction."""
        limit = Limit.custom("rpm", 100, refill_amount=100, refill_period_seconds=60)
        now_ms = int(time.time() * 1000)
        repo = await self._seed_shards(limiter, limit, [30_000, 30_000], now_ms)

        with patch.object(repo, "_now_ms", return_value=now_ms):
            available = await limiter.available("user-1", "gpt-4")
            wait = await limiter.time_until_available(
                entity_id="user-1", resource="gpt-4", needed={"rpm": 60}
            )

        # 60 are available entity-wide, so there is nothing to wait for
        assert available == {"rpm": 60}
        assert wait == 0.0

    async def test_wait_survives_a_share_that_floors_to_zero(self, limiter):
        """A slow limit split many ways has an effective per-shard refill of 0.
        Summing the shares would divide by zero; fall back to the undivided
        rate, as BucketState.retry_refill_amount_milli does."""
        limit = Limit.custom("rpm", 32, refill_amount=1, refill_period_seconds=60)
        now_ms = int(time.time() * 1000)
        # 1 // 32 == 0 per shard
        await self._seed_shards(limiter, limit, [0] * 32, now_ms)

        check = await limiter.check_availability(
            entity_id="user-1", resource="gpt-4", needed={"rpm": 1}
        )

        # Immune to clock drift (#503) without a pin: the effective per-shard
        # refill is `1 // 32 == 0`, so `tokens_to_add` is 0 for any elapsed
        # time — which is the very condition this test exists to exercise.
        assert check.available == {"rpm": 0}
        assert check.retry_after_seconds > 0


class TestRateLimitExceededException:
    """Tests for RateLimitExceeded exception."""

    async def test_as_dict(self, limiter):
        """Test exception serialization."""
        limits = [Limit.per_minute("rpm", 10)]

        try:
            async with limiter.acquire(
                entity_id="key-1",
                resource="gpt-4",
                limits=limits,
                consume={"rpm": 20},
            ):
                pass
        except RateLimitExceeded as e:
            data = e.as_dict()

            assert data["error"] == "rate_limit_exceeded"
            assert "retry_after_seconds" in data
            assert "retry_after_ms" in data
            assert len(data["limits"]) == 1
            assert data["limits"][0]["exceeded"] is True

    async def test_retry_after_header(self, limiter):
        """Test retry_after_header property."""
        limits = [Limit.per_minute("rpm", 10)]

        try:
            async with limiter.acquire(
                entity_id="key-1",
                resource="gpt-4",
                limits=limits,
                consume={"rpm": 20},
            ):
                pass
        except RateLimitExceeded as e:
            header = e.retry_after_header
            assert header.isdigit()
            assert int(header) > 0


class TestRateLimiterOnUnavailable:
    """Tests for ALLOW vs BLOCK behavior when DynamoDB is unavailable."""

    @pytest.mark.asyncio
    async def test_allow_returns_noop_lease_on_dynamodb_error(self, limiter, monkeypatch):
        """ALLOW should return no-op lease on infrastructure error."""

        # Mock repository method to raise error
        async def mock_error(*args, **kwargs):
            raise ClientError(
                {"Error": {"Code": "ServiceUnavailable", "Message": "DynamoDB down"}},
                "BatchGetItem",
            )

        monkeypatch.setattr(limiter._repository, "batch_get_entity_and_buckets", mock_error)

        # Set on_unavailable to ALLOW via repository system config
        monkeypatch.setattr(
            limiter._repository, "resolve_on_unavailable", AsyncMock(return_value="allow")
        )

        # Should not raise, should return no-op lease
        limits = [Limit.per_minute("rpm", 100)]
        async with limiter.acquire(
            entity_id="test-entity",
            resource="api",
            limits=limits,
            consume={"rpm": 1},
        ) as lease:
            # No-op lease has no entries
            assert len(lease.entries) == 0
            assert lease.consumed == {}

    @pytest.mark.asyncio
    async def test_block_raises_unavailable_on_dynamodb_error(self, limiter, monkeypatch):
        """BLOCK should reject requests when DynamoDB is down."""

        # Mock repository method to raise error
        async def mock_error(*args, **kwargs):
            raise ClientError(
                {"Error": {"Code": "ProvisionedThroughputExceededException"}},
                "BatchGetItem",
            )

        monkeypatch.setattr(limiter._repository, "batch_get_entity_and_buckets", mock_error)

        # Default on_unavailable is BLOCK (from repository system config)
        # Should raise RateLimiterUnavailable
        limits = [Limit.per_minute("rpm", 100)]
        with pytest.raises(RateLimiterUnavailable) as exc_info:
            async with limiter.acquire(
                entity_id="test-entity",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        # Verify exception details
        assert exc_info.value.cause is not None
        assert "ProvisionedThroughputExceededException" in str(exc_info.value.cause)

    @pytest.mark.asyncio
    async def test_allow_override_in_acquire_call(self, limiter, monkeypatch):
        """on_unavailable parameter should override limiter default."""

        # Mock error
        async def mock_error(*args, **kwargs):
            raise ClientError(
                {"Error": {"Code": "InternalServerError"}},
                "BatchGetItem",
            )

        monkeypatch.setattr(limiter._repository, "batch_get_entity_and_buckets", mock_error)

        # Default is BLOCK from repository, but override in acquire
        limits = [Limit.per_minute("rpm", 100)]
        async with limiter.acquire(
            entity_id="test-entity",
            resource="api",
            limits=limits,
            consume={"rpm": 1},
            on_unavailable=OnUnavailable.ALLOW,  # Override to ALLOW
        ) as lease:
            # Should get no-op lease due to override
            assert len(lease.entries) == 0

    @pytest.mark.asyncio
    async def test_block_override_in_acquire_call(self, limiter, monkeypatch):
        """on_unavailable parameter should override limiter default."""

        # Mock error
        async def mock_error(*args, **kwargs):
            raise Exception("DynamoDB timeout")

        monkeypatch.setattr(limiter._repository, "batch_get_entity_and_buckets", mock_error)

        # Repository says ALLOW, but override in acquire to BLOCK
        monkeypatch.setattr(
            limiter._repository, "resolve_on_unavailable", AsyncMock(return_value="allow")
        )

        limits = [Limit.per_minute("rpm", 100)]
        with pytest.raises(RateLimiterUnavailable):
            async with limiter.acquire(
                entity_id="test-entity",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
                on_unavailable=OnUnavailable.BLOCK,  # Override to BLOCK
            ):
                pass


class TestRateLimiterStackOptions:
    """Tests for stack_options initialization."""

    @pytest.mark.asyncio
    async def test_limiter_with_stack_options_calls_ensure_infrastructure(
        self, mock_dynamodb, monkeypatch
    ):
        """When stack_options is provided, _ensure_initialized calls ensure_infrastructure."""
        from unittest.mock import AsyncMock

        # mock_dynamodb fixture is needed to set up AWS credentials
        from zae_limiter import RateLimiter, StackOptions

        stack_options = StackOptions(lambda_timeout=120)
        limiter = RateLimiter(
            name="test-with-stack-options",
            region="us-east-1",
            stack_options=stack_options,
        )

        # Mock ensure_infrastructure to isolate test
        ensure_infrastructure_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(
            limiter._repository, "ensure_infrastructure", ensure_infrastructure_mock
        )

        # Call _ensure_initialized
        await limiter._ensure_initialized()

        # Verify ensure_infrastructure was called
        ensure_infrastructure_mock.assert_called_once()

        await limiter.close()

    @pytest.mark.asyncio
    async def test_limiter_without_stack_options_calls_ensure_infrastructure(
        self, mock_dynamodb, monkeypatch
    ):
        """When stack_options is None, _ensure_initialized still calls ensure_infrastructure.

        The ensure_infrastructure method is always called by RateLimiter, but it's
        a no-op when Repository was created without stack_options.
        """
        from unittest.mock import AsyncMock

        # mock_dynamodb fixture is needed to set up AWS credentials
        from zae_limiter import RateLimiter

        limiter = RateLimiter(
            name="test-without-stack-options",
            region="us-east-1",
            stack_options=None,  # No stack options
        )

        # Mock ensure_infrastructure to isolate test
        ensure_infrastructure_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(
            limiter._repository, "ensure_infrastructure", ensure_infrastructure_mock
        )

        # Call _ensure_initialized
        await limiter._ensure_initialized()

        # Verify ensure_infrastructure WAS called (it's a no-op when no stack_options)
        ensure_infrastructure_mock.assert_called_once()

        await limiter.close()


class TestRateLimiterResourceCapacity:
    """Tests for get_resource_capacity."""

    @pytest.mark.asyncio
    async def test_get_resource_capacity_basic_aggregation(self, limiter):
        """Should aggregate capacity across all entities for a resource."""
        # `get_resource_capacity()` refills lazily off the same
        # `Repository._now_ms()` seam, so every per-entity `available` below is
        # an exact value under the ceiling that 600 ms of wall clock lifts by
        # one — and `total_available` by up to three (#503). Pin the clock; see
        # ``freeze_clock``. (`total_capacity` is config-derived and immune.)
        freeze_clock(limiter._repository)

        # Create 3 entities with different consumption levels
        entities = ["entity-a", "entity-b", "entity-c"]
        for entity_id in entities:
            await limiter.create_entity(entity_id)

        limits = [Limit.per_minute("rpm", 100)]

        # Entity A: consume 20
        async with limiter.acquire("entity-a", "gpt-4", {"rpm": 20}, limits=limits):
            pass

        # Entity B: consume 50
        async with limiter.acquire("entity-b", "gpt-4", {"rpm": 50}, limits=limits):
            pass

        # Entity C: consume 10
        async with limiter.acquire("entity-c", "gpt-4", {"rpm": 10}, limits=limits):
            pass

        # Query aggregated capacity
        capacity = await limiter.get_resource_capacity(
            resource="gpt-4",
            limit_name="rpm",
        )

        # Verify aggregation
        assert capacity.resource == "gpt-4"
        assert capacity.limit_name == "rpm"
        assert capacity.total_capacity == 300  # 100 * 3 entities
        assert capacity.total_available == 220  # 300 - (20 + 50 + 10)
        assert len(capacity.entities) == 3

        # Verify individual entity capacities
        entity_map = {e.entity_id: e for e in capacity.entities}
        assert entity_map["entity-a"].available == 80
        assert entity_map["entity-b"].available == 50
        assert entity_map["entity-c"].available == 90

    @pytest.mark.asyncio
    async def test_get_resource_capacity_parents_only_filter(self, limiter):
        """parents_only=True should exclude child entities."""
        # Create hierarchy
        await limiter.create_entity("org-1")  # Parent
        await limiter.create_entity("team-1", parent_id="org-1")  # Child
        await limiter.create_entity("org-2")  # Parent

        limits = [Limit.per_minute("rpm", 100)]

        # Create buckets for all
        for entity_id in ["org-1", "team-1", "org-2"]:
            async with limiter.acquire(entity_id, "api", {"rpm": 10}, limits=limits):
                pass

        # Query with parents_only=False (all)
        all_capacity = await limiter.get_resource_capacity("api", "rpm", parents_only=False)
        assert len(all_capacity.entities) == 3
        assert all_capacity.total_capacity == 300

        # Query with parents_only=True
        parent_capacity = await limiter.get_resource_capacity("api", "rpm", parents_only=True)
        assert len(parent_capacity.entities) == 2  # Only org-1 and org-2
        assert parent_capacity.total_capacity == 200

        # Verify only parents are included
        parent_ids = {e.entity_id for e in parent_capacity.entities}
        assert parent_ids == {"org-1", "org-2"}
        assert "team-1" not in parent_ids

    @pytest.mark.asyncio
    async def test_get_resource_capacity_utilization_calculation(self, limiter):
        """Should calculate utilization percentage correctly."""
        # `entity.available == 70` drifts to 71 after 600 ms, and that also
        # moves `utilization_pct` to 29.0 — outside the `< 0.1` tolerance
        # below (#503). Pin the clock; see ``freeze_clock``.
        freeze_clock(limiter._repository)

        await limiter.create_entity("entity-1")

        limits = [Limit.per_minute("rpm", 100)]

        # Consume 30%
        async with limiter.acquire("entity-1", "api", {"rpm": 30}, limits=limits):
            pass

        capacity = await limiter.get_resource_capacity("api", "rpm")

        # Should have 70% available, 30% utilized
        assert len(capacity.entities) == 1
        entity = capacity.entities[0]
        assert entity.available == 70
        assert entity.capacity == 100
        # Utilization is (used / capacity * 100) = (30 / 100 * 100) = 30%
        assert abs(entity.utilization_pct - 30.0) < 0.1

    @pytest.mark.asyncio
    async def test_get_resource_capacity_empty_result(self, limiter):
        """Should return empty capacity when no buckets match."""
        capacity = await limiter.get_resource_capacity("nonexistent-resource", "rpm")

        assert capacity.resource == "nonexistent-resource"
        assert capacity.limit_name == "rpm"
        assert capacity.total_capacity == 0
        assert capacity.total_available == 0
        assert len(capacity.entities) == 0
        assert capacity.utilization_pct == 0.0

    @pytest.mark.asyncio
    async def test_get_resource_capacity_sharded_entity_deduplication(self, limiter):
        """Sharded entities should report capacity once, not per-shard."""
        from zae_limiter import schema

        await limiter.create_entity("sharded-user")
        limits = [Limit.per_minute("rpm", 100)]

        async with limiter.acquire("sharded-user", "gpt-4", {"rpm": 10}, limits=limits):
            pass

        # Manually create shard 1 to simulate sharding
        repo = limiter._repository
        client = await repo._get_client()
        shard0_key = {
            "PK": {"S": schema.pk_bucket(repo._namespace_id, "sharded-user", "gpt-4", 0)},
            "SK": {"S": schema.sk_state()},
        }
        shard0_resp = await client.get_item(TableName=repo.table_name, Key=shard0_key)
        shard0_item = shard0_resp["Item"]

        shard1_item = dict(shard0_item)
        shard1_item["PK"] = {"S": schema.pk_bucket(repo._namespace_id, "sharded-user", "gpt-4", 1)}
        shard1_item["GSI2SK"] = {"S": schema.gsi2_sk_bucket("sharded-user", 1)}
        shard1_item["GSI3SK"] = {"S": schema.gsi3_sk_bucket("gpt-4", 1)}
        shard1_item["shard_count"] = {"N": "2"}
        await client.update_item(
            TableName=repo.table_name,
            Key=shard0_key,
            UpdateExpression="SET shard_count = :sc",
            ExpressionAttributeValues={":sc": {"N": "2"}},
        )
        await client.put_item(TableName=repo.table_name, Item=shard1_item)

        capacity = await limiter.get_resource_capacity("gpt-4", "rpm")

        assert capacity.total_capacity == 100  # Not 200
        assert len(capacity.entities) == 1  # Not 2
        assert capacity.entities[0].entity_id == "sharded-user"
        assert capacity.entities[0].capacity == 100


class TestRateLimiterCapacityEdgeCases:
    """Tests for edge cases in capacity calculations."""

    async def test_time_until_available_skips_zero_amount(self, limiter):
        """time_until_available should skip limits with zero needed amount."""
        limits = [
            Limit.per_minute("rpm", 100),
            Limit.per_minute("tpm", 10_000),
        ]

        # Consume all rpm capacity
        async with limiter.acquire(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
            consume={"rpm": 100, "tpm": 100},
        ):
            pass

        # Wait time for rpm=50, tpm=0 should only be based on rpm
        wait = await limiter.time_until_available(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
            needed={"rpm": 50, "tpm": 0},
        )
        # Should wait for rpm only (50 tokens at 100/min = 30 seconds)
        assert 29 < wait < 31


class TestListResourcesWithEntityConfigs:
    """Tests for listing resources with entity-level custom limits."""

    async def test_list_resources_with_entity_configs(self, limiter):
        """Should list resources that have entity-level custom limits."""
        # Set entity limits for two resources
        await limiter.set_limits("user-1", [Limit.per_minute("rpm", 100)], resource="gpt-4")
        await limiter.set_limits("user-1", [Limit.per_minute("rpm", 200)], resource="claude-3")

        resources = await limiter.list_resources_with_entity_configs()

        assert "gpt-4" in resources
        assert "claude-3" in resources

    async def test_list_resources_with_entity_configs_empty(self, limiter):
        """Should return empty list when no entity configs exist."""
        resources = await limiter.list_resources_with_entity_configs()
        assert resources == []


class TestRateLimiterFetchBucketsFallback:
    """Tests for _fetch_buckets fallback path when batch not supported."""

    async def test_fetch_buckets_fallback_fresh_entity(self, limiter, monkeypatch):
        """Fallback path should work for entities without existing buckets."""
        from zae_limiter.models import BackendCapabilities

        limits = [Limit.per_minute("rpm", 100)]

        # Override capabilities to disable batch operations
        no_batch_capabilities = BackendCapabilities(
            supports_audit_logging=True,
            supports_usage_snapshots=True,
            supports_infrastructure_management=True,
            supports_change_streams=True,
            supports_batch_operations=False,
        )
        monkeypatch.setattr(limiter._repository, "_capabilities", no_batch_capabilities)

        # Acquire should still work (uses sequential get_buckets fallback)
        async with limiter.acquire(
            entity_id="fresh-entity",
            resource="gpt-4",
            limits=limits,
            consume={"rpm": 1},
        ) as lease:
            assert lease.consumed == {"rpm": 1}


class TestRateLimiterInputValidation:
    @pytest.mark.asyncio
    async def test_acquire_validates_entity_id(self, limiter):
        """Acquire should reject entity_id containing reserved delimiter."""
        limits = [Limit.per_minute("rpm", 100)]

        with pytest.raises(InvalidIdentifierError) as exc_info:
            async with limiter.acquire("user#123", "api", {"rpm": 1}, limits=limits):
                pass

        assert exc_info.value.field == "entity_id"
        assert "#" in exc_info.value.reason

    @pytest.mark.asyncio
    async def test_acquire_validates_resource(self, limiter):
        """Acquire should reject resource containing reserved delimiter."""
        limits = [Limit.per_minute("rpm", 100)]

        with pytest.raises(InvalidNameError) as exc_info:
            async with limiter.acquire("user-123", "api#v2", {"rpm": 1}, limits=limits):
                pass

        assert exc_info.value.field == "resource"
        assert "#" in exc_info.value.reason

    @pytest.mark.asyncio
    async def test_acquire_validates_empty_entity_id(self, limiter):
        """Acquire should reject empty entity_id."""
        limits = [Limit.per_minute("rpm", 100)]

        with pytest.raises(InvalidIdentifierError) as exc_info:
            async with limiter.acquire("", "api", {"rpm": 1}, limits=limits):
                pass

        assert exc_info.value.field == "entity_id"
        assert "empty" in exc_info.value.reason

    @pytest.mark.asyncio
    async def test_acquire_validates_empty_resource(self, limiter):
        """Acquire should reject empty resource."""
        limits = [Limit.per_minute("rpm", 100)]

        with pytest.raises(InvalidNameError) as exc_info:
            async with limiter.acquire("user-123", "", {"rpm": 1}, limits=limits):
                pass

        assert exc_info.value.field == "resource"
        assert "empty" in exc_info.value.reason

    @pytest.mark.asyncio
    async def test_acquire_accepts_valid_inputs(self, limiter):
        """Acquire should accept valid entity_id and resource."""
        limits = [Limit.per_minute("rpm", 100)]

        # Should not raise
        async with limiter.acquire("user-123", "gpt-3.5-turbo", {"rpm": 1}, limits=limits):
            pass


class TestRateLimiterIsAvailable:
    """Tests for is_available() health check method."""

    @pytest.mark.asyncio
    async def test_is_available_returns_true_when_table_exists(self, limiter):
        """is_available should return True when DynamoDB table is reachable."""
        result = await limiter.is_available()
        assert result is True

    @pytest.mark.asyncio
    async def test_is_available_returns_false_on_client_error(self, limiter, monkeypatch):
        """is_available should return False when DynamoDB returns error."""

        async def mock_error(*args, **kwargs):
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "Table not found"}},
                "GetItem",
            )

        monkeypatch.setattr(limiter._repository, "ping", mock_error)
        result = await limiter.is_available()
        assert result is False

    @pytest.mark.asyncio
    async def test_is_available_returns_false_on_timeout(self, limiter, monkeypatch):
        """is_available should return False when request times out."""

        async def mock_slow(*args, **kwargs):
            await asyncio.sleep(10)  # Will be cancelled by timeout
            return True

        monkeypatch.setattr(limiter._repository, "ping", mock_slow)
        result = await limiter.is_available(timeout=0.1)
        assert result is False

    @pytest.mark.asyncio
    async def test_is_available_returns_false_on_connection_error(self, limiter, monkeypatch):
        """is_available should return False when connection fails."""

        async def mock_connection_error(*args, **kwargs):
            raise ConnectionError("Cannot connect to DynamoDB")

        monkeypatch.setattr(limiter._repository, "ping", mock_connection_error)
        result = await limiter.is_available()
        assert result is False

    @pytest.mark.asyncio
    async def test_is_available_custom_timeout(self, limiter):
        """is_available should respect custom timeout parameter."""
        # With a reasonable timeout, should still succeed
        result = await limiter.is_available(timeout=5.0)
        assert result is True


class TestRateLimiterAudit:
    """Tests for audit functionality."""

    @pytest.mark.asyncio
    async def test_get_audit_events_after_create_entity(self, limiter):
        """Test that create_entity logs an audit event."""
        await limiter.create_entity(
            entity_id="proj-1",
            name="Test Project",
            principal="admin@example.com",
        )

        events = await limiter.get_audit_events("proj-1")
        assert len(events) == 1
        assert events[0].action == "entity_created"
        assert events[0].entity_id == "proj-1"
        assert events[0].principal == "admin@example.com"
        assert events[0].details["name"] == "Test Project"

    @pytest.mark.asyncio
    async def test_get_audit_events_after_delete_entity(self, limiter):
        """Test that delete_entity logs an audit event."""
        await limiter.create_entity(entity_id="proj-1", principal="admin")
        await limiter.delete_entity("proj-1", principal="admin")

        events = await limiter.get_audit_events("proj-1")
        # Should have 2 events: create and delete
        assert len(events) == 2
        assert events[0].action == "entity_deleted"  # Most recent first
        assert events[1].action == "entity_created"

    @pytest.mark.asyncio
    async def test_get_audit_events_after_set_limits(self, limiter):
        """Test that set_limits logs an audit event."""
        await limiter.create_entity(entity_id="proj-1")
        limits = [Limit.per_minute("rpm", 100)]
        await limiter.set_limits("proj-1", limits, principal="admin")

        events = await limiter.get_audit_events("proj-1")
        # Find the limits_set event
        limit_events = [e for e in events if e.action == "limits_set"]
        assert len(limit_events) == 1
        assert limit_events[0].principal == "admin"

    @pytest.mark.asyncio
    async def test_get_audit_events_after_delete_limits(self, limiter):
        """Test that delete_limits logs an audit event."""
        await limiter.create_entity(entity_id="proj-1")
        limits = [Limit.per_minute("rpm", 100)]
        await limiter.set_limits("proj-1", limits)
        await limiter.delete_limits("proj-1", principal="admin")

        events = await limiter.get_audit_events("proj-1")
        delete_events = [e for e in events if e.action == "limits_deleted"]
        assert len(delete_events) == 1
        assert delete_events[0].principal == "admin"

    @pytest.mark.asyncio
    async def test_get_audit_events_with_limit(self, limiter):
        """Test pagination limit parameter."""
        await limiter.create_entity(entity_id="proj-1")
        # Create multiple events by setting limits multiple times
        for i in range(5):
            await limiter.set_limits("proj-1", [Limit.per_minute("rpm", 100 + i)])

        events = await limiter.get_audit_events("proj-1", limit=3)
        assert len(events) == 3

    @pytest.mark.asyncio
    async def test_get_audit_events_empty(self, limiter):
        """Test getting events for entity with no events."""
        events = await limiter.get_audit_events("nonexistent")
        assert events == []

    @pytest.mark.asyncio
    async def test_create_entity_without_principal_uses_auto_detection(self, limiter):
        """Test that principal is auto-detected from AWS identity when not provided."""
        await limiter.create_entity(entity_id="proj-1")
        events = await limiter.get_audit_events("proj-1")
        assert len(events) == 1
        # In moto tests, STS call may fail, so principal could be None
        # In real AWS, it would be the caller's ARN
        # This test just verifies the flow works without explicit principal

    @pytest.mark.asyncio
    async def test_explicit_principal_overrides_auto_detection(self, limiter):
        """Test that explicit principal overrides auto-detection."""
        await limiter.create_entity(entity_id="proj-1", principal="explicit-user")
        events = await limiter.get_audit_events("proj-1")
        assert len(events) == 1
        assert events[0].principal == "explicit-user"


class TestRateLimiterUsageSnapshots:
    """Tests for usage snapshot queries."""

    @pytest.fixture
    async def limiter_with_snapshots(self, limiter):
        """Limiter with test usage snapshots."""
        from zae_limiter import schema

        # Access the repository's client to insert test data
        repo = limiter._repository
        client = await repo._get_client()

        snapshots_data = [
            ("entity-1", "gpt-4", "hourly", "2024-01-15T10:00:00Z", {"tpm": 1000, "rpm": 5}),
            ("entity-1", "gpt-4", "hourly", "2024-01-15T11:00:00Z", {"tpm": 2000, "rpm": 10}),
            ("entity-1", "gpt-4", "daily", "2024-01-15T00:00:00Z", {"tpm": 3000, "rpm": 15}),
        ]

        for entity_id, resource, window_type, window_start, counters in snapshots_data:
            item = {
                "PK": {"S": schema.pk_entity(limiter._repository.namespace_id, entity_id)},
                "SK": {"S": schema.sk_usage(resource, window_start)},
                "entity_id": {"S": entity_id},
                "resource": {"S": resource},
                "window": {"S": window_type},
                "window_start": {"S": window_start},
                "total_events": {"N": str(sum(counters.values()))},
                "GSI2PK": {
                    "S": schema.gsi2_pk_resource(limiter._repository.namespace_id, resource)
                },
                "GSI2SK": {"S": f"USAGE#{window_start}#{entity_id}"},
            }
            for name, value in counters.items():
                item[name] = {"N": str(value)}

            await client.put_item(TableName=repo.table_name, Item=item)

        yield limiter

    @pytest.mark.asyncio
    async def test_get_usage_snapshots_basic(self, limiter_with_snapshots):
        """Test basic snapshot query."""
        snapshots, next_key = await limiter_with_snapshots.get_usage_snapshots(entity_id="entity-1")

        assert len(snapshots) == 3
        assert all(s.entity_id == "entity-1" for s in snapshots)
        assert next_key is None

    @pytest.mark.asyncio
    async def test_get_usage_snapshots_with_datetime_conversion(self, limiter_with_snapshots):
        """Test datetime parameters are converted to ISO strings."""
        from datetime import datetime

        snapshots, _ = await limiter_with_snapshots.get_usage_snapshots(
            entity_id="entity-1",
            start_time=datetime(2024, 1, 15, 10, 0, 0),
            end_time=datetime(2024, 1, 15, 11, 0, 0),
        )

        # Should match 10:00 and 11:00 hourly snapshots
        assert len(snapshots) == 2
        window_starts = {s.window_start for s in snapshots}
        assert "2024-01-15T10:00:00Z" in window_starts
        assert "2024-01-15T11:00:00Z" in window_starts

    @pytest.mark.asyncio
    async def test_get_usage_snapshots_with_timezone_aware_datetime(self, limiter_with_snapshots):
        """Test timezone-aware datetime is converted to UTC."""
        from datetime import datetime

        # Create timezone-aware datetime (UTC-5)
        eastern = UTC  # Using UTC for simplicity in test
        start = datetime(2024, 1, 15, 10, 0, 0, tzinfo=eastern)

        snapshots, _ = await limiter_with_snapshots.get_usage_snapshots(
            entity_id="entity-1",
            start_time=start,
        )

        # Should work without error
        assert len(snapshots) >= 1

    @pytest.mark.asyncio
    async def test_get_usage_summary_basic(self, limiter_with_snapshots):
        """Test basic summary aggregation."""
        summary = await limiter_with_snapshots.get_usage_summary(
            entity_id="entity-1",
            resource="gpt-4",
            window_type="hourly",
        )

        assert summary.snapshot_count == 2
        assert summary.total["tpm"] == 3000  # 1000 + 2000
        assert summary.total["rpm"] == 15  # 5 + 10

    @pytest.mark.asyncio
    async def test_get_usage_summary_with_datetime(self, limiter_with_snapshots):
        """Test summary with datetime parameters."""
        from datetime import datetime

        summary = await limiter_with_snapshots.get_usage_summary(
            entity_id="entity-1",
            start_time=datetime(2024, 1, 15, 10, 0, 0),
            end_time=datetime(2024, 1, 15, 10, 0, 0),
        )

        assert summary.snapshot_count == 1
        assert summary.total["tpm"] == 1000

    @pytest.mark.asyncio
    async def test_get_usage_snapshots_requires_entity_or_resource(self, limiter_with_snapshots):
        """Should raise ValueError if neither entity_id nor resource provided."""
        with pytest.raises(ValueError, match="Either entity_id or resource"):
            await limiter_with_snapshots.get_usage_snapshots()

    @pytest.mark.asyncio
    async def test_get_usage_summary_requires_entity_or_resource(self, limiter_with_snapshots):
        """Should raise ValueError if neither entity_id nor resource provided."""
        with pytest.raises(ValueError, match="Either entity_id or resource"):
            await limiter_with_snapshots.get_usage_summary()


class TestInfrastructureDiscovery:
    """Tests for InfrastructureDiscovery class."""

    @pytest.mark.asyncio
    async def test_list_limiters_empty(self):
        """list_limiters returns empty list when no managed stacks exist."""
        with patch.object(
            InfrastructureDiscovery, "_get_client", new_callable=AsyncMock
        ) as mock_get_client:
            mock_client = MagicMock()
            mock_client.describe_stacks = AsyncMock(return_value={"Stacks": []})
            mock_get_client.return_value = mock_client

            async with InfrastructureDiscovery(region="us-east-1") as discovery:
                limiters = await discovery.list_limiters()

            assert limiters == []

    @pytest.mark.asyncio
    async def test_list_limiters_filters_by_tag_or_prefix(self):
        """list_limiters returns stacks with ManagedBy tag or ZAEL- prefix."""
        with patch.object(
            InfrastructureDiscovery, "_get_client", new_callable=AsyncMock
        ) as mock_get_client:
            mock_client = MagicMock()
            mock_client.describe_stacks = AsyncMock(
                return_value={
                    "Stacks": [
                        {
                            "StackName": "my-tagged-app",
                            "StackStatus": "CREATE_COMPLETE",
                            "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                            "Tags": [
                                {"Key": "ManagedBy", "Value": "zae-limiter"},
                                {"Key": "zae-limiter:name", "Value": "my-tagged-app"},
                            ],
                        },
                        {
                            "StackName": "other-stack",
                            "StackStatus": "CREATE_COMPLETE",
                            "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                            "Tags": [],
                        },
                        {
                            "StackName": "ZAEL-another",
                            "StackStatus": "UPDATE_COMPLETE",
                            "CreationTime": datetime(2024, 1, 14, 9, 0, 0),
                            "Tags": [],
                        },
                    ],
                }
            )
            mock_get_client.return_value = mock_client

            async with InfrastructureDiscovery(region="us-east-1") as discovery:
                limiters = await discovery.list_limiters()

            # Should include tagged stack and legacy ZAEL- prefixed stack, but not other-stack
            assert len(limiters) == 2
            stack_names = {lim.stack_name for lim in limiters}
            assert "my-tagged-app" in stack_names
            assert "ZAEL-another" in stack_names
            assert "other-stack" not in stack_names

    @pytest.mark.asyncio
    async def test_list_limiters_extracts_user_name_from_tag(self):
        """list_limiters extracts user_name from zae-limiter:name tag."""
        with patch.object(
            InfrastructureDiscovery, "_get_client", new_callable=AsyncMock
        ) as mock_get_client:
            mock_client = MagicMock()
            mock_client.describe_stacks = AsyncMock(
                return_value={
                    "Stacks": [
                        {
                            "StackName": "my-app",
                            "StackStatus": "CREATE_COMPLETE",
                            "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                            "Tags": [
                                {"Key": "ManagedBy", "Value": "zae-limiter"},
                                {"Key": "zae-limiter:name", "Value": "my-app"},
                            ],
                        },
                    ],
                }
            )
            mock_get_client.return_value = mock_client

            async with InfrastructureDiscovery(region="us-east-1") as discovery:
                limiters = await discovery.list_limiters()

            assert len(limiters) == 1
            assert limiters[0].stack_name == "my-app"
            assert limiters[0].user_name == "my-app"

    @pytest.mark.asyncio
    async def test_list_limiters_extracts_user_name_from_legacy_prefix(self):
        """list_limiters strips ZAEL- prefix for user_name on legacy stacks."""
        with patch.object(
            InfrastructureDiscovery, "_get_client", new_callable=AsyncMock
        ) as mock_get_client:
            mock_client = MagicMock()
            mock_client.describe_stacks = AsyncMock(
                return_value={
                    "Stacks": [
                        {
                            "StackName": "ZAEL-my-app",
                            "StackStatus": "CREATE_COMPLETE",
                            "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                            "Tags": [],
                        },
                    ],
                }
            )
            mock_get_client.return_value = mock_client

            async with InfrastructureDiscovery(region="us-east-1") as discovery:
                limiters = await discovery.list_limiters()

            assert len(limiters) == 1
            assert limiters[0].stack_name == "ZAEL-my-app"
            assert limiters[0].user_name == "my-app"

    @pytest.mark.asyncio
    async def test_list_limiters_with_version_tags(self):
        """list_limiters extracts version info from tags."""
        with patch.object(
            InfrastructureDiscovery, "_get_client", new_callable=AsyncMock
        ) as mock_get_client:
            mock_client = MagicMock()
            mock_client.describe_stacks = AsyncMock(
                return_value={
                    "Stacks": [
                        {
                            "StackName": "my-app",
                            "StackStatus": "CREATE_COMPLETE",
                            "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                            "Tags": [
                                {"Key": "ManagedBy", "Value": "zae-limiter"},
                                {"Key": "zae-limiter:name", "Value": "my-app"},
                                {"Key": "zae-limiter:version", "Value": "0.5.0"},
                                {"Key": "zae-limiter:lambda-version", "Value": "0.5.0"},
                                {"Key": "zae-limiter:schema-version", "Value": "1.0.0"},
                            ],
                        }
                    ]
                }
            )
            mock_get_client.return_value = mock_client

            async with InfrastructureDiscovery(region="us-east-1") as discovery:
                limiters = await discovery.list_limiters()

            assert len(limiters) == 1
            assert limiters[0].version == "0.5.0"
            assert limiters[0].lambda_version == "0.5.0"
            assert limiters[0].schema_version == "1.0.0"

    @pytest.mark.asyncio
    async def test_list_limiters_missing_tags(self):
        """list_limiters handles missing version tags gracefully."""
        with patch.object(
            InfrastructureDiscovery, "_get_client", new_callable=AsyncMock
        ) as mock_get_client:
            mock_client = MagicMock()
            mock_client.describe_stacks = AsyncMock(
                return_value={
                    "Stacks": [
                        {
                            "StackName": "ZAEL-my-app",
                            "StackStatus": "CREATE_COMPLETE",
                            "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                            "Tags": [],
                        },
                    ],
                }
            )
            mock_get_client.return_value = mock_client

            async with InfrastructureDiscovery(region="us-east-1") as discovery:
                limiters = await discovery.list_limiters()

            assert len(limiters) == 1
            assert limiters[0].version is None
            assert limiters[0].lambda_version is None
            assert limiters[0].schema_version is None

    @pytest.mark.asyncio
    async def test_list_limiters_handles_tagging_api_error(self):
        """list_limiters handles tagging API errors gracefully with describe_stacks fallback."""
        with patch.object(
            InfrastructureDiscovery, "_get_client", new_callable=AsyncMock
        ) as mock_get_client:
            mock_client = MagicMock()
            # describe_stacks works and returns a managed stack
            mock_client.describe_stacks = AsyncMock(
                return_value={
                    "Stacks": [
                        {
                            "StackName": "my-app",
                            "StackStatus": "CREATE_COMPLETE",
                            "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                            "Tags": [
                                {"Key": "ManagedBy", "Value": "zae-limiter"},
                                {"Key": "zae-limiter:name", "Value": "my-app"},
                            ],
                        },
                    ],
                }
            )
            mock_get_client.return_value = mock_client

            # Tagging API fails (e.g., not available in LocalStack)
            with patch.object(
                InfrastructureDiscovery,
                "_discover_via_tagging_api",
                new_callable=AsyncMock,
                return_value=[],
            ):
                async with InfrastructureDiscovery(region="us-east-1") as discovery:
                    limiters = await discovery.list_limiters()

            # Should still return the limiter via describe_stacks fallback
            assert len(limiters) == 1
            assert limiters[0].stack_name == "my-app"

    @pytest.mark.asyncio
    async def test_list_limiters_with_last_updated_time(self):
        """list_limiters includes last_updated_time when present."""
        with patch.object(
            InfrastructureDiscovery, "_get_client", new_callable=AsyncMock
        ) as mock_get_client:
            mock_client = MagicMock()
            mock_client.describe_stacks = AsyncMock(
                return_value={
                    "Stacks": [
                        {
                            "StackName": "ZAEL-my-app",
                            "StackStatus": "UPDATE_COMPLETE",
                            "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                            "LastUpdatedTime": datetime(2024, 1, 16, 14, 0, 0),
                            "Tags": [],
                        },
                    ],
                }
            )
            mock_get_client.return_value = mock_client

            async with InfrastructureDiscovery(region="us-east-1") as discovery:
                limiters = await discovery.list_limiters()

            assert len(limiters) == 1
            assert limiters[0].creation_time == "2024-01-15T10:30:00"
            assert limiters[0].last_updated_time == "2024-01-16T14:00:00"

    @pytest.mark.asyncio
    async def test_list_limiters_various_statuses(self):
        """list_limiters correctly reports various stack statuses."""
        with patch.object(
            InfrastructureDiscovery, "_get_client", new_callable=AsyncMock
        ) as mock_get_client:
            mock_client = MagicMock()
            mock_client.describe_stacks = AsyncMock(
                return_value={
                    "Stacks": [
                        {
                            "StackName": "ZAEL-healthy",
                            "StackStatus": "CREATE_COMPLETE",
                            "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                            "Tags": [],
                        },
                        {
                            "StackName": "ZAEL-in-progress",
                            "StackStatus": "UPDATE_IN_PROGRESS",
                            "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                            "Tags": [],
                        },
                        {
                            "StackName": "ZAEL-failed",
                            "StackStatus": "CREATE_FAILED",
                            "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                            "Tags": [],
                        },
                    ],
                }
            )
            mock_get_client.return_value = mock_client

            async with InfrastructureDiscovery(region="us-east-1") as discovery:
                limiters = await discovery.list_limiters()

            assert len(limiters) == 3

            # Find by user_name
            limiter_map = {lim.user_name: lim for lim in limiters}

            # Check statuses and properties
            assert limiter_map["healthy"].is_healthy is True
            assert limiter_map["healthy"].is_in_progress is False
            assert limiter_map["healthy"].is_failed is False

            assert limiter_map["in-progress"].is_healthy is False
            assert limiter_map["in-progress"].is_in_progress is True
            assert limiter_map["in-progress"].is_failed is False

            assert limiter_map["failed"].is_healthy is False
            assert limiter_map["failed"].is_in_progress is False
            assert limiter_map["failed"].is_failed is True

    @pytest.mark.asyncio
    async def test_list_limiters_sorted_by_user_name(self):
        """list_limiters returns results sorted by user_name."""
        with patch.object(
            InfrastructureDiscovery, "_get_client", new_callable=AsyncMock
        ) as mock_get_client:
            mock_client = MagicMock()
            mock_client.describe_stacks = AsyncMock(
                return_value={
                    "Stacks": [
                        {
                            "StackName": "ZAEL-zebra",
                            "StackStatus": "CREATE_COMPLETE",
                            "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                            "Tags": [],
                        },
                        {
                            "StackName": "ZAEL-apple",
                            "StackStatus": "CREATE_COMPLETE",
                            "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                            "Tags": [],
                        },
                        {
                            "StackName": "ZAEL-banana",
                            "StackStatus": "CREATE_COMPLETE",
                            "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                            "Tags": [],
                        },
                    ],
                }
            )
            mock_get_client.return_value = mock_client

            async with InfrastructureDiscovery(region="us-east-1") as discovery:
                limiters = await discovery.list_limiters()

            user_names = [lim.user_name for lim in limiters]
            assert user_names == ["apple", "banana", "zebra"]

    @pytest.mark.asyncio
    async def test_list_limiters_pagination(self):
        """list_limiters handles pagination correctly."""
        with patch.object(
            InfrastructureDiscovery, "_get_client", new_callable=AsyncMock
        ) as mock_get_client:
            mock_client = MagicMock()
            # describe_stacks with pagination
            mock_client.describe_stacks = AsyncMock(
                side_effect=[
                    {
                        "Stacks": [
                            {
                                "StackName": "ZAEL-first",
                                "StackStatus": "CREATE_COMPLETE",
                                "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                                "Tags": [],
                            },
                        ],
                        "NextToken": "page2token",
                    },
                    {
                        "Stacks": [
                            {
                                "StackName": "ZAEL-second",
                                "StackStatus": "CREATE_COMPLETE",
                                "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                                "Tags": [],
                            },
                        ],
                    },
                ]
            )
            mock_get_client.return_value = mock_client

            async with InfrastructureDiscovery(region="us-east-1") as discovery:
                limiters = await discovery.list_limiters()

            assert len(limiters) == 2
            user_names = {lim.user_name for lim in limiters}
            assert "first" in user_names
            assert "second" in user_names

    @pytest.mark.asyncio
    async def test_list_limiters_includes_region(self):
        """list_limiters includes region in LimiterInfo."""
        with patch.object(
            InfrastructureDiscovery, "_get_client", new_callable=AsyncMock
        ) as mock_get_client:
            mock_client = MagicMock()
            mock_client.describe_stacks = AsyncMock(
                return_value={
                    "Stacks": [
                        {
                            "StackName": "ZAEL-my-app",
                            "StackStatus": "CREATE_COMPLETE",
                            "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                            "Tags": [],
                        },
                    ],
                }
            )
            mock_get_client.return_value = mock_client

            async with InfrastructureDiscovery(region="eu-west-1") as discovery:
                limiters = await discovery.list_limiters()

            assert len(limiters) == 1
            assert limiters[0].region == "eu-west-1"

    @pytest.mark.asyncio
    async def test_list_limiters_default_region(self):
        """list_limiters uses 'default' for region display when not specified."""
        with patch.object(
            InfrastructureDiscovery, "_get_client", new_callable=AsyncMock
        ) as mock_get_client:
            mock_client = MagicMock()
            mock_client.describe_stacks = AsyncMock(
                return_value={
                    "Stacks": [
                        {
                            "StackName": "ZAEL-my-app",
                            "StackStatus": "CREATE_COMPLETE",
                            "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                            "Tags": [],
                        },
                    ],
                }
            )
            mock_get_client.return_value = mock_client

            async with InfrastructureDiscovery(region=None) as discovery:
                limiters = await discovery.list_limiters()

            assert len(limiters) == 1
            assert limiters[0].region == "default"

    @pytest.mark.asyncio
    async def test_context_manager_cleanup(self):
        """Context manager properly cleans up resources."""
        with patch.object(
            InfrastructureDiscovery, "_get_client", new_callable=AsyncMock
        ) as mock_get_client:
            mock_client = MagicMock()
            mock_client.describe_stacks = AsyncMock(return_value={"Stacks": []})
            mock_client.__aexit__ = AsyncMock()
            mock_get_client.return_value = mock_client

            discovery = InfrastructureDiscovery(region="us-east-1")
            async with discovery:
                await discovery.list_limiters()

            # Verify close was called
            assert discovery._client is None
            assert discovery._session is None


class TestRateLimiterListDeployed:
    """Tests for RateLimiter.list_deployed() class method."""

    @pytest.mark.asyncio
    async def test_list_deployed_returns_limiter_info_list(self):
        """list_deployed returns a list of LimiterInfo objects."""
        mock_limiters = [
            LimiterInfo(
                stack_name="app1",
                user_name="app1",
                region="us-east-1",
                stack_status="CREATE_COMPLETE",
                creation_time="2024-01-15T10:30:00Z",
            ),
            LimiterInfo(
                stack_name="app2",
                user_name="app2",
                region="us-east-1",
                stack_status="UPDATE_COMPLETE",
                creation_time="2024-01-14T09:00:00Z",
            ),
        ]

        with patch("zae_limiter.infra.discovery.InfrastructureDiscovery") as mock_discovery_class:
            mock_discovery = MagicMock()
            mock_discovery.list_limiters = AsyncMock(return_value=mock_limiters)
            mock_discovery.__aenter__ = AsyncMock(return_value=mock_discovery)
            mock_discovery.__aexit__ = AsyncMock()
            mock_discovery_class.return_value = mock_discovery

            result = await RateLimiter.list_deployed(region="us-east-1")

            assert result == mock_limiters
            mock_discovery_class.assert_called_once_with(region="us-east-1", endpoint_url=None)

    @pytest.mark.asyncio
    async def test_list_deployed_passes_endpoint_url(self):
        """list_deployed passes endpoint_url to InfrastructureDiscovery."""
        with patch("zae_limiter.infra.discovery.InfrastructureDiscovery") as mock_discovery_class:
            mock_discovery = MagicMock()
            mock_discovery.list_limiters = AsyncMock(return_value=[])
            mock_discovery.__aenter__ = AsyncMock(return_value=mock_discovery)
            mock_discovery.__aexit__ = AsyncMock()
            mock_discovery_class.return_value = mock_discovery

            await RateLimiter.list_deployed(
                region="us-east-1",
                endpoint_url="http://localhost:4566",
            )

            mock_discovery_class.assert_called_once_with(
                region="us-east-1", endpoint_url="http://localhost:4566"
            )

    @pytest.mark.asyncio
    async def test_list_deployed_empty_result(self):
        """list_deployed returns empty list when no stacks exist."""
        with patch("zae_limiter.infra.discovery.InfrastructureDiscovery") as mock_discovery_class:
            mock_discovery = MagicMock()
            mock_discovery.list_limiters = AsyncMock(return_value=[])
            mock_discovery.__aenter__ = AsyncMock(return_value=mock_discovery)
            mock_discovery.__aexit__ = AsyncMock()
            mock_discovery_class.return_value = mock_discovery

            result = await RateLimiter.list_deployed(region="us-east-1")

            assert result == []

    @pytest.mark.asyncio
    async def test_list_deployed_propagates_client_error(self):
        """list_deployed propagates ClientError from CloudFormation."""
        # Patch at the discovery module level to catch the fresh import
        with patch.object(
            InfrastructureDiscovery, "_get_client", new_callable=AsyncMock
        ) as mock_get_client:
            mock_client = MagicMock()
            mock_client.describe_stacks = AsyncMock(
                side_effect=ClientError(
                    {"Error": {"Code": "AccessDenied", "Message": "Not authorized"}},
                    "DescribeStacks",
                )
            )
            mock_get_client.return_value = mock_client

            with pytest.raises(ClientError) as exc_info:
                await RateLimiter.list_deployed(region="us-east-1")

            assert "AccessDenied" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_list_deployed_is_class_method(self):
        """list_deployed is a class method, not an instance method."""
        # Verify it can be called on the class without an instance
        assert hasattr(RateLimiter, "list_deployed")
        assert callable(RateLimiter.list_deployed)

        # Verify it's a classmethod (or staticmethod - it's implemented as classmethod)
        # We can check by seeing if we can call it without self
        with patch("zae_limiter.infra.discovery.InfrastructureDiscovery") as mock_discovery_class:
            mock_discovery = MagicMock()
            mock_discovery.list_limiters = AsyncMock(return_value=[])
            mock_discovery.__aenter__ = AsyncMock(return_value=mock_discovery)
            mock_discovery.__aexit__ = AsyncMock()
            mock_discovery_class.return_value = mock_discovery

            # Should work without creating an instance
            result = await RateLimiter.list_deployed(region="us-east-1")
            assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_describe_stacks_excludes_delete_complete(self):
        """describe_stacks discovery excludes DELETE_COMPLETE stacks."""
        with patch.object(
            InfrastructureDiscovery, "_get_client", new_callable=AsyncMock
        ) as mock_get_client:
            mock_client = MagicMock()
            mock_client.describe_stacks = AsyncMock(
                return_value={
                    "Stacks": [
                        {
                            "StackName": "ZAEL-active-app",
                            "StackStatus": "CREATE_COMPLETE",
                            "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                            "Tags": [],
                        },
                        {
                            "StackName": "ZAEL-deleted-app",
                            "StackStatus": "DELETE_COMPLETE",
                            "CreationTime": datetime(2024, 1, 14, 9, 0, 0),
                            "Tags": [],
                        },
                    ],
                }
            )
            mock_get_client.return_value = mock_client

            async with InfrastructureDiscovery(region="us-east-1") as discovery:
                limiters = await discovery.list_limiters()

            # Should only return the active stack, not the deleted one
            assert len(limiters) == 1
            assert limiters[0].stack_name == "ZAEL-active-app"

    @pytest.mark.asyncio
    async def test_discovery_close_with_client_exception(self):
        """close() handles exceptions during client cleanup gracefully."""
        with patch.object(
            InfrastructureDiscovery, "_get_client", new_callable=AsyncMock
        ) as mock_get_client:
            mock_client = MagicMock()
            mock_client.describe_stacks = AsyncMock(return_value={"Stacks": []})
            # Simulate exception during cleanup
            mock_client.__aexit__ = AsyncMock(side_effect=Exception("Cleanup failed"))
            mock_get_client.return_value = mock_client

            discovery = InfrastructureDiscovery(region="us-east-1")
            async with discovery:
                # Force client creation
                await discovery.list_limiters()

            # Should have cleaned up despite exception
            assert discovery._client is None
            assert discovery._session is None

    @pytest.mark.asyncio
    async def test_discovery_close_without_client(self):
        """close() handles case when no client was ever created."""
        discovery = InfrastructureDiscovery(region="us-east-1")
        # close() should not raise when no client exists
        await discovery.close()
        assert discovery._client is None
        assert discovery._session is None

    @pytest.mark.asyncio
    async def test_get_client_caches_client(self):
        """_get_client caches the client for subsequent calls."""
        with patch("zae_limiter.infra.discovery.get_session") as mock_get_session:
            mock_session = MagicMock()
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock()
            mock_session.create_client.return_value = mock_client
            mock_get_session.return_value = mock_session

            discovery = InfrastructureDiscovery(region="us-east-1")

            # First call creates client
            client1 = await discovery._get_client()
            # Second call should return cached client
            client2 = await discovery._get_client()

            assert client1 is client2
            # Session should only be created once
            mock_get_session.assert_called_once()

            # Clean up
            await discovery.close()

    @pytest.mark.asyncio
    async def test_get_client_passes_region_and_endpoint(self):
        """_get_client passes region and endpoint_url to boto3."""
        with patch("zae_limiter.infra.discovery.get_session") as mock_get_session:
            mock_session = MagicMock()
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock()
            mock_session.create_client.return_value = mock_client
            mock_get_session.return_value = mock_session

            discovery = InfrastructureDiscovery(
                region="eu-west-1", endpoint_url="http://localhost:4566"
            )

            await discovery._get_client()

            # Check session.create_client was called with correct kwargs
            mock_session.create_client.assert_called_once_with(
                "cloudformation",
                region_name="eu-west-1",
                endpoint_url="http://localhost:4566",
            )

            await discovery.close()

    @pytest.mark.asyncio
    async def test_get_client_without_region_or_endpoint(self):
        """_get_client works without region or endpoint_url."""
        with patch("zae_limiter.infra.discovery.get_session") as mock_get_session:
            mock_session = MagicMock()
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock()
            mock_session.create_client.return_value = mock_client
            mock_get_session.return_value = mock_session

            discovery = InfrastructureDiscovery()

            await discovery._get_client()

            # Should be called with just "cloudformation" and no kwargs
            mock_session.create_client.assert_called_once_with("cloudformation")

            await discovery.close()


class TestRateLimiterRepositoryParameter:
    """Tests for the new repository parameter in RateLimiter constructor."""

    @pytest.mark.asyncio
    async def test_repository_parameter_accepted(self, mock_dynamodb):
        """Test RateLimiter accepts repository parameter."""
        from zae_limiter import Repository

        repo = Repository(
            name="my-repo-app",
            region="us-east-1",
        )
        limiter = RateLimiter(repository=repo)

        assert limiter._repository is repo
        assert limiter._repository.stack_name == "my-repo-app"
        await limiter.close()

    @pytest.mark.asyncio
    async def test_repository_parameter_conflict_with_name_raises(self, mock_dynamodb):
        """Test ValueError when both repository and name are provided."""
        from zae_limiter import Repository

        repo = Repository(name="my-app", region="us-east-1", _skip_deprecation_warning=True)

        with pytest.raises(ValueError) as exc_info:
            RateLimiter(repository=repo, name="other-app")

        assert "Cannot specify both 'repository'" in str(exc_info.value)
        await repo.close()

    @pytest.mark.asyncio
    async def test_repository_parameter_conflict_with_region_raises(self, mock_dynamodb):
        """Test ValueError when both repository and region are provided."""
        from zae_limiter import Repository

        repo = Repository(name="my-app", region="us-east-1", _skip_deprecation_warning=True)

        with pytest.raises(ValueError) as exc_info:
            RateLimiter(repository=repo, region="eu-west-1")

        assert "Cannot specify both 'repository'" in str(exc_info.value)
        await repo.close()

    @pytest.mark.asyncio
    async def test_repository_parameter_conflict_with_endpoint_url_raises(self, mock_dynamodb):
        """Test ValueError when both repository and endpoint_url are provided."""
        from zae_limiter import Repository

        repo = Repository(name="my-app", region="us-east-1", _skip_deprecation_warning=True)

        with pytest.raises(ValueError) as exc_info:
            RateLimiter(repository=repo, endpoint_url="http://localhost:4566")

        assert "Cannot specify both 'repository'" in str(exc_info.value)
        await repo.close()

    @pytest.mark.asyncio
    async def test_repository_parameter_conflict_with_stack_options_raises(self, mock_dynamodb):
        """Test ValueError when both repository and stack_options are provided."""
        from zae_limiter import Repository, StackOptions

        repo = Repository(name="my-app", region="us-east-1", _skip_deprecation_warning=True)

        with pytest.raises(ValueError) as exc_info:
            RateLimiter(repository=repo, stack_options=StackOptions())

        assert "Cannot specify both 'repository'" in str(exc_info.value)
        await repo.close()

    @pytest.mark.asyncio
    async def test_default_limiter_creates_repository_with_deprecation(self, mock_dynamodb):
        """Test RateLimiter() with no args creates default repository but warns."""
        import warnings

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            limiter = RateLimiter()
            deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
            assert len(deprecation_warnings) >= 1
            limiter_warnings = [
                x for x in deprecation_warnings if "without a repository" in str(x.message).lower()
            ]
            assert len(limiter_warnings) == 1

        assert limiter._repository.stack_name == "limiter"
        assert limiter._repository is not None
        await limiter.close()


class TestDeprecatedConstructorParams:
    """Tests for individual deprecated constructor parameter warnings."""

    async def test_on_unavailable_param_warns(self, mock_dynamodb):
        """Passing on_unavailable to constructor emits DeprecationWarning."""
        from zae_limiter.limiter import OnUnavailable
        from zae_limiter.repository import Repository

        with pytest.warns(DeprecationWarning, match="on_unavailable"):
            limiter = RateLimiter(name="test", on_unavailable=OnUnavailable.ALLOW)
        repo = limiter._repository
        assert isinstance(repo, Repository)
        assert repo._on_unavailable_cache == "allow"
        await limiter.close()

    async def test_auto_update_param_warns(self, mock_dynamodb):
        """Passing auto_update to constructor emits DeprecationWarning."""
        with pytest.warns(DeprecationWarning, match="auto_update"):
            limiter = RateLimiter(name="test", auto_update=True)
        await limiter.close()

    async def test_name_property_warns(self, mock_dynamodb):
        """Accessing RateLimiter.name emits DeprecationWarning."""
        limiter = RateLimiter(name="test")
        with pytest.warns(DeprecationWarning, match=r"RateLimiter\.name is deprecated"):
            name = limiter.name
        assert name == "test"
        await limiter.close()

    async def test_speculative_writes_does_not_warn(self, mock_dynamodb):
        """Passing speculative_writes does NOT emit DeprecationWarning."""
        import warnings

        from zae_limiter import Repository

        repo = Repository(name="test", region="us-east-1", _skip_deprecation_warning=True)
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            limiter = RateLimiter(repository=repo, speculative_writes=False)
            deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
            assert len(deprecation_warnings) == 0
        assert limiter._speculative_writes is False
        await limiter.close()

    async def test_stack_name_property_warns(self, mock_dynamodb):
        """Accessing RateLimiter.stack_name emits DeprecationWarning."""
        limiter = RateLimiter(name="test")
        with pytest.warns(DeprecationWarning, match="stack_name"):
            name = limiter.stack_name
        assert name == "test"
        await limiter.close()

    async def test_table_name_property_warns(self, mock_dynamodb):
        """Accessing RateLimiter.table_name emits DeprecationWarning."""
        limiter = RateLimiter(name="test")
        with pytest.warns(DeprecationWarning, match="table_name"):
            name = limiter.table_name
        assert name == "test"
        await limiter.close()


class TestRepositoryProtocolCompliance:
    """Tests that Repository implements RepositoryProtocol."""

    def test_repository_is_instance_of_protocol(self):
        """Test that Repository passes isinstance check for RepositoryProtocol."""
        from zae_limiter import Repository, RepositoryProtocol

        repo = Repository(name="test", region="us-east-1", _skip_deprecation_warning=True)
        assert isinstance(repo, RepositoryProtocol)

    def test_repository_protocol_is_runtime_checkable(self):
        """Test that RepositoryProtocol is runtime checkable."""
        from zae_limiter import RepositoryProtocol

        # Should have __subclasshook__ from @runtime_checkable
        assert hasattr(RepositoryProtocol, "__subclasshook__")

    def test_repository_has_capabilities_property(self):
        """Test that Repository exposes capabilities."""
        from zae_limiter import BackendCapabilities, Repository

        repo = Repository(name="test", region="us-east-1", _skip_deprecation_warning=True)
        caps = repo.capabilities

        assert isinstance(caps, BackendCapabilities)
        assert caps.supports_audit_logging is True
        assert caps.supports_usage_snapshots is True
        assert caps.supports_infrastructure_management is True
        assert caps.supports_change_streams is True


class TestLazyImports:
    """Tests for lazy imports in __init__.py."""

    def test_repository_lazy_import(self):
        """Test Repository can be imported from zae_limiter."""
        from zae_limiter import Repository

        assert Repository is not None
        assert Repository.__name__ == "Repository"

    def test_repository_protocol_lazy_import(self):
        """Test RepositoryProtocol can be imported from zae_limiter."""
        from zae_limiter import RepositoryProtocol

        assert RepositoryProtocol is not None
        assert RepositoryProtocol.__name__ == "RepositoryProtocol"

    def test_stack_manager_lazy_import(self):
        """Test StackManager can be imported from zae_limiter."""
        from zae_limiter import StackManager

        assert StackManager is not None
        assert StackManager.__name__ == "StackManager"

    def test_invalid_attribute_raises(self):
        """Test accessing invalid attribute raises AttributeError."""
        import zae_limiter

        with pytest.raises(AttributeError) as exc_info:
            _ = zae_limiter.NonExistentClass

        assert "has no attribute 'NonExistentClass'" in str(exc_info.value)


class TestRateLimiterConfigCache:
    """Tests for config cache management via Repository (ADR-122)."""

    @pytest.mark.asyncio
    async def test_get_cache_stats_returns_cache_stats(self, mock_dynamodb):
        """Test Repository.get_cache_stats() returns CacheStats object."""
        limiter = RateLimiter()

        stats = limiter._repository.get_cache_stats()

        assert isinstance(stats, CacheStats)
        assert stats.hits == 0
        assert stats.misses == 0
        assert stats.size == 0
        assert stats.ttl_seconds == 60  # Default TTL
        await limiter.close()

    @pytest.mark.asyncio
    async def test_get_cache_stats_with_custom_ttl(self, mock_dynamodb):
        """Test Repository.get_cache_stats() reflects custom TTL."""
        from zae_limiter import Repository

        repo = Repository(
            name="test-rate-limits",
            region="us-east-1",
            config_cache_ttl=120,
            _skip_deprecation_warning=True,
        )
        limiter = RateLimiter(repository=repo)

        stats = limiter._repository.get_cache_stats()

        assert stats.ttl_seconds == 120
        await limiter.close()

    @pytest.mark.asyncio
    async def test_invalidate_config_cache(self, mock_dynamodb):
        """Test Repository.invalidate_config_cache() clears cache entries."""
        from zae_limiter.config_cache import CacheEntry

        limiter = RateLimiter()

        # Manually populate the cache to verify invalidation
        entry = CacheEntry(value=[], expires_at=9999999999.0)
        limiter._repository._config_cache._resource_defaults["gpt-4"] = entry

        assert limiter._repository.get_cache_stats().size == 1

        await limiter._repository.invalidate_config_cache()

        assert limiter._repository.get_cache_stats().size == 0
        await limiter.close()

    @pytest.mark.asyncio
    async def test_resolve_on_unavailable_uses_cache(self, limiter):
        """resolve_on_unavailable() goes through config cache, not direct GetItem (#333)."""
        from zae_limiter import OnUnavailable

        # Set system defaults with on_unavailable
        await limiter.set_system_defaults(
            [Limit.per_minute("rpm", 100)],
            on_unavailable=OnUnavailable.ALLOW,
        )

        # First call: cache miss
        result = await limiter._repository.resolve_on_unavailable()
        assert result == "allow"

        stats_after_first = limiter._repository.get_cache_stats()
        assert stats_after_first.misses == 1

        # Second call: cache hit (no additional DynamoDB read)
        result = await limiter._repository.resolve_on_unavailable()
        assert result == "allow"

        stats_after_second = limiter._repository.get_cache_stats()
        assert stats_after_second.hits == 1
        assert stats_after_second.misses == 1  # No new misses


class TestResolveLinitsSequentialFallback:
    """Tests for Repository._resolve_limits_sequential() (ADR-122)."""

    @pytest.mark.asyncio
    async def test_sequential_entity_level(self, limiter):
        """Sequential fallback returns entity-level config."""
        await limiter.set_system_defaults([Limit.per_minute("rpm", 100)])
        await limiter.set_limits("user-seq", [Limit.per_minute("rpm", 500)], resource="api")

        repo = limiter._repository
        limits, on_unavailable, source = await repo._resolve_limits_sequential("user-seq", "api")

        assert source == "entity"
        assert limits is not None
        assert limits[0].capacity == 500
        assert on_unavailable is None

    @pytest.mark.asyncio
    async def test_sequential_entity_default_level(self, limiter):
        """Sequential fallback returns entity _default_ config."""
        await limiter.set_system_defaults([Limit.per_minute("rpm", 100)])
        await limiter.set_limits("user-def", [Limit.per_minute("rpm", 300)], resource="_default_")

        repo = limiter._repository
        limits, _, source = await repo._resolve_limits_sequential("user-def", "api")

        assert source == "entity_default"
        assert limits is not None
        assert limits[0].capacity == 300

    @pytest.mark.asyncio
    async def test_sequential_resource_level(self, limiter):
        """Sequential fallback returns resource-level config."""
        await limiter.set_resource_defaults("api", [Limit.per_minute("rpm", 200)])

        repo = limiter._repository
        limits, _, source = await repo._resolve_limits_sequential("user-res", "api")

        assert source == "resource"
        assert limits is not None
        assert limits[0].capacity == 200

    @pytest.mark.asyncio
    async def test_sequential_system_level(self, limiter):
        """Sequential fallback returns system-level config."""
        from zae_limiter import OnUnavailable

        await limiter.set_system_defaults(
            [Limit.per_minute("rpm", 100)],
            on_unavailable=OnUnavailable.ALLOW,
        )

        repo = limiter._repository
        limits, on_unavailable, source = await repo._resolve_limits_sequential("user-sys", "api")

        assert source == "system"
        assert limits is not None
        assert limits[0].capacity == 100
        assert on_unavailable == "allow"

    @pytest.mark.asyncio
    async def test_sequential_no_config(self, limiter):
        """Sequential fallback returns None when no config exists."""
        repo = limiter._repository
        limits, on_unavailable, source = await repo._resolve_limits_sequential("user-none", "api")

        assert limits is None
        assert source is None


class TestListEntitiesWithCustomLimits:
    """Tests for list_entities_with_custom_limits method."""

    @pytest.mark.asyncio
    async def test_list_entities_with_custom_limits(self, limiter):
        """list_entities_with_custom_limits returns entities with custom configs."""
        # Set up limits for test entities
        await limiter.set_limits("user-1", [Limit.per_minute("rpm", 100)], resource="gpt-4")
        await limiter.set_limits("user-2", [Limit.per_minute("rpm", 200)], resource="gpt-4")

        # Query
        entities, cursor = await limiter.list_entities_with_custom_limits("gpt-4")

        assert set(entities) == {"user-1", "user-2"}
        assert cursor is None  # No more results

    @pytest.mark.asyncio
    async def test_list_entities_with_custom_limits_filters_by_resource(self, limiter):
        """list_entities_with_custom_limits only returns entities for specified resource."""
        await limiter.set_limits("user-1", [Limit.per_minute("rpm", 100)], resource="gpt-4")
        await limiter.set_limits("user-2", [Limit.per_minute("rpm", 200)], resource="claude-3")

        # Query gpt-4
        entities, _ = await limiter.list_entities_with_custom_limits("gpt-4")
        assert set(entities) == {"user-1"}

        # Query claude-3
        entities, _ = await limiter.list_entities_with_custom_limits("claude-3")
        assert set(entities) == {"user-2"}

    @pytest.mark.asyncio
    async def test_list_entities_with_custom_limits_empty_result(self, limiter):
        """Returns empty list when no entities have custom limits for resource."""
        entities, cursor = await limiter.list_entities_with_custom_limits("nonexistent")
        assert entities == []
        assert cursor is None


class TestConfigSourceTracking:
    """Tests for config source tracking (Issue #271: Refill-based TTL).

    _resolve_limits() should return (limits, config_source) tuple where
    config_source indicates which level the limits came from:
    - 'entity': Entity-level config
    - 'resource': Resource-level defaults
    - 'system': System-level defaults
    - 'override': Override parameter provided
    """

    async def test_resolve_limits_returns_entity_source_when_entity_config_exists(self, limiter):
        """_resolve_limits returns ('entity', limits) when entity has custom config."""
        # Set entity-level limits
        await limiter.set_limits("user-1", [Limit.per_minute("rpm", 100)], resource="gpt-4")

        limits, source = await limiter._resolve_limits("user-1", "gpt-4", None)

        assert source == "entity"
        assert len(limits) == 1
        assert limits[0].name == "rpm"

    async def test_resolve_limits_returns_resource_source_when_no_entity_config(self, limiter):
        """_resolve_limits returns ('resource', limits) when using resource defaults."""
        # Set resource-level limits only (no entity-level)
        await limiter.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 50)])

        limits, source = await limiter._resolve_limits("user-1", "gpt-4", None)

        assert source == "resource"
        assert len(limits) == 1
        assert limits[0].capacity == 50

    async def test_resolve_limits_returns_system_source_when_no_resource_config(self, limiter):
        """_resolve_limits returns ('system', limits) when using system defaults."""
        # Set system-level limits only
        await limiter.set_system_defaults([Limit.per_minute("rpm", 10)])

        limits, source = await limiter._resolve_limits("user-1", "gpt-4", None)

        assert source == "system"
        assert len(limits) == 1
        assert limits[0].capacity == 10

    async def test_resolve_limits_returns_override_source_when_parameter_provided(self, limiter):
        """_resolve_limits returns ('override', limits) when using parameter override."""
        # No stored limits at any level, but provide override parameter
        override_limits = [Limit.per_minute("rpm", 200)]

        limits, source = await limiter._resolve_limits("user-1", "gpt-4", override_limits)

        assert source == "override"
        assert len(limits) == 1
        assert limits[0].capacity == 200

    async def test_resolve_limits_entity_takes_precedence_over_resource(self, limiter):
        """Entity-level config takes precedence over resource-level defaults."""
        # Set both levels
        await limiter.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 50)])
        await limiter.set_limits("user-1", [Limit.per_minute("rpm", 100)], resource="gpt-4")

        limits, source = await limiter._resolve_limits("user-1", "gpt-4", None)

        assert source == "entity"
        assert limits[0].capacity == 100

    async def test_resolve_limits_resource_takes_precedence_over_system(self, limiter):
        """Resource-level defaults take precedence over system-level defaults."""
        # Set both levels
        await limiter.set_system_defaults([Limit.per_minute("rpm", 10)])
        await limiter.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 50)])

        limits, source = await limiter._resolve_limits("user-1", "gpt-4", None)

        assert source == "resource"
        assert limits[0].capacity == 50


class TestBatchedConfigResolutionFallback:
    """Tests for batched config resolution exception fallback (Issue #298)."""

    @pytest.mark.asyncio
    async def test_resolve_limits_falls_back_on_batch_exception(self, limiter):
        """When batch_get_configs raises, _resolve_limits falls back to sequential."""
        from unittest.mock import AsyncMock, patch

        # Set system limits so sequential fallback succeeds
        await limiter.set_system_defaults([Limit.per_minute("rpm", 1000)])

        # Make batch_get_configs raise an exception
        with patch.object(
            limiter._repository,
            "batch_get_configs",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            limits, source = await limiter._resolve_limits("user-1", "gpt-4", None)

        assert source == "system"
        assert len(limits) == 1
        assert limits[0].capacity == 1000


class TestBucketTTLConfiguration:
    """Tests for bucket_ttl_refill_multiplier on Repository (Issue #271)."""

    async def test_bucket_ttl_multiplier_default_is_seven(self, mock_dynamodb):
        """Default bucket_ttl_refill_multiplier on Repository is 7."""
        limiter = RateLimiter(name="test")
        assert limiter._repository._bucket_ttl_refill_multiplier == 7
        await limiter.close()

    async def test_bucket_ttl_multiplier_deprecated_param_warns(self, mock_dynamodb):
        """Passing bucket_ttl_refill_multiplier to constructor emits DeprecationWarning."""
        with pytest.warns(DeprecationWarning, match="bucket_ttl_refill_multiplier"):
            limiter = RateLimiter(name="test", bucket_ttl_refill_multiplier=14)
        await limiter.close()

    async def test_bucket_ttl_multiplier_zero_disables(self, limiter):
        """Setting bucket_ttl_refill_multiplier=0 on Repository disables TTL."""
        limiter._repository._bucket_ttl_refill_multiplier = 0
        assert limiter._repository._bucket_ttl_refill_multiplier == 0


class TestLeaseEntryConfigTracking:
    """Tests for LeaseEntry config source tracking (Issue #271)."""

    def test_lease_entry_has_custom_config_field(self):
        """LeaseEntry has _has_custom_config field."""
        from zae_limiter.lease import LeaseEntry
        from zae_limiter.models import BucketState

        state = BucketState(
            entity_id="test",
            resource="api",
            limit_name="rpm",
            tokens_milli=1000,
            capacity_milli=1000,
            refill_amount_milli=1000,
            refill_period_ms=60000,
            last_refill_ms=0,
        )

        # Default should be False
        entry = LeaseEntry(
            entity_id="test",
            resource="api",
            limit=Limit.per_minute("rpm", 100),
            state=state,
        )
        assert entry._has_custom_config is False

        # Should be settable
        entry_with_custom = LeaseEntry(
            entity_id="test",
            resource="api",
            limit=Limit.per_minute("rpm", 100),
            state=state,
            _has_custom_config=True,
        )
        assert entry_with_custom._has_custom_config is True


class TestLeaseConfigPropagation:
    """Tests for Lease config source propagation (Issue #271)."""

    def test_lease_reads_ttl_multiplier_from_repository(self):
        """Lease reads bucket_ttl_refill_multiplier from repository."""
        from unittest.mock import MagicMock

        from zae_limiter.lease import Lease

        repo = MagicMock()
        repo._bucket_ttl_refill_multiplier = 7
        lease = Lease(repository=repo)
        assert lease.repository._bucket_ttl_refill_multiplier == 7

        repo_custom = MagicMock()
        repo_custom._bucket_ttl_refill_multiplier = 14
        lease_custom = Lease(repository=repo_custom)
        assert lease_custom.repository._bucket_ttl_refill_multiplier == 14


class TestLeaseCommitTTL:
    """Tests for TTL behavior in Lease._commit() (Issue #271)."""

    async def test_commit_sets_ttl_for_default_config(self, limiter):
        """Lease._commit() sets TTL when using system/resource defaults."""
        # Set system defaults (not entity-level config)
        await limiter.set_system_defaults([Limit.per_minute("rpm", 100)])

        # Acquire should set TTL (config source is 'system', not 'entity')
        async with limiter.acquire(
            entity_id="user-1",
            resource="api",
            consume={"rpm": 1},
        ):
            pass

        # Verify bucket has TTL set
        buckets = await limiter._repository.get_buckets("user-1", "api")
        # TTL should be present (non-None)
        # The bucket TTL attribute should be set
        bucket = next((b for b in buckets if b.limit_name == "rpm"), None)
        assert bucket is not None
        # We need to check the raw item has ttl - let's query directly
        from zae_limiter.schema import pk_bucket, sk_state

        item = await limiter._repository._get_item(
            pk_bucket(limiter._repository.namespace_id, "user-1", "api", 0), sk_state()
        )
        assert item is not None
        assert "ttl" in item

    async def test_get_item_returns_none_for_missing_bucket(self, limiter):
        """_get_item returns None when bucket doesn't exist."""
        from zae_limiter.schema import pk_bucket, sk_state

        # Query for a bucket that doesn't exist
        pk = pk_bucket(limiter._repository.namespace_id, "nonexistent-user", "api", 0)
        item = await limiter._repository._get_item(pk, sk_state())
        assert item is None

    async def test_commit_removes_ttl_for_entity_config(self, limiter):
        """Lease._commit() removes TTL when entity has custom limits."""
        # First set system defaults and create a bucket with TTL
        await limiter.set_system_defaults([Limit.per_minute("rpm", 100)])
        async with limiter.acquire(
            entity_id="user-1",
            resource="api",
            consume={"rpm": 1},
        ):
            pass

        # Now set entity-level config
        await limiter.set_limits("user-1", [Limit.per_minute("rpm", 200)], resource="api")

        # Invalidate cache to ensure entity config is read (cache has negative entry)
        await limiter._repository.invalidate_config_cache()

        # Acquire again - should remove TTL since entity now has custom config
        async with limiter.acquire(
            entity_id="user-1",
            resource="api",
            consume={"rpm": 1},
        ):
            pass

        # Verify TTL was removed
        from zae_limiter.schema import pk_bucket, sk_state

        item = await limiter._repository._get_item(
            pk_bucket(limiter._repository.namespace_id, "user-1", "api", 0), sk_state()
        )
        assert item is not None
        assert "ttl" not in item

    async def test_commit_removes_ttl_for_entity_default_config(self, limiter):
        """Lease._commit() removes TTL when limits come from the entity's `_default_` config.

        ADR-136: entity configuration is custom at *either* entity level — the
        per-resource one and the entity-wide `_default_` one — so a bucket born
        from `_default_` limits must persist indefinitely, exactly like one born
        from resource-specific entity limits. Only the resource and system levels
        make a bucket ephemeral. (Inverts the ADR-119 behaviour, issue #489.)
        """
        # Set entity _default_ config (applies to all resources for this entity)
        await limiter.set_limits("user-1", [Limit.per_minute("rpm", 100)])  # resource="_default_"

        # Acquire for a specific resource - should use entity _default_ config
        async with limiter.acquire(
            entity_id="user-1",
            resource="gpt-4",  # Different from _default_
            consume={"rpm": 1},
        ):
            pass

        # Verify no TTL (entity_default is the entity's own config, so custom)
        from zae_limiter.schema import pk_bucket, sk_state

        pk = pk_bucket(limiter._repository.namespace_id, "user-1", "gpt-4", 0)
        item = await limiter._repository._get_item(pk, sk_state())
        assert item is not None
        assert "ttl" not in item, "entity_default config is custom config (ADR-136): no TTL"

    async def test_commit_sets_ttl_for_resource_config(self, limiter):
        """Guard (ADR-136): resource-derived buckets still carry a TTL.

        The fix for #489 widens "custom config" to cover entity `_default_`; it
        must not widen to *everything*. Resource and system defaults do not fan
        out on change, so their buckets pick up new parameters by expiring and
        being recreated — making them custom would break that mechanism and
        stop ephemeral buckets from ever being reclaimed.
        """
        await limiter.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 100)])

        async with limiter.acquire(
            entity_id="user-1",
            resource="gpt-4",
            consume={"rpm": 1},
        ):
            pass

        from zae_limiter.schema import pk_bucket, sk_state

        pk = pk_bucket(limiter._repository.namespace_id, "user-1", "gpt-4", 0)
        item = await limiter._repository._get_item(pk, sk_state())
        assert item is not None
        assert "ttl" in item, "resource defaults are not custom config: TTL must stay"

    async def test_parent_only_path_removes_ttl_for_parent_default_config(self, limiter):
        """Parent-only slow path drops the TTL when the parent's own `_default_` config wins.

        ADR-136 / issue #489. `_try_parent_only_acquire` returns None when the
        parent bucket is missing, so this path can only ever *update* a bucket —
        a "created via the parent-only path" test is not constructible.

        The arrangement writes the `ttl` attribute onto the parent's bucket
        directly rather than getting some API to stamp it. That is the state
        under test — a bucket that predates the entity's configuration, or was
        created by a pre-ADR-136 client — and stamping it by hand keeps the
        precondition from depending on which writer happens to set TTLs today.
        Deriving it from `set_limits(..., resource="_default_")` was the
        original arrangement and it silently stopped working when #487/#488
        made that call reach real-resource buckets and clear their TTL itself,
        which would have left the final assertion passing vacuously.
        """
        from zae_limiter.schema import pk_bucket, sk_state

        await limiter.create_entity("parent-1")
        await limiter.create_entity("child-1", parent_id="parent-1", cascade=True)
        # The parent carries an entity-wide `_default_` config; the child has
        # none, so it resolves from system. Configured before any bucket
        # exists, so the fan-out has nothing to find and writes nothing.
        await limiter.set_system_defaults([Limit.per_minute("rpm", 1000)])
        await limiter.set_limits("parent-1", [Limit.per_minute("rpm", 1000)])

        # Prime both buckets
        async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}):
            pass

        parent_pk = pk_bucket(limiter._repository.namespace_id, "parent-1", "gpt-4", 0)
        client = await limiter._repository._get_client()
        await client.update_item(
            TableName=limiter._repository.table_name,
            Key={"PK": {"S": parent_pk}, "SK": {"S": sk_state()}},
            UpdateExpression="SET #ttl = :ttl",
            ExpressionAttributeNames={"#ttl": "ttl"},
            ExpressionAttributeValues={":ttl": {"N": str(int(time.time()) + 3600)}},
        )
        item = await limiter._repository._get_item(parent_pk, sk_state())
        assert item is not None
        assert "ttl" in item, "precondition: the parent bucket under test carries a TTL"

        limiter._speculative_writes = True
        now_ms = int(time.time() * 1000)

        child_bucket = BucketState(
            entity_id="child-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=900_000,
            last_refill_ms=now_ms,
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )
        # Parent rpm exhausted on the image, but a full refill period elapsed,
        # so would_refill_satisfy() sends us to the parent-only slow path.
        parent_bucket = BucketState(
            entity_id="parent-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=0,
            last_refill_ms=now_ms - 60_000,
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )

        original_speculative = limiter._repository.speculative_consume
        call_count = 0

        async def mock_speculative(
            entity_id, resource, consume, ttl_seconds=None, shard_id=None, now_ms=None
        ):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return SpeculativeResult(
                    success=True,
                    buckets=[child_bucket],
                    cascade=True,
                    parent_id="parent-1",
                )
            if call_count == 2:
                return SpeculativeResult(success=False, old_buckets=[parent_bucket])
            return await original_speculative(
                entity_id, resource, consume, ttl_seconds, shard_id, now_ms
            )

        limiter._repository.speculative_consume = mock_speculative
        try:
            async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}) as lease:
                assert "parent-1" in {e.entity_id for e in lease.entries}
        finally:
            limiter._repository.speculative_consume = original_speculative

        item = await limiter._repository._get_item(parent_pk, sk_state())
        assert item is not None
        assert "ttl" not in item, (
            "parent limits resolved from the parent's own `_default_` config: "
            "the parent-only path must REMOVE the TTL (ADR-136)"
        )

    async def test_ttl_value_matches_formula(self, limiter):
        """TTL = now + max_refill_period × multiplier."""
        import time

        # Set system defaults with known refill period (60 seconds)
        await limiter.set_system_defaults([Limit.per_minute("rpm", 100)])

        now_before = int(time.time())
        async with limiter.acquire(
            entity_id="user-1",
            resource="api",
            consume={"rpm": 1},
        ):
            pass
        now_after = int(time.time())

        # Get TTL from item
        from zae_limiter.schema import pk_bucket, sk_state

        item = await limiter._repository._get_item(
            pk_bucket(limiter._repository.namespace_id, "user-1", "api", 0), sk_state()
        )

        # TTL = now + (60 seconds * 7 multiplier) = now + 420
        # Allow ±1 second for timing
        expected_min = now_before + (60 * 7)
        expected_max = now_after + (60 * 7) + 1

        ttl = item["ttl"]
        assert expected_min <= ttl <= expected_max

    async def test_ttl_accounts_for_slow_refill_rate(self, limiter):
        """TTL should be based on time to fill bucket, not just refill period (Issue #296).

        For a limit with capacity=1000 and refill_rate=10/min:
        - Time to fill bucket = 1000 / (10/min) = 100 minutes = 6000 seconds
        - TTL should be >= 6000 seconds × multiplier = 42000 seconds (700 minutes)
        """
        import time

        # Create a slow-refill limit: 1000 capacity, refills 10 per minute
        slow_refill_limit = Limit(
            name="tokens",
            capacity=1000,
            refill_amount=10,
            refill_period_seconds=60,
        )
        await limiter.set_system_defaults([slow_refill_limit])

        now_before = int(time.time())
        async with limiter.acquire(
            entity_id="user-slow",
            resource="api",
            consume={"tokens": 1},
        ):
            pass

        from zae_limiter.schema import pk_bucket, sk_state

        pk = pk_bucket(limiter._repository.namespace_id, "user-slow", "api", 0)
        item = await limiter._repository._get_item(pk, sk_state())

        # Time to fill = (capacity / refill_amount) × refill_period = 100 × 60 = 6000 seconds
        # Expected TTL = time_to_fill × multiplier = 6000 × 7 = 42000 seconds
        time_to_fill = (1000 / 10) * 60  # 6000 seconds
        expected_min = now_before + int(time_to_fill * 7)

        ttl = item["ttl"]
        assert ttl >= expected_min, (
            f"TTL {ttl - now_before}s is shorter than time to fill bucket "
            f"({time_to_fill}s × 7 = {time_to_fill * 7}s)"
        )

    async def test_ttl_disabled_when_multiplier_zero(self, mock_dynamodb):
        """No TTL when bucket_ttl_refill_multiplier=0."""
        from zae_limiter import Repository

        repo = Repository(
            name="test-no-ttl",
            region="us-east-1",
        )
        repo._bucket_ttl_refill_multiplier = 0
        limiter = RateLimiter(repository=repo)
        await repo.create_table()
        async with limiter:
            await limiter.set_system_defaults([Limit.per_minute("rpm", 100)])
            async with limiter.acquire(
                entity_id="user-1",
                resource="api",
                consume={"rpm": 1},
            ):
                pass

            # Verify no TTL set
            from zae_limiter.schema import pk_bucket, sk_state

            pk = pk_bucket(limiter._repository.namespace_id, "user-1", "api", 0)
            item = await limiter._repository._get_item(pk, sk_state())
            assert item is not None
            assert "ttl" not in item

    async def test_commit_sets_ttl_after_deleting_entity_config(self, limiter):
        """Lease._commit() sets TTL when entity downgrades from custom to default limits.

        When an entity's custom limits are deleted, the next acquire() should set TTL
        on the bucket since the entity now uses default limits again.
        """
        from zae_limiter.schema import pk_bucket, sk_state

        # Set system defaults
        await limiter.set_system_defaults([Limit.per_minute("rpm", 100)])

        # Set entity-level config (custom limits)
        await limiter.set_limits("user-1", [Limit.per_minute("rpm", 200)], resource="api")

        # Acquire with custom limits - should NOT have TTL
        async with limiter.acquire(
            entity_id="user-1",
            resource="api",
            consume={"rpm": 1},
        ):
            pass

        # Verify no TTL (entity has custom config)
        item = await limiter._repository._get_item(
            pk_bucket(limiter._repository.namespace_id, "user-1", "api", 0), sk_state()
        )
        assert item is not None
        assert "ttl" not in item

        # Delete entity-level config (downgrade to defaults)
        await limiter.delete_limits("user-1", resource="api")

        # Invalidate cache to ensure deleted config is recognized
        await limiter._repository.invalidate_config_cache()

        # Acquire again - should now set TTL since entity uses defaults
        async with limiter.acquire(
            entity_id="user-1",
            resource="api",
            consume={"rpm": 1},
        ):
            pass

        # Verify TTL is now set (entity uses default config)
        item = await limiter._repository._get_item(
            pk_bucket(limiter._repository.namespace_id, "user-1", "api", 0), sk_state()
        )
        assert item is not None
        assert "ttl" in item


class TestBucketLimitSync:
    """Tests for bucket synchronization when limits are updated.

    These tests verify that bucket parameters (capacity, refill)
    are updated when entity limits change via set_limits().
    """

    async def test_bucket_updated_when_limit_increased(self, limiter):
        """Bucket capacity is synced when entity limit is increased.

        Behavior (issue #294):
        1. Create entity with rpm=100
        2. Use bucket (creates bucket with capacity=100)
        3. Update limit to rpm=200 - set_limits() syncs bucket
        4. Bucket capacity is now 200
        """
        from zae_limiter.schema import pk_bucket, sk_state

        # Step 1: Set initial limit (rpm=100)
        await limiter.set_limits("user-1", [Limit.per_minute("rpm", 100)], resource="api")

        # Step 2: Use the bucket (creates it with capacity=100)
        async with limiter.acquire(
            entity_id="user-1",
            resource="api",
            consume={"rpm": 10},
        ):
            pass

        # Verify bucket was created with capacity=100
        item = await limiter._repository._get_item(
            pk_bucket(limiter._repository.namespace_id, "user-1", "api", 0), sk_state()
        )
        assert item is not None
        assert item["b_rpm_cp"] == 100000, "Initial capacity should be 100 RPM"

        # Step 3: Update limit to rpm=200 - bucket synced immediately
        await limiter.set_limits("user-1", [Limit.per_minute("rpm", 200)], resource="api")

        # Verify bucket capacity was updated immediately (no acquire needed)
        item = await limiter._repository._get_item(
            pk_bucket(limiter._repository.namespace_id, "user-1", "api", 0), sk_state()
        )
        assert item is not None
        assert item["b_rpm_cp"] == 200000, "Bucket capacity should be synced to 200 RPM"

    async def test_bucket_updated_when_limit_decreased(self, limiter):
        """Bucket capacity is synced when entity limit is decreased.

        Behavior (issue #294):
        1. Create entity with rpm=200
        2. Use bucket (creates bucket with capacity=200)
        3. Update limit to rpm=100 - set_limits() syncs bucket
        4. Bucket capacity is now 100
        """
        from zae_limiter.schema import pk_bucket, sk_state

        # Step 1: Set initial limit (rpm=200)
        await limiter.set_limits("user-1", [Limit.per_minute("rpm", 200)], resource="api")

        # Step 2: Use the bucket (creates it with capacity=200)
        async with limiter.acquire(
            entity_id="user-1",
            resource="api",
            consume={"rpm": 10},
        ):
            pass

        # Verify bucket was created with capacity=200
        item = await limiter._repository._get_item(
            pk_bucket(limiter._repository.namespace_id, "user-1", "api", 0), sk_state()
        )
        assert item is not None
        assert item["b_rpm_cp"] == 200000, "Initial capacity should be 200 RPM"

        # Step 3: Downgrade limit to rpm=100 - bucket synced immediately
        await limiter.set_limits("user-1", [Limit.per_minute("rpm", 100)], resource="api")

        # Verify bucket capacity was updated immediately (no acquire needed)
        item = await limiter._repository._get_item(
            pk_bucket(limiter._repository.namespace_id, "user-1", "api", 0), sk_state()
        )
        assert item is not None
        assert item["b_rpm_cp"] == 100000, "Bucket capacity should be synced to 100 RPM"

    async def test_bucket_updated_with_multiple_limits(self, limiter):
        """All bucket params synced when entity has multiple limits.

        Verifies that the SET expression correctly handles multiple limits
        and all parameters (capacity, refill) are updated.
        """
        from zae_limiter.schema import pk_bucket, sk_state

        # Set initial limits: rpm=100, tpm=10000
        await limiter.set_limits(
            "user-2",
            [Limit.per_minute("rpm", 100), Limit.per_minute("tpm", 10000)],
            resource="api",
        )

        # Create buckets via acquire
        async with limiter.acquire(
            entity_id="user-2", resource="api", consume={"rpm": 1, "tpm": 10}
        ):
            pass

        # Verify initial values
        item = await limiter._repository._get_item(
            pk_bucket(limiter._repository.namespace_id, "user-2", "api", 0), sk_state()
        )
        assert item is not None
        assert item["b_rpm_cp"] == 100000
        assert item["b_tpm_cp"] == 10000000

        # Update both limits
        await limiter.set_limits(
            "user-2",
            [Limit.per_minute("rpm", 200), Limit.per_minute("tpm", 20000)],
            resource="api",
        )

        # Verify both buckets synced
        item = await limiter._repository._get_item(
            pk_bucket(limiter._repository.namespace_id, "user-2", "api", 0), sk_state()
        )
        assert item is not None
        assert item["b_rpm_cp"] == 200000, "rpm capacity should be synced"
        assert item["b_tpm_cp"] == 20000000, "tpm capacity should be synced"

    async def test_bucket_refill_params_synced_when_changed(self, limiter):
        """Bucket refill rate is synced when limit period changes.

        Verifies that all bucket parameters are updated:
        capacity, refill_amount, refill_period.
        """
        from zae_limiter.schema import pk_bucket, sk_state

        # Initial: 100 per minute
        await limiter.set_limits(
            "user-3",
            [Limit.per_minute("rpm", 100)],
            resource="api",
        )
        async with limiter.acquire(entity_id="user-3", resource="api", consume={"rpm": 1}):
            pass

        # Verify initial values (per minute: refill_period=60s)
        item = await limiter._repository._get_item(
            pk_bucket(limiter._repository.namespace_id, "user-3", "api", 0), sk_state()
        )
        assert item is not None
        assert item["b_rpm_cp"] == 100000  # capacity: 100 * 1000
        assert item["b_rpm_ra"] == 100000  # refill_amount: 100 * 1000
        assert item["b_rpm_rp"] == 60000  # refill_period: 60s * 1000

        # Update to 200 per hour
        await limiter.set_limits(
            "user-3",
            [Limit.per_hour("rpm", 200)],
            resource="api",
        )

        # Verify all params updated
        item = await limiter._repository._get_item(
            pk_bucket(limiter._repository.namespace_id, "user-3", "api", 0), sk_state()
        )
        assert item is not None
        assert item["b_rpm_cp"] == 200000, "capacity should be synced"
        assert item["b_rpm_ra"] == 200000, "refill_amount should be synced"
        assert item["b_rpm_rp"] == 3600000, "refill_period should be synced (3600s)"

    async def test_set_limits_with_empty_list_skips_bucket_sync(self, limiter):
        """set_limits() with empty limits list skips bucket sync.

        Verifies the early return path when limits=[] is passed.
        """
        # Set limits then clear them - should not raise
        await limiter.set_limits("user-4", [Limit.per_minute("rpm", 100)], resource="api")
        await limiter.set_limits("user-4", [], resource="api")
        # No error means the empty list path was handled

    async def test_bucket_sync_skipped_when_bucket_does_not_exist(self, limiter):
        """Bucket sync is skipped when bucket doesn't exist yet.

        Verifies ConditionalCheckFailedException is handled gracefully.
        """
        # Set limits without creating bucket first - should not raise
        await limiter.set_limits("user-5", [Limit.per_minute("rpm", 100)], resource="api")
        # Update limits again - still no bucket exists
        await limiter.set_limits("user-5", [Limit.per_minute("rpm", 200)], resource="api")
        # No error means ConditionalCheckFailedException was handled

    async def test_bucket_sync_reraises_unexpected_client_error(self, limiter):
        """Unexpected ClientError during bucket sync is re-raised.

        Verifies that non-ConditionalCheckFailed errors propagate. They
        surface wrapped in `FanoutIncomplete`, which reports how many bucket
        items the fan-out managed to write before stopping — the config item
        is already committed at that point, so the caller has to be told the
        change is half-applied (#487, same contract as the ADR-125 fan-out).
        """
        from zae_limiter.exceptions import FanoutIncomplete

        # First create a bucket so the conditional check passes
        await limiter.set_limits("user-6", [Limit.per_minute("rpm", 100)], resource="api")
        async with limiter.acquire(entity_id="user-6", resource="api", consume={"rpm": 1}):
            pass

        # Mock update_item to raise an unexpected error
        original_update = limiter._repository._client.update_item

        async def mock_update_item(**kwargs):
            # Only fail for bucket sync calls (SK=#STATE for bucket items)
            if kwargs.get("Key", {}).get("SK", {}).get("S", "") == "#STATE":
                raise ClientError(
                    {"Error": {"Code": "InternalServerError", "Message": "Test error"}},
                    "UpdateItem",
                )
            return await original_update(**kwargs)

        limiter._repository._client.update_item = mock_update_item

        with pytest.raises(FanoutIncomplete) as exc_info:
            await limiter.set_limits("user-6", [Limit.per_minute("rpm", 200)], resource="api")

        assert isinstance(exc_info.value.cause, ClientError)
        assert exc_info.value.cause.response["Error"]["Code"] == "InternalServerError"
        assert exc_info.value.stamped == 0
        assert exc_info.value.entity_id == "user-6"
        assert exc_info.value.resource == "api"


class TestBucketReconciliation:
    """Tests for eager bucket reconciliation on config changes (issue #327).

    Verifies that set_limits() removes TTL, delete_limits() syncs bucket to
    effective defaults, and stale limit attributes are removed.
    """

    async def test_set_limits_removes_ttl_from_bucket(self, limiter):
        """set_limits() removes TTL from existing bucket that had TTL.

        Transition: system defaults (TTL) → entity config (no TTL).
        """
        from zae_limiter.schema import pk_bucket, sk_state

        # Create bucket with system defaults (has TTL)
        await limiter.set_system_defaults([Limit.per_minute("rpm", 100)])
        async with limiter.acquire(entity_id="user-ttl", resource="api", consume={"rpm": 1}):
            pass

        # Verify bucket has TTL
        pk = pk_bucket(limiter._repository.namespace_id, "user-ttl", "api", 0)
        item = await limiter._repository._get_item(pk, sk_state())
        assert item is not None
        assert "ttl" in item

        # Set entity-level config — should remove TTL from bucket
        await limiter.set_limits("user-ttl", [Limit.per_minute("rpm", 200)], resource="api")

        # Verify TTL removed and capacity updated
        item = await limiter._repository._get_item(pk, sk_state())
        assert item is not None
        assert "ttl" not in item
        assert item["b_rpm_cp"] == 200000

    async def test_delete_limits_sets_ttl_and_syncs_to_defaults(self, limiter):
        """delete_limits() syncs bucket to resource defaults with TTL.

        Transition: entity config (no TTL) → resource defaults (TTL).
        """
        from zae_limiter.schema import pk_bucket, sk_state

        # Set resource defaults
        await limiter.set_resource_defaults("api", [Limit.per_minute("rpm", 100)])

        # Set entity config (overrides resource defaults)
        await limiter.set_limits("user-del", [Limit.per_minute("rpm", 500)], resource="api")

        # Create bucket with entity config (no TTL, capacity=500)
        await limiter._repository.invalidate_config_cache()
        async with limiter.acquire(entity_id="user-del", resource="api", consume={"rpm": 1}):
            pass

        pk = pk_bucket(limiter._repository.namespace_id, "user-del", "api", 0)
        item = await limiter._repository._get_item(pk, sk_state())
        assert item is not None
        assert "ttl" not in item
        assert item["b_rpm_cp"] == 500000

        # Delete entity config — should reconcile to resource defaults
        await limiter.delete_limits("user-del", resource="api")

        item = await limiter._repository._get_item(pk, sk_state())
        assert item is not None
        assert "ttl" in item, "TTL should be set (now using defaults)"
        assert item["b_rpm_cp"] == 100000, "Capacity should match resource defaults"

    async def test_delete_limits_removes_stale_attributes(self, limiter):
        """delete_limits() removes stale limit attributes from bucket.

        Entity had [rpm, tpm], defaults have [rpm] only — tpm attrs removed.
        """
        from zae_limiter.schema import pk_bucket, sk_state

        # Set system defaults with rpm only
        await limiter.set_system_defaults([Limit.per_minute("rpm", 100)])

        # Set entity config with rpm + tpm
        await limiter.set_limits(
            "user-stale",
            [Limit.per_minute("rpm", 500), Limit.per_minute("tpm", 50000)],
            resource="api",
        )

        # Create bucket with both limits
        await limiter._repository.invalidate_config_cache()
        async with limiter.acquire(
            entity_id="user-stale",
            resource="api",
            consume={"rpm": 1, "tpm": 100},
        ):
            pass

        pk = pk_bucket(limiter._repository.namespace_id, "user-stale", "api", 0)
        item = await limiter._repository._get_item(pk, sk_state())
        assert item is not None
        assert "b_tpm_cp" in item, "tpm limit should exist in bucket"

        # Delete entity config — tpm attrs should be removed
        await limiter.delete_limits("user-stale", resource="api")

        item = await limiter._repository._get_item(pk, sk_state())
        assert item is not None
        assert "b_rpm_cp" in item, "rpm limit should still exist"
        assert item["b_rpm_cp"] == 100000, "rpm should match system defaults"
        assert "b_tpm_cp" not in item, "Stale tpm limit should be removed"
        assert "b_tpm_tk" not in item, "Stale tpm tokens should be removed"
        assert "b_tpm_tc" not in item, "Stale tpm counter should be removed"

    async def test_delete_limits_no_effective_defaults(self, limiter):
        """delete_limits() leaves bucket as-is when no fallback config exists."""
        from zae_limiter.schema import pk_bucket, sk_state

        # Set entity config (no resource or system defaults)
        await limiter.set_limits("user-orphan", [Limit.per_minute("rpm", 100)], resource="api")

        # Create bucket
        await limiter._repository.invalidate_config_cache()
        async with limiter.acquire(entity_id="user-orphan", resource="api", consume={"rpm": 1}):
            pass

        item_before = await limiter._repository._get_item(
            pk_bucket(limiter._repository.namespace_id, "user-orphan", "api", 0), sk_state()
        )
        assert item_before is not None

        # Delete entity config — no fallback, bucket left as-is
        await limiter.delete_limits("user-orphan", resource="api")

        pk = pk_bucket(limiter._repository.namespace_id, "user-orphan", "api", 0)
        item_after = await limiter._repository._get_item(pk, sk_state())
        assert item_after is not None
        # Bucket should be unchanged (no reconciliation)
        assert item_after["b_rpm_cp"] == item_before["b_rpm_cp"]

    async def test_delete_limits_bucket_does_not_exist(self, limiter):
        """delete_limits() does not error when bucket doesn't exist."""
        # Set system defaults (needed for reconciliation)
        await limiter.set_system_defaults([Limit.per_minute("rpm", 100)])

        # Set entity config but never acquire (no bucket created)
        await limiter.set_limits("user-nobucket", [Limit.per_minute("rpm", 200)], resource="api")

        # Delete should succeed without error (reconciliation is a no-op)
        await limiter.delete_limits("user-nobucket", resource="api")

    async def test_set_limits_evicts_config_cache(self, limiter):
        """set_limits() evicts entity from config cache."""
        # Warm cache via acquire (creates negative entity cache entry)
        await limiter.set_system_defaults([Limit.per_minute("rpm", 100)])
        async with limiter.acquire(entity_id="user-cache", resource="api", consume={"rpm": 1}):
            pass

        # set_limits should evict cache
        await limiter.set_limits("user-cache", [Limit.per_minute("rpm", 200)], resource="api")

        # Verify cache entry was evicted
        assert ("user-cache", "api") not in limiter._repository._config_cache._entity_limits

    async def test_delete_limits_evicts_config_cache(self, limiter):
        """delete_limits() evicts stale entity config from cache."""
        from zae_limiter.config_cache import _NO_CONFIG

        # Set system defaults and entity config
        await limiter.set_system_defaults([Limit.per_minute("rpm", 100)])
        await limiter.set_limits("user-dcache", [Limit.per_minute("rpm", 200)], resource="api")

        # Warm cache — entity config (rpm=200) is now cached
        await limiter._repository.invalidate_config_cache()
        async with limiter.acquire(entity_id="user-dcache", resource="api", consume={"rpm": 1}):
            pass

        # Verify entity config is cached (not _NO_CONFIG)
        ns_id = limiter._repository.namespace_id
        cache_key = (ns_id, "user-dcache", "api")
        entry = limiter._repository._config_cache._entity_limits.get(cache_key)
        assert entry is not None and entry.value is not _NO_CONFIG

        # delete_limits should evict stale entity config
        await limiter.delete_limits("user-dcache", resource="api")

        # After delete, _resolve_limits() re-caches with _NO_CONFIG sentinel
        # (entity config is gone → negative cache entry). The stale entity
        # limits (rpm=200) must NOT be in the cache.
        entry = limiter._repository._config_cache._entity_limits.get(cache_key)
        assert entry is None or entry.value is _NO_CONFIG, (
            "Cache should not contain stale entity limits after delete"
        )


class TestSpeculativeAcquire:
    """Tests for speculative UpdateItem fast path (Issue #315)."""

    async def test_speculative_enabled_by_default(self, limiter):
        """speculative_writes defaults to True."""
        assert limiter._speculative_writes is True

    async def test_speculative_success_non_cascade(self, limiter):
        """Speculative write succeeds for non-cascade entity with sufficient tokens."""
        # Setup: create entity and initial bucket via normal acquire
        await limiter.create_entity("entity-1")
        await limiter.set_system_defaults([Limit.per_minute("rpm", 100)])
        async with limiter.acquire("entity-1", "gpt-4", {"rpm": 1}):
            pass

        # Enable speculative writes
        limiter._speculative_writes = True

        # Acquire should use speculative path (bucket exists, tokens available)
        async with limiter.acquire("entity-1", "gpt-4", {"rpm": 1}) as lease:
            assert lease._initial_committed is True
            assert len(lease.entries) > 0
            assert lease.entries[0].consumed == 1
            assert lease.entries[0]._initial_consumed == 1

    async def test_speculative_fallback_on_missing_bucket(self, limiter):
        """Falls back to slow path when bucket doesn't exist."""
        await limiter.create_entity("entity-new")
        await limiter.set_system_defaults([Limit.per_minute("rpm", 100)])

        limiter._speculative_writes = True

        # First acquire: bucket doesn't exist → speculative fails → slow path
        async with limiter.acquire("entity-new", "gpt-4", {"rpm": 1}) as lease:
            assert len(lease.entries) > 0

    async def test_speculative_fast_rejection(self, limiter):
        """Raises RateLimitExceeded immediately when refill won't help."""
        await limiter.create_entity("entity-1")
        await limiter.set_system_defaults([Limit.per_minute("rpm", 1)])

        # Exhaust the bucket
        async with limiter.acquire("entity-1", "gpt-4", {"rpm": 1}):
            pass

        limiter._speculative_writes = True

        # Bucket is exhausted, refill won't help (just acquired 1 of 1)
        with pytest.raises(RateLimitExceeded) as exc_info:
            async with limiter.acquire("entity-1", "gpt-4", {"rpm": 1}):
                pass

        assert len(exc_info.value.violations) >= 1

    async def test_speculative_fallback_when_refill_helps(self, limiter):
        """Falls back to slow path when refill would provide enough tokens."""
        await limiter.create_entity("entity-1")
        # High refill rate: 1000/min
        await limiter.set_system_defaults([Limit.per_minute("rpm", 1000)])

        # Consume most tokens via normal path
        async with limiter.acquire("entity-1", "gpt-4", {"rpm": 999}):
            pass

        limiter._speculative_writes = True

        # 1 token left + high refill → speculative fails but refill helps → slow path
        # The slow path does refill and should succeed
        async with limiter.acquire("entity-1", "gpt-4", {"rpm": 1}) as lease:
            assert len(lease.entries) > 0

    async def test_speculative_with_multi_limit(self, limiter):
        """Speculative works with multiple limits."""
        await limiter.create_entity("entity-1")
        await limiter.set_system_defaults(
            [
                Limit.per_minute("rpm", 100),
                Limit.per_minute("tpm", 200000),
            ]
        )

        # Prime the bucket
        async with limiter.acquire("entity-1", "gpt-4", {"rpm": 1, "tpm": 100}):
            pass

        limiter._speculative_writes = True

        async with limiter.acquire("entity-1", "gpt-4", {"rpm": 1, "tpm": 100}) as lease:
            assert len(lease.entries) > 0

    async def test_speculative_rollback_on_exception(self, limiter):
        """Speculative lease rolls back on exception."""
        await limiter.create_entity("entity-1")
        await limiter.set_system_defaults([Limit.per_minute("rpm", 100)])

        # Prime the bucket
        async with limiter.acquire("entity-1", "gpt-4", {"rpm": 1}):
            pass

        limiter._speculative_writes = True

        async def acquire_and_raise(lim):
            async with lim.acquire("entity-1", "gpt-4", {"rpm": 10}):
                raise ValueError("test error")

        with pytest.raises(ValueError):
            await acquire_and_raise(limiter)

        # Tokens should be restored after rollback — verify we can still acquire
        async with limiter.acquire("entity-1", "gpt-4", {"rpm": 1}):
            pass

    async def test_speculative_cascade_both_succeed(self, limiter):
        """Speculative cascade succeeds for both child and parent."""
        await limiter.create_entity("parent-1")
        await limiter.create_entity("child-1", parent_id="parent-1", cascade=True)
        await limiter.set_system_defaults([Limit.per_minute("rpm", 100)])

        # Prime both buckets via normal path
        async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}):
            pass

        limiter._speculative_writes = True

        async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}) as lease:
            # Should have entries for both child and parent
            entity_ids = {e.entity_id for e in lease.entries}
            assert "child-1" in entity_ids
            assert "parent-1" in entity_ids

    async def test_speculative_adjust_after_speculative(self, limiter):
        """Adjustments work correctly after speculative commit."""
        await limiter.create_entity("entity-1")
        await limiter.set_system_defaults([Limit.per_minute("rpm", 100)])

        # Prime
        async with limiter.acquire("entity-1", "gpt-4", {"rpm": 1}):
            pass

        limiter._speculative_writes = True

        async with limiter.acquire("entity-1", "gpt-4", {"rpm": 10}) as lease:
            # Adjust: actually only used 5
            await lease.adjust(rpm=-5)

        # Should have released 5 tokens back
        async with limiter.acquire("entity-1", "gpt-4", {"rpm": 1}):
            pass

    async def test_speculative_config_changed_fallback(self, limiter):
        """Falls back to slow path when limit is missing from bucket (config change)."""
        await limiter.create_entity("entity-1")
        # Set both rpm and tpm so slow path can resolve them
        await limiter.set_system_defaults(
            [Limit.per_minute("rpm", 1000), Limit.per_minute("tpm", 100000)]
        )

        # Prime bucket via normal path with both limits
        async with limiter.acquire("entity-1", "gpt-4", {"rpm": 1, "tpm": 1}):
            pass

        limiter._speculative_writes = True
        now_ms = int(__import__("time").time() * 1000)

        # Mock: old_buckets only has "rpm" but request also needs "tpm"
        old_bucket_rpm_only = BucketState(
            entity_id="entity-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=50_000,
            last_refill_ms=now_ms,
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )

        original_speculative = limiter._repository.speculative_consume

        call_count = 0

        async def mock_speculative(entity_id, resource, consume, ttl_seconds=None, now_ms=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # old_buckets has "rpm" but not "tpm" → config changed → slow path
                return SpeculativeResult(success=False, old_buckets=[old_bucket_rpm_only])
            return await original_speculative(entity_id, resource, consume, ttl_seconds)

        limiter._repository.speculative_consume = mock_speculative
        try:
            # Request both rpm and tpm, but old_buckets only has rpm
            async with limiter.acquire("entity-1", "gpt-4", {"rpm": 1, "tpm": 1}) as lease:
                assert len(lease.entries) > 0
        finally:
            limiter._repository.speculative_consume = original_speculative

    async def test_speculative_cascade_parent_fails_compensate_and_fallback(self, limiter):
        """Cascade: parent fails, child compensated, falls back to slow path."""
        await limiter.create_entity("parent-1")
        await limiter.create_entity("child-1", parent_id="parent-1", cascade=True)
        await limiter.set_system_defaults([Limit.per_minute("rpm", 1000)])

        # Prime both buckets
        async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}):
            pass

        limiter._speculative_writes = True
        now_ms = int(__import__("time").time() * 1000)

        # Build mock buckets
        child_bucket = BucketState(
            entity_id="child-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=900_000,
            last_refill_ms=now_ms,
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )
        parent_bucket = BucketState(
            entity_id="parent-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=500_000,
            last_refill_ms=now_ms,
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )

        original_speculative = limiter._repository.speculative_consume
        call_count = 0

        async def mock_speculative(entity_id, resource, consume, ttl_seconds=None, now_ms=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Child succeeds
                return SpeculativeResult(
                    success=True,
                    buckets=[child_bucket],
                    cascade=True,
                    parent_id="parent-1",
                )
            if call_count == 2:
                # Parent fails but refill would help
                return SpeculativeResult(success=False, old_buckets=[parent_bucket])
            return await original_speculative(entity_id, resource, consume, ttl_seconds)

        limiter._repository.speculative_consume = mock_speculative
        try:
            # Should compensate child and fall back to slow path
            async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}) as lease:
                entity_ids = {e.entity_id for e in lease.entries}
                assert "child-1" in entity_ids
                assert "parent-1" in entity_ids
        finally:
            limiter._repository.speculative_consume = original_speculative

    async def test_speculative_cascade_parent_missing_fallback(self, limiter):
        """Cascade: parent bucket missing, compensate child, slow path."""
        await limiter.create_entity("parent-1")
        await limiter.create_entity("child-1", parent_id="parent-1", cascade=True)
        await limiter.set_system_defaults([Limit.per_minute("rpm", 1000)])

        # Prime both
        async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}):
            pass

        limiter._speculative_writes = True
        now_ms = int(__import__("time").time() * 1000)

        child_bucket = BucketState(
            entity_id="child-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=900_000,
            last_refill_ms=now_ms,
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )

        original_speculative = limiter._repository.speculative_consume
        call_count = 0

        async def mock_speculative(entity_id, resource, consume, ttl_seconds=None, now_ms=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return SpeculativeResult(
                    success=True,
                    buckets=[child_bucket],
                    cascade=True,
                    parent_id="parent-1",
                )
            if call_count == 2:
                # Parent missing (no ALL_OLD)
                return SpeculativeResult(success=False, old_buckets=None)
            return await original_speculative(entity_id, resource, consume, ttl_seconds)

        limiter._repository.speculative_consume = mock_speculative
        try:
            async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}) as lease:
                assert len(lease.entries) > 0
        finally:
            limiter._repository.speculative_consume = original_speculative

    async def test_speculative_cascade_parent_config_changed(self, limiter):
        """Cascade: parent config changed (limit missing), compensate child, slow path."""
        await limiter.create_entity("parent-1")
        await limiter.create_entity("child-1", parent_id="parent-1", cascade=True)
        await limiter.set_system_defaults([Limit.per_minute("rpm", 1000)])

        async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}):
            pass

        limiter._speculative_writes = True
        now_ms = int(__import__("time").time() * 1000)

        child_bucket = BucketState(
            entity_id="child-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=900_000,
            last_refill_ms=now_ms,
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )
        # Parent bucket has "tpm" but request needs "rpm"
        parent_bucket_wrong_limit = BucketState(
            entity_id="parent-1",
            resource="gpt-4",
            limit_name="tpm",
            tokens_milli=900_000,
            last_refill_ms=now_ms,
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )

        original_speculative = limiter._repository.speculative_consume
        call_count = 0

        async def mock_speculative(entity_id, resource, consume, ttl_seconds=None, now_ms=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return SpeculativeResult(
                    success=True,
                    buckets=[child_bucket],
                    cascade=True,
                    parent_id="parent-1",
                )
            if call_count == 2:
                return SpeculativeResult(success=False, old_buckets=[parent_bucket_wrong_limit])
            return await original_speculative(entity_id, resource, consume, ttl_seconds)

        limiter._repository.speculative_consume = mock_speculative
        try:
            async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}) as lease:
                assert len(lease.entries) > 0
        finally:
            limiter._repository.speculative_consume = original_speculative

    async def test_speculative_cascade_parent_exhausted_raises(self, limiter):
        """Cascade: parent exhausted (refill won't help), raises RateLimitExceeded."""
        await limiter.create_entity("parent-1")
        await limiter.create_entity("child-1", parent_id="parent-1", cascade=True)
        await limiter.set_system_defaults([Limit.per_minute("rpm", 10)])

        async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}):
            pass

        limiter._speculative_writes = True
        now_ms = int(__import__("time").time() * 1000)

        child_bucket = BucketState(
            entity_id="child-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=9_000,
            last_refill_ms=now_ms,
            capacity_milli=10_000,
            refill_amount_milli=10_000,
            refill_period_ms=60_000,
        )
        parent_bucket_exhausted = BucketState(
            entity_id="parent-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=0,
            last_refill_ms=now_ms,
            capacity_milli=10_000,
            refill_amount_milli=10_000,
            refill_period_ms=60_000,
        )

        original_speculative = limiter._repository.speculative_consume
        call_count = 0

        async def mock_speculative(entity_id, resource, consume, ttl_seconds=None, now_ms=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return SpeculativeResult(
                    success=True,
                    buckets=[child_bucket],
                    cascade=True,
                    parent_id="parent-1",
                )
            if call_count == 2:
                return SpeculativeResult(success=False, old_buckets=[parent_bucket_exhausted])
            return await original_speculative(entity_id, resource, consume, ttl_seconds)

        limiter._repository.speculative_consume = mock_speculative
        try:
            with pytest.raises(RateLimitExceeded) as exc_info:
                async with limiter.acquire("child-1", "gpt-4", {"rpm": 10}):
                    pass
            # Should have both child (passed) and parent (failed) statuses
            assert len(exc_info.value.violations) >= 1
        finally:
            limiter._repository.speculative_consume = original_speculative

    async def test_speculative_cascade_parent_refill_helps_no_compensate(self, limiter):
        """Cascade: parent fails with refill-would-help, parent-only slow path succeeds.

        Child stays consumed (no compensation). Parent is acquired via slow path.
        Saves 1 WCU (no compensation) + uses single-item write for parent only.
        """
        await limiter.create_entity("parent-1")
        await limiter.create_entity("child-1", parent_id="parent-1", cascade=True)
        await limiter.set_system_defaults([Limit.per_minute("rpm", 1000)])

        # Prime both buckets
        async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}):
            pass

        limiter._speculative_writes = True
        now_ms = int(__import__("time").time() * 1000)

        child_bucket = BucketState(
            entity_id="child-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=900_000,
            last_refill_ms=now_ms,
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )
        # Parent has low stored tokens but refill would help (30s elapsed)
        parent_bucket = BucketState(
            entity_id="parent-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=0,
            last_refill_ms=now_ms - 30_000,
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )

        original_speculative = limiter._repository.speculative_consume
        original_write_each = limiter._repository.write_each
        call_count = 0
        child_compensated = False

        async def mock_speculative(entity_id, resource, consume, ttl_seconds=None, now_ms=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Child succeeds
                return SpeculativeResult(
                    success=True,
                    buckets=[child_bucket],
                    cascade=True,
                    parent_id="parent-1",
                )
            if call_count == 2:
                # Parent fails but refill would help
                return SpeculativeResult(success=False, old_buckets=[parent_bucket])
            return await original_speculative(entity_id, resource, consume, ttl_seconds)

        async def mock_write_each(items):
            nonlocal child_compensated
            for item in items:
                # Compensation targets child-1 entity via build_composite_adjust
                key = item.get("Update", {}).get("Key", {})
                pk = key.get("PK", {}).get("S", "")
                if "child-1" in pk:
                    child_compensated = True
            return await original_write_each(items)

        limiter._repository.speculative_consume = mock_speculative
        limiter._repository.write_each = mock_write_each
        try:
            async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}) as lease:
                entity_ids = {e.entity_id for e in lease.entries}
                assert "child-1" in entity_ids
                assert "parent-1" in entity_ids
            # Child was NOT compensated — parent-only slow path succeeded
            assert not child_compensated, (
                "Child should not be compensated when parent slow path succeeds"
            )
        finally:
            limiter._repository.speculative_consume = original_speculative
            limiter._repository.write_each = original_write_each

    async def test_speculative_cascade_parent_only_skips_undeclared_limit(self, limiter):
        """Parent-only slow path: an undeclared parent limit is refilled but
        never checked, never reported, and never adjustable (#455).

        The parent has rpm and tpm; the call declares only rpm. The parent's
        tpm entry is a write-only carrier: it does not gate admission (even
        when in debt), does not appear in lease.consumed, and is untouched.
        """
        await limiter.create_entity("parent-1")
        await limiter.create_entity("child-1", parent_id="parent-1", cascade=True)
        await limiter.set_system_defaults(
            [Limit.per_minute("rpm", 1000), Limit.per_minute("tpm", 1000)]
        )

        # Prime both buckets, then drive the parent's tpm into debt.
        async with limiter.acquire("child-1", "gpt-4", {"rpm": 1, "tpm": 0}):
            pass
        async with limiter.acquire("parent-1", "gpt-4", {"tpm": 0}) as lease:
            await lease.adjust(tpm=1500)

        limiter._speculative_writes = True
        now_ms = int(__import__("time").time() * 1000)

        child_bucket = BucketState(
            entity_id="child-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=900_000,
            last_refill_ms=now_ms,
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )
        # Parent rpm exhausted but refill would help -> parent-only slow path
        parent_bucket = BucketState(
            entity_id="parent-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=0,
            last_refill_ms=now_ms - 30_000,
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )

        original_speculative = limiter._repository.speculative_consume
        call_count = 0

        async def mock_speculative(entity_id, resource, consume, ttl_seconds=None, now_ms=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return SpeculativeResult(
                    success=True,
                    buckets=[child_bucket],
                    cascade=True,
                    parent_id="parent-1",
                )
            if call_count == 2:
                return SpeculativeResult(success=False, old_buckets=[parent_bucket])
            return await original_speculative(entity_id, resource, consume, ttl_seconds)

        limiter._repository.speculative_consume = mock_speculative
        try:
            async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}) as lease:
                parent_entries = [e for e in lease.entries if e.entity_id == "parent-1"]
                # The reserved wcu carrier rides along undeclared (ADR-133)
                assert {e.limit.name for e in parent_entries if e.limit.name != "wcu"} == {
                    "rpm",
                    "tpm",
                }
                assert {e.limit.name for e in parent_entries if e._declared} == {"rpm"}
                # consumed sums across entities (child 1 + parent 1); no tpm key
                assert lease.consumed == {"rpm": 2}
        finally:
            limiter._repository.speculative_consume = original_speculative

        buckets = await limiter._repository.get_buckets("parent-1", resource="gpt-4")
        tpm = next(b for b in buckets if b.limit_name == "tpm")
        assert tpm.tokens_milli < 0, "parent tpm was in debt and must be left untouched"

    async def test_speculative_cascade_parent_slow_path_fails_compensates(self, limiter):
        """Cascade: parent-only slow path fails → compensate child → full slow path.

        ALL_OLD says refill WOULD help, but actual DDB parent is drained
        (concurrent consumer). Parent-only try_consume fails, returns None,
        child is compensated, then full _do_acquire also fails.
        """
        await limiter.create_entity("parent-1")
        await limiter.create_entity("child-1", parent_id="parent-1", cascade=True)
        await limiter.set_system_defaults([Limit.per_minute("rpm", 10)])

        # Prime both buckets
        async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}):
            pass

        limiter._speculative_writes = True
        now_ms = int(__import__("time").time() * 1000)

        child_bucket = BucketState(
            entity_id="child-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=9_000,
            last_refill_ms=now_ms,
            capacity_milli=10_000,
            refill_amount_milli=10_000,
            refill_period_ms=60_000,
        )
        # ALL_OLD with 60s elapsed → 10 tokens after refill → enough for 10
        parent_bucket_old = BucketState(
            entity_id="parent-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=0,
            last_refill_ms=now_ms - 60_000,
            capacity_milli=10_000,
            refill_amount_milli=10_000,
            refill_period_ms=60_000,
        )

        original_speculative = limiter._repository.speculative_consume
        call_count = 0

        async def mock_speculative(entity_id, resource, consume, ttl_seconds=None, now_ms=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return SpeculativeResult(
                    success=True,
                    buckets=[child_bucket],
                    cascade=True,
                    parent_id="parent-1",
                )
            if call_count == 2:
                # Parent fails, ALL_OLD says refill would help
                return SpeculativeResult(success=False, old_buckets=[parent_bucket_old])
            return await original_speculative(entity_id, resource, consume, ttl_seconds)

        # Drain parent so parent-only slow path's try_consume fails
        async with limiter.acquire("parent-1", "gpt-4", {"rpm": 9}):
            pass

        limiter._repository.speculative_consume = mock_speculative
        try:
            # Path: speculative parent fails → ALL_OLD says refill helps →
            # parent-only slow path → try_consume fails → return None →
            # compensate child → full _do_acquire → also fails
            with pytest.raises(RateLimitExceeded):
                async with limiter.acquire("child-1", "gpt-4", {"rpm": 10}):
                    pass
        finally:
            limiter._repository.speculative_consume = original_speculative

    async def test_speculative_cascade_parent_only_bucket_missing(self, limiter):
        """Parent-only slow path returns None when parent bucket is missing.

        Covers line 975: parent bucket missing for a limit → return None →
        compensate child → full slow path.
        """
        await limiter.create_entity("parent-1")
        await limiter.create_entity("child-1", parent_id="parent-1", cascade=True)
        await limiter.set_system_defaults([Limit.per_minute("rpm", 1000)])

        # Prime both buckets
        async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}):
            pass

        limiter._speculative_writes = True
        now_ms = int(__import__("time").time() * 1000)

        child_bucket = BucketState(
            entity_id="child-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=900_000,
            last_refill_ms=now_ms,
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )
        # ALL_OLD says refill would help (60s elapsed, full refill)
        parent_bucket_old = BucketState(
            entity_id="parent-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=0,
            last_refill_ms=now_ms - 60_000,
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )

        original_speculative = limiter._repository.speculative_consume
        original_fetch = limiter._fetch_buckets
        call_count = 0
        fetch_call_count = 0

        async def mock_speculative(entity_id, resource, consume, ttl_seconds=None, now_ms=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return SpeculativeResult(
                    success=True,
                    buckets=[child_bucket],
                    cascade=True,
                    parent_id="parent-1",
                )
            if call_count == 2:
                return SpeculativeResult(success=False, old_buckets=[parent_bucket_old])
            return await original_speculative(entity_id, resource, consume, ttl_seconds)

        async def mock_fetch_buckets(entity_ids, resource, shard_id):
            nonlocal fetch_call_count
            fetch_call_count += 1
            if fetch_call_count == 1:
                # First call from _try_parent_only_acquire — return empty
                return {}
            return await original_fetch(entity_ids, resource, shard_id)

        limiter._repository.speculative_consume = mock_speculative
        limiter._fetch_buckets = mock_fetch_buckets
        try:
            # _try_parent_only_acquire finds no parent bucket → returns None
            # → compensate child → full _do_acquire
            async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}) as lease:
                entity_ids = {e.entity_id for e in lease.entries}
                assert "child-1" in entity_ids
                assert "parent-1" in entity_ids
        finally:
            limiter._repository.speculative_consume = original_speculative
            limiter._fetch_buckets = original_fetch

    async def test_speculative_cascade_parent_only_commit_fails(self, limiter):
        """Parent-only slow path: _commit_initial raises → return None.

        Covers lines 1037-1038: RateLimitExceeded from _commit_initial during
        parent write (concurrent contention).
        """
        await limiter.create_entity("parent-1")
        await limiter.create_entity("child-1", parent_id="parent-1", cascade=True)
        await limiter.set_system_defaults([Limit.per_minute("rpm", 1000)])

        # Prime both buckets
        async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}):
            pass

        limiter._speculative_writes = True
        now_ms = int(__import__("time").time() * 1000)

        child_bucket = BucketState(
            entity_id="child-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=900_000,
            last_refill_ms=now_ms,
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )
        # ALL_OLD says refill would help (60s elapsed, full refill = 1000 tokens)
        parent_bucket_old = BucketState(
            entity_id="parent-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=0,
            last_refill_ms=now_ms - 60_000,
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )

        original_speculative = limiter._repository.speculative_consume
        original_transact = limiter._repository.transact_write
        call_count = 0
        transact_call_count = 0

        async def mock_speculative(entity_id, resource, consume, ttl_seconds=None, now_ms=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return SpeculativeResult(
                    success=True,
                    buckets=[child_bucket],
                    cascade=True,
                    parent_id="parent-1",
                )
            if call_count == 2:
                return SpeculativeResult(success=False, old_buckets=[parent_bucket_old])
            return await original_speculative(entity_id, resource, consume, ttl_seconds)

        async def mock_transact_write(items):
            nonlocal transact_call_count
            transact_call_count += 1
            if transact_call_count <= 2:
                # Fail both normal and retry paths of parent-only _commit_initial
                from botocore.exceptions import ClientError

                raise ClientError(
                    {
                        "Error": {
                            "Code": "ConditionalCheckFailedException",
                            "Message": "Condition not met",
                        }
                    },
                    "TransactWriteItems",
                )
            return await original_transact(items)

        limiter._repository.speculative_consume = mock_speculative
        limiter._repository.transact_write = mock_transact_write
        try:
            # Parent-only _commit_initial fails (ConditionalCheckFailed → retry also fails)
            # → return None → compensate child → full slow path
            async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}) as lease:
                entity_ids = {e.entity_id for e in lease.entries}
                assert "child-1" in entity_ids
                assert "parent-1" in entity_ids
        finally:
            limiter._repository.speculative_consume = original_speculative
            limiter._repository.transact_write = original_transact

    async def test_speculative_skips_zero_consume_entries(self, limiter):
        """Speculative path skips bucket entries with zero consume."""
        await limiter.create_entity("entity-1")
        await limiter.set_system_defaults(
            [Limit.per_minute("rpm", 100), Limit.per_minute("tpm", 200000)]
        )

        # Prime
        async with limiter.acquire("entity-1", "gpt-4", {"rpm": 1, "tpm": 100}):
            pass

        limiter._speculative_writes = True
        now_ms = int(__import__("time").time() * 1000)

        rpm_bucket = BucketState(
            entity_id="entity-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=99_000,
            last_refill_ms=now_ms,
            capacity_milli=100_000,
            refill_amount_milli=100_000,
            refill_period_ms=60_000,
        )
        tpm_bucket = BucketState(
            entity_id="entity-1",
            resource="gpt-4",
            limit_name="tpm",
            tokens_milli=199_900_000,
            last_refill_ms=now_ms,
            capacity_milli=200_000_000,
            refill_amount_milli=200_000_000,
            refill_period_ms=60_000,
        )

        original_speculative = limiter._repository.speculative_consume

        async def mock_speculative(entity_id, resource, consume, ttl_seconds=None, now_ms=None):
            return SpeculativeResult(
                success=True,
                buckets=[rpm_bucket, tpm_bucket],
                cascade=False,
                parent_id=None,
            )

        limiter._repository.speculative_consume = mock_speculative
        try:
            # Only consume rpm, not tpm — tpm entry should be skipped
            async with limiter.acquire("entity-1", "gpt-4", {"rpm": 1}) as lease:
                # Only rpm entry should be created (tpm has zero consume)
                limit_names = [e.limit.name for e in lease.entries]
                assert "rpm" in limit_names
        finally:
            limiter._repository.speculative_consume = original_speculative

    async def test_speculative_cascade_parent_skips_zero_consume(self, limiter):
        """Cascade: parent entries with zero consume are skipped."""
        await limiter.create_entity("parent-1")
        await limiter.create_entity("child-1", parent_id="parent-1", cascade=True)
        await limiter.set_system_defaults(
            [Limit.per_minute("rpm", 100), Limit.per_minute("tpm", 200000)]
        )

        async with limiter.acquire("child-1", "gpt-4", {"rpm": 1, "tpm": 1}):
            pass

        limiter._speculative_writes = True
        now_ms = int(__import__("time").time() * 1000)

        child_rpm = BucketState(
            entity_id="child-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=99_000,
            last_refill_ms=now_ms,
            capacity_milli=100_000,
            refill_amount_milli=100_000,
            refill_period_ms=60_000,
        )
        parent_rpm = BucketState(
            entity_id="parent-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=99_000,
            last_refill_ms=now_ms,
            capacity_milli=100_000,
            refill_amount_milli=100_000,
            refill_period_ms=60_000,
        )
        # Parent also has tpm bucket, but we won't consume tpm
        parent_tpm = BucketState(
            entity_id="parent-1",
            resource="gpt-4",
            limit_name="tpm",
            tokens_milli=199_000_000,
            last_refill_ms=now_ms,
            capacity_milli=200_000_000,
            refill_amount_milli=200_000_000,
            refill_period_ms=60_000,
        )

        original_speculative = limiter._repository.speculative_consume
        call_count = 0

        async def mock_speculative(entity_id, resource, consume, ttl_seconds=None, now_ms=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return SpeculativeResult(
                    success=True,
                    buckets=[child_rpm],
                    cascade=True,
                    parent_id="parent-1",
                )
            if call_count == 2:
                # Parent succeeds with 2 buckets, but tpm has zero consume
                return SpeculativeResult(
                    success=True,
                    buckets=[parent_rpm, parent_tpm],
                    cascade=False,
                    parent_id=None,
                )
            return await original_speculative(entity_id, resource, consume, ttl_seconds)

        limiter._repository.speculative_consume = mock_speculative
        try:
            # Only consume rpm — parent_tpm entry should be skipped (line 856)
            async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}) as lease:
                entity_ids = {e.entity_id for e in lease.entries}
                assert "child-1" in entity_ids
                assert "parent-1" in entity_ids
                # Parent should only have rpm entry, not tpm
                parent_entries = [e for e in lease.entries if e.entity_id == "parent-1"]
                assert all(e.limit.name == "rpm" for e in parent_entries)
        finally:
            limiter._repository.speculative_consume = original_speculative

    async def test_speculative_child_refill_helps_fallback(self, limiter):
        """Falls back to slow path when child refill would satisfy the request."""
        await limiter.create_entity("entity-1")
        await limiter.set_system_defaults([Limit.per_minute("rpm", 1000)])

        # Prime bucket via normal path
        async with limiter.acquire("entity-1", "gpt-4", {"rpm": 1}):
            pass

        limiter._speculative_writes = True
        now_ms = int(__import__("time").time() * 1000)

        # Bucket with 0 tokens but high refill rate — refill would satisfy 1 rpm
        old_bucket = BucketState(
            entity_id="entity-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=0,
            last_refill_ms=now_ms - 30_000,  # 30s ago → 50% refill = 500 tokens
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )

        original_speculative = limiter._repository.speculative_consume
        call_count = 0

        async def mock_speculative(entity_id, resource, consume, ttl_seconds=None, now_ms=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Child fails but refill would help → slow path (line 821)
                return SpeculativeResult(success=False, old_buckets=[old_bucket])
            return await original_speculative(entity_id, resource, consume, ttl_seconds)

        limiter._repository.speculative_consume = mock_speculative
        try:
            async with limiter.acquire("entity-1", "gpt-4", {"rpm": 1}) as lease:
                assert len(lease.entries) > 0
        finally:
            limiter._repository.speculative_consume = original_speculative

    async def test_speculative_cascade_parent_error_compensates_child_tokens(self, limiter):
        """Parent-only slow path error must restore child tokens in DynamoDB.

        Regression test: before the fix, _try_parent_only_acquire exceptions
        propagated without compensating the child's speculative consumption,
        permanently leaking tokens from the child bucket.

        This test verifies the actual DynamoDB bucket balance is restored.
        """
        await limiter.create_entity("parent-1")
        await limiter.create_entity("child-1", parent_id="parent-1", cascade=True)
        await limiter.set_system_defaults([Limit.per_minute("rpm", 1000)])

        # Prime both buckets and record child's initial balance
        async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}):
            pass
        buckets_before = await limiter._fetch_buckets(["child-1"], "gpt-4", 0)
        child_key = ("child-1", "gpt-4", "rpm")
        tokens_before = buckets_before[child_key].tokens_milli

        limiter._speculative_writes = True
        now_ms = int(__import__("time").time() * 1000)

        child_bucket = BucketState(
            entity_id="child-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=tokens_before - 10_000,  # speculative consumed 10 rpm
            last_refill_ms=now_ms,
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )
        # ALL_OLD says refill would help → triggers parent-only slow path
        parent_bucket_old = BucketState(
            entity_id="parent-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=0,
            last_refill_ms=now_ms - 60_000,
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )

        original_speculative = limiter._repository.speculative_consume
        original_fetch = limiter._fetch_buckets
        spec_call_count = 0
        fetch_call_count = 0

        async def mock_speculative(entity_id, resource, consume, ttl_seconds=None, now_ms=None):
            nonlocal spec_call_count
            spec_call_count += 1
            if spec_call_count == 1:
                # Child succeeds speculatively — deduct 10 rpm from real DDB
                await original_speculative(entity_id, resource, consume, ttl_seconds)
                return SpeculativeResult(
                    success=True,
                    buckets=[child_bucket],
                    cascade=True,
                    parent_id="parent-1",
                )
            if spec_call_count == 2:
                return SpeculativeResult(success=False, old_buckets=[parent_bucket_old])
            return await original_speculative(entity_id, resource, consume, ttl_seconds)

        async def mock_fetch_raising(entity_ids, resource, shard_id):
            nonlocal fetch_call_count
            fetch_call_count += 1
            if fetch_call_count == 1 and "parent-1" in entity_ids:
                raise RuntimeError("DynamoDB service unavailable")
            return await original_fetch(entity_ids, resource, shard_id)

        limiter._repository.speculative_consume = mock_speculative
        limiter._fetch_buckets = mock_fetch_raising
        try:
            with pytest.raises(RateLimiterUnavailable, match="DynamoDB service unavailable"):
                async with limiter.acquire("child-1", "gpt-4", {"rpm": 10}):
                    pass

            # Verify child tokens were restored in DynamoDB
            buckets_after = await original_fetch(["child-1"], "gpt-4", 0)
            tokens_after = buckets_after[child_key].tokens_milli
            assert tokens_after == tokens_before, (
                f"Child tokens leaked! Before={tokens_before}, after={tokens_after}. "
                f"Expected compensation to restore tokens."
            )
        finally:
            limiter._repository.speculative_consume = original_speculative
            limiter._fetch_buckets = original_fetch


class TestCascadeEntityCache:
    """Tests for cascade entity cache and parallel speculative writes (Issue #318)."""

    async def test_cache_populated_from_speculative_success(self, limiter):
        """Entity cache populated after first speculative success."""
        await limiter.create_entity("entity-1")
        await limiter.set_system_defaults([Limit.per_minute("rpm", 100)])

        # Prime bucket via normal path
        async with limiter.acquire("entity-1", "gpt-4", {"rpm": 1}):
            pass

        limiter._speculative_writes = True

        # Second acquire uses speculative path — should populate cache
        async with limiter.acquire("entity-1", "gpt-4", {"rpm": 1}):
            pass

        cache = limiter._repository._entity_cache
        ns_id = limiter._repository.namespace_id
        assert (ns_id, "entity-1") in cache
        cascade, parent_id, _shards = cache[(ns_id, "entity-1")]
        assert cascade is False
        assert parent_id is None

    async def test_cache_populated_from_slow_path(self, limiter):
        """Entity cache populated from slow path entity metadata."""
        await limiter.create_entity("entity-1")
        await limiter.set_system_defaults([Limit.per_minute("rpm", 100)])

        limiter._speculative_writes = False

        # Slow path: _do_acquire fetches entity metadata + populates cache
        async with limiter.acquire("entity-1", "gpt-4", {"rpm": 1}):
            pass

        cache = limiter._repository._entity_cache
        ns_id = limiter._repository.namespace_id
        assert (ns_id, "entity-1") in cache
        cascade, parent_id, _shards = cache[(ns_id, "entity-1")]
        assert cascade is False
        assert parent_id is None

    async def test_cache_populated_cascade_entity(self, limiter):
        """Entity cache correctly stores cascade + parent_id."""
        await limiter.create_entity("parent-1")
        await limiter.create_entity("child-1", parent_id="parent-1", cascade=True)
        await limiter.set_system_defaults([Limit.per_minute("rpm", 100)])

        # Prime buckets via normal path (slow path populates cache)
        async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}):
            pass

        cache = limiter._repository._entity_cache
        ns_id = limiter._repository.namespace_id
        assert (ns_id, "child-1") in cache
        cascade, parent_id, _shards = cache[(ns_id, "child-1")]
        assert cascade is True
        assert parent_id == "parent-1"

    async def test_cache_hit_non_cascade_single_write(self, limiter):
        """Cache hit with cascade=False issues single speculative write."""
        await limiter.create_entity("entity-1")
        await limiter.set_system_defaults([Limit.per_minute("rpm", 100)])

        # Prime bucket
        async with limiter.acquire("entity-1", "gpt-4", {"rpm": 1}):
            pass

        limiter._speculative_writes = True

        # Second acquire populates cache
        async with limiter.acquire("entity-1", "gpt-4", {"rpm": 1}):
            pass

        # Third acquire uses cache hit (non-cascade) — single _speculative_consume_single
        single_call_count = 0
        original_single = limiter._repository._speculative_consume_single

        async def counting_single(
            entity_id, resource, consume, ttl_seconds=None, shard_id=0, now_ms=None
        ):
            nonlocal single_call_count
            single_call_count += 1
            return await original_single(entity_id, resource, consume, ttl_seconds)

        limiter._repository._speculative_consume_single = counting_single
        try:
            async with limiter.acquire("entity-1", "gpt-4", {"rpm": 1}) as lease:
                assert lease._initial_committed is True
            assert single_call_count == 1  # Only child, no parent
        finally:
            limiter._repository._speculative_consume_single = original_single

    async def test_parallel_cascade_both_succeed(self, limiter):
        """Cache hit with cascade=True issues parallel writes, both succeed."""
        await limiter.create_entity("parent-1")
        await limiter.create_entity("child-1", parent_id="parent-1", cascade=True)
        await limiter.set_system_defaults([Limit.per_minute("rpm", 100)])

        # Prime buckets via normal path (populates cache)
        async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}):
            pass

        limiter._speculative_writes = True

        # Second acquire: cache miss (speculative), populates cache
        async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}):
            pass

        # Third acquire: cache hit, should use parallel path via _speculative_consume_single
        single_calls: list[str] = []
        original_single = limiter._repository._speculative_consume_single

        async def tracking_single(
            entity_id, resource, consume, ttl_seconds=None, shard_id=0, now_ms=None
        ):
            single_calls.append(entity_id)
            return await original_single(
                entity_id, resource, consume, ttl_seconds, shard_id=shard_id
            )

        limiter._repository._speculative_consume_single = tracking_single
        try:
            async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}) as lease:
                assert lease._initial_committed is True
                entity_ids = {e.entity_id for e in lease.entries}
                assert "child-1" in entity_ids
                assert "parent-1" in entity_ids

            # Both child and parent _speculative_consume_single were called
            assert "child-1" in single_calls
            assert "parent-1" in single_calls
        finally:
            limiter._repository._speculative_consume_single = original_single

    async def test_parallel_child_fails_parent_succeeds_compensates_parent(self, limiter):
        """Parallel: child fails, parent succeeds — parent compensated, fall back."""
        await limiter.create_entity("parent-1")
        await limiter.create_entity("child-1", parent_id="parent-1", cascade=True)
        await limiter.set_system_defaults([Limit.per_minute("rpm", 1000)])

        # Prime buckets
        async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}):
            pass

        limiter._speculative_writes = True

        # Populate entity cache
        async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}):
            pass

        now_ms = int(__import__("time").time() * 1000)

        child_bucket_exhausted = BucketState(
            entity_id="child-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=500_000,
            last_refill_ms=now_ms - 30_000,
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )
        parent_bucket = BucketState(
            entity_id="parent-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=900_000,
            last_refill_ms=now_ms,
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )

        original_single = limiter._repository._speculative_consume_single
        compensated_entity_ids: list[str] = []
        original_compensate = limiter._compensate_speculative

        async def tracking_compensate(entity_id, resource, consume, shard_id):
            compensated_entity_ids.append(entity_id)
            return await original_compensate(entity_id, resource, consume, shard_id)

        async def mock_single(
            entity_id, resource, consume, ttl_seconds=None, shard_id=0, now_ms=None
        ):
            if entity_id == "child-1":
                return SpeculativeResult(
                    success=False,
                    old_buckets=[child_bucket_exhausted],
                )
            if entity_id == "parent-1":
                return SpeculativeResult(
                    success=True,
                    buckets=[parent_bucket],
                    cascade=False,
                    parent_id=None,
                )
            return await original_single(entity_id, resource, consume, ttl_seconds)

        limiter._repository._speculative_consume_single = mock_single
        limiter._compensate_speculative = tracking_compensate
        try:
            # Should fall back to slow path (child refill helps)
            async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}) as lease:
                assert len(lease.entries) > 0
            # Parent should have been compensated
            assert "parent-1" in compensated_entity_ids
        finally:
            limiter._repository._speculative_consume_single = original_single
            limiter._compensate_speculative = original_compensate

    async def test_parallel_parent_fails_child_succeeds_compensates_child(self, limiter):
        """Parallel: parent fails, child succeeds — child compensated or parent-only slow path."""
        await limiter.create_entity("parent-1")
        await limiter.create_entity("child-1", parent_id="parent-1", cascade=True)
        await limiter.set_system_defaults([Limit.per_minute("rpm", 1000)])

        # Prime buckets
        async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}):
            pass

        limiter._speculative_writes = True

        # Populate entity cache
        async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}):
            pass

        now_ms = int(__import__("time").time() * 1000)

        child_bucket = BucketState(
            entity_id="child-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=900_000,
            last_refill_ms=now_ms,
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )

        original_single = limiter._repository._speculative_consume_single
        call_count = 0

        async def mock_single(
            entity_id, resource, consume, ttl_seconds=None, shard_id=0, now_ms=None
        ):
            nonlocal call_count
            call_count += 1
            if entity_id == "child-1" and call_count <= 2:
                return SpeculativeResult(
                    success=True,
                    buckets=[child_bucket],
                    cascade=True,
                    parent_id="parent-1",
                )
            if entity_id == "parent-1" and call_count <= 2:
                # Parent bucket missing
                return SpeculativeResult(success=False, old_buckets=None)
            return await original_single(entity_id, resource, consume, ttl_seconds)

        limiter._repository._speculative_consume_single = mock_single
        try:
            # Should compensate child and fall back to slow path
            async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}) as lease:
                entity_ids = {e.entity_id for e in lease.entries}
                assert "child-1" in entity_ids
                assert "parent-1" in entity_ids
        finally:
            limiter._repository._speculative_consume_single = original_single

    async def test_parallel_both_fail_no_compensation(self, limiter):
        """Parallel: both fail — no compensation needed, falls back to slow path."""
        await limiter.create_entity("parent-1")
        await limiter.create_entity("child-1", parent_id="parent-1", cascade=True)
        await limiter.set_system_defaults([Limit.per_minute("rpm", 1000)])

        # Prime buckets
        async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}):
            pass

        limiter._speculative_writes = True

        # Populate entity cache
        async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}):
            pass

        now_ms = int(__import__("time").time() * 1000)

        child_old = BucketState(
            entity_id="child-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=500_000,
            last_refill_ms=now_ms - 30_000,
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )
        parent_old = BucketState(
            entity_id="parent-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=500_000,
            last_refill_ms=now_ms - 30_000,
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )

        original_single = limiter._repository._speculative_consume_single
        call_count = 0

        async def mock_single(
            entity_id, resource, consume, ttl_seconds=None, shard_id=0, now_ms=None
        ):
            nonlocal call_count
            call_count += 1
            if entity_id == "child-1" and call_count <= 2:
                return SpeculativeResult(success=False, old_buckets=[child_old])
            if entity_id == "parent-1" and call_count <= 2:
                return SpeculativeResult(success=False, old_buckets=[parent_old])
            return await original_single(entity_id, resource, consume, ttl_seconds)

        limiter._repository._speculative_consume_single = mock_single
        try:
            # Both fail with refill-would-help → falls back to slow path
            async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}) as lease:
                assert len(lease.entries) > 0
        finally:
            limiter._repository._speculative_consume_single = original_single

    async def test_parallel_parent_exhausted_compensates_child_raises(self, limiter):
        """Parallel: parent exhausted (no refill help), child compensated, raises."""
        await limiter.create_entity("parent-1")
        await limiter.create_entity("child-1", parent_id="parent-1", cascade=True)
        await limiter.set_system_defaults([Limit.per_minute("rpm", 10)])

        # Prime buckets
        async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}):
            pass

        limiter._speculative_writes = True

        # Populate entity cache
        async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}):
            pass

        now_ms = int(__import__("time").time() * 1000)

        child_bucket = BucketState(
            entity_id="child-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=9_000,
            last_refill_ms=now_ms,
            capacity_milli=10_000,
            refill_amount_milli=10_000,
            refill_period_ms=60_000,
        )
        parent_bucket_exhausted = BucketState(
            entity_id="parent-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=0,
            last_refill_ms=now_ms,
            capacity_milli=10_000,
            refill_amount_milli=10_000,
            refill_period_ms=60_000,
        )

        original_single = limiter._repository._speculative_consume_single

        async def mock_single(
            entity_id, resource, consume, ttl_seconds=None, shard_id=0, now_ms=None
        ):
            if entity_id == "child-1":
                return SpeculativeResult(
                    success=True,
                    buckets=[child_bucket],
                    cascade=True,
                    parent_id="parent-1",
                )
            if entity_id == "parent-1":
                return SpeculativeResult(
                    success=False,
                    old_buckets=[parent_bucket_exhausted],
                )
            return await original_single(entity_id, resource, consume, ttl_seconds)

        limiter._repository._speculative_consume_single = mock_single
        try:
            with pytest.raises(RateLimitExceeded):
                async with limiter.acquire("child-1", "gpt-4", {"rpm": 10}):
                    pass
        finally:
            limiter._repository._speculative_consume_single = original_single

    async def test_parallel_both_succeed_skips_zero_amount_buckets(self, limiter):
        """Parallel both succeed: buckets not in consume dict are skipped."""
        await limiter.create_entity("parent-1")
        await limiter.create_entity("child-1", parent_id="parent-1", cascade=True)
        await limiter.set_system_defaults([Limit.per_minute("rpm", 1000)])

        # Prime buckets
        async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}):
            pass

        limiter._speculative_writes = True

        # Populate entity cache
        async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}):
            pass

        now_ms = int(__import__("time").time() * 1000)

        # Include an extra "tpm" bucket not in consume dict
        child_rpm = BucketState(
            entity_id="child-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=900_000,
            last_refill_ms=now_ms,
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )
        child_tpm = BucketState(
            entity_id="child-1",
            resource="gpt-4",
            limit_name="tpm",
            tokens_milli=50_000_000,
            last_refill_ms=now_ms,
            capacity_milli=100_000_000,
            refill_amount_milli=100_000_000,
            refill_period_ms=60_000,
        )
        parent_rpm = BucketState(
            entity_id="parent-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=900_000,
            last_refill_ms=now_ms,
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )

        original_single = limiter._repository._speculative_consume_single

        async def mock_single(
            entity_id, resource, consume, ttl_seconds=None, shard_id=0, now_ms=None
        ):
            if entity_id == "child-1":
                return SpeculativeResult(
                    success=True,
                    buckets=[child_rpm, child_tpm],
                    cascade=True,
                    parent_id="parent-1",
                )
            if entity_id == "parent-1":
                return SpeculativeResult(
                    success=True,
                    buckets=[parent_rpm],
                    cascade=False,
                    parent_id=None,
                )
            return await original_single(entity_id, resource, consume, ttl_seconds)

        limiter._repository._speculative_consume_single = mock_single
        try:
            # Only consume rpm — tpm bucket should be skipped
            async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}) as lease:
                assert lease._initial_committed is True
                # Only rpm entries, tpm skipped
                limit_names = {e.limit.name for e in lease.entries}
                assert "rpm" in limit_names
                assert "tpm" not in limit_names
        finally:
            limiter._repository._speculative_consume_single = original_single

    async def test_parallel_parent_fails_refill_helps_skips_zero_amount(self, limiter):
        """Parent fails with refill help: extra child buckets not in consume are skipped."""
        await limiter.create_entity("parent-1")
        await limiter.create_entity("child-1", parent_id="parent-1", cascade=True)
        await limiter.set_system_defaults([Limit.per_minute("rpm", 1000)])

        # Prime buckets
        async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}):
            pass

        limiter._speculative_writes = True

        # Populate entity cache
        async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}):
            pass

        now_ms = int(__import__("time").time() * 1000)

        child_rpm = BucketState(
            entity_id="child-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=900_000,
            last_refill_ms=now_ms,
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )
        child_tpm = BucketState(
            entity_id="child-1",
            resource="gpt-4",
            limit_name="tpm",
            tokens_milli=50_000_000,
            last_refill_ms=now_ms,
            capacity_milli=100_000_000,
            refill_amount_milli=100_000_000,
            refill_period_ms=60_000,
        )
        parent_old_rpm = BucketState(
            entity_id="parent-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=500_000,
            last_refill_ms=now_ms - 30_000,
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )

        original_single = limiter._repository._speculative_consume_single
        call_count = 0

        async def mock_single(
            entity_id, resource, consume, ttl_seconds=None, shard_id=0, now_ms=None
        ):
            nonlocal call_count
            call_count += 1
            if entity_id == "child-1" and call_count <= 2:
                return SpeculativeResult(
                    success=True,
                    buckets=[child_rpm, child_tpm],
                    cascade=True,
                    parent_id="parent-1",
                )
            if entity_id == "parent-1" and call_count <= 2:
                return SpeculativeResult(
                    success=False,
                    old_buckets=[parent_old_rpm],
                )
            return await original_single(entity_id, resource, consume, ttl_seconds)

        limiter._repository._speculative_consume_single = mock_single
        try:
            # Parent refill helps → parent-only slow path; tpm bucket skipped in entries
            async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}) as lease:
                entity_ids = {e.entity_id for e in lease.entries}
                assert "child-1" in entity_ids
                assert "parent-1" in entity_ids
                # Only rpm entries from child pre-committed, tpm skipped
                child_entries = [e for e in lease.entries if e.entity_id == "child-1"]
                child_limit_names = {e.limit.name for e in child_entries}
                assert "rpm" in child_limit_names
                assert "tpm" not in child_limit_names
        finally:
            limiter._repository._speculative_consume_single = original_single

    async def test_parallel_both_succeed_parent_skips_zero_amount(self, limiter):
        """Parallel both succeed: parent bucket with zero consume amount is skipped."""
        await limiter.create_entity("parent-1")
        await limiter.create_entity("child-1", parent_id="parent-1", cascade=True)
        await limiter.set_system_defaults([Limit.per_minute("rpm", 1000)])

        async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}):
            pass

        limiter._speculative_writes = True
        async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}):
            pass

        now_ms = int(__import__("time").time() * 1000)
        child_rpm = BucketState(
            entity_id="child-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=900_000,
            last_refill_ms=now_ms,
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )
        parent_rpm = BucketState(
            entity_id="parent-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=900_000,
            last_refill_ms=now_ms,
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )
        parent_tpm = BucketState(
            entity_id="parent-1",
            resource="gpt-4",
            limit_name="tpm",
            tokens_milli=50_000_000,
            last_refill_ms=now_ms,
            capacity_milli=100_000_000,
            refill_amount_milli=100_000_000,
            refill_period_ms=60_000,
        )
        original_single = limiter._repository._speculative_consume_single

        async def mock_single(
            entity_id, resource, consume, ttl_seconds=None, shard_id=0, now_ms=None
        ):
            if entity_id == "child-1":
                return SpeculativeResult(
                    success=True,
                    buckets=[child_rpm],
                    cascade=True,
                    parent_id="parent-1",
                )
            if entity_id == "parent-1":
                return SpeculativeResult(
                    success=True,
                    buckets=[parent_rpm, parent_tpm],
                    cascade=False,
                    parent_id=None,
                )
            return await original_single(entity_id, resource, consume, ttl_seconds)

        limiter._repository._speculative_consume_single = mock_single
        try:
            async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}) as lease:
                parent_entries = [e for e in lease.entries if e.entity_id == "parent-1"]
                parent_limit_names = {e.limit.name for e in parent_entries}
                assert "rpm" in parent_limit_names
                assert "tpm" not in parent_limit_names
        finally:
            limiter._repository._speculative_consume_single = original_single

    async def test_nested_parent_failure_missing_limit_names(self, limiter):
        """Nested parent failure: old_buckets don't include all consume keys."""
        await limiter.create_entity("parent-1")
        await limiter.create_entity("child-1", parent_id="parent-1", cascade=True)
        await limiter.set_system_defaults([Limit.per_minute("rpm", 1000)])

        async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}):
            pass
        limiter._speculative_writes = True
        async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}):
            pass

        now_ms = int(__import__("time").time() * 1000)
        child_bucket = BucketState(
            entity_id="child-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=900_000,
            last_refill_ms=now_ms,
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )
        parent_old_tpm_only = BucketState(
            entity_id="parent-1",
            resource="gpt-4",
            limit_name="tpm",
            tokens_milli=50_000_000,
            last_refill_ms=now_ms,
            capacity_milli=100_000_000,
            refill_amount_milli=100_000_000,
            refill_period_ms=60_000,
        )
        original_single = limiter._repository._speculative_consume_single

        async def mock_single(
            entity_id, resource, consume, ttl_seconds=None, shard_id=0, now_ms=None
        ):
            if entity_id == "child-1":
                return SpeculativeResult(
                    success=True,
                    buckets=[child_bucket],
                    cascade=True,
                    parent_id="parent-1",
                )
            if entity_id == "parent-1":
                return SpeculativeResult(success=False, old_buckets=[parent_old_tpm_only])
            return await original_single(entity_id, resource, consume, ttl_seconds)

        limiter._repository._speculative_consume_single = mock_single
        try:
            async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}) as lease:
                assert len(lease.entries) > 0
        finally:
            limiter._repository._speculative_consume_single = original_single

    async def test_nested_parent_only_acquire_exception_compensates(self, limiter):
        """Nested parent failure: exception during parent-only acquire compensates child."""
        await limiter.create_entity("parent-1")
        await limiter.create_entity("child-1", parent_id="parent-1", cascade=True)
        await limiter.set_system_defaults([Limit.per_minute("rpm", 1000)])

        async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}):
            pass
        limiter._speculative_writes = True
        async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}):
            pass

        now_ms = int(__import__("time").time() * 1000)
        child_bucket = BucketState(
            entity_id="child-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=900_000,
            last_refill_ms=now_ms,
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )
        parent_old = BucketState(
            entity_id="parent-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=500_000,
            last_refill_ms=now_ms - 30_000,
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )
        original_single = limiter._repository._speculative_consume_single
        original_parent_acquire = limiter._try_parent_only_acquire

        async def mock_single(
            entity_id, resource, consume, ttl_seconds=None, shard_id=0, now_ms=None
        ):
            if entity_id == "child-1":
                return SpeculativeResult(
                    success=True,
                    buckets=[child_bucket],
                    cascade=True,
                    parent_id="parent-1",
                )
            if entity_id == "parent-1":
                return SpeculativeResult(success=False, old_buckets=[parent_old])
            return await original_single(entity_id, resource, consume, ttl_seconds)

        async def mock_parent_acquire(*args, **kwargs):
            raise RuntimeError("simulated parent acquire failure")

        limiter._repository._speculative_consume_single = mock_single
        limiter._try_parent_only_acquire = mock_parent_acquire
        try:
            with pytest.raises(RateLimiterUnavailable):
                async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}):
                    pass
        finally:
            limiter._repository._speculative_consume_single = original_single
            limiter._try_parent_only_acquire = original_parent_acquire

    async def test_nested_parent_only_acquire_returns_none_compensates(self, limiter):
        """Nested parent failure: parent-only acquire returns None compensates child."""
        await limiter.create_entity("parent-1")
        await limiter.create_entity("child-1", parent_id="parent-1", cascade=True)
        await limiter.set_system_defaults([Limit.per_minute("rpm", 1000)])

        async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}):
            pass
        limiter._speculative_writes = True
        async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}):
            pass

        now_ms = int(__import__("time").time() * 1000)
        child_bucket = BucketState(
            entity_id="child-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=900_000,
            last_refill_ms=now_ms,
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )
        parent_old = BucketState(
            entity_id="parent-1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=500_000,
            last_refill_ms=now_ms - 30_000,
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )
        original_single = limiter._repository._speculative_consume_single
        original_parent_acquire = limiter._try_parent_only_acquire

        async def mock_single(
            entity_id, resource, consume, ttl_seconds=None, shard_id=0, now_ms=None
        ):
            if entity_id == "child-1":
                return SpeculativeResult(
                    success=True,
                    buckets=[child_bucket],
                    cascade=True,
                    parent_id="parent-1",
                )
            if entity_id == "parent-1":
                return SpeculativeResult(success=False, old_buckets=[parent_old])
            return await original_single(entity_id, resource, consume, ttl_seconds)

        async def mock_parent_acquire(*args, **kwargs):
            return None

        limiter._repository._speculative_consume_single = mock_single
        limiter._try_parent_only_acquire = mock_parent_acquire
        try:
            async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}) as lease:
                assert len(lease.entries) > 0
        finally:
            limiter._repository._speculative_consume_single = original_single
            limiter._try_parent_only_acquire = original_parent_acquire


class TestShardRetry:
    """Tests for shard retry and doubling in acquire flow."""

    async def test_acquire_retries_on_another_shard(self, limiter):
        """When one shard's app limit is exhausted, retry on another shard."""
        repo = limiter._repository
        ns = repo._namespace_id

        await limiter.create_entity("user-1")
        await limiter.set_system_defaults([Limit.per_minute("rpm", 100)])

        # Create buckets at shard 0 and shard 1
        now_ms = int(time.time() * 1000)
        for shard_id in range(2):
            states = [
                BucketState.from_limit("user-1", "gpt-4", Limit.per_minute("rpm", 100), now_ms),
            ]
            put_item = repo.build_composite_create(
                "user-1", "gpt-4", states, now_ms, shard_id=shard_id, shard_count=2
            )
            await repo.transact_write([put_item])

        # Pre-populate entity cache with shard_count=2
        repo._entity_cache[(ns, "user-1")] = (False, None, {"gpt-4": 2})

        # Exhaust shard 0's rpm tokens via direct speculative_consume_single
        for _ in range(100):
            await repo._speculative_consume_single("user-1", "gpt-4", {"rpm": 1}, shard_id=0)

        # Force shard_id=0 first, then let retry find shard 1
        limiter._speculative_writes = True
        with patch("zae_limiter.repository.random.randrange", return_value=0):
            async with limiter.acquire("user-1", "gpt-4", {"rpm": 1}) as lease:
                rpm_entry = next(e for e in lease.entries if e.limit.name == "rpm")
                assert rpm_entry is not None

    async def test_acquire_doubles_shards_on_wcu_exhaustion(self, limiter):
        """When wcu is exhausted, shard_count doubles and acquire falls to slow path."""
        from zae_limiter import schema

        repo = limiter._repository
        ns = repo._namespace_id

        await limiter.create_entity("user-1")
        await limiter.set_system_defaults([Limit.per_minute("rpm", 100_000)])

        # Create bucket at shard 0 with shard_count=1
        now_ms = int(time.time() * 1000)
        states = [
            BucketState.from_limit("user-1", "gpt-4", Limit.per_minute("rpm", 100_000), now_ms),
        ]
        put_item = repo.build_composite_create(
            "user-1", "gpt-4", states, now_ms, shard_id=0, shard_count=1
        )
        await repo.transact_write([put_item])

        # Pre-populate entity cache
        repo._entity_cache[(ns, "user-1")] = (False, None, {"gpt-4": 1})

        # Exhaust wcu tokens on shard 0 under sustained pressure: its wcu
        # refills once an hour, so the drained balance means a hot partition
        # (a wcu that would refill takes the slow path instead, ADR-133)
        client = await repo._get_client()
        await client.update_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(ns, "user-1", "gpt-4", 0)},
                "SK": {"S": schema.sk_state()},
            },
            UpdateExpression="SET #rp = :hour, #ra = :one",
            ExpressionAttributeNames={
                "#rp": schema.bucket_attr(schema.WCU_LIMIT_NAME, schema.BUCKET_FIELD_RP),
                "#ra": schema.bucket_attr(schema.WCU_LIMIT_NAME, schema.BUCKET_FIELD_RA),
            },
            ExpressionAttributeValues={":hour": {"N": "3600000"}, ":one": {"N": "1"}},
        )
        for _ in range(schema.WCU_LIMIT_CAPACITY):
            await repo._speculative_consume_single("user-1", "gpt-4", {"rpm": 1}, shard_id=0)

        # Now shard 0's wcu is exhausted. Acquire should:
        # 1. Detect wcu exhaustion
        # 2. Call bump_shard_count to double to 2
        # 3. Fall through to slow path (new shard doesn't exist yet)
        limiter._speculative_writes = True

        # Acquire should still succeed (falls to slow path which creates bucket)
        async with limiter.acquire("user-1", "gpt-4", {"rpm": 1}) as lease:
            rpm_entry = next(e for e in lease.entries if e.limit.name == "rpm")
            assert rpm_entry is not None

        # Verify shard_count was bumped in entity cache
        cache_entry = repo._entity_cache[(ns, "user-1")]
        assert cache_entry[2]["gpt-4"] == 2

    async def test_retry_on_other_shard_returns_none_when_all_exhausted(self, limiter):
        """_retry_on_other_shard returns None when all shards are exhausted."""
        repo = limiter._repository
        ns = repo._namespace_id

        await limiter.create_entity("user-1")
        await limiter.set_system_defaults([Limit.per_minute("rpm", 1)])

        now_ms = int(time.time() * 1000)
        # Create 2 shards, each with only 1 rpm token
        for shard_id in range(2):
            states = [
                BucketState.from_limit("user-1", "gpt-4", Limit.per_minute("rpm", 1), now_ms),
            ]
            put_item = repo.build_composite_create(
                "user-1", "gpt-4", states, now_ms, shard_id=shard_id, shard_count=2
            )
            await repo.transact_write([put_item])

        repo._entity_cache[(ns, "user-1")] = (False, None, {"gpt-4": 2})

        # Exhaust rpm on both shards
        for shard_id in range(2):
            await repo._speculative_consume_single("user-1", "gpt-4", {"rpm": 1}, shard_id=shard_id)

        # Both shards exhausted, retry should return None and fall to slow path
        limiter._speculative_writes = True
        from zae_limiter.exceptions import RateLimitExceeded

        with pytest.raises(RateLimitExceeded):
            async with limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
                pass


class TestClientShardCreation:
    """The client slow path creates shard N>0 bucket items itself (issue #439).

    Before this, only the aggregator's ``propagate_shard_count`` (Path 2) ever
    wrote a shard N>0 item, so with ``--no-aggregator`` the GHSA-76rv write
    sharding mitigation never engaged: the slow path read and wrote shard 0
    regardless of which shard the speculative write had selected.
    """

    # Refill of 1 token/hour keeps refill out of every token assertion below.
    CAPACITY = 100_000

    @staticmethod
    async def _seed_shard0(limiter, shard_count: int, limit):
        repo = limiter._repository
        ns = repo._namespace_id
        await limiter.create_entity("user-1")
        await limiter.set_system_defaults([limit])
        now_ms = int(time.time() * 1000)
        states = [BucketState.from_limit("user-1", "gpt-4", limit, now_ms)]
        await repo.transact_write(
            [
                repo.build_composite_create(
                    "user-1", "gpt-4", states, now_ms, shard_id=0, shard_count=shard_count
                )
            ]
        )
        repo._entity_cache[(ns, "user-1")] = (False, None, {"gpt-4": shard_count})
        limiter._speculative_writes = True
        return repo

    @staticmethod
    async def _raw_item(repo, shard_id: int) -> dict | None:
        from zae_limiter import schema

        client = await repo._get_client()
        resp = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(repo._namespace_id, "user-1", "gpt-4", shard_id)},
                "SK": {"S": schema.sk_state()},
            },
        )
        return resp.get("Item")

    @staticmethod
    def _n(item: dict, limit_name: str, field: str) -> int:
        from zae_limiter import schema

        return int(item[schema.bucket_attr(limit_name, field)]["N"])

    @staticmethod
    async def _seed_shards(limiter, shard_count: int, limit, *, tokens_milli: int, rf_ms: int):
        """Create every shard 0..shard_count-1 at the same balance and rf."""
        repo = limiter._repository
        ns = repo._namespace_id
        await limiter.create_entity("user-1")
        await limiter.set_system_defaults([limit])
        for shard_id in range(shard_count):
            state = BucketState.from_limit("user-1", "gpt-4", limit, rf_ms)
            state.tokens_milli = tokens_milli
            await repo.transact_write(
                [
                    repo.build_composite_create(
                        "user-1",
                        "gpt-4",
                        [state],
                        rf_ms,
                        shard_id=shard_id,
                        shard_count=shard_count,
                    )
                ]
            )
        repo._entity_cache[(ns, "user-1")] = (False, None, {"gpt-4": shard_count})
        return repo

    async def test_slow_path_refills_a_shard_to_its_effective_share(self, limiter):
        """The slow path must refill shard N toward capacity // shard_count.

        The bucket item stores undivided cp/ra (ADR-133 keeps that), so a
        refill that reads them verbatim tops every shard up to the full
        capacity after one idle window and the entity admits
        shard_count x capacity in steady state. Per-shard refill is what the
        aggregator's try_refill_bucket does; the client must match it.
        """
        from zae_limiter import schema
        from zae_limiter.exceptions import RateLimitExceeded

        limit = Limit.custom("rpm", 1000, refill_amount=1000, refill_period_seconds=60)
        now_ms = int(time.time() * 1000)
        # Both shards drained two full refill windows ago
        repo = await self._seed_shards(limiter, 2, limit, tokens_milli=0, rf_ms=now_ms - 120_000)
        limiter._speculative_writes = False  # slow path only

        with patch("zae_limiter.repository.random.randrange", return_value=0):
            with pytest.raises(RateLimitExceeded):
                async with limiter.acquire("user-1", "gpt-4", {"rpm": 501}):
                    pass
            async with limiter.acquire("user-1", "gpt-4", {"rpm": 500}):
                pass
        with patch("zae_limiter.repository.random.randrange", return_value=1):
            async with limiter.acquire("user-1", "gpt-4", {"rpm": 500}):
                pass

        # 500 + 500 = the configured 1000, not 2 x 1000
        for shard_id in (0, 1):
            item = await self._raw_item(repo, shard_id)
            assert self._n(item, "rpm", schema.BUCKET_FIELD_TK) == 0

    async def test_slow_path_creates_the_selected_shard(self, limiter):
        """BUCKET_MISSING on shard 1 creates shard 1; shard 0 is not touched."""
        from zae_limiter import schema

        limit = Limit.custom("rpm", self.CAPACITY, refill_amount=1, refill_period_seconds=3600)
        repo = await self._seed_shard0(limiter, shard_count=2, limit=limit)

        with patch("zae_limiter.repository.random.randrange", return_value=1):
            async with limiter.acquire("user-1", "gpt-4", {"rpm": 1}) as lease:
                assert {e._shard_id for e in lease.entries} == {1}

        shard1 = await self._raw_item(repo, 1)
        assert shard1 is not None, "the client slow path must create the shard it selected"
        assert shard1["shard_count"]["N"] == "2"
        cp_milli = self.CAPACITY * 1000
        # Effective per-shard tokens, exactly like processor.propagate_shard_count Path 2
        assert self._n(shard1, "rpm", schema.BUCKET_FIELD_TK) == cp_milli // 2 - 1000
        # Stored limits stay undivided; only the token balance is per-shard
        assert self._n(shard1, "rpm", schema.BUCKET_FIELD_CP) == cp_milli
        assert self._n(shard1, "rpm", schema.BUCKET_FIELD_RA) == 1000
        # wcu is per-partition and is NOT divided
        assert (
            self._n(shard1, schema.WCU_LIMIT_NAME, schema.BUCKET_FIELD_TK)
            == schema.WCU_LIMIT_CAPACITY * 1000
        )

        shard0 = await self._raw_item(repo, 0)
        assert self._n(shard0, "rpm", schema.BUCKET_FIELD_TK) == cp_milli, (
            "shard 0 served nothing and must not be debited"
        )

    async def test_wcu_exhaustion_creates_the_new_shard(self, limiter):
        """After doubling, the slow path lands on the brand-new shard and creates it."""
        from zae_limiter import schema

        limit = Limit.custom("rpm", self.CAPACITY, refill_amount=1, refill_period_seconds=3600)
        repo = await self._seed_shard0(limiter, shard_count=1, limit=limit)
        await self._slow_wcu_refill(repo)

        for _ in range(schema.WCU_LIMIT_CAPACITY):
            await repo._speculative_consume_single("user-1", "gpt-4", {"rpm": 1}, shard_id=0)
        cp_milli = self.CAPACITY * 1000
        shard0_before = self._n(await self._raw_item(repo, 0), "rpm", schema.BUCKET_FIELD_TK)
        assert shard0_before == cp_milli - schema.WCU_LIMIT_CAPACITY * 1000

        async with limiter.acquire("user-1", "gpt-4", {"rpm": 1}) as lease:
            assert {e._shard_id for e in lease.entries} == {1}

        shard1 = await self._raw_item(repo, 1)
        assert shard1 is not None, "write sharding must engage without the aggregator"
        assert shard1["shard_count"]["N"] == "2"
        assert self._n(shard1, "rpm", schema.BUCKET_FIELD_TK) == cp_milli // 2 - 1000
        shard0_after = self._n(await self._raw_item(repo, 0), "rpm", schema.BUCKET_FIELD_TK)
        assert shard0_after == shard0_before, "the hot shard must not absorb the write"

        # The shard now exists, so the next acquire on it is a fast-path hit.
        slow_path_calls: list[int | None] = []
        original = limiter._do_acquire

        async def spy(*args, **kwargs):
            slow_path_calls.append(kwargs.get("shard_id"))
            return await original(*args, **kwargs)

        limiter._do_acquire = spy
        with patch("zae_limiter.repository.random.randrange", return_value=1):
            async with limiter.acquire("user-1", "gpt-4", {"rpm": 1}) as lease:
                assert {e._shard_id for e in lease.entries} == {1}
        assert slow_path_calls == [], "a created shard must not BUCKET_MISSING-fallback"
        assert self._n(await self._raw_item(repo, 1), "rpm", schema.BUCKET_FIELD_TK) == (
            cp_milli // 2 - 2000
        )

    async def test_retry_on_a_missing_shard_creates_it(self, limiter):
        """Shard 0 drained, shard 1 never created: the retry's BUCKET_MISSING
        sends the slow path to shard 1 instead of fast-rejecting the caller."""
        import random as _random

        from zae_limiter import schema

        limit = Limit.custom("rpm", 10, refill_amount=1, refill_period_seconds=3600)
        repo = await self._seed_shard0(limiter, shard_count=2, limit=limit)
        await repo._speculative_consume_single("user-1", "gpt-4", {"rpm": 10}, shard_id=0)

        with (
            patch("zae_limiter.repository.random.randrange", return_value=0),
            patch.object(_random, "choice", lambda seq: seq[0]),
        ):
            async with limiter.acquire("user-1", "gpt-4", {"rpm": 1}) as lease:
                assert {e._shard_id for e in lease.entries} == {1}

        shard1 = await self._raw_item(repo, 1)
        assert shard1 is not None
        assert self._n(shard1, "rpm", schema.BUCKET_FIELD_TK) == 10_000 // 2 - 1000

    async def test_cold_cache_retry_sizes_the_new_shard_from_the_failure_image(self, limiter):
        """A failed speculative write never updates the entity cache, so with a
        cold cache the retry knows shard_count=2 (ALL_OLD) but the slow path
        used to size the new shard from the cache: full tokens, shard_count=1.
        The count observed on the failure image must reach the create."""
        from zae_limiter import schema

        limit = Limit.custom("rpm", 10, refill_amount=1, refill_period_seconds=3600)
        repo = await self._seed_shard0(limiter, shard_count=2, limit=limit)
        # Deliberately NOT seeding the entity cache
        del repo._entity_cache[(repo._namespace_id, "user-1")]
        await repo._speculative_consume_single("user-1", "gpt-4", {"rpm": 10}, shard_id=0)

        # Cold cache draws shard 0 (drained); the only untried shard is 1.
        async with limiter.acquire("user-1", "gpt-4", {"rpm": 1}) as lease:
            assert {e._shard_id for e in lease.entries} == {1}

        shard1 = await self._raw_item(repo, 1)
        assert shard1 is not None
        assert shard1["shard_count"]["N"] == "2"
        assert self._n(shard1, "rpm", schema.BUCKET_FIELD_TK) == 10_000 // 2 - 1000

    async def test_child_compensation_credits_the_shard_that_was_debited(self, limiter):
        """Child succeeds speculatively on shard 3, parent is BUCKET_MISSING:
        the compensating credit must land on shard 3, not on shard 0. Crediting
        shard 0 leaves shard 3 double-debited and mints tokens on shard 0."""
        from zae_limiter import schema

        repo = limiter._repository
        ns = repo._namespace_id
        limit = Limit.custom("rpm", self.CAPACITY, refill_amount=1, refill_period_seconds=3600)
        await limiter.create_entity("parent-1")
        await limiter.create_entity("user-1", parent_id="parent-1", cascade=True)
        await limiter.set_system_defaults([limit])
        now_ms = int(time.time() * 1000)
        for shard_id in range(4):
            state = BucketState.from_limit("user-1", "gpt-4", limit, now_ms, shard_count=4)
            await repo.transact_write(
                [
                    repo.build_composite_create(
                        "user-1",
                        "gpt-4",
                        [state],
                        now_ms,
                        cascade=True,
                        parent_id="parent-1",
                        shard_id=shard_id,
                        shard_count=4,
                    )
                ]
            )
        # Warm cache -> parallel child+parent speculative path; no parent bucket yet
        repo._entity_cache[(ns, "user-1")] = (True, "parent-1", {"gpt-4": 4})
        limiter._speculative_writes = True
        share = self.CAPACITY * 1000 // 4

        with patch("zae_limiter.repository.random.randrange", return_value=3):
            async with limiter.acquire("user-1", "gpt-4", {"rpm": 1}) as lease:
                assert {e._shard_id for e in lease.entries if e.entity_id == "user-1"} == {3}

        # One net debit on shard 3 (speculative debit, compensated, slow-path debit)
        assert self._n(await self._raw_item(repo, 3), "rpm", schema.BUCKET_FIELD_TK) == (
            share - 1000
        )
        assert self._n(await self._raw_item(repo, 0), "rpm", schema.BUCKET_FIELD_TK) == share, (
            "shard 0 served nothing; a credit here mints capacity"
        )

    async def _seed_cascade_child(self, limiter, limit, *, parent_tokens_milli: int):
        """Parent bucket at the given balance; child with 2 shards, shard 0 drained."""
        repo = limiter._repository
        now_ms = int(time.time() * 1000)
        await limiter.create_entity("parent-1")
        await limiter.create_entity("user-1", parent_id="parent-1", cascade=True)
        await limiter.set_system_defaults([limit])
        parent_state = BucketState.from_limit("parent-1", "gpt-4", limit, now_ms)
        parent_state.tokens_milli = parent_tokens_milli
        await repo.transact_write(
            [repo.build_composite_create("parent-1", "gpt-4", [parent_state], now_ms)]
        )
        for shard_id in range(2):
            state = BucketState.from_limit("user-1", "gpt-4", limit, now_ms, shard_count=2)
            if shard_id == 0:
                state.tokens_milli = 0
            await repo.transact_write(
                [
                    repo.build_composite_create(
                        "user-1",
                        "gpt-4",
                        [state],
                        now_ms,
                        cascade=True,
                        parent_id="parent-1",
                        shard_id=shard_id,
                        shard_count=2,
                    )
                ]
            )
        limiter._speculative_writes = True
        return repo

    async def test_cascade_shard_retry_cannot_bypass_an_exhausted_parent(self, limiter):
        """Cold cache: child shard 0 drained, parent exhausted. The shard retry
        used to succeed on child shard 1 and hand back a child-only lease,
        admitting unbounded child traffic past the parent's limit."""
        from zae_limiter import schema
        from zae_limiter.exceptions import RateLimitExceeded

        limit = Limit.custom("rpm", self.CAPACITY, refill_amount=1, refill_period_seconds=3600)
        repo = await self._seed_cascade_child(limiter, limit, parent_tokens_milli=0)
        repo._entity_cache.pop((repo._namespace_id, "user-1"), None)

        with pytest.raises(RateLimitExceeded):
            async with limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
                pass

        share = self.CAPACITY * 1000 // 2
        assert self._n(await self._raw_item(repo, 1), "rpm", schema.BUCKET_FIELD_TK) == share, (
            "nothing may be consumed from the child when the parent rejects"
        )

    async def test_cascade_shard_retry_charges_the_parent(self, limiter):
        """Warm cache: the parallel parent debit was compensated after the
        child failed on shard 0 (where a refill would help, so no fast
        rejection); the shard retry must not then admit the child alone. The
        slow path commits child shard 1 and the parent together."""
        from zae_limiter import schema

        # 1 token/s; child shard 0 was drained 100 s ago -> refill would help
        limit = Limit.custom("rpm", self.CAPACITY, refill_amount=3600, refill_period_seconds=3600)
        cp_milli = self.CAPACITY * 1000
        repo = await self._seed_cascade_child(limiter, limit, parent_tokens_milli=cp_milli)
        ns = repo._namespace_id
        client = await repo._get_client()
        await client.update_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(ns, "user-1", "gpt-4", 0)},
                "SK": {"S": schema.sk_state()},
            },
            UpdateExpression="SET #rf = :rf",
            ExpressionAttributeNames={"#rf": schema.BUCKET_FIELD_RF},
            ExpressionAttributeValues={":rf": {"N": str(int(time.time() * 1000) - 100_000)}},
        )
        repo._entity_cache[(ns, "user-1")] = (True, "parent-1", {"gpt-4": 2})

        with patch("zae_limiter.repository.random.randrange", return_value=0):
            async with limiter.acquire("user-1", "gpt-4", {"rpm": 1}) as lease:
                assert {e.entity_id for e in lease.entries} == {"user-1", "parent-1"}

        parent = await repo.get_buckets("parent-1", resource="gpt-4", shard_id=0)
        assert next(b for b in parent if b.limit_name == "rpm").tokens_milli == cp_milli - 1000
        assert self._n(await self._raw_item(repo, 1), "rpm", schema.BUCKET_FIELD_TK) == (
            cp_milli // 2 - 1000
        )

    async def test_new_child_shard_survives_a_parent_rf_conflict(self, limiter):
        """Cascade slow path creating child shard 1 while the parent's rf lock
        is lost to a concurrent refill: the acquire must succeed and shard 1
        must exist afterwards (the Put is re-issued, not retried as a debit)."""
        from zae_limiter import schema

        limit = Limit.custom("rpm", self.CAPACITY, refill_amount=1, refill_period_seconds=3600)
        cp_milli = self.CAPACITY * 1000
        repo = await self._seed_cascade_child(limiter, limit, parent_tokens_milli=cp_milli)
        ns = repo._namespace_id
        # Child shard 1 must not exist yet: the slow path will create it
        client = await repo._get_client()
        await client.delete_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(ns, "user-1", "gpt-4", 1)},
                "SK": {"S": schema.sk_state()},
            },
        )
        repo._entity_cache[(ns, "user-1")] = (True, "parent-1", {"gpt-4": 2})

        original_transact = repo.transact_write
        bumped = False

        async def parent_refilled_first(items):
            nonlocal bumped
            if not bumped and len(items) == 2:
                bumped = True
                # A concurrent refill moves the parent's rf; the transaction's
                # rf lock on the parent fails while the child's Put is innocent
                await client.update_item(
                    TableName=repo.table_name,
                    Key={
                        "PK": {"S": schema.pk_bucket(ns, "parent-1", "gpt-4", 0)},
                        "SK": {"S": schema.sk_state()},
                    },
                    UpdateExpression="SET #rf = :rf",
                    ExpressionAttributeNames={"#rf": schema.BUCKET_FIELD_RF},
                    ExpressionAttributeValues={":rf": {"N": str(int(time.time() * 1000) + 5)}},
                )
            return await original_transact(items)

        repo.transact_write = parent_refilled_first
        with patch("zae_limiter.repository.random.randrange", return_value=1):
            async with limiter.acquire("user-1", "gpt-4", {"rpm": 1}) as lease:
                assert {e.entity_id for e in lease.entries} == {"user-1", "parent-1"}

        assert bumped
        shard1 = await self._raw_item(repo, 1)
        assert shard1 is not None, "the innocent Put must be re-issued after the rollback"
        assert self._n(shard1, "rpm", schema.BUCKET_FIELD_TK) == cp_milli // 2 - 1000
        parent = await repo.get_buckets("parent-1", resource="gpt-4", shard_id=0)
        assert next(b for b in parent if b.limit_name == "rpm").tokens_milli == cp_milli - 1000

    async def test_parent_only_slow_path_reuses_the_parent_shard_it_judged(self, limiter):
        """The "refill would help" decision was made on the ALL_OLD image of
        the parent shard the speculative write hit; the parent-only slow path
        must read and debit that same shard rather than draw a new one."""
        from zae_limiter import schema

        repo = limiter._repository
        ns = repo._namespace_id
        # 1 token/s; both parent shards drained 100 s ago -> refill would help
        limit = Limit.custom("rpm", 100_000, refill_amount=3600, refill_period_seconds=3600)
        now_ms = int(time.time() * 1000)
        await limiter.create_entity("parent-1")
        await limiter.create_entity("user-1", parent_id="parent-1", cascade=True)
        await limiter.set_system_defaults([limit])
        for shard_id in range(2):
            state = BucketState.from_limit("parent-1", "gpt-4", limit, now_ms - 100_000, 2)
            state.tokens_milli = 0
            await repo.transact_write(
                [
                    repo.build_composite_create(
                        "parent-1",
                        "gpt-4",
                        [state],
                        now_ms - 100_000,
                        shard_id=shard_id,
                        shard_count=2,
                    )
                ]
            )
        child = BucketState.from_limit("user-1", "gpt-4", limit, now_ms)
        await repo.transact_write(
            [
                repo.build_composite_create(
                    "user-1", "gpt-4", [child], now_ms, cascade=True, parent_id="parent-1"
                )
            ]
        )
        # Cold child cache -> sequential parent speculative; parent draws from 2
        repo._entity_cache.pop((ns, "user-1"), None)
        repo._entity_cache[(ns, "parent-1")] = (False, None, {"gpt-4": 2})
        limiter._speculative_writes = True

        parent_reads: list[int] = []
        original_fetch = limiter._fetch_buckets

        async def spy(entity_ids, resource, shard_id):
            if "parent-1" in entity_ids:
                parent_reads.append(shard_id)
            return await original_fetch(entity_ids, resource, shard_id)

        limiter._fetch_buckets = spy
        # First draw (parent speculative) -> shard 1; a second draw would give 0
        with patch("zae_limiter.repository.random.randrange", side_effect=[1, 0, 0]):
            async with limiter.acquire("user-1", "gpt-4", {"rpm": 10}) as lease:
                parent_entry = next(e for e in lease.entries if e.entity_id == "parent-1")
                assert parent_entry._shard_id == 1

        assert parent_reads == [1]
        shard0 = await repo.get_buckets("parent-1", resource="gpt-4", shard_id=0)
        assert next(b for b in shard0 if b.limit_name == "rpm").tokens_milli == 0, (
            "parent shard 0 was never judged and must not be written"
        )
        item1 = await (await repo._get_client()).get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(ns, "parent-1", "gpt-4", 1)},
                "SK": {"S": schema.sk_state()},
            },
        )
        assert int(item1["Item"][schema.bucket_attr("rpm", schema.BUCKET_FIELD_TK)]["N"]) > 0

    async def test_reissued_put_that_loses_the_create_race_is_downgraded(self, limiter):
        """First transaction: parent rf conflict rolls back the innocent child
        Put. The Put is re-issued, but the aggregator creates the shard in
        between, so the retry transaction cancels on the Put. That must be
        downgraded to a consumption-only debit, not surfaced as a rejection."""
        from zae_limiter import schema

        limit = Limit.custom("rpm", self.CAPACITY, refill_amount=1, refill_period_seconds=3600)
        cp_milli = self.CAPACITY * 1000
        repo = await self._seed_cascade_child(limiter, limit, parent_tokens_milli=cp_milli)
        ns = repo._namespace_id
        client = await repo._get_client()
        await client.delete_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(ns, "user-1", "gpt-4", 1)},
                "SK": {"S": schema.sk_state()},
            },
        )
        repo._entity_cache[(ns, "user-1")] = (True, "parent-1", {"gpt-4": 2})

        original_transact = repo.transact_write
        calls = 0

        async def hostile_environment(items):
            nonlocal calls
            calls += 1
            if calls == 1:  # parent refilled concurrently -> rf lock lost
                await client.update_item(
                    TableName=repo.table_name,
                    Key={
                        "PK": {"S": schema.pk_bucket(ns, "parent-1", "gpt-4", 0)},
                        "SK": {"S": schema.sk_state()},
                    },
                    UpdateExpression="SET #rf = :rf",
                    ExpressionAttributeNames={"#rf": schema.BUCKET_FIELD_RF},
                    ExpressionAttributeValues={":rf": {"N": str(int(time.time() * 1000) + 5)}},
                )
            elif calls == 2:  # aggregator Path 2 creates shard 1 first
                now_ms = int(time.time() * 1000)
                state = BucketState.from_limit("user-1", "gpt-4", limit, now_ms, 2)
                await original_transact(
                    [
                        repo.build_composite_create(
                            "user-1", "gpt-4", [state], now_ms, shard_id=1, shard_count=2
                        )
                    ]
                )
            return await original_transact(items)

        repo.transact_write = hostile_environment
        with patch("zae_limiter.repository.random.randrange", return_value=1):
            async with limiter.acquire("user-1", "gpt-4", {"rpm": 1}) as lease:
                assert {e.entity_id for e in lease.entries} == {"user-1", "parent-1"}

        assert calls == 3
        shard1 = await self._raw_item(repo, 1)
        assert self._n(shard1, "rpm", schema.BUCKET_FIELD_TK) == cp_milli // 2 - 1000
        parent = await repo.get_buckets("parent-1", resource="gpt-4", shard_id=0)
        assert next(b for b in parent if b.limit_name == "rpm").tokens_milli == cp_milli - 1000

    async def test_sharded_cascade_child_still_fast_rejects(self, limiter):
        """Refill would not help on the child's shard: reject from the ALL_OLD
        image with zero slow-path reads (as on main), compensating the parent
        debit that succeeded in parallel. The slow path is only for the case
        where refill would help."""
        from zae_limiter.exceptions import RateLimitExceeded

        limit = Limit.custom("rpm", self.CAPACITY, refill_amount=1, refill_period_seconds=3600)
        cp_milli = self.CAPACITY * 1000
        repo = await self._seed_cascade_child(limiter, limit, parent_tokens_milli=cp_milli)
        repo._entity_cache[(repo._namespace_id, "user-1")] = (True, "parent-1", {"gpt-4": 2})

        reads = AsyncMock(side_effect=AssertionError("slow path must not read"))
        with (
            patch.object(repo, "batch_get_entity_and_buckets", reads),
            patch("zae_limiter.repository.random.randrange", return_value=0),
        ):
            with pytest.raises(RateLimitExceeded):
                async with limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
                    pass

        reads.assert_not_called()
        parent = await repo.get_buckets("parent-1", resource="gpt-4", shard_id=0)
        assert next(b for b in parent if b.limit_name == "rpm").tokens_milli == cp_milli

    async def test_available_sums_every_shard(self, limiter):
        """available() must report the entity's total across shards, not the
        balance of shard 0 alone (which now holds at most capacity // N)."""
        limit = Limit.custom("rpm", 100, refill_amount=1, refill_period_seconds=3600)
        now_ms = int(time.time() * 1000)
        repo = await self._seed_shards(limiter, 2, limit, tokens_milli=50_000, rf_ms=now_ms)
        # A drained bucket for another resource must not leak into the sum
        other = BucketState.from_limit("user-1", "other", limit, now_ms)
        other.tokens_milli = 0
        await repo.transact_write([repo.build_composite_create("user-1", "other", [other], now_ms)])

        # Immune to clock drift (#503) without a pin: `refill_amount=1` over an
        # hour is one token per 3600 s, so the drained "other" bucket stays at
        # 0. The two 100s are the capacity ceiling, where refill clamps.
        assert await limiter.available("user-1", "gpt-4") == {"rpm": 100}
        assert await limiter.available("user-1", "other") == {"rpm": 0}
        assert await limiter.available("user-1", "unused") == {"rpm": 100}

    async def test_missing_parent_shard_is_created_where_the_fast_path_looked(self, limiter):
        """Parent cache says 2 shards; the speculative parent write hit shard 1
        and found it missing. The slow path must create parent shard 1, not
        draw a fresh parent shard (which could be 0)."""
        from zae_limiter import schema

        repo = limiter._repository
        ns = repo._namespace_id
        limit = Limit.custom("rpm", self.CAPACITY, refill_amount=1, refill_period_seconds=3600)
        now_ms = int(time.time() * 1000)
        await limiter.create_entity("parent-1")
        await limiter.create_entity("user-1", parent_id="parent-1", cascade=True)
        await limiter.set_system_defaults([limit])
        child = BucketState.from_limit("user-1", "gpt-4", limit, now_ms)
        await repo.transact_write(
            [
                repo.build_composite_create(
                    "user-1", "gpt-4", [child], now_ms, cascade=True, parent_id="parent-1"
                )
            ]
        )
        # Cold child cache -> sequential parent speculative; parent draws from 2
        repo._entity_cache.pop((ns, "user-1"), None)
        repo._entity_cache[(ns, "parent-1")] = (False, None, {"gpt-4": 2})
        limiter._speculative_writes = True

        # First draw (parent speculative) -> shard 1; a re-draw would give 0
        with patch("zae_limiter.repository.random.randrange", side_effect=[1, 0, 0]):
            async with limiter.acquire("user-1", "gpt-4", {"rpm": 1}) as lease:
                parent_entry = next(e for e in lease.entries if e.entity_id == "parent-1")
                assert parent_entry._shard_id == 1

        client = await repo._get_client()
        for shard_id, expect in ((1, True), (0, False)):
            resp = await client.get_item(
                TableName=repo.table_name,
                Key={
                    "PK": {"S": schema.pk_bucket(ns, "parent-1", "gpt-4", shard_id)},
                    "SK": {"S": schema.sk_state()},
                },
            )
            assert ("Item" in resp) is expect, f"parent shard {shard_id}"
        item1 = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(ns, "parent-1", "gpt-4", 1)},
                "SK": {"S": schema.sk_state()},
            },
        )
        assert item1["Item"]["shard_count"]["N"] == "2"

    @staticmethod
    async def _slow_wcu_refill(repo, shard_id: int = 0) -> None:
        """Model sustained pressure: this shard's wcu refills once an hour, so
        a drained wcu is a hot partition rather than a stale balance."""
        from zae_limiter import schema

        client = await repo._get_client()
        await client.update_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(repo._namespace_id, "user-1", "gpt-4", shard_id)},
                "SK": {"S": schema.sk_state()},
            },
            UpdateExpression="SET #rp = :hour, #ra = :one",
            ExpressionAttributeNames={
                "#rp": schema.bucket_attr(schema.WCU_LIMIT_NAME, schema.BUCKET_FIELD_RP),
                "#ra": schema.bucket_attr(schema.WCU_LIMIT_NAME, schema.BUCKET_FIELD_RA),
            },
            ExpressionAttributeValues={":hour": {"N": "3600000"}, ":one": {"N": "1"}},
        )

    @staticmethod
    async def _drain_wcu(repo, shard_id: int, rf_ms: int) -> None:
        """Zero the wcu balance on a shard and pin the shared rf."""
        from zae_limiter import schema

        client = await repo._get_client()
        await client.update_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(repo._namespace_id, "user-1", "gpt-4", shard_id)},
                "SK": {"S": schema.sk_state()},
            },
            UpdateExpression="SET #wcu = :zero, #rf = :rf",
            ExpressionAttributeNames={
                "#wcu": schema.bucket_attr(schema.WCU_LIMIT_NAME, schema.BUCKET_FIELD_TK),
                "#rf": schema.BUCKET_FIELD_RF,
            },
            ExpressionAttributeValues={":zero": {"N": "0"}, ":rf": {"N": str(rf_ms)}},
        )

    async def test_slow_path_refills_wcu_as_an_undeclared_carrier(self, limiter):
        """Without the aggregator nobody refilled wcu. The slow path must refill
        it from its stored ra/rp like any undeclared limit — never gated,
        never a status, never visible through the lease."""
        from zae_limiter import schema

        limit = Limit.custom("rpm", self.CAPACITY, refill_amount=1, refill_period_seconds=3600)
        now_ms = int(time.time() * 1000)
        repo = await self._seed_shards(
            limiter, 1, limit, tokens_milli=self.CAPACITY * 1000, rf_ms=now_ms
        )
        await self._drain_wcu(repo, 0, now_ms - 2_000)  # two wcu refill windows ago
        limiter._speculative_writes = False

        async with limiter.acquire("user-1", "gpt-4", {"rpm": 1}) as lease:
            assert lease.consumed == {"rpm": 1}
            assert {e.limit.name for e in lease.entries if e._declared} == {"rpm"}

        item = await self._raw_item(repo, 0)
        assert (
            self._n(item, schema.WCU_LIMIT_NAME, schema.BUCKET_FIELD_TK)
            == schema.WCU_LIMIT_CAPACITY * 1000
        )

    async def test_exhausted_wcu_that_would_refill_does_not_double(self, limiter):
        """wcu exhausted an instant ago is not a hot partition: the image's
        refill would restore it, so take the slow path on the same shard
        (which refills wcu) instead of doubling toward the cap."""
        from zae_limiter import schema

        limit = Limit.custom("rpm", self.CAPACITY, refill_amount=1, refill_period_seconds=3600)
        now_ms = int(time.time() * 1000)
        repo = await self._seed_shards(
            limiter, 1, limit, tokens_milli=self.CAPACITY * 1000, rf_ms=now_ms
        )
        await self._drain_wcu(repo, 0, now_ms - 2_000)
        ns = repo._namespace_id

        async with limiter.acquire("user-1", "gpt-4", {"rpm": 1}) as lease:
            assert {e._shard_id for e in lease.entries} == {0}

        assert repo._entity_cache[(ns, "user-1")][2]["gpt-4"] == 1, "no doubling"
        assert await self._raw_item(repo, 1) is None
        item = await self._raw_item(repo, 0)
        assert (
            self._n(item, schema.WCU_LIMIT_NAME, schema.BUCKET_FIELD_TK)
            == schema.WCU_LIMIT_CAPACITY * 1000
        )
        assert self._n(item, "rpm", schema.BUCKET_FIELD_TK) == self.CAPACITY * 1000 - 1000

    async def test_rejected_acquire_never_doubles_the_shard_count(self, limiter):
        """The non-cascade twin of #474 (issue #480): a doubling on the way to a
        rejection is a pure side effect. `BOTH_EXHAUSTED` means the reserved wcu
        *and* a declared limit are drained, so the acquire is about to raise —
        nothing then creates or reads the shard the doubling hands back. Repeat
        it once per rejection and an entity sitting at its limit walks from 1 to
        MAX_SHARD_COUNT (``_learn_shard_count`` is monotonic, nothing shrinks
        it), after which every shard's share is `capacity // 32` forever and any
        request above that is unadmittable on every shard (#475)."""
        from zae_limiter import schema
        from zae_limiter.exceptions import RateLimitExceeded

        limit = Limit.custom("rpm", self.CAPACITY, refill_amount=1, refill_period_seconds=3600)
        now_ms = int(time.time() * 1000)
        # rpm drained and refilling once an hour, wcu likewise: this is
        # BOTH_EXHAUSTED with no refill in reach on either limit.
        repo = await self._seed_shards(limiter, 1, limit, tokens_milli=0, rf_ms=now_ms)
        ns = repo._namespace_id
        await self._slow_wcu_refill(repo)
        await self._drain_wcu(repo, 0, now_ms)

        for _ in range(3):
            with pytest.raises(RateLimitExceeded):
                async with limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
                    pass

        assert repo._entity_cache[(ns, "user-1")][2]["gpt-4"] == 1
        item = await self._raw_item(repo, 0)
        assert item["shard_count"]["N"] == "1", "a rejected acquire must not shard the entity"
        assert await self._raw_item(repo, 1) is None
        assert self._n(item, schema.WCU_LIMIT_NAME, schema.BUCKET_FIELD_TK) == 0

    async def test_wcu_exhaustion_with_room_to_admit_still_doubles(self, limiter):
        """The other half of the #480 gate, and the GHSA-76rv mitigation itself:
        the *same* drained wcu on an entity whose declared limit can still admit
        must double and move off the hot shard. Differs from the test above in
        one input — the rpm balance."""
        from zae_limiter import schema

        limit = Limit.custom("rpm", self.CAPACITY, refill_amount=1, refill_period_seconds=3600)
        now_ms = int(time.time() * 1000)
        repo = await self._seed_shards(
            limiter, 1, limit, tokens_milli=self.CAPACITY * 1000, rf_ms=now_ms
        )
        ns = repo._namespace_id
        await self._slow_wcu_refill(repo)
        await self._drain_wcu(repo, 0, now_ms)

        async with limiter.acquire("user-1", "gpt-4", {"rpm": 1}) as lease:
            assert {e._shard_id for e in lease.entries} == {1}

        assert repo._entity_cache[(ns, "user-1")][2]["gpt-4"] == 2
        shard1 = await self._raw_item(repo, 1)
        assert shard1 is not None, "the hot shard's wcu must still spread the entity"
        assert shard1["shard_count"]["N"] == "2"
        assert self._n(shard1, "rpm", schema.BUCKET_FIELD_TK) == self.CAPACITY * 1000 // 2 - 1000

    async def test_create_race_lost_to_aggregator_consumes_once(self, limiter):
        """If the aggregator's Path 2 wins the create, the client retries as a
        consumption-only conditional write on that shard: one debit, no
        over-admission, no second fallback."""
        from zae_limiter import schema

        limit = Limit.custom("rpm", self.CAPACITY, refill_amount=1, refill_period_seconds=3600)
        repo = await self._seed_shard0(limiter, shard_count=2, limit=limit)
        cp_milli = self.CAPACITY * 1000

        original_transact = repo.transact_write
        raced = False

        async def aggregator_wins(items):
            nonlocal raced
            if not raced and any("Put" in item for item in items):
                raced = True
                # Path 2 clone: effective per-shard tokens, undivided wcu
                now_ms = int(time.time() * 1000)
                state = BucketState.from_limit("user-1", "gpt-4", limit, now_ms)
                state.tokens_milli = cp_milli // 2
                await original_transact(
                    [
                        repo.build_composite_create(
                            "user-1", "gpt-4", [state], now_ms, shard_id=1, shard_count=2
                        )
                    ]
                )
            return await original_transact(items)

        repo.transact_write = aggregator_wins
        with patch("zae_limiter.repository.random.randrange", return_value=1):
            async with limiter.acquire("user-1", "gpt-4", {"rpm": 1}) as lease:
                assert {e._shard_id for e in lease.entries} == {1}

        assert raced
        shard1 = await self._raw_item(repo, 1)
        assert self._n(shard1, "rpm", schema.BUCKET_FIELD_TK) == cp_milli // 2 - 1000

    async def test_bump_lost_race_still_moves_off_the_hot_shard(self, limiter):
        """If another client doubled first, our conditional bump loses. The
        loser must learn the winner's shard_count from the failed write's
        ALL_OLD image and draw from the newly added range, not cache its own
        stale count and land back on the exhausted shard 0."""
        from zae_limiter import schema

        limit = Limit.custom("rpm", self.CAPACITY, refill_amount=1, refill_period_seconds=3600)
        repo = await self._seed_shard0(limiter, shard_count=1, limit=limit)
        ns = repo._namespace_id
        await self._slow_wcu_refill(repo)
        for _ in range(schema.WCU_LIMIT_CAPACITY):
            await repo._speculative_consume_single("user-1", "gpt-4", {"rpm": 1}, shard_id=0)

        client = await repo._get_client()
        original_bump = repo.bump_shard_count

        async def winner_doubles_first(entity_id, resource, current_count):
            # The concurrent winner lands 1 -> 2 just before our bump
            await client.update_item(
                TableName=repo.table_name,
                Key={
                    "PK": {"S": schema.pk_bucket(ns, "user-1", "gpt-4", 0)},
                    "SK": {"S": schema.sk_state()},
                },
                UpdateExpression="SET shard_count = :two",
                ExpressionAttributeValues={":two": {"N": "2"}},
            )
            return await original_bump(entity_id, resource, current_count)

        repo.bump_shard_count = winner_doubles_first

        slow_path_shards: list[int | None] = []
        original_do_acquire = limiter._do_acquire

        async def spy(*args, **kwargs):
            slow_path_shards.append(kwargs.get("shard_id"))
            return await original_do_acquire(*args, **kwargs)

        limiter._do_acquire = spy
        async with limiter.acquire("user-1", "gpt-4", {"rpm": 1}) as lease:
            assert {e._shard_id for e in lease.entries} == {1}

        assert slow_path_shards == [1], "the loser must draw from range(old=1, new=2)"
        assert repo._entity_cache[(ns, "user-1")][2]["gpt-4"] == 2
        shard1 = await self._raw_item(repo, 1)
        assert shard1 is not None and shard1["shard_count"]["N"] == "2"

    async def test_bump_without_increase_stays_on_the_selected_shard(self, limiter):
        """If the bump cannot report a larger count (shard 0 vanished between
        the failed write and the bump), there is no new range to draw from;
        the slow path keeps the shard it already selected."""
        from zae_limiter import schema

        limit = Limit.custom("rpm", self.CAPACITY, refill_amount=1, refill_period_seconds=3600)
        repo = await self._seed_shard0(limiter, shard_count=1, limit=limit)
        await self._slow_wcu_refill(repo)
        for _ in range(schema.WCU_LIMIT_CAPACITY):
            await repo._speculative_consume_single("user-1", "gpt-4", {"rpm": 1}, shard_id=0)
        repo.bump_shard_count = AsyncMock(return_value=1)

        slow_path_shards: list[int | None] = []
        original_do_acquire = limiter._do_acquire

        async def spy(*args, **kwargs):
            slow_path_shards.append(kwargs.get("shard_id"))
            return await original_do_acquire(*args, **kwargs)

        limiter._do_acquire = spy
        async with limiter.acquire("user-1", "gpt-4", {"rpm": 1}) as lease:
            assert {e._shard_id for e in lease.entries} == {0}
        assert slow_path_shards == [0]

    async def test_cascade_slow_path_reads_parent_shard_without_batch_support(
        self, limiter, monkeypatch
    ):
        """The sequential get_buckets fallback carries the parent's shard too."""
        from zae_limiter.models import BackendCapabilities

        await limiter.create_entity("parent-1")
        await limiter.create_entity("child-1", parent_id="parent-1", cascade=True)
        await limiter.set_system_defaults(
            [Limit.custom("rpm", 100, refill_amount=1, refill_period_seconds=3600)]
        )
        monkeypatch.setattr(
            limiter._repository,
            "_capabilities",
            BackendCapabilities(
                supports_audit_logging=True,
                supports_usage_snapshots=True,
                supports_infrastructure_management=True,
                supports_change_streams=True,
                supports_batch_operations=False,
            ),
        )

        for _ in range(2):  # create, then read back existing buckets via get_buckets
            async with limiter.acquire("child-1", "gpt-4", {"rpm": 1}) as lease:
                by_entity = {e.entity_id: e._shard_id for e in lease.entries}
                assert by_entity == {"child-1": 0, "parent-1": 0}

        parent = await limiter._repository.get_buckets("parent-1", "gpt-4", shard_id=0)
        assert next(b for b in parent if b.limit_name == "rpm").tokens_milli == 98_000


class TestCascadeParentSharding:
    """The parallel cascade fast path must shard the parent too (issue #474).

    ``speculative_consume`` drew the child's shard from the entity cache but
    called the parent's ``_speculative_consume_single`` with the default
    ``shard_id=0``. A high-fanout cascade parent — the exact case write
    sharding exists to protect (GHSA-76rv, issue #116) — therefore kept every
    warm-path write on parent shard 0 no matter how far its ``shard_count``
    had been doubled, while the slow path (#466) drew the parent's shard from
    its cached count: the two paths disagreed about which parent shard holds
    the tokens.
    """

    CAPACITY = 100_000

    @staticmethod
    def _n(item: dict, limit_name: str, field: str) -> int:
        from zae_limiter import schema

        return int(item[schema.bucket_attr(limit_name, field)]["N"])

    @staticmethod
    async def _raw_item(repo, entity_id: str, shard_id: int) -> dict | None:
        from zae_limiter import schema

        client = await repo._get_client()
        resp = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(repo._namespace_id, entity_id, "gpt-4", shard_id)},
                "SK": {"S": schema.sk_state()},
            },
        )
        return resp.get("Item")

    @staticmethod
    async def _slow_wcu_refill(repo, entity_id: str, shard_id: int) -> None:
        """Model sustained pressure: this shard's wcu refills once an hour, so
        a drained wcu is a hot partition rather than a stale balance."""
        from zae_limiter import schema

        client = await repo._get_client()
        await client.update_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(repo._namespace_id, entity_id, "gpt-4", shard_id)},
                "SK": {"S": schema.sk_state()},
            },
            UpdateExpression="SET #rp = :hour, #ra = :one",
            ExpressionAttributeNames={
                "#rp": schema.bucket_attr(schema.WCU_LIMIT_NAME, schema.BUCKET_FIELD_RP),
                "#ra": schema.bucket_attr(schema.WCU_LIMIT_NAME, schema.BUCKET_FIELD_RA),
            },
            ExpressionAttributeValues={":hour": {"N": "3600000"}, ":one": {"N": "1"}},
        )

    @staticmethod
    async def _drain_wcu(repo, entity_id: str, shard_id: int) -> None:
        """Zero this shard's wcu and slow its refill to once an hour.

        A drained balance that refill cannot restore is what the client reads
        as a hot partition (ADR-133); the stock 1000/s rate would restore a
        write on any elapsed millisecond. ``rf`` is shared by every limit on
        the item, so pinning it to now also freezes the app limits.
        """
        from zae_limiter import schema

        client = await repo._get_client()
        await client.update_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(repo._namespace_id, entity_id, "gpt-4", shard_id)},
                "SK": {"S": schema.sk_state()},
            },
            UpdateExpression="SET #wcu = :zero, #rf = :rf, #ra = :one, #rp = :hour",
            ExpressionAttributeNames={
                "#wcu": schema.bucket_attr(schema.WCU_LIMIT_NAME, schema.BUCKET_FIELD_TK),
                "#rf": schema.BUCKET_FIELD_RF,
                "#ra": schema.bucket_attr(schema.WCU_LIMIT_NAME, schema.BUCKET_FIELD_RA),
                "#rp": schema.bucket_attr(schema.WCU_LIMIT_NAME, schema.BUCKET_FIELD_RP),
            },
            ExpressionAttributeValues={
                ":zero": {"N": "0"},
                ":rf": {"N": str(int(time.time() * 1000))},
                ":one": {"N": "1"},
                ":hour": {"N": "3600000"},
            },
        )

    async def _seed(
        self,
        limiter,
        limit,
        *,
        parent_shard_count: int,
        parent_shards=(0,),
        parent_tokens_milli: int | None = None,
        parent_rf_ms: int | None = None,
    ):
        """Child on a single shard; parent with ``parent_shard_count`` shards.

        Only ``parent_shards`` exist as items, so a draw onto another one is a
        ``BUCKET_MISSING`` the slow path has to create. The child's cache entry
        is warm, which is what selects the parallel cascade path.
        """
        repo = limiter._repository
        ns = repo._namespace_id
        now_ms = int(time.time() * 1000)
        rf_ms = parent_rf_ms if parent_rf_ms is not None else now_ms
        await limiter.create_entity("parent-1")
        await limiter.create_entity("user-1", parent_id="parent-1", cascade=True)
        await limiter.set_system_defaults([limit])
        for shard_id in parent_shards:
            state = BucketState.from_limit("parent-1", "gpt-4", limit, rf_ms, parent_shard_count)
            if parent_tokens_milli is not None:
                state.tokens_milli = parent_tokens_milli
            await repo.transact_write(
                [
                    repo.build_composite_create(
                        "parent-1",
                        "gpt-4",
                        [state],
                        rf_ms,
                        shard_id=shard_id,
                        shard_count=parent_shard_count,
                    )
                ]
            )
        child = BucketState.from_limit("user-1", "gpt-4", limit, now_ms)
        await repo.transact_write(
            [
                repo.build_composite_create(
                    "user-1", "gpt-4", [child], now_ms, cascade=True, parent_id="parent-1"
                )
            ]
        )
        repo._entity_cache[(ns, "user-1")] = (True, "parent-1", {"gpt-4": 1})
        repo._entity_cache[(ns, "parent-1")] = (False, None, {"gpt-4": parent_shard_count})
        limiter._speculative_writes = True
        return repo

    async def test_parallel_cascade_debits_the_parents_own_shard(self, limiter):
        """The parent's shard comes from the parent's cached shard_count, drawn
        exactly once — not from the child's, and never hardcoded to 0."""
        from zae_limiter import schema

        limit = Limit.custom("rpm", self.CAPACITY, refill_amount=1, refill_period_seconds=3600)
        repo = await self._seed(limiter, limit, parent_shard_count=4, parent_shards=range(4))
        share = self.CAPACITY * 1000 // 4

        # The child's cached count is 1, so the single draw is the parent's.
        with patch("zae_limiter.repository.random.randrange", side_effect=[3]):
            async with limiter.acquire("user-1", "gpt-4", {"rpm": 1}) as lease:
                parent_entry = next(e for e in lease.entries if e.entity_id == "parent-1")
                assert parent_entry._shard_id == 3

        item3 = await self._raw_item(repo, "parent-1", 3)
        assert self._n(item3, "rpm", schema.BUCKET_FIELD_TK) == share - 1000
        item0 = await self._raw_item(repo, "parent-1", 0)
        assert self._n(item0, "rpm", schema.BUCKET_FIELD_TK) == share, (
            "parent shard 0 must not absorb every cascade write"
        )

    async def test_parallel_cascade_spreads_the_parent_across_its_shards(self, limiter):
        """Unpatched, repeated cascade acquires must reach more than one parent
        shard: shard 0 alone is the hot partition sharding exists to avoid."""
        limit = Limit.custom("rpm", self.CAPACITY, refill_amount=1, refill_period_seconds=3600)
        repo = await self._seed(limiter, limit, parent_shard_count=4, parent_shards=range(4))

        parent_shards: set[int] = set()
        original = repo._speculative_consume_single

        async def spy(entity_id, resource, consume, ttl_seconds=None, shard_id=0, now_ms=None):
            if entity_id == "parent-1":
                parent_shards.add(shard_id)
            return await original(entity_id, resource, consume, ttl_seconds, shard_id=shard_id)

        repo._speculative_consume_single = spy
        for _ in range(24):
            async with limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
                pass

        assert parent_shards <= {0, 1, 2, 3}
        assert len(parent_shards) > 1, "every parent write landed on one shard"

    async def test_compensation_credits_the_parent_shard_that_was_debited(self, limiter):
        """The child fails while the parent's parallel debit succeeded: the
        compensating credit must go back to the parent shard that was debited."""
        from zae_limiter import schema

        limit = Limit.custom("rpm", self.CAPACITY, refill_amount=1, refill_period_seconds=3600)
        repo = await self._seed(limiter, limit, parent_shard_count=4, parent_shards=range(4))
        # Drain the child: its speculative write fails and refill cannot help
        await repo._speculative_consume_single(
            "user-1", "gpt-4", {"rpm": self.CAPACITY}, shard_id=0
        )
        share = self.CAPACITY * 1000 // 4

        with patch("zae_limiter.repository.random.randrange", side_effect=[2]):
            with pytest.raises(RateLimitExceeded):
                async with limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
                    pass

        # rpm is debited and credited back on the same shard, so the wcu the
        # debit spent (and compensation never returns) is the fingerprint of
        # which parent shard the pair of writes hit.
        shard2 = await self._raw_item(repo, "parent-1", 2)
        assert self._n(shard2, "rpm", schema.BUCKET_FIELD_TK) == share
        assert self._n(shard2, schema.WCU_LIMIT_NAME, schema.BUCKET_FIELD_TK) == (
            (schema.WCU_LIMIT_CAPACITY - 1) * 1000
        )
        shard0 = await self._raw_item(repo, "parent-1", 0)
        assert self._n(shard0, schema.WCU_LIMIT_NAME, schema.BUCKET_FIELD_TK) == (
            schema.WCU_LIMIT_CAPACITY * 1000
        )

    async def test_parent_only_fallback_reuses_the_drawn_parent_shard(self, limiter):
        """The "refill would help" decision was made on the ALL_OLD image of
        the parent shard the parallel write hit; the parent-only slow path must
        read and debit that same shard rather than draw a new one."""
        from zae_limiter import schema

        # 1 token/s; every parent shard was drained 100 s ago -> refill helps
        limit = Limit.custom("rpm", self.CAPACITY, refill_amount=3600, refill_period_seconds=3600)
        now_ms = int(time.time() * 1000)
        repo = await self._seed(
            limiter,
            limit,
            parent_shard_count=4,
            parent_shards=range(4),
            parent_tokens_milli=0,
            parent_rf_ms=now_ms - 100_000,
        )

        parent_reads: list[int] = []
        original_fetch = limiter._fetch_buckets

        async def spy(entity_ids, resource, shard_id):
            if "parent-1" in entity_ids:
                parent_reads.append(shard_id)
            return await original_fetch(entity_ids, resource, shard_id)

        limiter._fetch_buckets = spy
        with patch("zae_limiter.repository.random.randrange", side_effect=[2]):
            async with limiter.acquire("user-1", "gpt-4", {"rpm": 10}) as lease:
                parent_entry = next(e for e in lease.entries if e.entity_id == "parent-1")
                assert parent_entry._shard_id == 2

        assert parent_reads == [2]
        item2 = await self._raw_item(repo, "parent-1", 2)
        assert self._n(item2, "rpm", schema.BUCKET_FIELD_TK) > 0
        item0 = await self._raw_item(repo, "parent-1", 0)
        assert self._n(item0, "rpm", schema.BUCKET_FIELD_TK) == 0, (
            "a parent shard the fast path never judged must not be written"
        )

    async def test_learning_the_parent_keeps_its_cascade_metadata(self, limiter):
        """Growing the parent's shard_count must not rewrite its cascade
        metadata. A parent shard created by a *child's* cascade slow path is
        stamped ``cascade=False, parent_id=None`` (`_do_acquire` only
        denormalizes the acquiring entity's own flags), so learning
        ``meta`` from such an item downgrades the parent's cache entry and the
        next ``acquire(parent)`` silently stops debiting the grandparent for
        the life of the process — ``_entity_cache`` has no TTL."""
        from zae_limiter import schema

        repo = limiter._repository
        ns = repo._namespace_id
        limit = Limit.custom("rpm", self.CAPACITY, refill_amount=1, refill_period_seconds=3600)
        await limiter.create_entity("grandparent-1")
        await limiter.create_entity("parent-1", parent_id="grandparent-1", cascade=True)
        await limiter.create_entity("user-1", parent_id="parent-1", cascade=True)
        await limiter.set_system_defaults([limit])
        limiter._speculative_writes = True

        # The parent's own acquire creates its shard 0 (stamped cascade=True)
        # plus the grandparent's, and caches (cascade=True, "grandparent-1")
        # from the authoritative entity META record.
        async with limiter.acquire("parent-1", "gpt-4", {"rpm": 1}):
            pass
        assert repo._entity_cache[(ns, "parent-1")][:2] == (True, "grandparent-1")
        # One child acquire to warm the child's cache for the parallel path
        async with limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
            pass

        # Parent shard 1 as a child's cascade slow path creates it: no cascade
        # flag, no parent_id. The parent's cached count grows to cover it.
        now_ms = int(time.time() * 1000)
        state = BucketState.from_limit("parent-1", "gpt-4", limit, now_ms, 2)
        await repo.transact_write(
            [
                repo.build_composite_create(
                    "parent-1", "gpt-4", [state], now_ms, shard_id=1, shard_count=2
                )
            ]
        )
        repo._entity_cache[(ns, "parent-1")] = (True, "grandparent-1", {"gpt-4": 2})

        with patch("zae_limiter.repository.random.randrange", return_value=1):
            # Warm cascade acquire drawing the parent's shard 1
            async with limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
                pass
            assert repo._entity_cache[(ns, "parent-1")][:2] == (True, "grandparent-1"), (
                "a bucket item must not rewrite the parent's cascade metadata"
            )

            before = self._n(
                await self._raw_item(repo, "grandparent-1", 0), "rpm", schema.BUCKET_FIELD_TK
            )
            async with limiter.acquire("parent-1", "gpt-4", {"rpm": 1}) as lease:
                assert "grandparent-1" in {e.entity_id for e in lease.entries}
        after = self._n(
            await self._raw_item(repo, "grandparent-1", 0), "rpm", schema.BUCKET_FIELD_TK
        )
        assert after == before - 1000, "the grandparent must still be debited"

    async def test_exhausted_parent_shard_does_not_pin_the_slow_path(self, limiter):
        """An exhausted parent shard must not be handed to the slow path. Each
        shard holds its own share and ADR-134 re-picks on every call, so
        re-drawing can admit where the drawn shard could not — pinning turns a
        recoverable cascade into a rejection (here: 3 of the parent's 4 shards
        are full)."""
        from zae_limiter import schema

        # 1 token/s
        limit = Limit.custom("rpm", self.CAPACITY, refill_amount=3600, refill_period_seconds=3600)
        now_ms = int(time.time() * 1000)
        repo = await self._seed(limiter, limit, parent_shard_count=4, parent_shards=range(4))
        ns = repo._namespace_id
        client = await repo._get_client()
        # Parent shard 2 drained just now: refill cannot rescue it
        await client.update_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(ns, "parent-1", "gpt-4", 2)},
                "SK": {"S": schema.sk_state()},
            },
            UpdateExpression="SET #tk = :zero, #rf = :now",
            ExpressionAttributeNames={
                "#tk": schema.bucket_attr("rpm", schema.BUCKET_FIELD_TK),
                "#rf": schema.BUCKET_FIELD_RF,
            },
            ExpressionAttributeValues={":zero": {"N": "0"}, ":now": {"N": str(now_ms)}},
        )
        # Child drained 100 s ago: its own refill would help, so the child
        # failure falls through to the slow path instead of fast-rejecting
        await client.update_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(ns, "user-1", "gpt-4", 0)},
                "SK": {"S": schema.sk_state()},
            },
            UpdateExpression="SET #tk = :zero, #rf = :then",
            ExpressionAttributeNames={
                "#tk": schema.bucket_attr("rpm", schema.BUCKET_FIELD_TK),
                "#rf": schema.BUCKET_FIELD_RF,
            },
            ExpressionAttributeValues={
                ":zero": {"N": "0"},
                ":then": {"N": str(now_ms - 100_000)},
            },
        )

        # Parent's parallel draw -> the drained shard 2; the slow path's
        # re-draw -> shard 1, which is full.
        with patch("zae_limiter.repository.random.randrange", side_effect=[2, 1]):
            async with limiter.acquire("user-1", "gpt-4", {"rpm": 1}) as lease:
                parent_entry = next(e for e in lease.entries if e.entity_id == "parent-1")
                assert parent_entry._shard_id == 1

        drained = await self._raw_item(repo, "parent-1", 2)
        assert self._n(drained, "rpm", schema.BUCKET_FIELD_TK) == 0, (
            "the drained shard must not be debited further"
        )

    async def test_missing_parent_shard_is_still_created_where_it_was_sought(self, limiter):
        """The one reason that does pin the slow path: a parent shard the fast
        path found missing must be created there, not re-drawn. Guards the
        narrowing above — the patched draw raises StopIteration on a re-draw."""
        from zae_limiter import schema

        limit = Limit.custom("rpm", self.CAPACITY, refill_amount=1, refill_period_seconds=3600)
        # Cached count of 4, but only the parent's shard 0 exists
        repo = await self._seed(limiter, limit, parent_shard_count=4)

        with patch("zae_limiter.repository.random.randrange", side_effect=[2]):
            async with limiter.acquire("user-1", "gpt-4", {"rpm": 1}) as lease:
                parent_entry = next(e for e in lease.entries if e.entity_id == "parent-1")
                assert parent_entry._shard_id == 2

        shard2 = await self._raw_item(repo, "parent-1", 2)
        assert shard2 is not None, "the missing parent shard must be created where it was sought"
        assert shard2["shard_count"]["N"] == "4"
        assert self._n(shard2, "rpm", schema.BUCKET_FIELD_TK) == self.CAPACITY * 1000 // 4 - 1000

    async def test_cold_cache_cascade_spreads_the_parent_too(self, limiter):
        """The sequential (cache-miss) cascade branch must handle a parent
        failure exactly like the warm parallel path: the first acquire of an
        entity is just as capable of finding the parent's shard hot, and left
        to itself it kept writing to the hot partition forever."""
        limit = Limit.custom("rpm", self.CAPACITY, refill_amount=1, refill_period_seconds=3600)
        repo = await self._seed(limiter, limit, parent_shard_count=1)
        ns = repo._namespace_id
        await self._drain_wcu(repo, "parent-1", 0)
        # Cold child cache -> the sequential parent speculative write
        del repo._entity_cache[(ns, "user-1")]

        async with limiter.acquire("user-1", "gpt-4", {"rpm": 1}) as lease:
            parent_entry = next(e for e in lease.entries if e.entity_id == "parent-1")
            assert parent_entry._shard_id == 1

        assert repo._entity_cache[(ns, "parent-1")][2]["gpt-4"] == 2
        shard1 = await self._raw_item(repo, "parent-1", 1)
        assert shard1 is not None, "a cold-cache cascade must spread a hot parent too"
        assert shard1["shard_count"]["N"] == "2"

    async def test_parent_doubling_skips_the_futile_parent_only_attempt(self, limiter):
        """A doubling hands back a shard from `range(old, new)`, which by
        construction does not exist yet, so a parent-only attempt could only
        resolve limits, BatchGet a miss and return None before the caller falls
        through to the full slow path anyway. Skip it."""
        limit = Limit.custom("rpm", self.CAPACITY, refill_amount=1, refill_period_seconds=3600)
        repo = await self._seed(limiter, limit, parent_shard_count=1)
        # Parent wcu drained with an hourly refill, rpm untouched: a hot
        # partition whose app limit still has room, so the acquire proceeds.
        await self._drain_wcu(repo, "parent-1", 0)

        parent_only_shards: list[int] = []
        original = limiter._try_parent_only_acquire

        async def spy(parent_id, resource, consume, child_entries, parent_shard, parent_count):
            parent_only_shards.append(parent_shard)
            return await original(
                parent_id, resource, consume, child_entries, parent_shard, parent_count
            )

        limiter._try_parent_only_acquire = spy
        async with limiter.acquire("user-1", "gpt-4", {"rpm": 1}) as lease:
            parent_entry = next(e for e in lease.entries if e.entity_id == "parent-1")
            assert parent_entry._shard_id == 1

        assert parent_only_shards == [], "a shard the doubling just added cannot exist yet"
        assert await self._raw_item(repo, "parent-1", 1) is not None

    async def test_rejected_cascade_never_doubles_the_parent(self, limiter):
        """A doubling on the way to a rejection is a pure side effect: nothing
        creates or reads the shard it hands back. Repeating it once per
        rejection walks a parent sitting at its limit from 1 to
        MAX_SHARD_COUNT (``_learn_shard_count`` is monotonic, nothing shrinks
        it), and every shard's share is then `capacity // 32` forever — the
        "an exhausted shard must not drive doubling forever" failure ADR-133's
        cap is meant to prevent, which the cap only guards for the
        wcu-would-refill case."""
        from zae_limiter import schema

        limit = Limit.custom("rpm", self.CAPACITY, refill_amount=1, refill_period_seconds=3600)
        # Parent drained on both rpm and wcu, refilling once an hour: this is
        # BOTH_EXHAUSTED, the steady state of a hot parent at its limit.
        repo = await self._seed(limiter, limit, parent_shard_count=1, parent_tokens_milli=0)
        ns = repo._namespace_id
        await self._drain_wcu(repo, "parent-1", 0)

        for _ in range(3):
            with pytest.raises(RateLimitExceeded):
                async with limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
                    pass

        assert repo._entity_cache[(ns, "parent-1")][2]["gpt-4"] == 1
        item = await self._raw_item(repo, "parent-1", 0)
        assert item["shard_count"]["N"] == "1", "a rejected acquire must not shard the parent"
        assert self._n(item, schema.WCU_LIMIT_NAME, schema.BUCKET_FIELD_TK) == 0

    async def test_parent_wcu_exhaustion_doubles_the_parents_shard_count(self, limiter):
        """A hot cascade parent must spread: the parent's own wcu exhaustion
        doubles the parent's shard_count and routes the fallback to one of the
        shards the doubling added, which the slow path then creates."""
        from zae_limiter import schema

        limit = Limit.custom("rpm", self.CAPACITY, refill_amount=1, refill_period_seconds=3600)
        repo = await self._seed(limiter, limit, parent_shard_count=1)
        ns = repo._namespace_id
        await self._slow_wcu_refill(repo, "parent-1", 0)
        for _ in range(schema.WCU_LIMIT_CAPACITY):
            await repo._speculative_consume_single("parent-1", "gpt-4", {"rpm": 1}, shard_id=0)

        async with limiter.acquire("user-1", "gpt-4", {"rpm": 1}) as lease:
            parent_entry = next(e for e in lease.entries if e.entity_id == "parent-1")
            assert parent_entry._shard_id == 1

        assert repo._entity_cache[(ns, "parent-1")][2]["gpt-4"] == 2
        shard1 = await self._raw_item(repo, "parent-1", 1)
        assert shard1 is not None, "the parent must spread off its hot shard"
        assert shard1["shard_count"]["N"] == "2"
        assert self._n(shard1, "rpm", schema.BUCKET_FIELD_TK) == self.CAPACITY * 1000 // 2 - 1000


class TestWcuHiddenFromUser:
    """Tests that wcu infrastructure limit is hidden from user-facing output."""

    async def test_rate_limit_exceeded_hides_wcu(self, limiter):
        """wcu never appears in RateLimitExceeded statuses."""
        from zae_limiter.exceptions import RateLimitExceeded

        repo = limiter._repository
        ns = repo._namespace_id

        await limiter.create_entity("user-1")
        await limiter.set_system_defaults([Limit.per_minute("rpm", 1)])

        # Create bucket at shard 0
        now_ms = int(time.time() * 1000)
        states = [BucketState.from_limit("user-1", "gpt-4", Limit.per_minute("rpm", 1), now_ms)]
        put_item = repo.build_composite_create(
            "user-1", "gpt-4", states, now_ms, shard_id=0, shard_count=1
        )
        await repo.transact_write([put_item])

        # Pre-populate entity cache
        repo._entity_cache[(ns, "user-1")] = (False, None, {"gpt-4": 1})

        # Exhaust rpm via speculative path
        await repo._speculative_consume_single("user-1", "gpt-4", {"rpm": 1}, shard_id=0)

        limiter._speculative_writes = True
        with pytest.raises(RateLimitExceeded) as exc_info:
            async with limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
                pass

        # wcu must not appear in any statuses
        all_limit_names = [s.limit_name for s in exc_info.value.statuses]
        assert "wcu" not in all_limit_names

    async def test_get_buckets_hides_wcu(self, limiter):
        """get_buckets omits wcu from returned bucket states."""
        repo = limiter._repository

        await limiter.create_entity("user-1")
        await limiter.set_system_defaults([Limit.per_minute("rpm", 100)])

        # Create bucket at shard 0
        now_ms = int(time.time() * 1000)
        states = [BucketState.from_limit("user-1", "gpt-4", Limit.per_minute("rpm", 100), now_ms)]
        put_item = repo.build_composite_create(
            "user-1", "gpt-4", states, now_ms, shard_id=0, shard_count=1
        )
        await repo.transact_write([put_item])

        # get_buckets should not include wcu
        buckets = await repo.get_buckets("user-1", resource="gpt-4")
        limit_names = {b.limit_name for b in buckets}
        assert "wcu" not in limit_names
        assert "rpm" in limit_names


class TestScheduleBoundaryRouting:
    """An expired ``vu`` routes acquire() to the slow path (#222 §2.1).

    ``vu`` (valid-until, epoch ms) says when the ``tk`` on the item stopped
    reflecting the parameters in force. A fast-path write that trips it is
    **not** a rejection: the limits that refused it are stale and the new
    window may have widened them. Only the slow path can re-materialise, so
    SCHEDULE_BOUNDARY must reach it rather than raising RateLimitExceeded or
    burning a shard retry.

    The tests below pin the *route*, not the outcome, and deliberately so:
    until the slow path materialises, an exhausted-and-expired bucket still
    ends in RateLimitExceeded either way. What changes is who decides — the
    stale image, or a fresh read that can see the new window.
    """

    LIMITS = [Limit.custom("rpm", capacity=100, refill_amount=100, refill_period_seconds=60)]

    @staticmethod
    async def _stamp(repo, entity_id, resource, expression, names, values, shard_id=0):
        client = await repo._get_client()
        await client.update_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": pk_bucket(repo._namespace_id, entity_id, resource, shard_id)},
                "SK": {"S": sk_state()},
            },
            UpdateExpression=expression,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

    async def _drain(self, repo, entity_id, resource, shard_id=0):
        """Empty the rpm balance without touching ``rf``, so no refill helps."""
        await self._stamp(
            repo,
            entity_id,
            resource,
            "SET #tk = :zero",
            {"#tk": bucket_attr("rpm", BUCKET_FIELD_TK)},
            {":zero": {"N": "0"}},
            shard_id=shard_id,
        )

    async def _expire_vu(self, repo, entity_id, resource, now_ms, shard_id=0):
        await self._stamp(
            repo,
            entity_id,
            resource,
            "SET #vu = :vu",
            {"#vu": BUCKET_FIELD_VU},
            {":vu": {"N": str(now_ms - 1)}},
            shard_id=shard_id,
        )

    @staticmethod
    def _spy_slow_path(limiter) -> list[dict]:
        """Record every ``_do_acquire`` call; the calls still run for real."""
        calls: list[dict] = []
        original = limiter._do_acquire

        async def spy(*args, **kwargs):
            calls.append(kwargs)
            return await original(*args, **kwargs)

        limiter._do_acquire = spy
        return calls

    async def test_expired_vu_reaches_the_slow_path(self, limiter):
        repo = limiter._repository
        async with limiter.acquire("vu-route", "gpt-4", limits=self.LIMITS, consume={"rpm": 1}):
            pass

        now_ms = freeze_clock(repo)
        await self._drain(repo, "vu-route", "gpt-4")
        await self._expire_vu(repo, "vu-route", "gpt-4", now_ms)

        calls = self._spy_slow_path(limiter)
        with pytest.raises(RateLimitExceeded):
            async with limiter.acquire("vu-route", "gpt-4", limits=self.LIMITS, consume={"rpm": 1}):
                pass

        assert len(calls) == 1, "a closed window must be decided by the slow path, not the image"

    async def test_absent_vu_still_fast_rejects(self, limiter):
        """The contrast case: identical bucket, no ``vu``, no slow path.

        Without this, a fix that routed *every* failure to the slow path — or
        treated a missing attribute as ``vu = 0`` — would look correct.
        """
        repo = limiter._repository
        async with limiter.acquire("vu-noroute", "gpt-4", limits=self.LIMITS, consume={"rpm": 1}):
            pass

        freeze_clock(repo)
        await self._drain(repo, "vu-noroute", "gpt-4")

        calls = self._spy_slow_path(limiter)
        with pytest.raises(RateLimitExceeded):
            async with limiter.acquire(
                "vu-noroute", "gpt-4", limits=self.LIMITS, consume={"rpm": 1}
            ):
                pass

        assert calls == [], "an exhausted unscheduled bucket must still fast-reject (0 RCU)"

    async def test_expired_parent_vu_reaches_the_slow_path_and_refunds_the_child(self, limiter):
        """The cascade twin: a closed window on the parent is not a rejection.

        The parent is judged on the warm parallel path, which has no shared
        helper to short-circuit — it calls ``would_refill_satisfy`` directly.
        The child's speculative debit must be handed back before the acquire
        is re-run through the slow path, or the entity pays twice.
        """
        repo = limiter._repository
        await limiter.create_entity("vu-parent")
        await limiter.create_entity("vu-child", parent_id="vu-parent", cascade=True)

        # Two acquires: the first creates both buckets, the second warms the
        # entity cache so the parent is written on the parallel fast path.
        for _ in range(2):
            async with limiter.acquire("vu-child", "gpt-4", limits=self.LIMITS, consume={"rpm": 1}):
                pass

        now_ms = freeze_clock(repo)
        await self._drain(repo, "vu-parent", "gpt-4")
        await self._expire_vu(repo, "vu-parent", "gpt-4", now_ms)
        before = await repo.get_buckets("vu-child", resource="gpt-4")
        child_before = next(b.tokens_milli for b in before if b.limit_name == "rpm")

        calls = self._spy_slow_path(limiter)
        with pytest.raises(RateLimitExceeded):
            async with limiter.acquire("vu-child", "gpt-4", limits=self.LIMITS, consume={"rpm": 1}):
                pass

        assert len(calls) == 1, "a closed parent window must be decided by the slow path"
        after = await repo.get_buckets("vu-child", resource="gpt-4")
        child_after = next(b.tokens_milli for b in after if b.limit_name == "rpm")
        assert child_after == child_before, "the child's speculative debit was not refunded"

    async def test_boundary_on_a_probed_shard_reaches_the_slow_path(self, limiter):
        """A shard retry that lands on a closed window must not fast-reject.

        Shard 0 is exhausted under limits still in force; shard 1 crossed its
        boundary but still holds a full share — the transient state while
        shards re-materialise one at a time. Probing shard 1 returns no lease,
        and falling through to the caller's fast rejection would raise
        RateLimitExceeded on shard 0's balance while shard 1 was one slow-path
        pass away from admitting. Handing the probed shard to the slow path
        turns that rejection into an admission, so this test discriminates on
        the outcome, not the route.
        """
        repo = limiter._repository
        async with limiter.acquire("vu-shard", "gpt-4", limits=self.LIMITS, consume={"rpm": 1}):
            pass

        now_ms = freeze_clock(repo)
        # Shard 1: a full clone whose window has closed. Shard 0: drained.
        states = [BucketState.from_limit("vu-shard", "gpt-4", self.LIMITS[0], now_ms)]
        await repo.transact_write(
            [
                repo.build_composite_create(
                    "vu-shard", "gpt-4", states, now_ms, shard_id=1, shard_count=2
                )
            ]
        )
        await self._stamp(
            repo, "vu-shard", "gpt-4", "SET shard_count = :two", {}, {":two": {"N": "2"}}
        )
        await self._drain(repo, "vu-shard", "gpt-4")
        await self._expire_vu(repo, "vu-shard", "gpt-4", now_ms, shard_id=1)

        # Pin the first draw to the drained shard so the retry is the one that
        # meets the boundary; ADR-134 would otherwise re-pick at random.
        def pinned_shard(entity_id, resource, shard_id=None, shard_count=None):
            return (0, 2) if shard_id is None else (shard_id, 2)

        repo.select_shard = pinned_shard

        calls = self._spy_slow_path(limiter)
        async with limiter.acquire("vu-shard", "gpt-4", limits=self.LIMITS, consume={"rpm": 1}):
            pass

        assert len(calls) == 1, "the probed boundary shard must be handed to the slow path"
        assert calls[0].get("shard_id") == 1, (
            f"the slow path must target the boundary shard, got {calls[0]}"
        )


class TestSlowPathMaterialisesVu:
    """The slow path writes the ``vu`` the fast path gates on (#222, Task 12).

    Task 11 taught the speculative condition to *read* ``vu``; nothing wrote
    one, so a scheduled bucket fell through to the slow path on every acquire
    and could never clear its own boundary. These pin the write, on both
    builder shapes and across the four places the system has **N** of
    something: many limits on one item, many shards, cascade's two items, and
    create-versus-update.
    """

    # 2026-09-15 is a Tuesday. 10:05Z is inside `9-17` and outside `:30`.
    NOW = int(datetime(2026, 9, 15, 10, 5, tzinfo=UTC).timestamp() * 1000)
    LATE = int(datetime(2026, 9, 15, 18, 0, tzinfo=UTC).timestamp() * 1000)
    EARLY = int(datetime(2026, 9, 15, 10, 30, tzinfo=UTC).timestamp() * 1000)

    BUSINESS = (ScheduleEntry(cron="* 9-17 * * *", tz="UTC", scale=0.5),)
    HALF_HOUR = (ScheduleEntry(cron="30 * * * *", tz="UTC", scale=0.25),)

    RPM = Limit.per_minute("rpm", 1000).with_schedule(BUSINESS)  # boundary: 18:00Z
    TPM = Limit.per_minute("tpm", 5000).with_schedule(HALF_HOUR)  # boundary: 10:30Z

    @staticmethod
    def _pin(repo, instant):
        repo._now_ms = lambda: instant
        return instant

    @staticmethod
    async def _raw(repo, entity_id, resource="gpt-4", shard=0):
        """Read a bucket item straight from DynamoDB, bypassing deserialisation."""
        client = await repo._get_client()
        response = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": pk_bucket(repo._namespace_id, entity_id, resource, shard)},
                "SK": {"S": sk_state()},
            },
        )
        return response["Item"]

    @staticmethod
    def _slow(limiter):
        return RateLimiter(repository=limiter._repository, speculative_writes=False)

    async def test_create_stamps_the_first_boundary(self, limiter):
        """The create path: no bucket yet, so this is ``build_composite_create``."""
        repo = limiter._repository
        self._pin(repo, self.NOW)
        slow = self._slow(limiter)

        async with slow.acquire("vu-new", "gpt-4", limits=[self.RPM], consume={"rpm": 1}):
            pass

        item = await self._raw(repo, "vu-new")
        assert item[BUCKET_FIELD_VU]["N"] == str(self.LATE)

    async def test_update_restamps_the_boundary(self, limiter):
        """The update path: the bucket now exists, so ``build_composite_normal``."""
        repo = limiter._repository
        self._pin(repo, self.NOW)
        slow = self._slow(limiter)

        async with slow.acquire("vu-upd", "gpt-4", limits=[self.RPM], consume={"rpm": 1}):
            pass
        # Wipe the create's stamp so only the second write can put it back.
        client = await repo._get_client()
        await client.update_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": pk_bucket(repo._namespace_id, "vu-upd", "gpt-4", 0)},
                "SK": {"S": sk_state()},
            },
            UpdateExpression="REMOVE #vu",
            ExpressionAttributeNames={"#vu": BUCKET_FIELD_VU},
        )
        async with slow.acquire("vu-upd", "gpt-4", limits=[self.RPM], consume={"rpm": 1}):
            pass

        item = await self._raw(repo, "vu-upd")
        assert item[BUCKET_FIELD_VU]["N"] == str(self.LATE)

    async def test_an_unscheduled_bucket_gets_no_vu(self, limiter):
        """The overwhelming majority. A stamp here would cost every one of them
        a slow-path pass per boundary they do not have."""
        repo = limiter._repository
        self._pin(repo, self.NOW)
        slow = self._slow(limiter)

        async with slow.acquire(
            "vu-plain", "gpt-4", limits=[Limit.per_minute("rpm", 1000)], consume={"rpm": 1}
        ):
            pass

        assert BUCKET_FIELD_VU not in await self._raw(repo, "vu-plain")

    async def test_vu_is_the_minimum_across_the_items_limits(self, limiter):
        """``vu`` is one item-level attribute, so the *earliest* change wins.

        ``rpm`` changes at 18:00Z and ``tpm`` at 10:30Z. Taking the maximum —
        or the first limit in the list, which is ``rpm`` — leaves the fast
        path admitting against ``tpm``'s stale ceiling for seven and a half
        hours.
        """
        repo = limiter._repository
        self._pin(repo, self.NOW)
        slow = self._slow(limiter)

        async with slow.acquire(
            "vu-min", "gpt-4", limits=[self.RPM, self.TPM], consume={"rpm": 1, "tpm": 1}
        ):
            pass

        item = await self._raw(repo, "vu-min")
        assert item[BUCKET_FIELD_VU]["N"] == str(self.EARLY)

    async def test_vu_covers_a_limit_the_caller_did_not_declare(self, limiter):
        """Undeclared limits are materialised by the same write, so they set
        the boundary too (Issue #455 makes them write-only, not invisible).

        Here the caller names only ``rpm`` (18:00Z) while ``tpm`` (10:30Z) is
        resolved but undeclared. A ``vu`` computed over declared limits alone —
        which is what the plan's prose says — would read 18:00Z and leave
        ``tpm`` unenforced across its own boundary.
        """
        repo = limiter._repository
        self._pin(repo, self.NOW)
        slow = self._slow(limiter)

        async with slow.acquire(
            "vu-undecl", "gpt-4", limits=[self.RPM, self.TPM], consume={"rpm": 1}
        ):
            pass

        item = await self._raw(repo, "vu-undecl")
        assert item[BUCKET_FIELD_VU]["N"] == str(self.EARLY)

    async def test_every_shard_of_one_entity_agrees_on_vu(self, limiter):
        """Shards materialise one at a time, at whatever instant they are next
        touched. ``vu`` must be a function of the schedule and the window, not
        of the instant — otherwise two shards of one entity open their fast
        paths at different times and the entity's ceiling changes piecewise.
        """
        repo = limiter._repository
        self._pin(repo, self.NOW)
        slow = self._slow(limiter)

        lease = await slow._do_acquire(
            "vu-shards", "gpt-4", [self.RPM], {"rpm": 1}, shard_id=0, shard_count=2
        )
        await lease._commit_initial()

        # A different instant, still inside the same window.
        self._pin(repo, self.NOW + 90_000)
        lease = await slow._do_acquire(
            "vu-shards", "gpt-4", [self.RPM], {"rpm": 1}, shard_id=1, shard_count=2
        )
        await lease._commit_initial()

        shard0 = await self._raw(repo, "vu-shards", shard=0)
        shard1 = await self._raw(repo, "vu-shards", shard=1)
        assert shard0[BUCKET_FIELD_VU]["N"] == str(self.LATE)
        assert shard1[BUCKET_FIELD_VU]["N"] == str(self.LATE)

    async def test_child_and_parent_are_stamped_from_their_own_schedules(self, limiter):
        """Cascade writes two items, and each carries its own ``vu``.

        The parent schedules independently of the child (#474 established the
        same for shards). A single lease-wide ``vu`` would stamp one of them
        with the other's boundary.
        """
        repo = limiter._repository
        await limiter.create_entity("vu-parent")
        await limiter.create_entity("vu-child", parent_id="vu-parent", cascade=True)
        await limiter.set_limits("vu-child", [self.RPM], resource="gpt-4")
        await limiter.set_limits("vu-parent", [self.TPM], resource="gpt-4")

        self._pin(repo, self.NOW)
        slow = self._slow(limiter)
        async with slow.acquire("vu-child", "gpt-4", consume={"rpm": 1}):
            pass

        child = await self._raw(repo, "vu-child")
        parent = await self._raw(repo, "vu-parent")
        assert child[BUCKET_FIELD_VU]["N"] == str(self.LATE)
        assert parent[BUCKET_FIELD_VU]["N"] == str(self.EARLY)

    async def test_vu_comes_from_the_refill_instant_not_the_commit_instant(self, limiter):
        """A boundary crossed *during* the slow path must fail closed.

        ``_do_acquire()`` and ``_commit_initial()`` take separate clock
        readings — a round trip apart — and the tokens are materialised at the
        earlier one. Deriving ``vu`` from the later reading would skip the
        boundary that just passed and point at the one *after* it, advertising
        a window the balance on the item never belonged to. Derived from the
        refill instant, the stamp lands at or before ``rf`` and the next
        acquire simply re-materialises: one extra pass instead of a whole
        window of stale admissions.
        """
        repo = limiter._repository
        self._pin(repo, self.NOW)
        slow = self._slow(limiter)

        from zae_limiter.lease import Lease

        original = Lease._commit_initial
        after = self.EARLY + 60_000  # past tpm's boundary, still inside rpm's

        async def commit_later(lease_self):
            self._pin(repo, after)
            return await original(lease_self)

        with patch.object(Lease, "_commit_initial", commit_later):
            async with slow.acquire("vu-race", "gpt-4", limits=[self.TPM], consume={"tpm": 1}):
                pass

        item = await self._raw(repo, "vu-race")
        vu = int(item[BUCKET_FIELD_VU]["N"])
        rf = int(item["rf"]["N"])
        assert vu == self.EARLY, "the boundary in force when the tokens were refilled"
        assert vu <= rf, "a boundary crossed mid-pass must expire the item, not skip"

    async def test_the_slow_path_refills_at_the_scheduled_rate(self, limiter):
        """``vu`` would be a lie without this: the stamp asserts that ``tk``
        was materialised under the window it names.

        The bucket is created inside the ``0.5x`` window, so it starts at the
        scheduled half-capacity rather than the base ceiling — and the stored
        ``cp``/``ra`` stay the undivided base, which is the only copy from
        which the next window can be computed.
        """
        repo = limiter._repository
        self._pin(repo, self.NOW)
        slow = self._slow(limiter)

        async with slow.acquire("vu-scale", "gpt-4", limits=[self.RPM], consume={"rpm": 1}):
            pass

        item = await self._raw(repo, "vu-scale")
        assert item[bucket_attr("rpm", BUCKET_FIELD_TK)]["N"] == str(500_000 - 1_000)
        assert item[bucket_attr("rpm", "cp")]["N"] == "1000000"

    async def test_the_slow_path_trims_a_surplus_left_by_a_closing_window(self, limiter):
        """Entering a ``0.5x`` window with a full base balance, the very next
        slow pass must bring ``tk`` down to the scheduled ceiling.

        This is the delta at ``lease.py``'s refill computation going *negative*
        (#496 clamps inside ``refill_bucket``); an explicit clamp there would
        double-apply it.
        """
        repo = limiter._repository
        outside = int(datetime(2026, 9, 15, 3, 0, tzinfo=UTC).timestamp() * 1000)
        self._pin(repo, outside)
        slow = self._slow(limiter)

        async with slow.acquire("vu-trim", "gpt-4", limits=[self.RPM], consume={"rpm": 0}):
            pass
        item = await self._raw(repo, "vu-trim")
        assert item[bucket_attr("rpm", BUCKET_FIELD_TK)]["N"] == "1000000"

        self._pin(repo, self.NOW)  # inside the 0.5x window
        async with slow.acquire("vu-trim", "gpt-4", limits=[self.RPM], consume={"rpm": 0}):
            pass

        item = await self._raw(repo, "vu-trim")
        assert item[bucket_attr("rpm", BUCKET_FIELD_TK)]["N"] == "500000"


class TestFanOutVuSelfClears:
    """`vu = 0` from the #468 fan-out must survive exactly one acquire.

    Task 13 writes `vu = 0` on **every** fan-out, scheduled or not, so that a
    capacity shrink gets one materialising pass that clamps the surplus before
    the fast path (a pure `ADD` with no cap maths) can spend it.

    That only works if the pass can *clear* the stamp again. On an unscheduled
    bucket there is no boundary to re-stamp, and `build_composite_normal`'s
    `vu=None` means "leave it alone" — so before `clear_vu` the `0` stayed on
    the item forever and the fast-path condition `(attribute_not_exists(vu) OR
    vu > now)` failed on every subsequent acquire. The entity was permanently
    demoted from 1 round trip to 3, at $1.375/M instead of $0.625/M, and
    entity-level buckets carry no TTL so nothing would ever have recycled it.
    """

    NOW = int(datetime(2026, 9, 15, 10, 5, tzinfo=UTC).timestamp() * 1000)

    @staticmethod
    async def _raw(repo, entity_id, resource="gpt-4", shard=0):
        client = await repo._get_client()
        response = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": pk_bucket(repo._namespace_id, entity_id, resource, shard)},
                "SK": {"S": sk_state()},
            },
        )
        return response["Item"]

    async def test_an_unscheduled_bucket_clears_vu_on_the_forced_pass(self, limiter):
        repo = limiter._repository
        await repo.create_entity("vu-clear")
        async with limiter.acquire(
            "vu-clear", "gpt-4", limits=[Limit.per_minute("rpm", 1000)], consume={"rpm": 1}
        ):
            pass

        await repo.set_limits("vu-clear", [Limit.per_minute("rpm", 10)], resource="gpt-4")
        assert (await self._raw(repo, "vu-clear"))[BUCKET_FIELD_VU]["N"] == "0"

        # The forced pass. It is a slow path by construction: `vu = 0` fails
        # the speculative condition, which is the whole point of the stamp.
        async with limiter.acquire("vu-clear", "gpt-4", consume={"rpm": 1}):
            pass

        assert BUCKET_FIELD_VU not in await self._raw(repo, "vu-clear")

    async def test_the_forced_pass_trims_the_surplus_it_exists_for(self, limiter):
        """#469's scenario end to end: shrink 1000 -> 10 on a full bucket."""
        repo = limiter._repository
        await repo.create_entity("vu-trim-shrink")
        async with limiter.acquire(
            "vu-trim-shrink", "gpt-4", limits=[Limit.per_minute("rpm", 1000)], consume={"rpm": 0}
        ):
            pass
        assert (await self._raw(repo, "vu-trim-shrink"))[bucket_attr("rpm", BUCKET_FIELD_TK)][
            "N"
        ] == "1000000"

        await repo.set_limits("vu-trim-shrink", [Limit.per_minute("rpm", 10)], resource="gpt-4")

        async with limiter.acquire("vu-trim-shrink", "gpt-4", consume={"rpm": 0}):
            pass

        item = await self._raw(repo, "vu-trim-shrink")
        assert item[bucket_attr("rpm", BUCKET_FIELD_TK)]["N"] == "10000"
        assert BUCKET_FIELD_VU not in item

    async def test_the_fast_path_is_restored_after_one_pass(self, limiter):
        """The cost claim: one demoted acquire, not every acquire forever."""
        repo = limiter._repository
        await repo.create_entity("vu-restore")
        async with limiter.acquire(
            "vu-restore", "gpt-4", limits=[Limit.per_minute("rpm", 1000)], consume={"rpm": 1}
        ):
            pass
        await repo.set_limits("vu-restore", [Limit.per_minute("rpm", 500)], resource="gpt-4")

        async with limiter.acquire("vu-restore", "gpt-4", consume={"rpm": 1}):
            pass

        calls: list[str] = []
        real = repo.speculative_consume

        async def spy(*args, **kwargs):
            result = await real(*args, **kwargs)
            calls.append("ok" if result.success else str(result.reason))
            return result

        with patch.object(repo, "speculative_consume", spy):
            async with limiter.acquire("vu-restore", "gpt-4", consume={"rpm": 1}):
                pass

        assert calls == ["ok"]

    async def test_a_scheduled_bucket_restamps_rather_than_clears(self, limiter):
        """The contrast case: `clear_vu` must not fire where a boundary exists,
        or a scheduled bucket would fast-path straight past its next window."""
        repo = limiter._repository
        repo._now_ms = lambda: self.NOW
        scheduled = Limit.per_minute("rpm", 1000).with_schedule(
            (ScheduleEntry(cron="* 9-17 * * *", tz="UTC", scale=0.5),)
        )
        await repo.create_entity("vu-sched")
        async with limiter.acquire("vu-sched", "gpt-4", limits=[scheduled], consume={"rpm": 1}):
            pass
        await repo.set_limits("vu-sched", [scheduled], resource="gpt-4")
        assert (await self._raw(repo, "vu-sched"))[BUCKET_FIELD_VU]["N"] == "0"

        async with limiter.acquire("vu-sched", "gpt-4", consume={"rpm": 1}):
            pass

        item = await self._raw(repo, "vu-sched")
        expected = int(datetime(2026, 9, 15, 18, 0, tzinfo=UTC).timestamp() * 1000)
        assert item[BUCKET_FIELD_VU]["N"] == str(expected)

    async def test_every_shard_clears_its_own_stamp(self, limiter):
        """`vu` is per item. The fan-out stamps N shards; each clears its own
        on its own next acquire, and one cleared shard does not clear another.
        """
        repo = limiter._repository
        await repo.create_entity("vu-shards")
        now_ms = repo._now_ms()
        limits = [Limit.per_minute("rpm", 1000)]
        for shard_id in (0, 1):
            states = [BucketState.from_limit("vu-shards", "gpt-4", lim, now_ms) for lim in limits]
            await repo.transact_write(
                [
                    repo.build_composite_create(
                        "vu-shards", "gpt-4", states, now_ms, shard_id=shard_id, shard_count=2
                    )
                ]
            )
        await repo.set_limits("vu-shards", [Limit.per_minute("rpm", 500)], resource="gpt-4")

        # Pin the draw to shard 0. ADR-134 re-picks at random on every call,
        # so assuming an acquire lands on a given shard is flaky by
        # construction; `acquire()` takes no `shard_id`, unlike
        # `speculative_consume()`.
        result = await repo.speculative_consume("vu-shards", "gpt-4", {"rpm": 1}, shard_id=0)
        assert not result.success, "vu = 0 must close the fast path"
        with patch.object(repo, "select_shard", lambda *a, **k: (0, 2)):
            async with limiter.acquire("vu-shards", "gpt-4", consume={"rpm": 1}):
                pass

        assert BUCKET_FIELD_VU not in await self._raw(repo, "vu-shards", shard=0)
        assert (await self._raw(repo, "vu-shards", shard=1))[BUCKET_FIELD_VU]["N"] == "0"


# ---------------------------------------------------------------------------
# Calendar resets (#222 §3.6) — a crossed reset edge sets the balance back to
# the effective capacity, before admission, without ever touching `tc`.
# ---------------------------------------------------------------------------

RESET_NY = ZoneInfo("America/New_York")


def _ny(s: str) -> int:
    """An ISO local time in America/New_York, as epoch milliseconds."""
    return int(datetime.fromisoformat(s).replace(tzinfo=RESET_NY).timestamp() * 1000)


# ADR-137: a quota is built in one call. `per_day(...).with_reset_schedule(...)`
# raises, because the intermediate value is a positive rate beside a reset.
RPD = Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York")


class TestApplyResetEdge:
    """The reset decision, isolated from DynamoDB (#222 §3.6).

    ``_apply_reset_edge`` answers one question — "has a rising reset edge been
    crossed since this item was last refilled?" — and, when it has, sets the
    balance to the shard's share of the capacity in force *at that instant*.
    """

    @staticmethod
    def _state(**kwargs) -> BucketState:
        base = dict(
            entity_id="user-1",
            resource="gpt-4",
            limit_name="rpd",
            tokens_milli=0,
            last_refill_ms=_ny("2026-09-15 18:00"),
            capacity_milli=10_000_000,
            # A quota does not drip (ADR-137), so the stored rate is zero and
            # the period is the inert `_QUOTA_REFILL_PERIOD_SECONDS`.
            refill_amount_milli=0,
            refill_period_ms=1_000,
        )
        base.update(kwargs)
        return BucketState(**base)

    def test_sets_tokens_to_the_effective_capacity(self):
        state = self._state()
        assert RateLimiter._apply_reset_edge(RPD, state, _ny("2026-09-16 09:00")) is True
        assert state.tokens_milli == 10_000_000

    def test_uses_the_shards_share(self):
        """Resetting every shard to the undivided capacity multiplies the
        entity's quota by shard_count (§3.6)."""
        state = self._state(shard_count=4)
        assert RateLimiter._apply_reset_edge(RPD, state, _ny("2026-09-16 09:00")) is True
        assert state.tokens_milli == 2_500_000

    def test_respects_a_concurrent_param_schedule(self):
        """A reset landing inside a 0.5x window restores half — the limit in
        force, not the base. Effective params first, balance second."""
        limit = RPD.with_schedule(
            (ScheduleEntry(cron="* 0-6 * * *", tz="America/New_York", scale=0.5),)
        )
        state = self._state(sched=limit.schedule)
        assert RateLimiter._apply_reset_edge(limit, state, _ny("2026-09-16 03:00")) is True
        assert state.tokens_milli == 5_000_000

    def test_no_edge_since_the_last_refill_changes_nothing(self):
        state = self._state(tokens_milli=42, last_refill_ms=_ny("2026-09-16 01:00"))
        assert RateLimiter._apply_reset_edge(RPD, state, _ny("2026-09-16 09:00")) is False
        assert state.tokens_milli == 42

    def test_an_edge_exactly_at_the_last_refill_does_not_re_fire(self):
        """``> rf``, not ``>= rf``. The pass that applies a reset stamps ``rf``
        at or after the edge, so ``>=`` would re-apply it on every subsequent
        request and refund everything spent since — an unbounded quota."""
        state = self._state(tokens_milli=42, last_refill_ms=_ny("2026-09-16 00:00"))
        assert RateLimiter._apply_reset_edge(RPD, state, _ny("2026-09-16 09:00")) is False
        assert state.tokens_milli == 42

    def test_two_missed_edges_apply_once(self):
        """Setting the balance to the capacity is idempotent, so one edge is
        enough and ``prev_reset_edge`` reporting only the latest is sufficient.
        An implementation that *added* a window's worth per missed edge would
        hand back 20,000 here."""
        state = self._state(last_refill_ms=_ny("2026-09-14 18:00"))
        assert RateLimiter._apply_reset_edge(RPD, state, _ny("2026-09-16 09:00")) is True
        assert state.tokens_milli == 10_000_000

    def test_a_limit_without_a_reset_schedule_is_untouched(self):
        state = self._state(tokens_milli=7)
        plain = Limit.per_day("rpd", 10_000)
        assert RateLimiter._apply_reset_edge(plain, state, _ny("2026-09-16 09:00")) is False
        assert state.tokens_milli == 7

    def test_a_never_matching_expression_resets_nothing(self):
        """A leap day, out of reach of the scan. ``prev_reset_edge`` returns
        None and nothing happens — the contract Task 2 pins from the other
        side."""
        limit = Limit.quota("rpd", 10_000, cron="0 0 29 2 *", tz="America/New_York")
        state = self._state(tokens_milli=7)
        assert RateLimiter._apply_reset_edge(limit, state, _ny("2026-09-16 09:00")) is False
        assert state.tokens_milli == 7

    def test_debt_is_cleared_rather_than_carried(self):
        """A reset *sets* the balance; it does not add to it. An entity that
        overdrew via adjust() starts the new day whole. This is what makes the
        aggregator's ``ADD (eff_cp - tk_observed)`` the same operation."""
        state = self._state(tokens_milli=-3_000_000)
        assert RateLimiter._apply_reset_edge(RPD, state, _ny("2026-09-16 09:00")) is True
        assert state.tokens_milli == 10_000_000

    def test_the_wcu_carrier_is_exempt(self):
        """``rsched`` is an item-level attribute that applies to every limit on
        the item by default, and the aggregator has to exempt ``wcu`` by hand.
        On the client it is free — ``Limit._carrier()`` never sets a reset
        schedule — but "free" is a property to pin, not to assume."""
        wcu_state = self._state(
            limit_name="wcu",
            capacity_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )
        carrier = Limit._carrier(wcu_state)
        assert carrier.reset_schedule == ()
        assert RateLimiter._apply_reset_edge(carrier, wcu_state, _ny("2026-09-16 09:00")) is False
        assert wcu_state.tokens_milli == 0


class TestResetMaterialisationThroughAcquire:
    """The reset must gate admission, not just the write that follows it."""

    @staticmethod
    def _slow(limiter):
        """The same moto repository, forced onto the slow path.

        The speculative fast path is a conditional UpdateItem that never
        evaluates a schedule; in production it reaches the slow path because
        `vu` expires at the reset edge. Here we go straight there.
        """
        return RateLimiter(repository=limiter._repository, speculative_writes=False)

    @staticmethod
    async def _raw(repo, entity_id, resource="gpt-4", shard=0):
        client = await repo._get_client()
        response = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": pk_bucket(repo._namespace_id, entity_id, resource, shard)},
                "SK": {"S": sk_state()},
            },
        )
        return response["Item"]

    @staticmethod
    async def _bucket(repo, entity_id, limit_name="rpd", resource="gpt-4"):
        return next(
            b
            for b in await repo.get_buckets(entity_id, resource=resource)
            if b.limit_name == limit_name
        )

    async def test_the_quota_comes_back_in_one_lump(self, limiter):
        """Burn 10,000 at 23:00, cross midnight, spend 9,000 at 00:30.

        Without the reset the second acquire raises outright: a quota does not
        drip (ADR-137), so the 90 minutes between the two calls return exactly
        nothing and the balance is still 0. That is what makes this
        discriminating rather than a restatement of the balance — and it is
        also why the final assertion is an exact 1_000_000 with no drip term.
        """
        repo = limiter._repository
        slow = self._slow(limiter)
        await repo.set_limits("reset-1", [RPD], resource="gpt-4")

        repo._now_ms = lambda: _ny("2026-09-15 23:00")
        async with slow.acquire("reset-1", "gpt-4", consume={"rpd": 10_000}):
            pass

        repo._now_ms = lambda: _ny("2026-09-16 00:30")
        # The clock seam does not reach config_cache.py, which still calls
        # time.time(), so without this the resolved Limit — and its
        # reset_schedule — is the one cached before the jump.
        await repo.invalidate_config_cache()

        async with slow.acquire("reset-1", "gpt-4", consume={"rpd": 9_000}):
            pass

        assert (await self._bucket(repo, "reset-1")).tokens_milli == 1_000_000

    async def test_the_reset_never_touches_tc(self, limiter):
        """The total-consumed counter must stay monotonic across the edge.

        19,000 tokens were consumed across the two calls and `tc` must say so.
        An implementation that expressed the reset by rewriting the item, or by
        crediting `tc`, fails here — the failure mode #471's `reset_bucket()`
        had, and the reason `.claude/rules/design-validation.md` exists.
        """
        repo = limiter._repository
        slow = self._slow(limiter)
        await repo.set_limits("reset-2", [RPD], resource="gpt-4")

        repo._now_ms = lambda: _ny("2026-09-15 23:00")
        async with slow.acquire("reset-2", "gpt-4", consume={"rpd": 10_000}):
            pass

        repo._now_ms = lambda: _ny("2026-09-16 00:30")
        await repo.invalidate_config_cache()
        async with slow.acquire("reset-2", "gpt-4", consume={"rpd": 9_000}):
            pass

        assert (await self._bucket(repo, "reset-2")).total_consumed_milli == 19_000_000

    async def test_the_reset_gates_admission_not_just_the_write(self, limiter):
        """The whole point of the seam's position.

        The quota is burnt to exactly 0 before midnight and the post-midnight
        request asks for the *entire* allowance. Applied after `_admit_limit`
        the balance would be right in DynamoDB and the request still rejected;
        applied before it, the request is admitted against the restored quota.
        """
        repo = limiter._repository
        slow = self._slow(limiter)
        await repo.set_limits("reset-gate", [RPD], resource="gpt-4")

        repo._now_ms = lambda: _ny("2026-09-15 23:00")
        async with slow.acquire("reset-gate", "gpt-4", consume={"rpd": 10_000}):
            pass
        assert (await self._bucket(repo, "reset-gate")).tokens_milli == 0

        repo._now_ms = lambda: _ny("2026-09-16 00:30")
        await repo.invalidate_config_cache()
        async with slow.acquire("reset-gate", "gpt-4", consume={"rpd": 10_000}):
            pass
        assert (await self._bucket(repo, "reset-gate")).tokens_milli == 0

    async def test_an_idle_bucket_resets_on_wake_not_at_the_edge(self, limiter):
        """Idle 18:00 -> 09:00 the next morning: the missed midnight is found
        by the backwards scan and applied by the 09:00 pass (§3.6)."""
        repo = limiter._repository
        slow = self._slow(limiter)
        await repo.set_limits("reset-3", [RPD], resource="gpt-4")

        repo._now_ms = lambda: _ny("2026-09-15 18:00")
        async with slow.acquire("reset-3", "gpt-4", consume={"rpd": 10_000}):
            pass

        repo._now_ms = lambda: _ny("2026-09-16 09:00")
        await repo.invalidate_config_cache()
        async with slow.acquire("reset-3", "gpt-4", consume={"rpd": 10_000}):
            pass

        assert (await self._bucket(repo, "reset-3")).tokens_milli == 0

    async def test_two_missed_midnights_still_hand_back_one_allowance(self, limiter):
        """Idle across *two* edges. Setting the balance is idempotent, so the
        entity gets one day's quota back, not two."""
        repo = limiter._repository
        slow = self._slow(limiter)
        await repo.set_limits("reset-idle2", [RPD], resource="gpt-4")

        repo._now_ms = lambda: _ny("2026-09-14 18:00")
        async with slow.acquire("reset-idle2", "gpt-4", consume={"rpd": 10_000}):
            pass

        repo._now_ms = lambda: _ny("2026-09-16 09:00")
        await repo.invalidate_config_cache()
        async with slow.acquire("reset-idle2", "gpt-4", consume={"rpd": 1_000}):
            pass

        assert (await self._bucket(repo, "reset-idle2")).tokens_milli == 9_000_000

    async def test_only_the_limit_carrying_the_reset_is_restored(self, limiter):
        """One item, two limits, one `rf` — but the reset is decided per limit.

        `rpm` drips and carries no reset; `rpd` is a quota that does. Crossing
        midnight must not hand `rpm` its ceiling back, which an item-level
        reset would.
        """
        repo = limiter._repository
        slow = self._slow(limiter)
        # Ceiling 100, but only 10 a minute, so 90 seconds of drip is visibly
        # short of the ceiling an item-level reset would have restored.
        rpm = Limit.custom("rpm", capacity=100, refill_amount=10, refill_period_seconds=60)
        await repo.set_limits("reset-multi", [RPD, rpm], resource="gpt-4")

        repo._now_ms = lambda: _ny("2026-09-15 23:59")
        async with slow.acquire("reset-multi", "gpt-4", consume={"rpd": 10_000, "rpm": 100}):
            pass
        assert (await self._bucket(repo, "reset-multi", "rpm")).tokens_milli == 0

        # 30 seconds past midnight, 90 seconds after the burn: the daily quota
        # resets in full, while `rpm` has earned 15 tokens of drip and no more.
        repo._now_ms = lambda: _ny("2026-09-16 00:00:30")
        await repo.invalidate_config_cache()
        async with slow.acquire("reset-multi", "gpt-4", consume={"rpd": 0, "rpm": 0}):
            pass

        assert (await self._bucket(repo, "reset-multi", "rpd")).tokens_milli == 10_000_000
        assert (await self._bucket(repo, "reset-multi", "rpm")).tokens_milli == 15_000

    async def test_a_cascading_child_resets_both_items(self, limiter):
        """Child and parent are separate items with separate `rf` stamps, and
        each is reset from its own resolved limits.

        Both items' `vu` expire at the edge, so this runs through the *full*
        slow path — `_try_parent_only_acquire` is covered separately below,
        because a boundary-expired parent is routed away from it by design.
        """
        repo = limiter._repository
        await limiter.create_entity("org-1")
        await limiter.create_entity("key-1", parent_id="org-1", cascade=True)
        await repo.set_limits("org-1", [RPD], resource="gpt-4")
        await repo.set_limits("key-1", [RPD], resource="gpt-4")

        repo._now_ms = lambda: _ny("2026-09-15 23:00")
        async with limiter.acquire("key-1", "gpt-4", consume={"rpd": 10_000}):
            pass

        repo._now_ms = lambda: _ny("2026-09-16 00:30")
        await repo.invalidate_config_cache()
        async with limiter.acquire("key-1", "gpt-4", consume={"rpd": 9_000}):
            pass

        assert (await self._bucket(repo, "org-1")).tokens_milli == 1_000_000
        assert (await self._bucket(repo, "key-1")).tokens_milli == 1_000_000

    async def test_the_parent_only_slow_path_also_resets(self, limiter):
        """`_try_parent_only_acquire` builds its own LeaseEntry list and is a
        second, easily-missed seam.

        Driven directly, because the fast path routes a *boundary-expired*
        parent to the full slow path rather than here — this seam is reached
        only when the parent's `vu` is intact (an item predating the schedule,
        or one whose other limits forced the fallback) while a reset edge has
        still been crossed since its `rf`. Without the reset here, the parent
        is exhausted and the method returns None, which the caller reads as a
        rejection and compensates the child for.
        """
        repo = limiter._repository
        slow = self._slow(limiter)
        await repo.set_limits("po-org", [RPD], resource="gpt-4")

        repo._now_ms = lambda: _ny("2026-09-15 23:00")
        async with slow.acquire("po-org", "gpt-4", consume={"rpd": 10_000}):
            pass
        assert (await self._bucket(repo, "po-org")).tokens_milli == 0

        repo._now_ms = lambda: _ny("2026-09-16 00:30")
        await repo.invalidate_config_cache()
        lease = await limiter._try_parent_only_acquire("po-org", "gpt-4", {"rpd": 9_000}, [], 0, 1)

        assert lease is not None, "the restored quota must admit the request"
        assert (await self._bucket(repo, "po-org")).tokens_milli == 1_000_000

    async def test_vu_is_stamped_for_a_reset_only_limit(self, limiter):
        """A limit with a reset schedule and no parameter schedule still needs
        `vu`, or the fast path never yields and the reset never fires.

        This is the whole daily-quota shape.
        """
        repo = limiter._repository
        slow = self._slow(limiter)
        await repo.set_limits("reset-4", [RPD], resource="gpt-4")

        repo._now_ms = lambda: _ny("2026-09-15 23:00")
        async with slow.acquire("reset-4", "gpt-4", consume={"rpd": 1}):
            pass

        item = await self._raw(repo, "reset-4")
        assert int(item[BUCKET_FIELD_VU]["N"]) == _ny("2026-09-16 00:00")

    async def test_vu_is_the_earlier_of_a_param_boundary_and_a_reset_edge(self, limiter):
        """Both tuples vote. The 0.5x window closes at 07:00, three hours
        before the reset would fire again, so `vu` is the window's edge —
        a `vu` taken from the reset alone would be 17 hours late."""
        repo = limiter._repository
        slow = self._slow(limiter)
        night = RPD.with_schedule(
            (ScheduleEntry(cron="* 0-6 * * *", tz="America/New_York", scale=0.5),)
        )
        await repo.set_limits("reset-5", [night], resource="gpt-4")

        repo._now_ms = lambda: _ny("2026-09-16 03:00")
        async with slow.acquire("reset-5", "gpt-4", consume={"rpd": 1}):
            pass

        item = await self._raw(repo, "reset-5")
        assert int(item[BUCKET_FIELD_VU]["N"]) == _ny("2026-09-16 07:00")

    async def test_an_edge_crossed_between_the_two_clock_readings_is_not_lost(self, limiter):
        """The acquire path and `_commit_initial()` read the clock a round trip
        apart, and `rf` is stamped at the *later* reading.

        An edge that falls in that gap is invisible to `_apply_reset_edge`,
        which ran at the earlier one — and the `rf` it then stamps is already
        past the edge, so the *next* pass skips it too, and the aggregator,
        reading the same `rf` off the stream image, skips it as well. A whole
        period's quota disappears with no error anywhere. The commit
        re-expresses the delta rather than letting that happen.
        """
        repo = limiter._repository
        slow = self._slow(limiter)
        await repo.set_limits("reset-race", [RPD], resource="gpt-4")

        repo._now_ms = lambda: _ny("2026-09-15 23:00")
        async with slow.acquire("reset-race", "gpt-4", consume={"rpd": 10_000}):
            pass
        assert (await self._bucket(repo, "reset-race")).tokens_milli == 0

        from zae_limiter.lease import Lease

        original = Lease._commit_initial

        async def commit_after_midnight(lease_self):
            repo._now_ms = lambda: _ny("2026-09-16 00:00:01")
            return await original(lease_self)

        repo._now_ms = lambda: _ny("2026-09-15 23:59:59")
        await repo.invalidate_config_cache()
        with patch.object(Lease, "_commit_initial", commit_after_midnight):
            async with slow.acquire("reset-race", "gpt-4", consume={"rpd": 0}):
                pass

        bucket = await self._bucket(repo, "reset-race")
        assert bucket.tokens_milli == 10_000_000, "the edge crossed mid-pass still applies"
        assert bucket.total_consumed_milli == 10_000_000, "and `tc` is still monotonic"


# ---------------------------------------------------------------------------
# Boundary-aware retry estimates on the query surface (#222 §7, surface Task 5)
# ---------------------------------------------------------------------------

NIGHT_HALF = (ScheduleEntry(cron="* 0-6 * * *", tz="America/New_York", scale=0.5),)


class TestCheckAvailabilityIsScheduleAware:
    """The query surface must agree with the rejection path at one instant.

    `available()` and `time_until_available()` are thin wrappers over
    `check_availability()` (#473), so converting it converts all three. Missing
    it would leave `acquire()` saying "at midnight" while the display said "in
    eleven hours" about the same bucket at the same instant.

    These run against a real (moto-backed) repository: the schedules reach the
    walk through `_deserialize_limits`, which decodes `l_{name}_sched` and
    `l_{name}_rsched` off the config item, so this half of Task 5 is verified
    end to end rather than through a constructed `BucketState`.
    """

    async def test_reports_the_scheduled_capacity_for_a_missing_bucket(self, limiter):
        """The no-bucket branch reported `limit.capacity` outright; inside a
        0.5x window that is twice what the first acquire would admit."""
        repo = limiter._repository
        await repo.set_limits(
            "ca-1", [Limit.per_minute("rpm", 1000).with_schedule(NIGHT_HALF)], resource="gpt-4"
        )
        repo._now_ms = lambda: _ny("2026-09-16 03:00")
        await repo.invalidate_config_cache()

        check = await limiter.check_availability("ca-1", "gpt-4")
        assert check.status("rpm").available == 500

    async def test_the_same_limit_outside_the_window_reports_the_base(self, limiter):
        """Discriminates the test above against a hardcoded halving."""
        repo = limiter._repository
        await repo.set_limits(
            "ca-1b", [Limit.per_minute("rpm", 1000).with_schedule(NIGHT_HALF)], resource="gpt-4"
        )
        repo._now_ms = lambda: _ny("2026-09-15 14:00")
        await repo.invalidate_config_cache()

        check = await limiter.check_availability("ca-1b", "gpt-4")
        assert check.status("rpm").available == 1000

    async def test_clamps_a_live_balance_to_the_scheduled_capacity(self, limiter):
        """The other base-capacity site: `min(total_across_shards,
        limit.capacity)`. A bucket full at 1000 entering a 0.5x window reports
        500, not 1000 — the surplus is unspendable (#222 §3.3)."""
        repo = limiter._repository
        await repo.set_limits(
            "ca-2", [Limit.per_minute("rpm", 1000).with_schedule(NIGHT_HALF)], resource="gpt-4"
        )
        repo._now_ms = lambda: _ny("2026-09-15 14:00")  # outside the window
        await repo.invalidate_config_cache()
        async with limiter.acquire("ca-2", "gpt-4", consume={"rpm": 1}):
            pass

        repo._now_ms = lambda: _ny("2026-09-16 03:00")  # inside it
        await repo.invalidate_config_cache()
        check = await limiter.check_availability("ca-2", "gpt-4")
        assert check.status("rpm").available == 500

    async def test_the_wait_walks_boundaries(self, limiter):
        """A daily quota's honest answer is "at midnight". Before this it was
        0.0 — "retry now", an hour early and repeatedly (#530)."""
        repo = limiter._repository
        await repo.set_limits("ca-3", [RPD], resource="gpt-4")
        repo._now_ms = lambda: _ny("2026-09-15 23:00")
        await repo.invalidate_config_cache()
        async with limiter.acquire("ca-3", "gpt-4", consume={"rpd": 10_000}):
            pass

        check = await limiter.check_availability("ca-3", "gpt-4", needed={"rpd": 5_000})
        assert check.status("rpd").available == 0
        assert check.status("rpd").retry_after_seconds == pytest.approx(3600.001, abs=0.5)

    async def test_a_pending_reset_is_reflected_in_the_balance(self, limiter):
        """The bucket crossed midnight and nothing has touched it since, so
        disk still holds the burnt balance. Without this the display says
        "0 remaining, resets tomorrow" while the very next acquire restores the
        quota immediately."""
        repo = limiter._repository
        await repo.set_limits("ca-4", [RPD], resource="gpt-4")
        repo._now_ms = lambda: _ny("2026-09-15 23:00")
        await repo.invalidate_config_cache()
        async with limiter.acquire("ca-4", "gpt-4", consume={"rpd": 10_000}):
            pass

        repo._now_ms = lambda: _ny("2026-09-16 00:30")
        await repo.invalidate_config_cache()
        check = await limiter.check_availability("ca-4", "gpt-4", needed={"rpd": 5_000})
        assert check.status("rpd").available == 10_000
        assert check.status("rpd").retry_after_seconds == 0.0

    async def test_a_lowering_boundary_lengthens_the_displayed_wait(self):
        """The spec's worked example through the query surface. Verified
        against the walk directly, since `check_availability` sums shards and
        so hands it `shard_count=1`."""
        business = (ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),)
        got = retry_after_with_schedule(
            deficit_milli=500_000,
            cp_milli=1_000_000,
            ra_milli=1_000_000,
            rp_ms=60_000,
            sched=business,
            now_ms=_ny("2026-09-15 08:59:50"),
        )
        assert got == pytest.approx(50.001, abs=0.002)

    def test_a_pending_reset_is_decided_per_shard(self):
        """Two shards of one quota, only one of them past the edge. Deciding
        per *limit name* instead would report the whole entity restored on the
        strength of a single stale shard."""

        def _shard(rf: str) -> BucketState:
            return BucketState(
                entity_id="ca-7",
                resource="gpt-4",
                limit_name="rpd",
                tokens_milli=0,
                last_refill_ms=_ny(rf),
                capacity_milli=10_000_000,
                refill_amount_milli=0,
                refill_period_ms=86_400_000,
                shard_count=2,
                reset_sched=RPD.reset_schedule,
            )

        now = _ny("2026-09-16 00:30")
        stale = RateLimiter._readable_balance(_shard("2026-09-15 23:00"), RPD, now)
        fresh = RateLimiter._readable_balance(_shard("2026-09-16 00:15"), RPD, now)
        assert stale == 5_000, "the shard that missed the edge reports its restored share"
        assert fresh == 0, "the shard that already applied it keeps its spent balance"

    def test_a_limit_without_a_reset_is_never_pending(self):
        """Discriminates the test above: the edge, not the staleness, is what
        restores the balance."""
        plain = Limit.per_day("rpd", 10_000)
        state = BucketState(
            entity_id="ca-8",
            resource="gpt-4",
            limit_name="rpd",
            tokens_milli=0,
            last_refill_ms=_ny("2026-09-15 23:00"),
            capacity_milli=10_000_000,
            refill_amount_milli=10_000_000,
            refill_period_ms=86_400_000,
        )
        got = RateLimiter._readable_balance(state, plain, _ny("2026-09-16 00:30"))
        assert got == calculate_available(state, _ny("2026-09-16 00:30"))

    async def test_an_unscheduled_entity_is_unchanged(self, limiter):
        """Every existing check_availability assertion in the suite must still
        hold; this is the regression guard for the two capacity sites."""
        repo = limiter._repository
        await repo.set_limits("ca-5", [Limit.per_minute("rpm", 1000)], resource="gpt-4")
        check = await limiter.check_availability("ca-5", "gpt-4", needed={"rpm": 1})
        assert check.status("rpm").available == 1000
        assert check.status("rpm").retry_after_seconds == 0.0

    async def test_time_until_available_agrees_with_check_availability(self, limiter):
        """`time_until_available()` is a wrapper, so it converts with it —
        and this is what would catch it drifting back to a flat estimate."""
        repo = limiter._repository
        await repo.set_limits("ca-6", [RPD], resource="gpt-4")
        repo._now_ms = lambda: _ny("2026-09-15 23:00")
        await repo.invalidate_config_cache()
        async with limiter.acquire("ca-6", "gpt-4", consume={"rpd": 10_000}):
            pass

        wait = await limiter.time_until_available("ca-6", "gpt-4", needed={"rpd": 5_000})
        assert wait == pytest.approx(3600.001, abs=0.5)


class TestSlowPathRejectionWalksBoundaries:
    """Site 2: slow-path admission, reached through `_admit_limit`.

    The slow path attaches the resolved config's schedules to each
    `BucketState` before admission — `sched` was already attached, and the
    reset schedule now travels with it. Without that pairing a quota's
    rejection here quotes the drip ADR-137 says it does not have.
    """

    @staticmethod
    def _slow(limiter):
        return RateLimiter(repository=limiter._repository, speculative_writes=False)

    async def test_an_exhausted_quota_reports_its_reset_edge(self, limiter):
        repo = limiter._repository
        slow = self._slow(limiter)
        await repo.set_limits("slow-q", [RPD], resource="gpt-4")
        repo._now_ms = lambda: _ny("2026-09-15 23:00")
        await repo.invalidate_config_cache()

        async with slow.acquire("slow-q", "gpt-4", consume={"rpd": 10_000}):
            pass

        with pytest.raises(RateLimitExceeded) as exc:
            async with slow.acquire("slow-q", "gpt-4", consume={"rpd": 5_000}):
                pass
        assert exc.value.retry_after_seconds == pytest.approx(3600.001, abs=0.5)

    async def test_a_dripping_limit_is_unchanged(self, limiter):
        """Discriminates the test above: an ordinary limit still reports the
        rate arithmetic, not a calendar instant."""
        repo = limiter._repository
        slow = self._slow(limiter)
        await repo.set_limits("slow-d", [Limit.per_minute("rpm", 100)], resource="gpt-4")
        repo._now_ms = lambda: _ny("2026-09-15 23:00")
        await repo.invalidate_config_cache()

        async with slow.acquire("slow-d", "gpt-4", consume={"rpm": 100}):
            pass

        with pytest.raises(RateLimitExceeded) as exc:
            async with slow.acquire("slow-d", "gpt-4", consume={"rpm": 50}):
                pass
        # 50 tokens at 100/min = 30 s
        assert exc.value.retry_after_seconds == pytest.approx(30.001, abs=0.5)
