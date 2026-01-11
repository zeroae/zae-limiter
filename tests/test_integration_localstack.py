"""Integration tests using LocalStack.

These tests run against a real LocalStack instance with full AWS service emulation
including CloudFormation, DynamoDB, DynamoDB Streams, and Lambda.

To run these tests locally:
    docker run -p 4566:4566 \\
      -e SERVICES=dynamodb,dynamodbstreams,lambda,cloudformation,logs,iam \\
      localstack/localstack

    AWS_ENDPOINT_URL=http://localhost:4566 pytest -m integration -v
"""

import os

import pytest

from zae_limiter import Limit, RateLimiter, RateLimitExceeded, StackOptions, SyncRateLimiter

pytestmark = pytest.mark.integration


@pytest.fixture
def localstack_endpoint():
    """Get LocalStack endpoint from environment."""
    endpoint = os.getenv("AWS_ENDPOINT_URL")
    if not endpoint:
        pytest.skip("AWS_ENDPOINT_URL not set - LocalStack not available")
    return endpoint


@pytest.fixture
async def localstack_limiter(localstack_endpoint):
    """Create RateLimiter with LocalStack for integration testing."""
    limiter = RateLimiter(
        table_name="integration_test_rate_limits",
        endpoint_url=localstack_endpoint,
        region="us-east-1",
        stack_options=StackOptions(),  # Test full stack deployment
    )

    async with limiter:
        yield limiter


@pytest.fixture
def sync_localstack_limiter(localstack_endpoint):
    """Create SyncRateLimiter with LocalStack for integration testing."""
    limiter = SyncRateLimiter(
        table_name="integration_test_rate_limits_sync",
        endpoint_url=localstack_endpoint,
        region="us-east-1",
        stack_options=StackOptions(),
    )

    with limiter:
        yield limiter


class TestLocalStackIntegration:
    """Integration tests with full LocalStack deployment."""

    @pytest.mark.asyncio
    async def test_cloudformation_stack_deployment(self, localstack_endpoint):
        """Test CloudFormation stack creation in LocalStack."""
        limiter = RateLimiter(
            table_name="test_cloudformation_deployment",
            endpoint_url=localstack_endpoint,
            region="us-east-1",
            stack_options=StackOptions(),
        )

        async with limiter:
            # Verify stack was created by checking if we can perform operations
            entity = await limiter.create_entity("test-entity", name="Test Entity")
            assert entity.id == "test-entity"
            assert entity.name == "Test Entity"

    @pytest.mark.asyncio
    async def test_rate_limiting_operations(self, localstack_limiter):
        """Test basic rate limiting against real DynamoDB in LocalStack."""
        limits = [
            Limit.per_minute("rpm", 5),
            Limit.per_minute("tpm", 100),
        ]

        # Successful acquisitions
        for i in range(3):
            async with localstack_limiter.acquire(
                entity_id="test-user",
                resource="api",
                limits=limits,
                consume={"rpm": 1, "tpm": 10},
            ) as lease:
                assert lease.consumed == {"rpm": 1, "tpm": 10}

        # Check available capacity
        available = await localstack_limiter.available(
            entity_id="test-user",
            resource="api",
            limits=limits,
        )
        assert "rpm" in available
        assert "tpm" in available
        assert available["rpm"] <= 2  # Used 3 out of 5

    @pytest.mark.asyncio
    async def test_hierarchical_limits(self, localstack_limiter):
        """Test parent-child entity relationships with real DynamoDB."""
        # Create parent entity
        parent = await localstack_limiter.create_entity(
            entity_id="parent-org",
            name="Parent Organization",
        )
        assert parent.id == "parent-org"

        # Create child entity
        child = await localstack_limiter.create_entity(
            entity_id="child-team",
            name="Child Team",
            parent_id="parent-org",
        )
        assert child.id == "child-team"
        assert child.parent_id == "parent-org"

        # Query children
        children = await localstack_limiter.get_children("parent-org")
        assert len(children) >= 1
        child_ids = [c.id for c in children]
        assert "child-team" in child_ids

        # Test cascade consumption
        limits = [Limit.per_minute("rpm", 100)]
        async with localstack_limiter.acquire(
            entity_id="child-team",
            resource="api",
            limits=limits,
            consume={"rpm": 1},
            cascade=True,
        ) as lease:
            # Cascade consumes from both child and parent, so total is 2
            assert lease.consumed == {"rpm": 2}

        # Verify both child and parent were consumed
        child_available = await localstack_limiter.available(
            entity_id="child-team",
            resource="api",
            limits=limits,
        )
        parent_available = await localstack_limiter.available(
            entity_id="parent-org",
            resource="api",
            limits=limits,
        )
        assert child_available["rpm"] < 100  # Some consumed
        assert parent_available["rpm"] < 100  # Cascade consumed

    @pytest.mark.asyncio
    async def test_rate_limit_exceeded(self, localstack_limiter):
        """Test that rate limit violations are properly detected."""
        limits = [Limit.per_minute("rpm", 2)]

        # Consume the limit
        for _ in range(2):
            async with localstack_limiter.acquire(
                entity_id="test-limited-user",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        # Third request should fail
        with pytest.raises(RateLimitExceeded) as exc_info:
            async with localstack_limiter.acquire(
                entity_id="test-limited-user",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        # Verify exception details
        assert len(exc_info.value.violations) > 0
        assert exc_info.value.retry_after_seconds > 0
        assert "rpm" in [v.limit_name for v in exc_info.value.violations]

    @pytest.mark.asyncio
    async def test_stored_limits(self, localstack_limiter):
        """Test setting and using stored limits."""
        # Set stored limits for an entity
        stored_limits = [
            Limit.per_minute("rpm", 1000),
            Limit.per_minute("tpm", 50000),
        ]
        await localstack_limiter.set_limits(
            entity_id="premium-user",
            limits=stored_limits,
        )

        # Use stored limits
        default_limits = [Limit.per_minute("rpm", 10)]
        async with localstack_limiter.acquire(
            entity_id="premium-user",
            resource="api",
            limits=default_limits,  # Fallback
            consume={"rpm": 1, "tpm": 100},
            use_stored_limits=True,  # Use stored limits instead
        ) as lease:
            assert lease.consumed == {"rpm": 1, "tpm": 100}

        # Verify stored limits were used (should have high capacity)
        available = await localstack_limiter.available(
            entity_id="premium-user",
            resource="api",
            limits=default_limits,
            use_stored_limits=True,
        )
        assert available["rpm"] > 900  # Should have premium limit
        assert "tpm" in available


class TestSyncLocalStackIntegration:
    """Integration tests for sync API with LocalStack."""

    def test_sync_rate_limiting(self, sync_localstack_limiter):
        """Test sync rate limiting operations."""
        limits = [Limit.per_minute("rpm", 10)]

        # Make sync requests
        for i in range(3):
            with sync_localstack_limiter.acquire(
                entity_id="sync-user",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ) as lease:
                assert lease.consumed == {"rpm": 1}

        # Check available (sync)
        available = sync_localstack_limiter.available(
            entity_id="sync-user",
            resource="api",
            limits=limits,
        )
        assert available["rpm"] <= 7  # Used 3 out of 10

    def test_sync_rate_limit_exceeded(self, sync_localstack_limiter):
        """Test sync rate limit violations."""
        limits = [Limit.per_minute("rpm", 2)]

        # Consume the limit
        for _ in range(2):
            with sync_localstack_limiter.acquire(
                entity_id="sync-limited-user",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        # Third request should fail
        with pytest.raises(RateLimitExceeded) as exc_info:
            with sync_localstack_limiter.acquire(
                entity_id="sync-limited-user",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        assert len(exc_info.value.violations) > 0
