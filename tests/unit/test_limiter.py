"""Tests for RateLimiter."""

import pytest
from botocore.exceptions import ClientError

from zae_limiter import (
    FailureMode,
    Limit,
    RateLimiterUnavailable,
    RateLimitExceeded,
)
from zae_limiter.exceptions import InvalidIdentifierError, InvalidNameError


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


class TestRateLimiterFailureMode:
    """Tests for FAIL_OPEN vs FAIL_CLOSED behavior."""

    @pytest.mark.asyncio
    async def test_fail_open_returns_noop_lease_on_dynamodb_error(self, limiter, monkeypatch):
        """FAIL_OPEN should return no-op lease on infrastructure error."""

        # Mock repository method to raise error
        async def mock_error(*args, **kwargs):
            raise ClientError(
                {"Error": {"Code": "ServiceUnavailable", "Message": "DynamoDB down"}},
                "GetItem",
            )

        monkeypatch.setattr(limiter._repository, "get_bucket", mock_error)

        # Set failure mode to FAIL_OPEN
        limiter.failure_mode = FailureMode.FAIL_OPEN

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
    async def test_fail_closed_raises_unavailable_on_dynamodb_error(self, limiter, monkeypatch):
        """FAIL_CLOSED should reject requests when DynamoDB is down."""

        # Mock repository method to raise error
        async def mock_error(*args, **kwargs):
            raise ClientError(
                {"Error": {"Code": "ProvisionedThroughputExceededException"}},
                "Query",
            )

        monkeypatch.setattr(limiter._repository, "get_bucket", mock_error)

        # Set failure mode to FAIL_CLOSED (default)
        limiter.failure_mode = FailureMode.FAIL_CLOSED

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
    async def test_fail_open_override_in_acquire_call(self, limiter, monkeypatch):
        """failure_mode parameter should override limiter default."""

        # Mock error
        async def mock_error(*args, **kwargs):
            raise ClientError(
                {"Error": {"Code": "InternalServerError"}},
                "TransactWriteItems",
            )

        monkeypatch.setattr(limiter._repository, "get_bucket", mock_error)

        # Set limiter to FAIL_CLOSED, but override in acquire
        limiter.failure_mode = FailureMode.FAIL_CLOSED

        limits = [Limit.per_minute("rpm", 100)]
        async with limiter.acquire(
            entity_id="test-entity",
            resource="api",
            limits=limits,
            consume={"rpm": 1},
            failure_mode=FailureMode.FAIL_OPEN,  # Override to FAIL_OPEN
        ) as lease:
            # Should get no-op lease due to override
            assert len(lease.entries) == 0

    @pytest.mark.asyncio
    async def test_fail_closed_override_in_acquire_call(self, limiter, monkeypatch):
        """failure_mode parameter should override limiter default."""

        # Mock error
        async def mock_error(*args, **kwargs):
            raise Exception("DynamoDB timeout")

        monkeypatch.setattr(limiter._repository, "get_bucket", mock_error)

        # Set limiter to FAIL_OPEN, but override in acquire
        limiter.failure_mode = FailureMode.FAIL_OPEN

        limits = [Limit.per_minute("rpm", 100)]
        with pytest.raises(RateLimiterUnavailable):
            async with limiter.acquire(
                entity_id="test-entity",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
                failure_mode=FailureMode.FAIL_CLOSED,  # Override to FAIL_CLOSED
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


class TestRateLimiterInputValidation:
    """Tests for input validation at API boundary."""

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
