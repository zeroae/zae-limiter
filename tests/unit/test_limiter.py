"""Tests for RateLimiter."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

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
from zae_limiter.exceptions import InvalidIdentifierError, InvalidNameError
from zae_limiter.infra.discovery import InfrastructureDiscovery
from zae_limiter.models import BucketState
from zae_limiter.repository_protocol import SpeculativeResult


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
        # Bucket should still have full capacity (no commit happened)
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
        """consume() on a committed lease raises RuntimeError."""
        limits = [Limit.per_minute("tpm", 10_000)]

        async with limiter.acquire(
            entity_id="key-edge-1",
            resource="gpt-4",
            limits=limits,
            consume={"tpm": 100},
        ) as lease:
            pass  # commit happens on exit

        with pytest.raises(RuntimeError, match="no longer active"):
            await lease.consume(tpm=50)

    async def test_adjust_after_commit_raises(self, limiter):
        """adjust() on a committed lease raises RuntimeError."""
        limits = [Limit.per_minute("tpm", 10_000)]

        async with limiter.acquire(
            entity_id="key-edge-2",
            resource="gpt-4",
            limits=limits,
            consume={"tpm": 100},
        ) as lease:
            pass

        with pytest.raises(RuntimeError, match="no longer active"):
            await lease.adjust(tpm=50)

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

    def test_is_condition_check_failure_transaction_canceled(self):
        """Detects TransactionCanceledException by class name."""
        from zae_limiter.lease import _is_condition_check_failure

        exc_cls = type("TransactionCanceledException", (Exception,), {})
        assert _is_condition_check_failure(exc_cls()) is True

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

    def test_build_retry_failure_statuses(self):
        """Builds LimitStatus list for retry failure."""
        from zae_limiter.lease import LeaseEntry, _build_retry_failure_statuses

        limit = Limit.per_minute("rpm", 100)
        state = MagicMock()
        state.tokens_milli = 50_000
        entry = LeaseEntry(
            entity_id="e1",
            resource="gpt-4",
            limit=limit,
            state=state,
            consumed=10,
        )
        statuses = _build_retry_failure_statuses([entry])
        assert len(statuses) == 1
        assert statuses[0].entity_id == "e1"
        assert statuses[0].available == 50
        assert statuses[0].requested == 10
        assert statuses[0].exceeded is True

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

        entry = LeaseEntry(
            entity_id="e1",
            resource="gpt-4",
            limit=limit,
            state=state,
            consumed=10,
            _original_tokens_milli=100_000,
            _original_rf_ms=1000,
        )

        # Create a mock repo that always raises TransactionCanceledException
        mock_repo = AsyncMock()
        exc_cls = type("TransactionCanceledException", (Exception,), {})
        mock_repo.transact_write.side_effect = exc_cls()
        mock_repo.build_composite_normal.return_value = {"Update": {}}
        mock_repo.build_composite_retry.return_value = {"Update": {}}

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
        mock_repo.transact_write.side_effect = [exc_cls(), None]

        lease = Lease(repository=mock_repo, entries=[entry])
        await lease._commit_initial()

        assert lease._initial_committed is True
        assert mock_repo.transact_write.call_count == 2
        # Second call uses retry items
        mock_repo.build_composite_retry.assert_called_once()

    async def test_retry_non_condition_check_reraises(self):
        """Non-condition-check error in retry path propagates (line 301)."""
        from zae_limiter.lease import Lease

        entry = self._make_entry()
        mock_repo = self._make_mock_repo()

        exc_cls = type("TransactionCanceledException", (Exception,), {})
        mock_repo.transact_write.side_effect = [exc_cls(), RuntimeError("retry fail")]

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
        transact_write to fail with TransactionCanceledException. The retry
        path uses build_composite_retry for both entries and succeeds.
        """
        from zae_limiter.lease import Lease

        child_entry = self._make_entry(consumed=5, entity_id="child-1")
        parent_entry = self._make_entry(consumed=5, entity_id="parent-1")
        mock_repo = self._make_mock_repo()

        exc_cls = type("TransactionCanceledException", (Exception,), {})
        # Normal path fails, retry path succeeds
        mock_repo.transact_write.side_effect = [exc_cls(), None]

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
        # Both normal and retry paths fail
        mock_repo.transact_write.side_effect = [exc_cls(), exc_cls()]

        lease = Lease(repository=mock_repo, entries=[child_entry, parent_entry])
        with pytest.raises(RateLimitExceeded) as exc_info:
            await lease._commit_initial()

        # Should have statuses for both entries
        assert len(exc_info.value.statuses) == 2
        entity_ids = {s.entity_id for s in exc_info.value.statuses}
        assert entity_ids == {"child-1", "parent-1"}

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
            bucket = buckets[0]
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
        assert available["rpm"] == 100

    async def test_cascade_writes_both_on_enter(self, limiter):
        """Cascade writes both child and parent buckets on enter."""
        await limiter.create_entity(entity_id="proj-cascade")
        await limiter.create_entity(entity_id="key-cascade", parent_id="proj-cascade", cascade=True)

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
        assert buckets[0].total_consumed_milli == 50_000


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
        bucket = buckets[0]
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
        bucket = buckets[0]
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
        bucket = buckets[0]
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
        bucket = buckets[0]
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
        bucket = buckets[0]
        # Counter: (100 + 200) * 1000 = 300000 millitokens
        assert bucket.total_consumed_milli == 300_000


class TestRateLimiterCascade:
    """Tests for cascade functionality (entity-level cascade)."""

    async def test_cascade_consumes_parent(self, limiter):
        """Test that entity with cascade=True consumes from parent too."""
        await limiter.create_entity(entity_id="proj-1")
        await limiter.create_entity(entity_id="key-1", parent_id="proj-1", cascade=True)

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

        # Set on_unavailable to ALLOW
        limiter.on_unavailable = OnUnavailable.ALLOW

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

        # Set on_unavailable to BLOCK (default)
        limiter.on_unavailable = OnUnavailable.BLOCK

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

        # Set limiter to BLOCK, but override in acquire
        limiter.on_unavailable = OnUnavailable.BLOCK

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

        # Set limiter to ALLOW, but override in acquire
        limiter.on_unavailable = OnUnavailable.ALLOW

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
        # Patch aiobotocore for moto compatibility
        from tests.unit.conftest import _patch_aiobotocore_response
        from zae_limiter import RateLimiter, StackOptions

        with _patch_aiobotocore_response():
            stack_options = StackOptions(lambda_timeout=120)
            limiter = RateLimiter(
                name="test-with-stack-options",
                region="us-east-1",
                stack_options=stack_options,
                skip_version_check=True,  # Skip version check to isolate test
            )

            # Mock ensure_infrastructure to track calls
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
        from tests.unit.conftest import _patch_aiobotocore_response
        from zae_limiter import RateLimiter

        with _patch_aiobotocore_response():
            limiter = RateLimiter(
                name="test-without-stack-options",
                region="us-east-1",
                stack_options=None,  # No stack options
                skip_version_check=True,
            )

            # Mock ensure_infrastructure to track if it's called
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


class TestRateLimiterGetStatus:
    """Tests for get_status method."""

    @pytest.mark.asyncio
    async def test_get_status_returns_status_object(self, limiter):
        """get_status should return a Status object with all fields."""
        from zae_limiter import Status

        status = await limiter.get_status()

        # Verify it returns a Status object
        assert isinstance(status, Status)

        # Verify all fields are present
        assert isinstance(status.available, bool)
        assert status.name == limiter.name
        assert isinstance(status.client_version, str)

    @pytest.mark.asyncio
    async def test_get_status_shows_available_when_table_exists(self, limiter):
        """get_status should show available=True when DynamoDB table exists."""
        status = await limiter.get_status()

        assert status.available is True
        assert status.latency_ms is not None
        assert status.latency_ms > 0
        assert status.table_status == "ACTIVE"

    @pytest.mark.asyncio
    async def test_get_status_shows_unavailable_when_no_connection(self, mock_dynamodb):
        """get_status should show available=False when DynamoDB is not reachable."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from tests.unit.conftest import _patch_aiobotocore_response
        from zae_limiter import RateLimiter

        with _patch_aiobotocore_response():
            limiter = RateLimiter(
                name="test-status-unavailable",
                region="us-east-1",
            )

            # Mock the client to raise an exception
            async def mock_describe_table(*args, **kwargs):
                raise Exception("Connection refused")

            mock_client = MagicMock()
            mock_client.describe_table = AsyncMock(side_effect=mock_describe_table)

            # Patch _get_client to return our mock
            async def mock_get_client():
                return mock_client

            with patch.object(limiter._repository, "_get_client", mock_get_client):
                status = await limiter.get_status()

            assert status.available is False
            assert status.latency_ms is None
            assert status.table_status is None

            await limiter.close()

    @pytest.mark.asyncio
    async def test_get_status_includes_version_info(self, limiter):
        """get_status should include version information when available."""
        from zae_limiter import __version__

        # First, set up version record
        from zae_limiter.version import get_schema_version

        await limiter._repository.set_version_record(
            schema_version=get_schema_version(),
            lambda_version="0.1.0",
            client_min_version="0.0.0",
        )

        status = await limiter.get_status()

        assert status.client_version == __version__
        assert status.schema_version == get_schema_version()
        assert status.lambda_version == "0.1.0"

    @pytest.mark.asyncio
    async def test_get_status_handles_missing_version_record(self, mock_dynamodb):
        """get_status should handle missing version record gracefully."""
        from tests.unit.conftest import _patch_aiobotocore_response
        from zae_limiter import RateLimiter, __version__

        with _patch_aiobotocore_response():
            # Create a fresh limiter without going through context manager
            # (which would call _ensure_initialized and create version record)
            limiter = RateLimiter(
                name="test-no-version",
                region="us-east-1",
                skip_version_check=True,
            )
            # Create table without version record
            await limiter._repository.create_table()

            status = await limiter.get_status()

            # Should still have client version
            assert status.client_version == __version__
            # Schema and lambda versions should be None when no version record
            assert status.schema_version is None
            assert status.lambda_version is None

            await limiter.close()

    @pytest.mark.asyncio
    async def test_get_status_returns_name_and_region(self, limiter):
        """get_status should return the correct name and region."""
        status = await limiter.get_status()

        assert status.name == limiter.name
        assert status.region == "us-east-1"

    @pytest.mark.asyncio
    async def test_get_status_returns_table_metrics(self, limiter):
        """get_status should return table metrics."""
        status = await limiter.get_status()

        # Item count should be available when table exists
        # Note: table_size_bytes may be None with moto mock
        assert status.table_item_count is not None
        assert status.table_item_count >= 0
        # table_size_bytes may be None in mocked environments
        # but should be an int >= 0 if present
        if status.table_size_bytes is not None:
            assert status.table_size_bytes >= 0

    @pytest.mark.asyncio
    async def test_get_status_with_stack_status(self, mock_dynamodb):
        """get_status should include stack status when available."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from tests.unit.conftest import _patch_aiobotocore_response
        from zae_limiter import RateLimiter

        with _patch_aiobotocore_response():
            limiter = RateLimiter(
                name="test-stack-status-in-get-status",
                region="us-east-1",
            )

            # Create table first
            await limiter._repository.create_table()

            # Mock StackManager to return a stack status
            mock_manager = MagicMock()
            mock_manager.get_stack_status = AsyncMock(return_value="CREATE_COMPLETE")
            mock_manager.__aenter__ = AsyncMock(return_value=mock_manager)
            mock_manager.__aexit__ = AsyncMock(return_value=None)

            with patch(
                "zae_limiter.infra.stack_manager.StackManager",
                MagicMock(return_value=mock_manager),
            ):
                status = await limiter.get_status()

            assert status.stack_status == "CREATE_COMPLETE"

            await limiter.close()

    @pytest.mark.asyncio
    async def test_get_status_includes_iam_role_arns(self, mock_dynamodb):
        """get_status should include IAM role ARNs when available in stack outputs."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from tests.unit.conftest import _patch_aiobotocore_response
        from zae_limiter import RateLimiter

        with _patch_aiobotocore_response():
            limiter = RateLimiter(
                name="test-iam-roles-status",
                region="us-east-1",
            )

            # Create table first
            await limiter._repository.create_table()

            # Mock StackManager to return stack status and outputs
            mock_cfn_client = MagicMock()
            mock_cfn_client.describe_stacks = AsyncMock(
                return_value={
                    "Stacks": [
                        {
                            "StackName": "test-iam-roles-status",
                            "StackStatus": "CREATE_COMPLETE",
                            "Outputs": [
                                {
                                    "OutputKey": "AppRoleArn",
                                    "OutputValue": "arn:aws:iam::123456789012:role/app-role",
                                },
                                {
                                    "OutputKey": "AdminRoleArn",
                                    "OutputValue": "arn:aws:iam::123456789012:role/admin-role",
                                },
                                {
                                    "OutputKey": "ReadOnlyRoleArn",
                                    "OutputValue": "arn:aws:iam::123456789012:role/readonly-role",
                                },
                            ],
                        }
                    ]
                }
            )

            mock_manager = MagicMock()
            mock_manager.get_stack_status = AsyncMock(return_value="CREATE_COMPLETE")
            mock_manager._get_client = AsyncMock(return_value=mock_cfn_client)
            mock_manager.__aenter__ = AsyncMock(return_value=mock_manager)
            mock_manager.__aexit__ = AsyncMock(return_value=None)

            with patch(
                "zae_limiter.infra.stack_manager.StackManager",
                MagicMock(return_value=mock_manager),
            ):
                status = await limiter.get_status()

            assert status.app_role_arn == "arn:aws:iam::123456789012:role/app-role"
            assert status.admin_role_arn == "arn:aws:iam::123456789012:role/admin-role"
            assert status.readonly_role_arn == "arn:aws:iam::123456789012:role/readonly-role"

            await limiter.close()

    @pytest.mark.asyncio
    async def test_get_status_role_arns_none_when_roles_disabled(self, mock_dynamodb):
        """get_status should return None for role ARNs when IAM roles are disabled."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from tests.unit.conftest import _patch_aiobotocore_response
        from zae_limiter import RateLimiter

        with _patch_aiobotocore_response():
            limiter = RateLimiter(
                name="test-no-iam-roles-status",
                region="us-east-1",
            )

            # Create table first
            await limiter._repository.create_table()

            # Mock StackManager with no role outputs (roles disabled)
            mock_cfn_client = MagicMock()
            mock_cfn_client.describe_stacks = AsyncMock(
                return_value={
                    "Stacks": [
                        {
                            "StackName": "test-no-iam-roles-status",
                            "StackStatus": "CREATE_COMPLETE",
                            "Outputs": [
                                {
                                    "OutputKey": "TableName",
                                    "OutputValue": "test-no-iam-roles-status",
                                },
                            ],
                        }
                    ]
                }
            )

            mock_manager = MagicMock()
            mock_manager.get_stack_status = AsyncMock(return_value="CREATE_COMPLETE")
            mock_manager._get_client = AsyncMock(return_value=mock_cfn_client)
            mock_manager.__aenter__ = AsyncMock(return_value=mock_manager)
            mock_manager.__aexit__ = AsyncMock(return_value=None)

            with patch(
                "zae_limiter.infra.stack_manager.StackManager",
                MagicMock(return_value=mock_manager),
            ):
                status = await limiter.get_status()

            # Role ARNs should be None when not in outputs
            assert status.app_role_arn is None
            assert status.admin_role_arn is None
            assert status.readonly_role_arn is None

            await limiter.close()

    @pytest.mark.asyncio
    async def test_get_status_logs_when_stack_manager_fails(self, mock_dynamodb):
        """get_status should log and continue when StackManager raises."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from tests.unit.conftest import _patch_aiobotocore_response
        from zae_limiter import RateLimiter

        with _patch_aiobotocore_response():
            limiter = RateLimiter(
                name="test-stack-fail",
                region="us-east-1",
            )
            await limiter._repository.create_table()

            # Mock StackManager to raise on __aenter__
            mock_manager = MagicMock()
            mock_manager.__aenter__ = AsyncMock(side_effect=Exception("CFN unavailable"))
            mock_manager.__aexit__ = AsyncMock(return_value=None)

            with patch(
                "zae_limiter.infra.stack_manager.StackManager",
                MagicMock(return_value=mock_manager),
            ):
                status = await limiter.get_status()

            # Stack status should be None but DynamoDB should still work
            assert status.stack_status is None
            assert status.available is True

            await limiter.close()

    @pytest.mark.asyncio
    async def test_get_status_logs_when_version_record_fails(self, limiter):
        """get_status should log and continue when version record retrieval fails."""
        from unittest.mock import AsyncMock, patch

        with patch.object(
            limiter._repository,
            "get_version_record",
            new=AsyncMock(side_effect=Exception("DynamoDB error")),
        ):
            status = await limiter.get_status()

        # Should still be available, but version info should be None
        assert status.available is True
        assert status.schema_version is None
        assert status.lambda_version is None


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


class TestRateLimiterVersionChecking:
    """Tests for version checking during initialization."""

    @pytest.mark.asyncio
    async def test_check_version_raises_incompatible_schema(self, mock_dynamodb):
        """Should raise IncompatibleSchemaError when schema version is incompatible."""
        from unittest.mock import AsyncMock, patch

        from tests.unit.conftest import _patch_aiobotocore_response
        from zae_limiter import RateLimiter
        from zae_limiter.exceptions import IncompatibleSchemaError
        from zae_limiter.version import CompatibilityResult, InfrastructureVersion

        with _patch_aiobotocore_response():
            limiter = RateLimiter(
                name="test-version-check",
                region="us-east-1",
                skip_version_check=False,
            )
            await limiter._repository.create_table()

            # Mock version record with incompatible schema
            incompatible_record = {
                "schema_version": "99.0.0",
                "lambda_version": "0.1.0",
                "client_min_version": "0.0.0",
            }

            with (
                patch.object(
                    limiter._repository,
                    "get_version_record",
                    new=AsyncMock(return_value=incompatible_record),
                ),
                patch(
                    "zae_limiter.version.check_compatibility",
                    return_value=CompatibilityResult(
                        is_compatible=False,
                        requires_schema_migration=True,
                        requires_lambda_update=False,
                        message="Major version mismatch",
                    ),
                ),
                patch(
                    "zae_limiter.version.InfrastructureVersion.from_record",
                    return_value=InfrastructureVersion(
                        schema_version="99.0.0",
                        lambda_version="0.1.0",
                        template_version=None,
                        client_min_version="0.0.0",
                    ),
                ),
            ):
                with pytest.raises(IncompatibleSchemaError):
                    await limiter._check_and_update_version()

            await limiter.close()

    @pytest.mark.asyncio
    async def test_check_version_raises_version_mismatch_strict(self, mock_dynamodb):
        """Should raise VersionMismatchError when strict mode is on and lambda needs update."""
        from unittest.mock import AsyncMock, patch

        from tests.unit.conftest import _patch_aiobotocore_response
        from zae_limiter import RateLimiter
        from zae_limiter.exceptions import VersionMismatchError
        from zae_limiter.version import CompatibilityResult, InfrastructureVersion

        with _patch_aiobotocore_response():
            limiter = RateLimiter(
                name="test-strict-version",
                region="us-east-1",
                skip_version_check=False,
                strict_version=True,
                auto_update=False,
            )
            await limiter._repository.create_table()

            mismatch_record = {
                "schema_version": "1.0.0",
                "lambda_version": "0.0.1",
                "client_min_version": "0.0.0",
            }

            with (
                patch.object(
                    limiter._repository,
                    "get_version_record",
                    new=AsyncMock(return_value=mismatch_record),
                ),
                patch(
                    "zae_limiter.version.check_compatibility",
                    return_value=CompatibilityResult(
                        is_compatible=True,
                        requires_schema_migration=False,
                        requires_lambda_update=True,
                        message="Lambda version outdated",
                    ),
                ),
                patch(
                    "zae_limiter.version.InfrastructureVersion.from_record",
                    return_value=InfrastructureVersion(
                        schema_version="1.0.0",
                        lambda_version="0.0.1",
                        template_version=None,
                        client_min_version="0.0.0",
                    ),
                ),
            ):
                with pytest.raises(VersionMismatchError):
                    await limiter._check_and_update_version()

            await limiter.close()

    @pytest.mark.asyncio
    async def test_perform_lambda_update(self, mock_dynamodb):
        """Should deploy Lambda code and update version record."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from tests.unit.conftest import _patch_aiobotocore_response
        from zae_limiter import RateLimiter

        with _patch_aiobotocore_response():
            limiter = RateLimiter(
                name="test-lambda-update",
                region="us-east-1",
            )
            await limiter._repository.create_table()

            # Mock StackManager
            mock_manager = AsyncMock()
            mock_manager.__aenter__ = AsyncMock(return_value=mock_manager)
            mock_manager.__aexit__ = AsyncMock(return_value=None)
            mock_manager.deploy_lambda_code = AsyncMock()

            # Mock set_version_record
            mock_set_version = AsyncMock()

            with (
                patch(
                    "zae_limiter.infra.stack_manager.StackManager",
                    MagicMock(return_value=mock_manager),
                ),
                patch.object(
                    limiter._repository,
                    "set_version_record",
                    mock_set_version,
                ),
            ):
                await limiter._perform_lambda_update()

            # Verify Lambda code was deployed
            mock_manager.deploy_lambda_code.assert_called_once()
            # Verify version record was updated
            mock_set_version.assert_called_once()

            await limiter.close()


class TestRateLimiterStackOperations:
    """Tests for create_stack and delete_stack methods."""

    @pytest.mark.asyncio
    async def test_create_stack(self, mock_dynamodb):
        """create_stack should delegate to StackManager."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from tests.unit.conftest import _patch_aiobotocore_response
        from zae_limiter import RateLimiter, StackOptions

        with _patch_aiobotocore_response():
            limiter = RateLimiter(
                name="test-create-stack",
                region="us-east-1",
            )

            mock_manager = AsyncMock()
            mock_manager.__aenter__ = AsyncMock(return_value=mock_manager)
            mock_manager.__aexit__ = AsyncMock(return_value=None)
            mock_manager.create_stack = AsyncMock(
                return_value={"StackId": "test-id", "StackName": "test-create-stack"}
            )

            with patch(
                "zae_limiter.infra.stack_manager.StackManager",
                MagicMock(return_value=mock_manager),
            ):
                result = await limiter.create_stack(stack_options=StackOptions())

            assert result["StackId"] == "test-id"
            mock_manager.create_stack.assert_called_once()

            await limiter.close()

    @pytest.mark.asyncio
    async def test_delete_stack(self, mock_dynamodb):
        """delete_stack should delegate to StackManager."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from tests.unit.conftest import _patch_aiobotocore_response
        from zae_limiter import RateLimiter

        with _patch_aiobotocore_response():
            limiter = RateLimiter(
                name="test-delete-stack",
                region="us-east-1",
            )

            mock_manager = AsyncMock()
            mock_manager.__aenter__ = AsyncMock(return_value=mock_manager)
            mock_manager.__aexit__ = AsyncMock(return_value=None)
            mock_manager.delete_stack = AsyncMock()

            with patch(
                "zae_limiter.infra.stack_manager.StackManager",
                MagicMock(return_value=mock_manager),
            ):
                await limiter.delete_stack()

            mock_manager.delete_stack.assert_called_once_with("test-delete-stack")

            await limiter.close()


class TestRateLimiterGetStatusFallbackPing:
    """Tests for get_status fallback to ping when describe_table fails."""

    @pytest.mark.asyncio
    async def test_get_status_fallback_ping_no_get_client(self, mock_dynamodb):
        """get_status should use ping() when repository has no _get_client."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from tests.unit.conftest import _patch_aiobotocore_response
        from zae_limiter import RateLimiter

        with _patch_aiobotocore_response():
            limiter = RateLimiter(
                name="test-fallback-ping",
                region="us-east-1",
            )

            # Create a mock repository without _get_client
            mock_repo = MagicMock(spec=[])
            mock_repo.ping = AsyncMock(return_value=True)
            mock_repo.region = "us-east-1"
            mock_repo.endpoint_url = None
            mock_repo.get_version_record = AsyncMock(return_value=None)

            # Remove _get_client attribute to trigger the fallback path
            original_repo = limiter._repository
            limiter._repository = mock_repo

            # Mock StackManager to avoid CloudFormation calls
            mock_manager = MagicMock()
            mock_manager.__aenter__ = AsyncMock(side_effect=Exception("No stack"))
            mock_manager.__aexit__ = AsyncMock(return_value=None)

            with patch(
                "zae_limiter.infra.stack_manager.StackManager",
                MagicMock(return_value=mock_manager),
            ):
                status = await limiter.get_status()

            assert status.available is True
            assert status.latency_ms is not None
            mock_repo.ping.assert_called_once()

            # Restore original repo for cleanup
            limiter._repository = original_repo
            await limiter.close()


class TestSyncRateLimiterGetStatus:
    """Tests for SyncRateLimiter.get_status method."""

    def test_sync_get_status_returns_status_object(self, sync_limiter):
        """SyncRateLimiter.get_status should return a Status object."""
        from zae_limiter import Status

        status = sync_limiter.get_status()

        assert isinstance(status, Status)
        assert status.available is True
        assert status.name == sync_limiter.name

    def test_sync_get_status_includes_all_fields(self, sync_limiter):
        """SyncRateLimiter.get_status should include all status fields."""
        status = sync_limiter.get_status()

        # All fields should be accessible
        assert hasattr(status, "available")
        assert hasattr(status, "latency_ms")
        assert hasattr(status, "stack_status")
        assert hasattr(status, "table_status")
        assert hasattr(status, "aggregator_enabled")
        assert hasattr(status, "name")
        assert hasattr(status, "region")
        assert hasattr(status, "schema_version")
        assert hasattr(status, "lambda_version")
        assert hasattr(status, "client_version")
        assert hasattr(status, "table_item_count")
        assert hasattr(status, "table_size_bytes")
        # IAM role ARN fields (Issue #132)
        assert hasattr(status, "app_role_arn")
        assert hasattr(status, "admin_role_arn")
        assert hasattr(status, "readonly_role_arn")


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
                "PK": {"S": schema.pk_entity(entity_id)},
                "SK": {"S": schema.sk_usage(resource, window_start)},
                "entity_id": {"S": entity_id},
                "resource": {"S": resource},
                "window": {"S": window_type},
                "window_start": {"S": window_start},
                "total_events": {"N": str(sum(counters.values()))},
                "GSI2PK": {"S": schema.gsi2_pk_resource(resource)},
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
        with patch("zae_limiter.infra.discovery.aioboto3") as mock_aioboto3:
            mock_session = MagicMock()
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock()
            mock_session.client.return_value = mock_client
            mock_aioboto3.Session.return_value = mock_session

            discovery = InfrastructureDiscovery(region="us-east-1")

            # First call creates client
            client1 = await discovery._get_client()
            # Second call should return cached client
            client2 = await discovery._get_client()

            assert client1 is client2
            # Session should only be created once
            mock_aioboto3.Session.assert_called_once()

            # Clean up
            await discovery.close()

    @pytest.mark.asyncio
    async def test_get_client_passes_region_and_endpoint(self):
        """_get_client passes region and endpoint_url to boto3."""
        with patch("zae_limiter.infra.discovery.aioboto3") as mock_aioboto3:
            mock_session = MagicMock()
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock()
            mock_session.client.return_value = mock_client
            mock_aioboto3.Session.return_value = mock_session

            discovery = InfrastructureDiscovery(
                region="eu-west-1", endpoint_url="http://localhost:4566"
            )

            await discovery._get_client()

            # Check session.client was called with correct kwargs
            mock_session.client.assert_called_once_with(
                "cloudformation",
                region_name="eu-west-1",
                endpoint_url="http://localhost:4566",
            )

            await discovery.close()

    @pytest.mark.asyncio
    async def test_get_client_without_region_or_endpoint(self):
        """_get_client works without region or endpoint_url."""
        with patch("zae_limiter.infra.discovery.aioboto3") as mock_aioboto3:
            mock_session = MagicMock()
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock()
            mock_session.client.return_value = mock_client
            mock_aioboto3.Session.return_value = mock_session

            discovery = InfrastructureDiscovery()

            await discovery._get_client()

            # Should be called with just "cloudformation" and no kwargs
            mock_session.client.assert_called_once_with("cloudformation")

            await discovery.close()


class TestRateLimiterRepositoryParameter:
    """Tests for the new repository parameter in RateLimiter constructor."""

    @pytest.mark.asyncio
    async def test_repository_parameter_accepted(self, mock_dynamodb):
        """Test RateLimiter accepts repository parameter."""
        from tests.unit.conftest import _patch_aiobotocore_response
        from zae_limiter import Repository

        with _patch_aiobotocore_response():
            repo = Repository(
                name="my-repo-app",
                region="us-east-1",
            )
            limiter = RateLimiter(repository=repo)

            assert limiter._repository is repo
            assert limiter.stack_name == "my-repo-app"
            assert limiter.name == "my-repo-app"
            await limiter.close()

    @pytest.mark.asyncio
    async def test_repository_parameter_conflict_with_name_raises(self, mock_dynamodb):
        """Test ValueError when both repository and name are provided."""
        from tests.unit.conftest import _patch_aiobotocore_response
        from zae_limiter import Repository

        with _patch_aiobotocore_response():
            repo = Repository(name="my-app", region="us-east-1")

            with pytest.raises(ValueError) as exc_info:
                RateLimiter(repository=repo, name="other-app")

            assert "Cannot specify both 'repository'" in str(exc_info.value)
            await repo.close()

    @pytest.mark.asyncio
    async def test_repository_parameter_conflict_with_region_raises(self, mock_dynamodb):
        """Test ValueError when both repository and region are provided."""
        from tests.unit.conftest import _patch_aiobotocore_response
        from zae_limiter import Repository

        with _patch_aiobotocore_response():
            repo = Repository(name="my-app", region="us-east-1")

            with pytest.raises(ValueError) as exc_info:
                RateLimiter(repository=repo, region="eu-west-1")

            assert "Cannot specify both 'repository'" in str(exc_info.value)
            await repo.close()

    @pytest.mark.asyncio
    async def test_repository_parameter_conflict_with_endpoint_url_raises(self, mock_dynamodb):
        """Test ValueError when both repository and endpoint_url are provided."""
        from tests.unit.conftest import _patch_aiobotocore_response
        from zae_limiter import Repository

        with _patch_aiobotocore_response():
            repo = Repository(name="my-app", region="us-east-1")

            with pytest.raises(ValueError) as exc_info:
                RateLimiter(repository=repo, endpoint_url="http://localhost:4566")

            assert "Cannot specify both 'repository'" in str(exc_info.value)
            await repo.close()

    @pytest.mark.asyncio
    async def test_repository_parameter_conflict_with_stack_options_raises(self, mock_dynamodb):
        """Test ValueError when both repository and stack_options are provided."""
        from tests.unit.conftest import _patch_aiobotocore_response
        from zae_limiter import Repository, StackOptions

        with _patch_aiobotocore_response():
            repo = Repository(name="my-app", region="us-east-1")

            with pytest.raises(ValueError) as exc_info:
                RateLimiter(repository=repo, stack_options=StackOptions())

            assert "Cannot specify both 'repository'" in str(exc_info.value)
            await repo.close()

    @pytest.mark.asyncio
    async def test_default_limiter_creates_repository(self, mock_dynamodb):
        """Test RateLimiter with no args creates default repository."""
        from tests.unit.conftest import _patch_aiobotocore_response

        with _patch_aiobotocore_response():
            # No deprecation warning for default behavior
            import warnings

            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                limiter = RateLimiter()
                # Filter for our specific deprecation warning
                deprecation_warnings = [x for x in w if "deprecated" in str(x.message).lower()]
                assert len(deprecation_warnings) == 0

            assert limiter.stack_name == "limiter"
            assert limiter._repository is not None
            await limiter.close()


class TestRepositoryProtocolCompliance:
    """Tests that Repository implements RepositoryProtocol."""

    def test_repository_is_instance_of_protocol(self):
        """Test that Repository passes isinstance check for RepositoryProtocol."""
        from zae_limiter import Repository, RepositoryProtocol

        repo = Repository(name="test", region="us-east-1")
        assert isinstance(repo, RepositoryProtocol)

    def test_repository_protocol_is_runtime_checkable(self):
        """Test that RepositoryProtocol is runtime checkable."""
        from zae_limiter import RepositoryProtocol

        # Should have __subclasshook__ from @runtime_checkable
        assert hasattr(RepositoryProtocol, "__subclasshook__")

    def test_repository_has_capabilities_property(self):
        """Test that Repository exposes capabilities."""
        from zae_limiter import BackendCapabilities, Repository

        repo = Repository(name="test", region="us-east-1")
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
        """Test get_cache_stats() delegates to repository and returns CacheStats."""
        from tests.unit.conftest import _patch_aiobotocore_response

        with _patch_aiobotocore_response():
            limiter = RateLimiter()

            stats = limiter.get_cache_stats()

            assert isinstance(stats, CacheStats)
            assert stats.hits == 0
            assert stats.misses == 0
            assert stats.size == 0
            assert stats.ttl_seconds == 60  # Default TTL
            await limiter.close()

    @pytest.mark.asyncio
    async def test_get_cache_stats_with_custom_ttl(self, mock_dynamodb):
        """Test get_cache_stats() reflects custom TTL from Repository."""
        from tests.unit.conftest import _patch_aiobotocore_response
        from zae_limiter import Repository

        with _patch_aiobotocore_response():
            repo = Repository(name="test-rate-limits", region="us-east-1", config_cache_ttl=120)
            limiter = RateLimiter(repository=repo)

            stats = limiter.get_cache_stats()

            assert stats.ttl_seconds == 120
            await limiter.close()

    @pytest.mark.asyncio
    async def test_invalidate_config_cache(self, mock_dynamodb):
        """Test invalidate_config_cache() delegates to repository."""
        from tests.unit.conftest import _patch_aiobotocore_response
        from zae_limiter.config_cache import CacheEntry

        with _patch_aiobotocore_response():
            limiter = RateLimiter()

            # Manually populate the cache to verify invalidation
            entry = CacheEntry(value=[], expires_at=9999999999.0)
            limiter._repository._config_cache._resource_defaults["gpt-4"] = entry

            assert limiter.get_cache_stats().size == 1

            await limiter.invalidate_config_cache()

            assert limiter.get_cache_stats().size == 0
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

        stats_after_first = limiter.get_cache_stats()
        assert stats_after_first.misses == 1

        # Second call: cache hit (no additional DynamoDB read)
        result = await limiter._repository.resolve_on_unavailable()
        assert result == "allow"

        stats_after_second = limiter.get_cache_stats()
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
    """Tests for bucket_ttl_refill_multiplier parameter (Issue #271)."""

    async def test_bucket_ttl_multiplier_default_is_seven(self, mock_dynamodb):
        """Default bucket_ttl_refill_multiplier is 7."""
        from tests.unit.conftest import _patch_aiobotocore_response

        with _patch_aiobotocore_response():
            limiter = RateLimiter(name="test")
            assert limiter._bucket_ttl_refill_multiplier == 7
            await limiter.close()

    async def test_bucket_ttl_multiplier_custom_value(self, mock_dynamodb):
        """Custom bucket_ttl_refill_multiplier is accepted."""
        from tests.unit.conftest import _patch_aiobotocore_response

        with _patch_aiobotocore_response():
            limiter = RateLimiter(name="test", bucket_ttl_refill_multiplier=14)
            assert limiter._bucket_ttl_refill_multiplier == 14
            await limiter.close()

    async def test_bucket_ttl_multiplier_zero_disables(self, mock_dynamodb):
        """Setting bucket_ttl_refill_multiplier=0 disables TTL."""
        from tests.unit.conftest import _patch_aiobotocore_response

        with _patch_aiobotocore_response():
            limiter = RateLimiter(name="test", bucket_ttl_refill_multiplier=0)
            assert limiter._bucket_ttl_refill_multiplier == 0
            await limiter.close()


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
            burst_milli=1000,
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

    def test_lease_has_bucket_ttl_multiplier_field(self):
        """Lease has bucket_ttl_refill_multiplier field."""
        from unittest.mock import MagicMock

        from zae_limiter.lease import Lease

        repo = MagicMock()
        lease = Lease(repository=repo)
        assert lease.bucket_ttl_refill_multiplier == 7  # Default

        lease_custom = Lease(repository=repo, bucket_ttl_refill_multiplier=14)
        assert lease_custom.bucket_ttl_refill_multiplier == 14


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
        from zae_limiter.schema import pk_entity, sk_bucket

        item = await limiter._repository._get_item(pk_entity("user-1"), sk_bucket("api"))
        assert item is not None
        assert "ttl" in item

    async def test_get_item_returns_none_for_missing_bucket(self, limiter):
        """_get_item returns None when bucket doesn't exist."""
        from zae_limiter.schema import pk_entity, sk_bucket

        # Query for a bucket that doesn't exist
        item = await limiter._repository._get_item(pk_entity("nonexistent-user"), sk_bucket("api"))
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
        await limiter.invalidate_config_cache()

        # Acquire again - should remove TTL since entity now has custom config
        async with limiter.acquire(
            entity_id="user-1",
            resource="api",
            consume={"rpm": 1},
        ):
            pass

        # Verify TTL was removed
        from zae_limiter.schema import pk_entity, sk_bucket

        item = await limiter._repository._get_item(pk_entity("user-1"), sk_bucket("api"))
        assert item is not None
        assert "ttl" not in item

    async def test_commit_sets_ttl_for_entity_default_config(self, limiter):
        """Lease._commit() sets TTL when entity has _default_ config (not resource-specific).

        Entity _default_ config is treated as a kind of default (not custom config),
        so TTL should be applied. This contrasts with resource-specific entity config
        which removes TTL.
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

        # Verify TTL IS set (entity_default is treated as a default, not custom)
        from zae_limiter.schema import pk_entity, sk_bucket

        item = await limiter._repository._get_item(pk_entity("user-1"), sk_bucket("gpt-4"))
        assert item is not None
        assert "ttl" in item, "entity_default config should have TTL (treated as default)"

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
        from zae_limiter.schema import pk_entity, sk_bucket

        item = await limiter._repository._get_item(pk_entity("user-1"), sk_bucket("api"))

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
            burst=1000,
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

        from zae_limiter.schema import pk_entity, sk_bucket

        item = await limiter._repository._get_item(pk_entity("user-slow"), sk_bucket("api"))

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
        from tests.unit.conftest import _patch_aiobotocore_response

        with _patch_aiobotocore_response():
            limiter = RateLimiter(
                name="test-no-ttl",
                region="us-east-1",
                bucket_ttl_refill_multiplier=0,
            )
            await limiter._repository.create_table()
            async with limiter:
                await limiter.set_system_defaults([Limit.per_minute("rpm", 100)])
                async with limiter.acquire(
                    entity_id="user-1",
                    resource="api",
                    consume={"rpm": 1},
                ):
                    pass

                # Verify no TTL set
                from zae_limiter.schema import pk_entity, sk_bucket

                item = await limiter._repository._get_item(pk_entity("user-1"), sk_bucket("api"))
                assert item is not None
                assert "ttl" not in item

    async def test_commit_sets_ttl_after_deleting_entity_config(self, limiter):
        """Lease._commit() sets TTL when entity downgrades from custom to default limits.

        When an entity's custom limits are deleted, the next acquire() should set TTL
        on the bucket since the entity now uses default limits again.
        """
        from zae_limiter.schema import pk_entity, sk_bucket

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
        item = await limiter._repository._get_item(pk_entity("user-1"), sk_bucket("api"))
        assert item is not None
        assert "ttl" not in item

        # Delete entity-level config (downgrade to defaults)
        await limiter.delete_limits("user-1", resource="api")

        # Invalidate cache to ensure deleted config is recognized
        await limiter.invalidate_config_cache()

        # Acquire again - should now set TTL since entity uses defaults
        async with limiter.acquire(
            entity_id="user-1",
            resource="api",
            consume={"rpm": 1},
        ):
            pass

        # Verify TTL is now set (entity uses default config)
        item = await limiter._repository._get_item(pk_entity("user-1"), sk_bucket("api"))
        assert item is not None
        assert "ttl" in item


class TestBucketLimitSync:
    """Tests for bucket synchronization when limits are updated.

    These tests verify that bucket parameters (capacity, burst, refill)
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
        from zae_limiter.schema import pk_entity, sk_bucket

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
        item = await limiter._repository._get_item(pk_entity("user-1"), sk_bucket("api"))
        assert item is not None
        assert item["b_rpm_cp"] == 100000, "Initial capacity should be 100 RPM"

        # Step 3: Update limit to rpm=200 - bucket synced immediately
        await limiter.set_limits("user-1", [Limit.per_minute("rpm", 200)], resource="api")

        # Verify bucket capacity was updated immediately (no acquire needed)
        item = await limiter._repository._get_item(pk_entity("user-1"), sk_bucket("api"))
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
        from zae_limiter.schema import pk_entity, sk_bucket

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
        item = await limiter._repository._get_item(pk_entity("user-1"), sk_bucket("api"))
        assert item is not None
        assert item["b_rpm_cp"] == 200000, "Initial capacity should be 200 RPM"

        # Step 3: Downgrade limit to rpm=100 - bucket synced immediately
        await limiter.set_limits("user-1", [Limit.per_minute("rpm", 100)], resource="api")

        # Verify bucket capacity was updated immediately (no acquire needed)
        item = await limiter._repository._get_item(pk_entity("user-1"), sk_bucket("api"))
        assert item is not None
        assert item["b_rpm_cp"] == 100000, "Bucket capacity should be synced to 100 RPM"

    async def test_bucket_updated_with_multiple_limits(self, limiter):
        """All bucket params synced when entity has multiple limits.

        Verifies that the SET expression correctly handles multiple limits
        and all parameters (capacity, burst, refill) are updated.
        """
        from zae_limiter.schema import pk_entity, sk_bucket

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
        item = await limiter._repository._get_item(pk_entity("user-2"), sk_bucket("api"))
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
        item = await limiter._repository._get_item(pk_entity("user-2"), sk_bucket("api"))
        assert item is not None
        assert item["b_rpm_cp"] == 200000, "rpm capacity should be synced"
        assert item["b_tpm_cp"] == 20000000, "tpm capacity should be synced"

    async def test_bucket_refill_params_synced_when_changed(self, limiter):
        """Bucket refill rate is synced when limit period changes.

        Verifies that all four bucket parameters are updated:
        capacity, burst, refill_amount, refill_period.
        """
        from zae_limiter.schema import pk_entity, sk_bucket

        # Initial: 100 per minute
        await limiter.set_limits(
            "user-3",
            [Limit.per_minute("rpm", 100, burst=150)],
            resource="api",
        )
        async with limiter.acquire(entity_id="user-3", resource="api", consume={"rpm": 1}):
            pass

        # Verify initial values (per minute: refill_period=60s)
        item = await limiter._repository._get_item(pk_entity("user-3"), sk_bucket("api"))
        assert item is not None
        assert item["b_rpm_cp"] == 100000  # capacity: 100 * 1000
        assert item["b_rpm_bx"] == 150000  # burst: 150 * 1000
        assert item["b_rpm_ra"] == 100000  # refill_amount: 100 * 1000
        assert item["b_rpm_rp"] == 60000  # refill_period: 60s * 1000

        # Update to 200 per hour with different burst
        await limiter.set_limits(
            "user-3",
            [Limit.per_hour("rpm", 200, burst=300)],
            resource="api",
        )

        # Verify all params updated
        item = await limiter._repository._get_item(pk_entity("user-3"), sk_bucket("api"))
        assert item is not None
        assert item["b_rpm_cp"] == 200000, "capacity should be synced"
        assert item["b_rpm_bx"] == 300000, "burst should be synced"
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

        Verifies that non-ConditionalCheckFailed errors propagate.
        """

        # First create a bucket so the conditional check passes
        await limiter.set_limits("user-6", [Limit.per_minute("rpm", 100)], resource="api")
        async with limiter.acquire(entity_id="user-6", resource="api", consume={"rpm": 1}):
            pass

        # Mock update_item to raise an unexpected error
        original_update = limiter._repository._client.update_item

        async def mock_update_item(**kwargs):
            # Only fail for bucket sync calls (not other updates)
            if kwargs.get("Key", {}).get("SK", {}).get("S", "").startswith("#BUCKET#"):
                raise ClientError(
                    {"Error": {"Code": "InternalServerError", "Message": "Test error"}},
                    "UpdateItem",
                )
            return await original_update(**kwargs)

        limiter._repository._client.update_item = mock_update_item

        with pytest.raises(ClientError) as exc_info:
            await limiter.set_limits("user-6", [Limit.per_minute("rpm", 200)], resource="api")

        assert exc_info.value.response["Error"]["Code"] == "InternalServerError"


class TestBucketReconciliation:
    """Tests for eager bucket reconciliation on config changes (issue #327).

    Verifies that set_limits() removes TTL, delete_limits() syncs bucket to
    effective defaults, and stale limit attributes are removed.
    """

    async def test_set_limits_removes_ttl_from_bucket(self, limiter):
        """set_limits() removes TTL from existing bucket that had TTL.

        Transition: system defaults (TTL) → entity config (no TTL).
        """
        from zae_limiter.schema import pk_entity, sk_bucket

        # Create bucket with system defaults (has TTL)
        await limiter.set_system_defaults([Limit.per_minute("rpm", 100)])
        async with limiter.acquire(entity_id="user-ttl", resource="api", consume={"rpm": 1}):
            pass

        # Verify bucket has TTL
        item = await limiter._repository._get_item(pk_entity("user-ttl"), sk_bucket("api"))
        assert item is not None
        assert "ttl" in item

        # Set entity-level config — should remove TTL from bucket
        await limiter.set_limits("user-ttl", [Limit.per_minute("rpm", 200)], resource="api")

        # Verify TTL removed and capacity updated
        item = await limiter._repository._get_item(pk_entity("user-ttl"), sk_bucket("api"))
        assert item is not None
        assert "ttl" not in item
        assert item["b_rpm_cp"] == 200000

    async def test_delete_limits_sets_ttl_and_syncs_to_defaults(self, limiter):
        """delete_limits() syncs bucket to resource defaults with TTL.

        Transition: entity config (no TTL) → resource defaults (TTL).
        """
        from zae_limiter.schema import pk_entity, sk_bucket

        # Set resource defaults
        await limiter.set_resource_defaults("api", [Limit.per_minute("rpm", 100)])

        # Set entity config (overrides resource defaults)
        await limiter.set_limits("user-del", [Limit.per_minute("rpm", 500)], resource="api")

        # Create bucket with entity config (no TTL, capacity=500)
        await limiter.invalidate_config_cache()
        async with limiter.acquire(entity_id="user-del", resource="api", consume={"rpm": 1}):
            pass

        item = await limiter._repository._get_item(pk_entity("user-del"), sk_bucket("api"))
        assert item is not None
        assert "ttl" not in item
        assert item["b_rpm_cp"] == 500000

        # Delete entity config — should reconcile to resource defaults
        await limiter.delete_limits("user-del", resource="api")

        item = await limiter._repository._get_item(pk_entity("user-del"), sk_bucket("api"))
        assert item is not None
        assert "ttl" in item, "TTL should be set (now using defaults)"
        assert item["b_rpm_cp"] == 100000, "Capacity should match resource defaults"

    async def test_delete_limits_removes_stale_attributes(self, limiter):
        """delete_limits() removes stale limit attributes from bucket.

        Entity had [rpm, tpm], defaults have [rpm] only — tpm attrs removed.
        """
        from zae_limiter.schema import pk_entity, sk_bucket

        # Set system defaults with rpm only
        await limiter.set_system_defaults([Limit.per_minute("rpm", 100)])

        # Set entity config with rpm + tpm
        await limiter.set_limits(
            "user-stale",
            [Limit.per_minute("rpm", 500), Limit.per_minute("tpm", 50000)],
            resource="api",
        )

        # Create bucket with both limits
        await limiter.invalidate_config_cache()
        async with limiter.acquire(
            entity_id="user-stale",
            resource="api",
            consume={"rpm": 1, "tpm": 100},
        ):
            pass

        item = await limiter._repository._get_item(pk_entity("user-stale"), sk_bucket("api"))
        assert item is not None
        assert "b_tpm_cp" in item, "tpm limit should exist in bucket"

        # Delete entity config — tpm attrs should be removed
        await limiter.delete_limits("user-stale", resource="api")

        item = await limiter._repository._get_item(pk_entity("user-stale"), sk_bucket("api"))
        assert item is not None
        assert "b_rpm_cp" in item, "rpm limit should still exist"
        assert item["b_rpm_cp"] == 100000, "rpm should match system defaults"
        assert "b_tpm_cp" not in item, "Stale tpm limit should be removed"
        assert "b_tpm_tk" not in item, "Stale tpm tokens should be removed"
        assert "b_tpm_tc" not in item, "Stale tpm counter should be removed"

    async def test_delete_limits_no_effective_defaults(self, limiter):
        """delete_limits() leaves bucket as-is when no fallback config exists."""
        from zae_limiter.schema import pk_entity, sk_bucket

        # Set entity config (no resource or system defaults)
        await limiter.set_limits("user-orphan", [Limit.per_minute("rpm", 100)], resource="api")

        # Create bucket
        await limiter.invalidate_config_cache()
        async with limiter.acquire(entity_id="user-orphan", resource="api", consume={"rpm": 1}):
            pass

        item_before = await limiter._repository._get_item(
            pk_entity("user-orphan"), sk_bucket("api")
        )
        assert item_before is not None

        # Delete entity config — no fallback, bucket left as-is
        await limiter.delete_limits("user-orphan", resource="api")

        item_after = await limiter._repository._get_item(pk_entity("user-orphan"), sk_bucket("api"))
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
        await limiter.invalidate_config_cache()
        async with limiter.acquire(entity_id="user-dcache", resource="api", consume={"rpm": 1}):
            pass

        # Verify entity config is cached (not _NO_CONFIG)
        entry = limiter._repository._config_cache._entity_limits.get(("user-dcache", "api"))
        assert entry is not None and entry.value is not _NO_CONFIG

        # delete_limits should evict stale entity config
        await limiter.delete_limits("user-dcache", resource="api")

        # After delete, _resolve_limits() re-caches with _NO_CONFIG sentinel
        # (entity config is gone → negative cache entry). The stale entity
        # limits (rpm=200) must NOT be in the cache.
        entry = limiter._repository._config_cache._entity_limits.get(("user-dcache", "api"))
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
            burst_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )

        original_speculative = limiter._repository.speculative_consume

        call_count = 0

        async def mock_speculative(entity_id, resource, consume, ttl_seconds=None):
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
            burst_milli=1_000_000,
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
            burst_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )

        original_speculative = limiter._repository.speculative_consume
        call_count = 0

        async def mock_speculative(entity_id, resource, consume, ttl_seconds=None):
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
            burst_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )

        original_speculative = limiter._repository.speculative_consume
        call_count = 0

        async def mock_speculative(entity_id, resource, consume, ttl_seconds=None):
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
            burst_milli=1_000_000,
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
            burst_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )

        original_speculative = limiter._repository.speculative_consume
        call_count = 0

        async def mock_speculative(entity_id, resource, consume, ttl_seconds=None):
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
            burst_milli=10_000,
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
            burst_milli=10_000,
            refill_amount_milli=10_000,
            refill_period_ms=60_000,
        )

        original_speculative = limiter._repository.speculative_consume
        call_count = 0

        async def mock_speculative(entity_id, resource, consume, ttl_seconds=None):
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
            burst_milli=1_000_000,
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
            burst_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )

        original_speculative = limiter._repository.speculative_consume
        original_write_each = limiter._repository.write_each
        call_count = 0
        child_compensated = False

        async def mock_speculative(entity_id, resource, consume, ttl_seconds=None):
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
            burst_milli=10_000,
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
            burst_milli=10_000,
            refill_amount_milli=10_000,
            refill_period_ms=60_000,
        )

        original_speculative = limiter._repository.speculative_consume
        call_count = 0

        async def mock_speculative(entity_id, resource, consume, ttl_seconds=None):
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
            burst_milli=1_000_000,
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
            burst_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )

        original_speculative = limiter._repository.speculative_consume
        original_fetch = limiter._fetch_buckets
        call_count = 0
        fetch_call_count = 0

        async def mock_speculative(entity_id, resource, consume, ttl_seconds=None):
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

        async def mock_fetch_buckets(entity_ids, resource):
            nonlocal fetch_call_count
            fetch_call_count += 1
            if fetch_call_count == 1:
                # First call from _try_parent_only_acquire — return empty
                return {}
            return await original_fetch(entity_ids, resource)

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
            burst_milli=1_000_000,
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
            burst_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )

        original_speculative = limiter._repository.speculative_consume
        original_transact = limiter._repository.transact_write
        call_count = 0
        transact_call_count = 0

        async def mock_speculative(entity_id, resource, consume, ttl_seconds=None):
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
            burst_milli=100_000,
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
            burst_milli=200_000_000,
            refill_amount_milli=200_000_000,
            refill_period_ms=60_000,
        )

        original_speculative = limiter._repository.speculative_consume

        async def mock_speculative(entity_id, resource, consume, ttl_seconds=None):
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
            burst_milli=100_000,
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
            burst_milli=100_000,
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
            burst_milli=200_000_000,
            refill_amount_milli=200_000_000,
            refill_period_ms=60_000,
        )

        original_speculative = limiter._repository.speculative_consume
        call_count = 0

        async def mock_speculative(entity_id, resource, consume, ttl_seconds=None):
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
            burst_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )

        original_speculative = limiter._repository.speculative_consume
        call_count = 0

        async def mock_speculative(entity_id, resource, consume, ttl_seconds=None):
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
        buckets_before = await limiter._fetch_buckets(["child-1"], "gpt-4")
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
            burst_milli=1_000_000,
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
            burst_milli=1_000_000,
            refill_amount_milli=1_000_000,
            refill_period_ms=60_000,
        )

        original_speculative = limiter._repository.speculative_consume
        original_fetch = limiter._fetch_buckets
        spec_call_count = 0
        fetch_call_count = 0

        async def mock_speculative(entity_id, resource, consume, ttl_seconds=None):
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

        async def mock_fetch_raising(entity_ids, resource):
            nonlocal fetch_call_count
            fetch_call_count += 1
            if fetch_call_count == 1 and "parent-1" in entity_ids:
                raise RuntimeError("DynamoDB service unavailable")
            return await original_fetch(entity_ids, resource)

        limiter._repository.speculative_consume = mock_speculative
        limiter._fetch_buckets = mock_fetch_raising
        try:
            with pytest.raises(RateLimiterUnavailable, match="DynamoDB service unavailable"):
                async with limiter.acquire("child-1", "gpt-4", {"rpm": 10}):
                    pass

            # Verify child tokens were restored in DynamoDB
            buckets_after = await original_fetch(["child-1"], "gpt-4")
            tokens_after = buckets_after[child_key].tokens_milli
            assert tokens_after == tokens_before, (
                f"Child tokens leaked! Before={tokens_before}, after={tokens_after}. "
                f"Expected compensation to restore tokens."
            )
        finally:
            limiter._repository.speculative_consume = original_speculative
            limiter._fetch_buckets = original_fetch
