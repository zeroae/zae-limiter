"""Tests for SyncRateLimiter."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from zae_limiter import Limit, RateLimitExceeded


class TestSyncRateLimiter:
    """Tests for synchronous rate limiter."""

    def test_create_entity(self, sync_limiter):
        """Test creating an entity."""
        entity = sync_limiter.create_entity(
            entity_id="proj-1",
            name="Test Project",
        )
        assert entity.id == "proj-1"

    def test_acquire_success(self, sync_limiter):
        """Test successful rate limit acquisition."""
        limits = [Limit.per_minute("rpm", 100)]

        with sync_limiter.acquire(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
            consume={"rpm": 1},
        ) as lease:
            assert lease.consumed == {"rpm": 1}

    def test_acquire_exceeds_limit(self, sync_limiter):
        """Test that exceeding limit raises exception."""
        limits = [Limit.per_minute("rpm", 10)]

        with pytest.raises(RateLimitExceeded):
            with sync_limiter.acquire(
                entity_id="key-1",
                resource="gpt-4",
                limits=limits,
                consume={"rpm": 20},
            ):
                pass

    def test_lease_adjust(self, sync_limiter):
        """Test adjusting consumption in sync lease."""
        limits = [Limit.per_minute("tpm", 1000)]

        with sync_limiter.acquire(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
            consume={"tpm": 500},
        ) as lease:
            lease.adjust(tpm=200)
            assert lease.consumed == {"tpm": 700}

    def test_available(self, sync_limiter):
        """Test checking available capacity."""
        limits = [Limit.per_minute("rpm", 100)]

        available = sync_limiter.available(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
        )
        assert available["rpm"] == 100

    def test_set_and_get_limits(self, sync_limiter):
        """Test storing and retrieving limits."""
        limits = [Limit.per_minute("rpm", 100)]
        sync_limiter.set_limits("key-1", limits)

        retrieved = sync_limiter.get_limits("key-1")
        assert len(retrieved) == 1
        assert retrieved[0].name == "rpm"


class TestSyncRateLimiterStackStatus:
    """Tests for SyncRateLimiter.stack_status property."""

    def test_stack_status_returns_status(self, mock_dynamodb):
        """stack_status property should return stack status string."""
        from unittest.mock import patch

        from tests.unit.conftest import _patch_aiobotocore_response
        from zae_limiter import SyncRateLimiter

        with _patch_aiobotocore_response():
            limiter = SyncRateLimiter(
                name="test-sync-stack-status",
                region="us-east-1",
            )

            # Mock StackManager.get_stack_status (internal method)
            mock_manager = MagicMock()
            mock_manager.get_stack_status = AsyncMock(return_value="CREATE_COMPLETE")
            mock_manager.__aenter__ = AsyncMock(return_value=mock_manager)
            mock_manager.__aexit__ = AsyncMock(return_value=None)

            with patch(
                "zae_limiter.infra.stack_manager.StackManager",
                MagicMock(return_value=mock_manager),
            ):
                status = limiter.stack_status  # Property access, not method call

            assert status == "CREATE_COMPLETE"
            mock_manager.get_stack_status.assert_called_once_with(limiter._limiter.stack_name)

            limiter.close()

    def test_stack_status_returns_none_when_not_exists(self, mock_dynamodb):
        """stack_status property should return None when stack doesn't exist."""
        from unittest.mock import patch

        from tests.unit.conftest import _patch_aiobotocore_response
        from zae_limiter import SyncRateLimiter

        with _patch_aiobotocore_response():
            limiter = SyncRateLimiter(
                name="test-sync-stack-none",
                region="us-east-1",
            )

            mock_manager = MagicMock()
            mock_manager.get_stack_status = AsyncMock(return_value=None)
            mock_manager.__aenter__ = AsyncMock(return_value=mock_manager)
            mock_manager.__aexit__ = AsyncMock(return_value=None)

            with patch(
                "zae_limiter.infra.stack_manager.StackManager",
                MagicMock(return_value=mock_manager),
            ):
                status = limiter.stack_status  # Property access, not method call

            assert status is None

            limiter.close()
