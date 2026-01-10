"""Tests for RateLimiter."""

import pytest

from zae_limiter import (
    FailureMode,
    Limit,
    RateLimitExceeded,
)


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

        names = {l.name for l in retrieved}
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
