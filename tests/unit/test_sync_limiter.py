"""Tests for SyncRateLimiter."""

import pytest
from botocore.exceptions import ClientError

from zae_limiter import Limit, OnUnavailable, RateLimiterUnavailable, RateLimitExceeded


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

    def test_is_available_returns_false_on_event_loop_error(self, sync_limiter):
        """is_available should return False when event loop fails."""
        original_run = sync_limiter._run

        def mock_run_error(coro):
            # Close the coroutine to avoid warning, then raise
            coro.close()
            raise RuntimeError("Event loop is closed")

        try:
            sync_limiter._run = mock_run_error
            result = sync_limiter.is_available()
            assert result is False
        finally:
            # Restore original _run for fixture cleanup
            sync_limiter._run = original_run


class TestSyncRateLimiterOnUnavailable:
    """Tests for ALLOW vs BLOCK behavior when DynamoDB is unavailable."""

    def test_allow_returns_noop_lease_on_dynamodb_error(self, sync_limiter, monkeypatch):
        """ALLOW should return no-op lease on infrastructure error."""

        # Mock repository method to raise error
        async def mock_error(*args, **kwargs):
            raise ClientError(
                {"Error": {"Code": "ServiceUnavailable", "Message": "DynamoDB down"}},
                "GetItem",
            )

        monkeypatch.setattr(sync_limiter._limiter._repository, "get_bucket", mock_error)

        # Set on_unavailable to ALLOW
        sync_limiter._limiter.on_unavailable = OnUnavailable.ALLOW

        # Should not raise, should return no-op lease
        limits = [Limit.per_minute("rpm", 100)]
        with sync_limiter.acquire(
            entity_id="test-entity",
            resource="api",
            limits=limits,
            consume={"rpm": 1},
        ) as lease:
            # No-op lease has no entries
            assert len(lease._lease.entries) == 0
            assert lease.consumed == {}

    def test_block_raises_unavailable_on_dynamodb_error(self, sync_limiter, monkeypatch):
        """BLOCK should reject requests when DynamoDB is down."""

        # Mock repository method to raise error
        async def mock_error(*args, **kwargs):
            raise ClientError(
                {"Error": {"Code": "ProvisionedThroughputExceededException"}},
                "Query",
            )

        monkeypatch.setattr(sync_limiter._limiter._repository, "get_bucket", mock_error)

        # Set on_unavailable to BLOCK (default)
        sync_limiter._limiter.on_unavailable = OnUnavailable.BLOCK

        # Should raise RateLimiterUnavailable
        limits = [Limit.per_minute("rpm", 100)]
        with pytest.raises(RateLimiterUnavailable) as exc_info:
            with sync_limiter.acquire(
                entity_id="test-entity",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        # Verify exception details
        assert exc_info.value.cause is not None
        assert "ProvisionedThroughputExceededException" in str(exc_info.value.cause)

    def test_allow_override_in_acquire_call(self, sync_limiter, monkeypatch):
        """on_unavailable parameter should override limiter default."""

        # Mock error
        async def mock_error(*args, **kwargs):
            raise ClientError(
                {"Error": {"Code": "InternalServerError"}},
                "TransactWriteItems",
            )

        monkeypatch.setattr(sync_limiter._limiter._repository, "get_bucket", mock_error)

        # Set limiter to BLOCK, but override in acquire
        sync_limiter._limiter.on_unavailable = OnUnavailable.BLOCK

        limits = [Limit.per_minute("rpm", 100)]
        with sync_limiter.acquire(
            entity_id="test-entity",
            resource="api",
            limits=limits,
            consume={"rpm": 1},
            on_unavailable=OnUnavailable.ALLOW,  # Override to ALLOW
        ) as lease:
            # Should get no-op lease due to override
            assert len(lease._lease.entries) == 0

    def test_block_override_in_acquire_call(self, sync_limiter, monkeypatch):
        """on_unavailable parameter should override limiter default."""

        # Mock error
        async def mock_error(*args, **kwargs):
            raise Exception("DynamoDB timeout")

        monkeypatch.setattr(sync_limiter._limiter._repository, "get_bucket", mock_error)

        # Set limiter to ALLOW, but override in acquire
        sync_limiter._limiter.on_unavailable = OnUnavailable.ALLOW

        limits = [Limit.per_minute("rpm", 100)]
        with pytest.raises(RateLimiterUnavailable):
            with sync_limiter.acquire(
                entity_id="test-entity",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
                on_unavailable=OnUnavailable.BLOCK,  # Override to BLOCK
            ):
                pass
