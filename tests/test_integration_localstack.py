"""Integration tests using LocalStack.

These tests run against a real LocalStack instance with full AWS service emulation
including CloudFormation, DynamoDB, DynamoDB Streams, and Lambda.

To run these tests locally:
    docker run -p 4566:4566 \\
      -e SERVICES=dynamodb,dynamodbstreams,lambda,cloudformation,logs,iam,cloudwatch,sqs \\
      -v /var/run/docker.sock:/var/run/docker.sock \\
      localstack/localstack

    AWS_ENDPOINT_URL=http://localhost:4566 pytest -m integration -v
"""

import pytest

from zae_limiter import Limit, RateLimiter, RateLimitExceeded, SyncRateLimiter

pytestmark = pytest.mark.integration

# Fixtures are defined in conftest.py:
# - localstack_endpoint
# - minimal_stack_options, aggregator_stack_options, full_stack_options


class TestLocalStackIntegration:
    """Integration tests with full LocalStack deployment."""

    @pytest.fixture(scope="class")
    async def shared_stack(
        self, localstack_endpoint, minimal_stack_options, unique_table_name_class
    ):
        """
        Create and manage the CloudFormation stack for all tests in this class.

        This fixture creates the stack once when the first test runs and
        deletes it after all tests in the class complete.
        """
        limiter = RateLimiter(
            table_name=unique_table_name_class,
            endpoint_url=localstack_endpoint,
            region="us-east-1",
            stack_options=minimal_stack_options,
        )

        async with limiter:
            yield unique_table_name_class

        try:
            await limiter.delete_stack()
        except Exception as e:
            print(f"Warning: Stack cleanup failed: {e}")

    @pytest.fixture
    async def localstack_limiter(self, localstack_endpoint, shared_stack):
        """
        Create a fresh RateLimiter instance for each test.

        Uses the shared stack (no stack_options) so no new stack is created.
        Each test gets its own RateLimiter to avoid event loop issues.
        """
        limiter = RateLimiter(
            table_name=shared_stack,
            endpoint_url=localstack_endpoint,
            region="us-east-1",
        )

        async with limiter:
            yield limiter

    @pytest.mark.asyncio
    async def test_cloudformation_stack_deployment(
        self, localstack_endpoint, full_stack_options, unique_table_name
    ):
        """Test full CloudFormation stack creation in LocalStack (with aggregator and alarms)."""
        limiter = RateLimiter(
            table_name=unique_table_name,
            endpoint_url=localstack_endpoint,
            region="us-east-1",
            stack_options=full_stack_options,
        )

        async with limiter:
            # Verify stack was created by checking if we can perform operations
            entity = await limiter.create_entity("cfn-full-entity", name="CFN Full Entity")
            assert entity.id == "cfn-full-entity"
            assert entity.name == "CFN Full Entity"

    @pytest.mark.asyncio
    async def test_cloudformation_stack_deployment_no_alarms(
        self, localstack_endpoint, aggregator_stack_options, unique_table_name
    ):
        """Test CloudFormation stack creation with aggregator but without alarms.

        This tests the edge case where EnableAggregator=true but EnableAlarms=false.
        The AggregatorDLQAlarmName output should not be created in this scenario.
        """
        limiter = RateLimiter(
            table_name=unique_table_name,
            endpoint_url=localstack_endpoint,
            region="us-east-1",
            stack_options=aggregator_stack_options,
        )

        async with limiter:
            # Verify stack was created by checking if we can perform operations
            entity = await limiter.create_entity("cfn-no-alarms-entity", name="CFN No Alarms Entity")
            assert entity.id == "cfn-no-alarms-entity"
            assert entity.name == "CFN No Alarms Entity"

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

    @pytest.fixture(scope="class")
    def shared_stack_sync(
        self, localstack_endpoint, minimal_stack_options, unique_table_name_class
    ):
        """
        Create and manage the CloudFormation stack for all sync tests in this class.

        This fixture creates the stack once when the first test runs and
        deletes it after all tests in the class complete.
        """
        limiter = SyncRateLimiter(
            table_name=unique_table_name_class,
            endpoint_url=localstack_endpoint,
            region="us-east-1",
            stack_options=minimal_stack_options,
        )

        with limiter:
            yield unique_table_name_class

        try:
            limiter.delete_stack()
        except Exception as e:
            print(f"Warning: Stack cleanup failed: {e}")

    @pytest.fixture
    def sync_localstack_limiter(self, localstack_endpoint, shared_stack_sync):
        """
        Create a fresh SyncRateLimiter instance for each test.

        Uses the shared stack (no stack_options) so no new stack is created.
        Each test gets its own SyncRateLimiter to avoid issues.
        """
        limiter = SyncRateLimiter(
            table_name=shared_stack_sync,
            endpoint_url=localstack_endpoint,
            region="us-east-1",
        )

        with limiter:
            yield limiter

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
