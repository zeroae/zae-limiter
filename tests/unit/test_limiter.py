"""Tests for RateLimiter."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from zae_limiter import (
    Limit,
    LimiterInfo,
    OnUnavailable,
    RateLimiter,
    RateLimiterUnavailable,
    RateLimitExceeded,
)
from zae_limiter.exceptions import InvalidIdentifierError, InvalidNameError
from zae_limiter.infra.discovery import InfrastructureDiscovery


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
    """Tests for cascade functionality."""

    async def test_cascade_consumes_parent(self, limiter):
        """Test that cascade consumes from parent too."""
        await limiter.create_entity(entity_id="proj-1")
        await limiter.create_entity(entity_id="key-1", parent_id="proj-1")

        limits = [Limit.per_minute("rpm", 100)]

        async with limiter.acquire(
            entity_id="key-1",
            resource="gpt-4",
            limits=limits,
            consume={"rpm": 1},
            cascade=True,
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

    async def test_cascade_parent_limit_exceeded(self, limiter):
        """Test that parent limit can block child."""
        await limiter.create_entity(entity_id="proj-1")
        await limiter.create_entity(entity_id="key-1", parent_id="proj-1")

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
                cascade=True,
                use_stored_limits=True,
            ):
                pass

        # The violation should be on the parent
        exc = exc_info.value
        assert any(v.entity_id == "proj-1" for v in exc.violations)


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

        # Default limits (lower)
        default_limits = [Limit.per_minute("rpm", 100)]

        # Use stored limits
        async with limiter.acquire(
            entity_id="key-1",
            resource="gpt-4",
            limits=default_limits,
            consume={"rpm": 200},  # exceeds default but not stored
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


class TestRateLimiterResourceLimits:
    """Tests for resource-level limit configs."""

    async def test_set_and_get_resource_limits(self, limiter):
        """Test storing and retrieving resource-level limits."""
        limits = [
            Limit.per_minute("rpm", 100),
            Limit.per_minute("tpm", 10_000),
        ]
        await limiter.set_resource_limits("gpt-4", limits)

        retrieved = await limiter.get_resource_limits("gpt-4")
        assert len(retrieved) == 2

        names = {limit.name for limit in retrieved}
        assert names == {"rpm", "tpm"}

    async def test_delete_resource_limits(self, limiter):
        """Test deleting resource-level limits."""
        limits = [Limit.per_minute("rpm", 100)]
        await limiter.set_resource_limits("gpt-4", limits)

        await limiter.delete_resource_limits("gpt-4")

        retrieved = await limiter.get_resource_limits("gpt-4")
        assert len(retrieved) == 0

    async def test_get_resource_limits_empty(self, limiter):
        """Test getting resource limits when none exist."""
        retrieved = await limiter.get_resource_limits("nonexistent")
        assert len(retrieved) == 0

    async def test_list_resources_with_limits(self, limiter):
        """Test listing resources with configured limits."""
        # Initially empty
        resources = await limiter.list_resources_with_limits()
        assert len(resources) == 0

        # Add limits for two resources
        limits = [Limit.per_minute("rpm", 100)]
        await limiter.set_resource_limits("gpt-4", limits)
        await limiter.set_resource_limits("claude-3", limits)

        resources = await limiter.list_resources_with_limits()
        assert "gpt-4" in resources
        assert "claude-3" in resources

    async def test_resource_limits_replace_on_update(self, limiter):
        """Test that setting limits replaces existing ones."""
        # Set initial limits
        await limiter.set_resource_limits("gpt-4", [Limit.per_minute("rpm", 100)])

        # Replace with different limits
        await limiter.set_resource_limits("gpt-4", [Limit.per_minute("tpm", 5000)])

        retrieved = await limiter.get_resource_limits("gpt-4")
        assert len(retrieved) == 1
        assert retrieved[0].name == "tpm"


class TestRateLimiterSystemLimits:
    """Tests for system-level limit configs."""

    async def test_set_and_get_system_limits(self, limiter):
        """Test storing and retrieving system-level limits."""
        limits = [
            Limit.per_minute("rpm", 50),
            Limit.per_minute("tpm", 5_000),
        ]
        await limiter.set_system_limits("gpt-4", limits)

        retrieved = await limiter.get_system_limits("gpt-4")
        assert len(retrieved) == 2

        names = {limit.name for limit in retrieved}
        assert names == {"rpm", "tpm"}

    async def test_delete_system_limits(self, limiter):
        """Test deleting system-level limits."""
        limits = [Limit.per_minute("rpm", 50)]
        await limiter.set_system_limits("gpt-4", limits)

        await limiter.delete_system_limits("gpt-4")

        retrieved = await limiter.get_system_limits("gpt-4")
        assert len(retrieved) == 0

    async def test_get_system_limits_empty(self, limiter):
        """Test getting system limits when none exist."""
        retrieved = await limiter.get_system_limits("nonexistent")
        assert len(retrieved) == 0

    async def test_list_system_resources_with_limits(self, limiter):
        """Test listing resources with system-level defaults."""
        # Initially empty
        resources = await limiter.list_system_resources_with_limits()
        assert len(resources) == 0

        # Add limits for two resources
        limits = [Limit.per_minute("rpm", 50)]
        await limiter.set_system_limits("gpt-4", limits)
        await limiter.set_system_limits("claude-3", limits)

        resources = await limiter.list_system_resources_with_limits()
        assert "gpt-4" in resources
        assert "claude-3" in resources

    async def test_system_limits_replace_on_update(self, limiter):
        """Test that setting limits replaces existing ones."""
        # Set initial limits
        await limiter.set_system_limits("gpt-4", [Limit.per_minute("rpm", 50)])

        # Replace with different limits
        await limiter.set_system_limits("gpt-4", [Limit.per_minute("tpm", 2500)])

        retrieved = await limiter.get_system_limits("gpt-4")
        assert len(retrieved) == 1
        assert retrieved[0].name == "tpm"


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
                "GetItem",
            )

        monkeypatch.setattr(limiter._repository, "get_bucket", mock_error)

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
                "Query",
            )

        monkeypatch.setattr(limiter._repository, "get_bucket", mock_error)

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
                "TransactWriteItems",
            )

        monkeypatch.setattr(limiter._repository, "get_bucket", mock_error)

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

        monkeypatch.setattr(limiter._repository, "get_bucket", mock_error)

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
    async def test_limiter_with_stack_options_calls_create_stack(self, mock_dynamodb, monkeypatch):
        """When stack_options is provided, _ensure_initialized should call create_stack."""
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

            # Mock the create_stack method to track calls
            create_stack_mock = AsyncMock(return_value=None)
            monkeypatch.setattr(limiter._repository, "create_stack", create_stack_mock)

            # Call _ensure_initialized
            await limiter._ensure_initialized()

            # Verify create_stack was called with correct stack_options
            create_stack_mock.assert_called_once_with(stack_options=stack_options)

            await limiter.close()

    @pytest.mark.asyncio
    async def test_limiter_without_stack_options_skips_create_stack(
        self, mock_dynamodb, monkeypatch
    ):
        """When stack_options is None, _ensure_initialized should not call create_stack."""
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

            # Mock create_stack to track if it's called
            create_stack_mock = AsyncMock(return_value=None)
            monkeypatch.setattr(limiter._repository, "create_stack", create_stack_mock)

            # Call _ensure_initialized
            await limiter._ensure_initialized()

            # Verify create_stack was NOT called
            create_stack_mock.assert_not_called()

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
        async with limiter.acquire("entity-a", "gpt-4", limits, {"rpm": 20}):
            pass

        # Entity B: consume 50
        async with limiter.acquire("entity-b", "gpt-4", limits, {"rpm": 50}):
            pass

        # Entity C: consume 10
        async with limiter.acquire("entity-c", "gpt-4", limits, {"rpm": 10}):
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
            async with limiter.acquire(entity_id, "api", limits, {"rpm": 10}):
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
        async with limiter.acquire("entity-1", "api", limits, {"rpm": 30}):
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
        assert status.name.startswith("ZAEL-")
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


class TestSyncRateLimiterGetStatus:
    """Tests for SyncRateLimiter.get_status method."""

    def test_sync_get_status_returns_status_object(self, sync_limiter):
        """SyncRateLimiter.get_status should return a Status object."""
        from zae_limiter import Status

        status = sync_limiter.get_status()

        assert isinstance(status, Status)
        assert status.available is True
        assert status.name == sync_limiter._limiter.name

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


class TestRateLimiterInputValidation:
    @pytest.mark.asyncio
    async def test_acquire_validates_entity_id(self, limiter):
        """Acquire should reject entity_id containing reserved delimiter."""
        limits = [Limit.per_minute("rpm", 100)]

        with pytest.raises(InvalidIdentifierError) as exc_info:
            async with limiter.acquire("user#123", "api", limits, {"rpm": 1}):
                pass

        assert exc_info.value.field == "entity_id"
        assert "#" in exc_info.value.reason

    @pytest.mark.asyncio
    async def test_acquire_validates_resource(self, limiter):
        """Acquire should reject resource containing reserved delimiter."""
        limits = [Limit.per_minute("rpm", 100)]

        with pytest.raises(InvalidNameError) as exc_info:
            async with limiter.acquire("user-123", "api#v2", limits, {"rpm": 1}):
                pass

        assert exc_info.value.field == "resource"
        assert "#" in exc_info.value.reason

    @pytest.mark.asyncio
    async def test_acquire_validates_empty_entity_id(self, limiter):
        """Acquire should reject empty entity_id."""
        limits = [Limit.per_minute("rpm", 100)]

        with pytest.raises(InvalidIdentifierError) as exc_info:
            async with limiter.acquire("", "api", limits, {"rpm": 1}):
                pass

        assert exc_info.value.field == "entity_id"
        assert "empty" in exc_info.value.reason

    @pytest.mark.asyncio
    async def test_acquire_validates_empty_resource(self, limiter):
        """Acquire should reject empty resource."""
        limits = [Limit.per_minute("rpm", 100)]

        with pytest.raises(InvalidNameError) as exc_info:
            async with limiter.acquire("user-123", "", limits, {"rpm": 1}):
                pass

        assert exc_info.value.field == "resource"
        assert "empty" in exc_info.value.reason

    @pytest.mark.asyncio
    async def test_acquire_accepts_valid_inputs(self, limiter):
        """Acquire should accept valid entity_id and resource."""
        limits = [Limit.per_minute("rpm", 100)]

        # Should not raise
        async with limiter.acquire("user-123", "gpt-3.5-turbo", limits, {"rpm": 1}):
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
        """list_limiters returns empty list when no ZAEL- stacks exist."""
        with patch.object(
            InfrastructureDiscovery, "_get_client", new_callable=AsyncMock
        ) as mock_get_client:
            mock_client = MagicMock()
            mock_client.list_stacks = AsyncMock(
                return_value={"StackSummaries": [], "NextToken": None}
            )
            mock_get_client.return_value = mock_client

            async with InfrastructureDiscovery(region="us-east-1") as discovery:
                limiters = await discovery.list_limiters()

            assert limiters == []

    @pytest.mark.asyncio
    async def test_list_limiters_filters_by_prefix(self):
        """list_limiters only returns stacks with ZAEL- prefix."""
        with patch.object(
            InfrastructureDiscovery, "_get_client", new_callable=AsyncMock
        ) as mock_get_client:
            mock_client = MagicMock()
            mock_client.list_stacks = AsyncMock(
                return_value={
                    "StackSummaries": [
                        {
                            "StackName": "ZAEL-my-app",
                            "StackStatus": "CREATE_COMPLETE",
                            "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                        },
                        {
                            "StackName": "other-stack",
                            "StackStatus": "CREATE_COMPLETE",
                            "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                        },
                        {
                            "StackName": "ZAEL-another",
                            "StackStatus": "UPDATE_COMPLETE",
                            "CreationTime": datetime(2024, 1, 14, 9, 0, 0),
                        },
                    ],
                    "NextToken": None,
                }
            )
            mock_client.describe_stacks = AsyncMock(return_value={"Stacks": [{"Tags": []}]})
            mock_get_client.return_value = mock_client

            async with InfrastructureDiscovery(region="us-east-1") as discovery:
                limiters = await discovery.list_limiters()

            # Should only include ZAEL- prefixed stacks
            assert len(limiters) == 2
            stack_names = {lim.stack_name for lim in limiters}
            assert "ZAEL-my-app" in stack_names
            assert "ZAEL-another" in stack_names
            assert "other-stack" not in stack_names

    @pytest.mark.asyncio
    async def test_list_limiters_extracts_user_name(self):
        """list_limiters correctly strips ZAEL- prefix for user_name."""
        with patch.object(
            InfrastructureDiscovery, "_get_client", new_callable=AsyncMock
        ) as mock_get_client:
            mock_client = MagicMock()
            mock_client.list_stacks = AsyncMock(
                return_value={
                    "StackSummaries": [
                        {
                            "StackName": "ZAEL-my-app",
                            "StackStatus": "CREATE_COMPLETE",
                            "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                        },
                    ],
                    "NextToken": None,
                }
            )
            mock_client.describe_stacks = AsyncMock(return_value={"Stacks": [{"Tags": []}]})
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
            mock_client.list_stacks = AsyncMock(
                return_value={
                    "StackSummaries": [
                        {
                            "StackName": "ZAEL-my-app",
                            "StackStatus": "CREATE_COMPLETE",
                            "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                        },
                    ],
                    "NextToken": None,
                }
            )
            mock_client.describe_stacks = AsyncMock(
                return_value={
                    "Stacks": [
                        {
                            "Tags": [
                                {"Key": "zae-limiter:version", "Value": "0.5.0"},
                                {"Key": "zae-limiter:lambda-version", "Value": "0.5.0"},
                                {"Key": "zae-limiter:schema-version", "Value": "1.0.0"},
                            ]
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
            mock_client.list_stacks = AsyncMock(
                return_value={
                    "StackSummaries": [
                        {
                            "StackName": "ZAEL-my-app",
                            "StackStatus": "CREATE_COMPLETE",
                            "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                        },
                    ],
                    "NextToken": None,
                }
            )
            # Return no tags
            mock_client.describe_stacks = AsyncMock(return_value={"Stacks": [{"Tags": []}]})
            mock_get_client.return_value = mock_client

            async with InfrastructureDiscovery(region="us-east-1") as discovery:
                limiters = await discovery.list_limiters()

            assert len(limiters) == 1
            assert limiters[0].version is None
            assert limiters[0].lambda_version is None
            assert limiters[0].schema_version is None

    @pytest.mark.asyncio
    async def test_list_limiters_handles_describe_stacks_error(self):
        """list_limiters handles describe_stacks errors (e.g., stack deleted between calls)."""
        with patch.object(
            InfrastructureDiscovery, "_get_client", new_callable=AsyncMock
        ) as mock_get_client:
            mock_client = MagicMock()
            mock_client.list_stacks = AsyncMock(
                return_value={
                    "StackSummaries": [
                        {
                            "StackName": "ZAEL-my-app",
                            "StackStatus": "CREATE_COMPLETE",
                            "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                        },
                    ],
                    "NextToken": None,
                }
            )
            # describe_stacks fails (race condition - stack deleted)
            mock_client.describe_stacks = AsyncMock(
                side_effect=ClientError(
                    {"Error": {"Code": "ValidationError", "Message": "Stack not found"}},
                    "DescribeStacks",
                )
            )
            mock_get_client.return_value = mock_client

            async with InfrastructureDiscovery(region="us-east-1") as discovery:
                limiters = await discovery.list_limiters()

            # Should still return the limiter, just with None versions
            assert len(limiters) == 1
            assert limiters[0].version is None

    @pytest.mark.asyncio
    async def test_list_limiters_with_last_updated_time(self):
        """list_limiters includes last_updated_time when present."""
        with patch.object(
            InfrastructureDiscovery, "_get_client", new_callable=AsyncMock
        ) as mock_get_client:
            mock_client = MagicMock()
            mock_client.list_stacks = AsyncMock(
                return_value={
                    "StackSummaries": [
                        {
                            "StackName": "ZAEL-my-app",
                            "StackStatus": "UPDATE_COMPLETE",
                            "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                            "LastUpdatedTime": datetime(2024, 1, 16, 14, 0, 0),
                        },
                    ],
                    "NextToken": None,
                }
            )
            mock_client.describe_stacks = AsyncMock(return_value={"Stacks": [{"Tags": []}]})
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
            mock_client.list_stacks = AsyncMock(
                return_value={
                    "StackSummaries": [
                        {
                            "StackName": "ZAEL-healthy",
                            "StackStatus": "CREATE_COMPLETE",
                            "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                        },
                        {
                            "StackName": "ZAEL-in-progress",
                            "StackStatus": "UPDATE_IN_PROGRESS",
                            "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                        },
                        {
                            "StackName": "ZAEL-failed",
                            "StackStatus": "CREATE_FAILED",
                            "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                        },
                    ],
                    "NextToken": None,
                }
            )
            mock_client.describe_stacks = AsyncMock(return_value={"Stacks": [{"Tags": []}]})
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
            mock_client.list_stacks = AsyncMock(
                return_value={
                    "StackSummaries": [
                        {
                            "StackName": "ZAEL-zebra",
                            "StackStatus": "CREATE_COMPLETE",
                            "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                        },
                        {
                            "StackName": "ZAEL-apple",
                            "StackStatus": "CREATE_COMPLETE",
                            "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                        },
                        {
                            "StackName": "ZAEL-banana",
                            "StackStatus": "CREATE_COMPLETE",
                            "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                        },
                    ],
                    "NextToken": None,
                }
            )
            mock_client.describe_stacks = AsyncMock(return_value={"Stacks": [{"Tags": []}]})
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
            # First page
            mock_client.list_stacks = AsyncMock(
                side_effect=[
                    {
                        "StackSummaries": [
                            {
                                "StackName": "ZAEL-first",
                                "StackStatus": "CREATE_COMPLETE",
                                "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                            },
                        ],
                        "NextToken": "page2token",
                    },
                    {
                        "StackSummaries": [
                            {
                                "StackName": "ZAEL-second",
                                "StackStatus": "CREATE_COMPLETE",
                                "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                            },
                        ],
                        "NextToken": None,
                    },
                ]
            )
            mock_client.describe_stacks = AsyncMock(return_value={"Stacks": [{"Tags": []}]})
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
            mock_client.list_stacks = AsyncMock(
                return_value={
                    "StackSummaries": [
                        {
                            "StackName": "ZAEL-my-app",
                            "StackStatus": "CREATE_COMPLETE",
                            "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                        },
                    ],
                    "NextToken": None,
                }
            )
            mock_client.describe_stacks = AsyncMock(return_value={"Stacks": [{"Tags": []}]})
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
            mock_client.list_stacks = AsyncMock(
                return_value={
                    "StackSummaries": [
                        {
                            "StackName": "ZAEL-my-app",
                            "StackStatus": "CREATE_COMPLETE",
                            "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                        },
                    ],
                    "NextToken": None,
                }
            )
            mock_client.describe_stacks = AsyncMock(return_value={"Stacks": [{"Tags": []}]})
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
            mock_client.list_stacks = AsyncMock(
                return_value={"StackSummaries": [], "NextToken": None}
            )
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
                stack_name="ZAEL-app1",
                user_name="app1",
                region="us-east-1",
                stack_status="CREATE_COMPLETE",
                creation_time="2024-01-15T10:30:00Z",
            ),
            LimiterInfo(
                stack_name="ZAEL-app2",
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
            mock_client.list_stacks = AsyncMock(
                side_effect=ClientError(
                    {"Error": {"Code": "AccessDenied", "Message": "Not authorized"}},
                    "ListStacks",
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
    async def test_get_version_tags_empty_stacks_response(self):
        """_get_version_tags handles empty Stacks list in response."""
        with patch.object(
            InfrastructureDiscovery, "_get_client", new_callable=AsyncMock
        ) as mock_get_client:
            mock_client = MagicMock()
            mock_client.list_stacks = AsyncMock(
                return_value={
                    "StackSummaries": [
                        {
                            "StackName": "ZAEL-my-app",
                            "StackStatus": "CREATE_COMPLETE",
                            "CreationTime": datetime(2024, 1, 15, 10, 30, 0),
                        },
                    ],
                    "NextToken": None,
                }
            )
            # describe_stacks returns empty Stacks list
            mock_client.describe_stacks = AsyncMock(return_value={"Stacks": []})
            mock_get_client.return_value = mock_client

            async with InfrastructureDiscovery(region="us-east-1") as discovery:
                limiters = await discovery.list_limiters()

            # Should still return limiter with None versions
            assert len(limiters) == 1
            assert limiters[0].version is None
            assert limiters[0].lambda_version is None
            assert limiters[0].schema_version is None

    @pytest.mark.asyncio
    async def test_discovery_close_with_client_exception(self):
        """close() handles exceptions during client cleanup gracefully."""
        with patch.object(
            InfrastructureDiscovery, "_get_client", new_callable=AsyncMock
        ) as mock_get_client:
            mock_client = MagicMock()
            mock_client.list_stacks = AsyncMock(
                return_value={"StackSummaries": [], "NextToken": None}
            )
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
