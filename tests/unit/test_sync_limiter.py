"""Tests for SyncRateLimiter."""

import pytest
from botocore.exceptions import ClientError

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


class TestSyncRateLimiterIsAvailable:
    """Tests for sync is_available() health check method."""

    def test_is_available_returns_true_when_table_exists(self, sync_limiter):
        """is_available should return True when DynamoDB table is reachable."""
        result = sync_limiter.is_available()
        assert result is True

    def test_is_available_returns_false_on_client_error(self, sync_limiter, monkeypatch):
        """is_available should return False when DynamoDB returns error."""

        async def mock_error(*args, **kwargs):
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "Table not found"}},
                "GetItem",
            )

        monkeypatch.setattr(sync_limiter._limiter._repository, "ping", mock_error)
        result = sync_limiter.is_available()
        assert result is False

    def test_is_available_custom_timeout(self, sync_limiter):
        """is_available should respect custom timeout parameter."""
        result = sync_limiter.is_available(timeout=5.0)
        assert result is True
