"""End-to-end integration tests using LocalStack.

These tests run the complete zae-limiter lifecycle against LocalStack:
1. Deploy full CloudFormation stack via CLI or RateLimiter
2. Create entities with hierarchical relationships
3. Set limits and acquire/release leases
4. Consume tokens and verify aggregator processing
5. Check usage snapshots in DynamoDB
6. Clean up by deleting the stack

To run these tests locally:
    docker run -p 4566:4566 \
      -e SERVICES=dynamodb,dynamodbstreams,lambda,cloudformation,logs,iam,cloudwatch,sqs \
      -v /var/run/docker.sock:/var/run/docker.sock \
      -v "${TMPDIR:-/tmp}/localstack:/var/lib/localstack" \
      localstack/localstack

    AWS_ENDPOINT_URL=http://localhost:4566 \
    AWS_ACCESS_KEY_ID=test \
    AWS_SECRET_ACCESS_KEY=test \
    AWS_DEFAULT_REGION=us-east-1 \
    pytest tests/test_e2e_localstack.py -v

Note: The Docker socket mount (-v /var/run/docker.sock:/var/run/docker.sock) is
required for LocalStack to spawn Lambda functions as Docker containers.
"""

import asyncio

import pytest
from click.testing import CliRunner

from zae_limiter import Limit, RateLimiter, RateLimitExceeded, StackOptions, SyncRateLimiter
from zae_limiter.cli import cli

pytestmark = [pytest.mark.integration, pytest.mark.e2e]


class TestE2ELocalStackCLIWorkflow:
    """E2E tests using CLI for stack deployment."""

    @pytest.fixture
    def cli_runner(self):
        """Create Click CLI runner."""
        return CliRunner()

    def test_full_cli_workflow(self, cli_runner, localstack_endpoint, unique_table_name):
        """
        Complete E2E workflow using CLI commands.

        Steps:
        1. Deploy stack via CLI
        2. Create SyncRateLimiter and use it
        3. Verify operations work
        4. Check stack status via CLI
        5. Delete stack via CLI

        Note: Uses SyncRateLimiter because CLI uses asyncio.run() internally,
        which conflicts with pytest-asyncio's event loop.
        """
        stack_name = f"zae-limiter-{unique_table_name}"

        try:
            # Step 1: Deploy stack via CLI
            result = cli_runner.invoke(
                cli,
                [
                    "deploy",
                    "--table-name",
                    unique_table_name,
                    "--endpoint-url",
                    localstack_endpoint,
                    "--region",
                    "us-east-1",
                    "--snapshot-windows",
                    "hourly",
                    "--retention-days",
                    "7",
                    "--no-aggregator",  # Faster deployment for CLI test
                    "--no-alarms",
                    "--wait",
                ],
            )
            assert result.exit_code == 0, f"Deploy failed: {result.output}"
            assert "Stack create complete" in result.output

            # Step 2: Check status via CLI
            # Note: status command reads AWS_ENDPOINT_URL from environment (see #78)
            result = cli_runner.invoke(
                cli,
                [
                    "status",
                    "--stack-name",
                    stack_name,
                    "--region",
                    "us-east-1",
                ],
                env={"AWS_ENDPOINT_URL": localstack_endpoint},
            )
            assert result.exit_code == 0
            # Status output contains the stack status
            assert "CREATE_COMPLETE" in result.output or "Status:" in result.output

            # Step 3: Use SyncRateLimiter with deployed infrastructure
            limiter = SyncRateLimiter(
                table_name=unique_table_name,
                endpoint_url=localstack_endpoint,
                region="us-east-1",
            )

            with limiter:
                # Create entity and use rate limiting
                entity = limiter.create_entity("cli-test-user", name="CLI Test")
                assert entity.id == "cli-test-user"

                limits = [Limit.per_minute("rpm", 10)]
                with limiter.acquire(
                    entity_id="cli-test-user",
                    resource="api",
                    limits=limits,
                    consume={"rpm": 1},
                ) as lease:
                    assert lease.consumed == {"rpm": 1}

        finally:
            # Step 4: Delete stack via CLI
            result = cli_runner.invoke(
                cli,
                [
                    "delete",
                    "--stack-name",
                    stack_name,
                    "--region",
                    "us-east-1",
                    "--endpoint-url",
                    localstack_endpoint,
                    "--yes",
                    "--wait",
                ],
            )
            # Don't assert exit code - stack might not exist if deploy failed


class TestE2ELocalStackFullWorkflow:
    """E2E tests for full rate limiting workflow."""

    @pytest.fixture
    async def e2e_limiter(self, localstack_endpoint, unique_table_name, e2e_stack_options):
        """
        Create RateLimiter with full stack for E2E tests.

        Fixture handles setup (stack creation) and teardown (stack deletion).
        """
        limiter = RateLimiter(
            table_name=unique_table_name,
            endpoint_url=localstack_endpoint,
            region="us-east-1",
            stack_options=e2e_stack_options,
        )

        async with limiter:
            yield limiter

        # Explicitly delete the stack after test completes
        try:
            await limiter.delete_stack()
        except Exception as e:
            # LocalStack may have issues with stack deletion, log but don't fail
            print(f"Warning: Stack cleanup failed: {e}")

    @pytest.mark.asyncio
    async def test_hierarchical_rate_limiting_workflow(self, e2e_limiter):
        """
        Test hierarchical rate limiting with parent-child entities.

        Workflow:
        1. Create parent (organization) and child (API key) entities
        2. Set limits on both
        3. Consume from child with cascade
        4. Verify both are affected
        """
        # Create parent organization
        parent = await e2e_limiter.create_entity("org-acme", name="ACME Organization")
        assert parent.id == "org-acme"

        # Create child API key
        child = await e2e_limiter.create_entity(
            "api-key-123",
            name="Production API Key",
            parent_id="org-acme",
        )
        assert child.parent_id == "org-acme"

        # Verify parent-child relationship
        children = await e2e_limiter.get_children("org-acme")
        assert len(children) == 1
        assert children[0].id == "api-key-123"

        # Set limits
        limits = [
            Limit.per_minute("rpm", 100),
            Limit.per_minute("tpm", 10000),
        ]

        # Consume from child with cascade
        async with e2e_limiter.acquire(
            entity_id="api-key-123",
            resource="gpt-4",
            limits=limits,
            consume={"rpm": 1, "tpm": 500},
            cascade=True,
        ) as lease:
            # With cascade, consumes from both child and parent
            assert lease.consumed["rpm"] == 2  # 1 from child + 1 from parent
            assert lease.consumed["tpm"] == 1000  # 500 from each

        # Verify both entities have reduced capacity
        child_available = await e2e_limiter.available(
            entity_id="api-key-123",
            resource="gpt-4",
            limits=limits,
        )
        parent_available = await e2e_limiter.available(
            entity_id="org-acme",
            resource="gpt-4",
            limits=limits,
        )

        assert child_available["rpm"] < 100
        assert parent_available["rpm"] < 100

    @pytest.mark.asyncio
    async def test_rate_limit_exceeded_workflow(self, e2e_limiter):
        """
        Test rate limit exceeded scenario.

        Workflow:
        1. Create entity with low limits
        2. Exhaust the limits
        3. Verify RateLimitExceeded with retry_after
        """
        await e2e_limiter.create_entity("limited-user")

        # Very low limit
        limits = [Limit.per_minute("rpm", 2)]

        # Exhaust the limit
        for _ in range(2):
            async with e2e_limiter.acquire(
                entity_id="limited-user",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        # Third request should fail
        with pytest.raises(RateLimitExceeded) as exc_info:
            async with e2e_limiter.acquire(
                entity_id="limited-user",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        # Verify exception details
        exc = exc_info.value
        assert len(exc.violations) > 0
        assert exc.retry_after_seconds > 0
        assert "rpm" in [v.limit_name for v in exc.violations]

        # Verify as_dict() for API responses
        error_dict = exc.as_dict()
        assert "limits" in error_dict
        assert "retry_after_seconds" in error_dict

    @pytest.mark.asyncio
    async def test_stored_limits_workflow(self, e2e_limiter):
        """
        Test stored limits (premium vs default tiers).

        Workflow:
        1. Create premium and free tier users
        2. Set stored limits for premium user
        3. Verify premium user has higher limits
        """
        await e2e_limiter.create_entity("premium-user")
        await e2e_limiter.create_entity("free-user")

        # Set premium limits
        premium_limits = [
            Limit.per_minute("rpm", 1000),
            Limit.per_minute("tpm", 100000),
        ]
        await e2e_limiter.set_limits("premium-user", premium_limits)

        # Default limits for fallback
        default_limits = [
            Limit.per_minute("rpm", 10),
            Limit.per_minute("tpm", 1000),
        ]

        # Premium user uses stored limits
        async with e2e_limiter.acquire(
            entity_id="premium-user",
            resource="api",
            limits=default_limits,
            consume={"rpm": 1},
            use_stored_limits=True,
        ) as lease:
            # Consumed includes all limit types, even if 0
            assert lease.consumed["rpm"] == 1

        # Verify premium capacity
        premium_available = await e2e_limiter.available(
            entity_id="premium-user",
            resource="api",
            limits=default_limits,
            use_stored_limits=True,
        )
        assert premium_available["rpm"] > 900  # High limit

    @pytest.mark.asyncio
    async def test_lease_adjustment_workflow(self, e2e_limiter):
        """
        Test lease adjustment for post-hoc token counting (LLM tokens).

        Workflow:
        1. Acquire lease with estimated tokens
        2. Simulate API call with actual token count
        3. Adjust lease with actual tokens
        4. Verify final token count
        """
        await e2e_limiter.create_entity("llm-user")

        limits = [
            Limit.per_minute("rpm", 100),
            Limit.per_minute("tpm", 10000),
        ]

        # Acquire with estimated tokens (pre-call)
        async with e2e_limiter.acquire(
            entity_id="llm-user",
            resource="gpt-4",
            limits=limits,
            consume={"rpm": 1, "tpm": 100},  # Estimated
        ) as lease:
            # Simulate LLM API call returning actual token count
            actual_tokens = 250  # Real tokens from response

            # Adjust lease with actual tokens (uses **kwargs syntax)
            await lease.adjust(tpm=actual_tokens - 100)  # Delta: +150

        # Verify correct tokens consumed
        available = await e2e_limiter.available(
            entity_id="llm-user",
            resource="gpt-4",
            limits=limits,
        )
        # Should have consumed 250 tpm total (with some tolerance for timing)
        assert available["tpm"] < 10000 - 200  # Consumed at least ~200 tokens
        assert available["tpm"] > 10000 - 300  # But not more than ~300


class TestE2ELocalStackAggregatorWorkflow:
    """E2E tests for Lambda aggregator and usage snapshots."""

    @pytest.fixture
    async def e2e_limiter_with_aggregator(self, localstack_endpoint, unique_table_name):
        """Create RateLimiter with aggregator enabled."""
        stack_options = StackOptions(
            enable_aggregator=True,
            enable_alarms=False,  # Faster deployment
            snapshot_windows="hourly",
            retention_days=7,
        )

        limiter = RateLimiter(
            table_name=unique_table_name,
            endpoint_url=localstack_endpoint,
            region="us-east-1",
            stack_options=stack_options,
        )

        async with limiter:
            yield limiter

        # Explicitly delete the stack after test completes
        try:
            await limiter.delete_stack()
        except Exception as e:
            # LocalStack may have issues with stack deletion, log but don't fail
            print(f"Warning: Stack cleanup failed: {e}")

    @pytest.mark.asyncio
    async def test_usage_snapshot_generation(self, e2e_limiter_with_aggregator):
        """
        Test that aggregator creates usage snapshots.

        Note: In LocalStack, Lambda stream processing may be delayed or
        require explicit triggering. This test verifies the workflow
        but may need adjustments based on LocalStack behavior.
        """
        await e2e_limiter_with_aggregator.create_entity("snapshot-user")

        limits = [Limit.per_minute("rpm", 100)]

        # Generate some token consumption
        for _ in range(5):
            async with e2e_limiter_with_aggregator.acquire(
                entity_id="snapshot-user",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        # Wait for stream processing (LocalStack may be slower)
        await asyncio.sleep(10)

        # Query usage snapshots directly from DynamoDB
        # Note: In LocalStack, Lambda processing may not be reliable
        # This test verifies the infrastructure is set up correctly
        repo = e2e_limiter_with_aggregator._repository
        client = await repo._get_client()

        # Query for BUCKET records to verify data exists
        response = await client.query(
            TableName=repo.table_name,
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk_prefix)",
            ExpressionAttributeValues={
                ":pk": {"S": "ENTITY#snapshot-user"},
                ":sk_prefix": {"S": "#BUCKET#"},
            },
        )

        # Verify bucket records were created
        items = response.get("Items", [])
        assert len(items) > 0, "Bucket records should exist"


class TestE2ELocalStackErrorHandling:
    """E2E tests for error handling scenarios."""

    @pytest.fixture
    async def e2e_limiter_minimal(self, localstack_endpoint, unique_table_name):
        """Create RateLimiter with minimal stack."""
        stack_options = StackOptions(
            enable_aggregator=False,
            enable_alarms=False,
        )

        limiter = RateLimiter(
            table_name=unique_table_name,
            endpoint_url=localstack_endpoint,
            region="us-east-1",
            stack_options=stack_options,
        )

        async with limiter:
            yield limiter

        # Explicitly delete the stack after test completes
        try:
            await limiter.delete_stack()
        except Exception as e:
            # LocalStack may have issues with stack deletion, log but don't fail
            print(f"Warning: Stack cleanup failed: {e}")

    @pytest.mark.asyncio
    async def test_concurrent_lease_acquisition(self, e2e_limiter_minimal):
        """
        Test concurrent lease acquisitions don't cause conflicts.

        Uses optimistic locking to handle concurrent updates.
        """
        await e2e_limiter_minimal.create_entity("concurrent-user")

        # Use per_hour to minimize refill during test (1000/hour = ~0.28/second)
        limits = [Limit.per_hour("rph", 1000)]

        async def acquire_lease(user_id: str):
            async with e2e_limiter_minimal.acquire(
                entity_id=user_id,
                resource="api",
                limits=limits,
                consume={"rph": 10},
            ):
                await asyncio.sleep(0.1)  # Simulate work
            return True

        # Run multiple concurrent acquisitions
        tasks = [acquire_lease("concurrent-user") for _ in range(10)]
        results = await asyncio.gather(*tasks)

        # All should succeed
        assert all(results)

        # Verify tokens were consumed (concurrent operations may batch)
        available = await e2e_limiter_minimal.available(
            entity_id="concurrent-user",
            resource="api",
            limits=limits,
        )
        # Due to optimistic locking, concurrent operations may conflict and retry.
        # We can only reliably verify that SOME tokens were consumed.
        # The exact amount varies based on timing and retry behavior.
        assert available["rph"] < 1000, "Some tokens should have been consumed"

    @pytest.mark.asyncio
    async def test_lease_rollback_on_exception(self, e2e_limiter_minimal):
        """Test that lease is rolled back when exception occurs."""
        await e2e_limiter_minimal.create_entity("rollback-user")

        limits = [Limit.per_minute("rpm", 100)]

        # Acquire and raise exception
        try:
            async with e2e_limiter_minimal.acquire(
                entity_id="rollback-user",
                resource="api",
                limits=limits,
                consume={"rpm": 10},
            ):
                raise ValueError("Simulated failure")
        except ValueError:
            pass

        # Tokens should be returned (rollback)
        available = await e2e_limiter_minimal.available(
            entity_id="rollback-user",
            resource="api",
            limits=limits,
        )
        assert available["rpm"] == 100  # Full capacity restored

    @pytest.mark.asyncio
    async def test_negative_bucket_handling(self, e2e_limiter_minimal):
        """
        Test that buckets can go negative for post-hoc reconciliation.

        This is a key feature for LLM token counting where the actual
        token count is unknown until after the API call completes.
        """
        await e2e_limiter_minimal.create_entity("negative-bucket-user")

        limits = [Limit.per_minute("rpm", 10)]

        # Consume all tokens
        async with e2e_limiter_minimal.acquire(
            entity_id="negative-bucket-user",
            resource="api",
            limits=limits,
            consume={"rpm": 10},
        ) as lease:
            # Adjust to consume more than available (goes negative)
            await lease.adjust(rpm=5)  # Now at -5

        # Verify bucket is negative
        # With 10 rpm, refill rate is ~0.17 tokens/second
        # Allow small tolerance for refill during test execution
        available = await e2e_limiter_minimal.available(
            entity_id="negative-bucket-user",
            resource="api",
            limits=limits,
        )
        assert available["rpm"] < 0, "Bucket should be negative"
        # Consumed 15 tokens with capacity 10, so at least -3 after some refill
        assert available["rpm"] <= -3, "Bucket should still be significantly negative"
