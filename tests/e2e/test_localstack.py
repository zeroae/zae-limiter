"""End-to-end integration tests using LocalStack.

These tests run the complete zae-limiter lifecycle against LocalStack:
1. Deploy full CloudFormation stack via CLI or RateLimiter
2. Create entities with hierarchical relationships
3. Set limits and acquire/release leases
4. Consume tokens and verify aggregator processing
5. Check usage snapshots in DynamoDB
6. Clean up by deleting the stack

To run these tests locally:
    # Start LocalStack (from project root)
    docker compose up -d

    # Set environment variables and run tests
    export AWS_ENDPOINT_URL=http://localhost:4566
    export AWS_ACCESS_KEY_ID=test
    export AWS_SECRET_ACCESS_KEY=test
    export AWS_DEFAULT_REGION=us-east-1
    pytest tests/e2e/test_localstack.py -v

Note: The docker-compose.yml includes the Docker socket mount required for
LocalStack to spawn Lambda functions as Docker containers.
"""

import asyncio
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from click.testing import CliRunner

from zae_limiter import (
    Limit,
    OnUnavailable,
    RateLimiter,
    RateLimiterUnavailable,
    RateLimitExceeded,
    Repository,
    ScheduleEntry,
    SyncRateLimiter,
    __version__,
    schema,
)
from zae_limiter.cli import cli
from zae_limiter.sync_repository import SyncRepository
from zae_limiter.version import CURRENT_SCHEMA_VERSION

pytestmark = [pytest.mark.integration, pytest.mark.e2e]


class TestE2ELocalStackCLIWorkflow:
    """E2E tests using CLI for stack deployment."""

    @pytest.fixture
    def cli_runner(self):
        """Create Click CLI runner."""
        return CliRunner()

    def test_full_cli_workflow(self, cli_runner, localstack_endpoint, unique_name):
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
        stack_name = unique_name

        try:
            # Step 1: Deploy stack via CLI
            result = cli_runner.invoke(
                cli,
                [
                    "deploy",
                    "--name",
                    unique_name,
                    "--endpoint-url",
                    localstack_endpoint,
                    "--region",
                    "us-east-1",
                    "--snapshot-windows",
                    "hourly",
                    "--usage-retention-days",
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
                    "--name",
                    stack_name,
                    "--endpoint-url",
                    localstack_endpoint,
                    "--region",
                    "us-east-1",
                ],
            )
            assert result.exit_code == 0, f"Status failed: {result.output}"

            # Verify CLI output format with all sections
            assert f"Status: {stack_name}" in result.output

            # Connectivity section
            assert "Connectivity" in result.output
            assert "Available:" in result.output
            assert "✓ Yes" in result.output
            assert "Latency:" in result.output
            assert "Region:" in result.output

            # Infrastructure section
            assert "Infrastructure" in result.output
            assert "Stack:" in result.output
            assert "Table:" in result.output
            assert "ACTIVE" in result.output
            assert "Aggregator:" in result.output

            # Versions section
            # After deploy, version record should exist (not N/A)
            assert "Versions" in result.output
            assert "Client:" in result.output
            assert "Schema:" in result.output
            # Schema should be initialized by deploy (not N/A)
            assert f"Schema:        {CURRENT_SCHEMA_VERSION}" in result.output
            assert "Lambda:" in result.output
            # Lambda version should match client version (fix #274)
            assert f"Lambda:        {__version__}" in result.output

            # Table Metrics section
            assert "Table Metrics" in result.output
            assert "Items:" in result.output
            assert "Size:" in result.output

            # Final status indicator
            assert (
                "✓ Infrastructure is ready" in result.output or "CREATE_COMPLETE" in result.output
            )

            # Step 3: Use SyncRateLimiter with deployed infrastructure
            # Use builder (not connect) because CLI deploy doesn't register namespaces
            repo = (
                SyncRepository.builder()
                .stack(unique_name)
                .region("us-east-1")
                .endpoint_url(localstack_endpoint)
                .build()
            )
            limiter = SyncRateLimiter(repository=repo)

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

            # Step 3b: Test repository connectivity
            assert limiter._repository.ping() is True

        finally:
            # Step 4: Delete stack via CLI
            result = cli_runner.invoke(
                cli,
                [
                    "delete",
                    "--name",
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

    def test_audit_list_cli_workflow(self, cli_runner, localstack_endpoint, unique_name):
        """
        E2E workflow for audit list CLI command.

        Steps:
        1. Deploy stack via CLI
        2. Create entity using SyncRateLimiter (generates audit event)
        3. Run audit list CLI command
        4. Verify table format output contains audit event data
        5. Delete stack via CLI
        """
        stack_name = unique_name

        try:
            # Step 1: Deploy stack via CLI
            result = cli_runner.invoke(
                cli,
                [
                    "deploy",
                    "--name",
                    unique_name,
                    "--endpoint-url",
                    localstack_endpoint,
                    "--region",
                    "us-east-1",
                    "--no-aggregator",
                    "--no-alarms",
                    "--wait",
                ],
            )
            assert result.exit_code == 0, f"Deploy failed: {result.output}"

            # Step 2: Create entity using SyncRateLimiter (generates audit event)
            repo = SyncRepository.open(
                stack=unique_name,
                region="us-east-1",
                endpoint_url=localstack_endpoint,
            )
            limiter = SyncRateLimiter(repository=repo)

            entity = limiter.create_entity(
                "audit-test-user",
                name="Audit Test User",
                principal="test-admin@example.com",
            )
            assert entity.id == "audit-test-user"

            # Step 3: Run audit list CLI command
            result = cli_runner.invoke(
                cli,
                [
                    "audit",
                    "list",
                    "--name",
                    unique_name,
                    "--endpoint-url",
                    localstack_endpoint,
                    "--region",
                    "us-east-1",
                    "--entity-id",
                    "audit-test-user",
                ],
            )
            assert result.exit_code == 0, f"Audit list failed: {result.output}"

            # Step 4: Verify table format output contains expected data
            # Table should have header row and at least one data row
            assert "Timestamp" in result.output, "Table header should include Timestamp"
            assert "Action" in result.output, "Table header should include Action"
            assert "Principal" in result.output, "Table header should include Principal"
            assert "Resource" in result.output, "Table header should include Resource"

            # Verify audit event data is present
            assert "entity_created" in result.output, "Should show entity_created action"
            assert "test-admin@example.com" in result.output, "Should show principal"

        finally:
            # Step 5: Delete stack via CLI
            result = cli_runner.invoke(
                cli,
                [
                    "delete",
                    "--name",
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

    def test_list_cli_workflow(self, cli_runner, localstack_endpoint, unique_name):
        """
        E2E workflow for list CLI command.

        Steps:
        1. Deploy stack via CLI
        2. Run list CLI command
        3. Verify table format output contains deployed stack
        4. Delete stack via CLI
        """
        try:
            # Step 1: Deploy stack via CLI
            result = cli_runner.invoke(
                cli,
                [
                    "deploy",
                    "--name",
                    unique_name,
                    "--endpoint-url",
                    localstack_endpoint,
                    "--region",
                    "us-east-1",
                    "--no-aggregator",
                    "--no-alarms",
                    "--wait",
                ],
            )
            assert result.exit_code == 0, f"Deploy failed: {result.output}"

            # Step 2: Run list CLI command
            result = cli_runner.invoke(
                cli,
                [
                    "list",
                    "--endpoint-url",
                    localstack_endpoint,
                    "--region",
                    "us-east-1",
                ],
            )
            assert result.exit_code == 0, f"List failed: {result.output}"

            # Step 3: Verify rich table format output
            assert "Rate Limiter Instances" in result.output
            # Box-drawing table headers
            assert "| Name" in result.output
            assert "| Status" in result.output
            assert "| Version" in result.output
            assert "| Created" in result.output
            assert "+-" in result.output  # Table border

            # Deployed stack should appear in list (full name shown for copy/paste)
            assert unique_name in result.output, f"Stack {unique_name} not in list"
            assert "CREATE_COMPLETE" in result.output, "Stack should show full status"
            assert "Total:" in result.output, "Should show total count"

        finally:
            # Step 4: Delete stack via CLI
            result = cli_runner.invoke(
                cli,
                [
                    "delete",
                    "--name",
                    unique_name,
                    "--region",
                    "us-east-1",
                    "--endpoint-url",
                    localstack_endpoint,
                    "--yes",
                    "--wait",
                ],
            )
            # Don't assert exit code - stack might not exist if deploy failed

    def test_usage_list_plot_cli_workflow(self, cli_runner, localstack_endpoint, unique_name):
        """
        E2E workflow for usage list --plot CLI command.

        Steps:
        1. Deploy stack via CLI
        2. Insert sample usage snapshots directly into DynamoDB
        3. Run usage list --plot CLI command
        4. Verify ASCII chart output
        5. Delete stack via CLI
        """
        import boto3

        stack_name = unique_name
        table_name = stack_name

        try:
            # Step 1: Deploy stack via CLI
            result = cli_runner.invoke(
                cli,
                [
                    "deploy",
                    "--name",
                    unique_name,
                    "--endpoint-url",
                    localstack_endpoint,
                    "--region",
                    "us-east-1",
                    "--no-aggregator",
                    "--no-alarms",
                    "--wait",
                ],
            )
            assert result.exit_code == 0, f"Deploy failed: {result.output}"

            # Step 2: Insert sample usage snapshots directly into DynamoDB
            dynamodb = boto3.client(
                "dynamodb",
                endpoint_url=localstack_endpoint,
                region_name="us-east-1",
            )

            # Look up the opaque namespace ID for "default"
            ns_record = dynamodb.get_item(
                TableName=table_name,
                Key={
                    "PK": {"S": "_/SYSTEM#"},
                    "SK": {"S": "#NAMESPACE#default"},
                },
            )
            ns_id = ns_record["Item"]["namespace_id"]["S"]

            # Create snapshots with varying values for interesting chart
            from datetime import datetime, timedelta

            base_time = datetime(2024, 1, 15, 0, 0, 0)
            for i in range(5):
                window_start = base_time + timedelta(hours=i)
                window_key = window_start.strftime("%Y-%m-%dT%H:00:00Z")

                # Vary the values
                tpm_value = 1000 + (i * 500)
                rpm_value = 10 + (i * 2)

                item = {
                    "PK": {"S": f"{ns_id}/ENTITY#plot-test-user"},
                    "SK": {"S": f"#USAGE#gpt-4#{window_key}"},
                    "entity_id": {"S": "plot-test-user"},
                    "resource": {"S": "gpt-4"},
                    "window": {"S": "hourly"},
                    "window_start": {"S": window_key},
                    "tpm": {"N": str(tpm_value)},
                    "rpm": {"N": str(rpm_value)},
                    "total_events": {"N": str(5 + i)},
                    "GSI2PK": {"S": f"{ns_id}/RESOURCE#gpt-4"},
                    "GSI2SK": {"S": f"USAGE#{window_key}#plot-test-user"},
                }
                dynamodb.put_item(TableName=table_name, Item=item)

            # Step 3: Run usage list --plot CLI command
            result = cli_runner.invoke(
                cli,
                [
                    "usage",
                    "list",
                    "--name",
                    unique_name,
                    "--endpoint-url",
                    localstack_endpoint,
                    "--region",
                    "us-east-1",
                    "--entity-id",
                    "plot-test-user",
                    "--plot",
                ],
            )
            assert result.exit_code == 0, f"Usage list --plot failed: {result.output}"

            # Step 4: Verify ASCII chart output
            # Should have header with entity/resource context
            assert "Usage Plot: gpt-4 (hourly)" in result.output, "Should have resource header"
            assert "Entity: plot-test-user" in result.output, "Should have entity header"
            # Counter labels
            assert "TPM" in result.output, "Should have TPM counter"
            assert "RPM" in result.output, "Should have RPM counter"

            # Should have time range info
            assert "Time range:" in result.output, "Should show time range"
            assert "Data points: 5" in result.output, "Should show 5 data points"

            # Should have total count
            assert "Total: 5 snapshots" in result.output, "Should show total count"

            # Step 5: Also test normal table output (without --plot)
            result = cli_runner.invoke(
                cli,
                [
                    "usage",
                    "list",
                    "--name",
                    unique_name,
                    "--endpoint-url",
                    localstack_endpoint,
                    "--region",
                    "us-east-1",
                    "--entity-id",
                    "plot-test-user",
                ],
            )
            assert result.exit_code == 0, f"Usage list failed: {result.output}"
            assert "Usage Snapshots" in result.output, "Should have table header"
            assert "Window Start" in result.output, "Should have column headers"
            assert "gpt-4" in result.output, "Should show resource"

        finally:
            # Step 6: Delete stack via CLI
            result = cli_runner.invoke(
                cli,
                [
                    "delete",
                    "--name",
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

    @pytest_asyncio.fixture(scope="class", loop_scope="class")
    async def e2e_limiter(self, shared_full_stack, unique_name_class):
        """Namespace-scoped RateLimiter on the shared full stack."""
        ns = f"full-{unique_name_class}"
        repo = await Repository.open(
            stack=shared_full_stack.name,
            region=shared_full_stack.region,
            endpoint_url=shared_full_stack.endpoint_url,
        )
        await repo.register_namespace(ns)
        scoped = await repo.namespace(ns)
        limiter = RateLimiter(repository=scoped)
        yield limiter
        await repo.close()

    @pytest.mark.asyncio(loop_scope="class")
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
            cascade=True,
        )
        assert child.parent_id == "org-acme"

        # Verify parent-child relationship
        children = await e2e_limiter.get_children("org-acme")
        assert len(children) == 1
        assert children[0].id == "api-key-123"

        # Use per_hour limits to prevent refill during test execution
        # per_minute refills ~1.67 tokens/second, per_hour refills ~0.028 tokens/second
        limits = [
            Limit.per_hour("rph", 100),
            Limit.per_hour("tph", 10000),
        ]

        # Consume from child with cascade
        async with e2e_limiter.acquire(
            entity_id="api-key-123",
            resource="gpt-4",
            limits=limits,
            consume={"rph": 1, "tph": 500},
        ) as lease:
            # With cascade, consumes from both child and parent
            assert lease.consumed["rph"] == 2  # 1 from child + 1 from parent
            assert lease.consumed["tph"] == 1000  # 500 from each

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

        # After consuming 1 rph from each, both should have 99 available
        assert child_available["rph"] == 99, f"child rph={child_available['rph']}"
        assert parent_available["rph"] == 99, f"parent rph={parent_available['rph']}"

    @pytest.mark.asyncio(loop_scope="class")
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

    @pytest.mark.asyncio(loop_scope="class")
    async def test_stored_limits_workflow(self, e2e_limiter):
        """
        Test stored limits with three-tier hierarchy (premium vs default tiers).

        Workflow:
        1. Create premium and free tier users
        2. Set entity-level limits for premium user (overrides resource/system)
        3. Set resource-level limits as fallback for free users
        4. Verify premium user has higher limits via entity config
        5. Verify free user falls back to resource config
        """
        # Invalidate cache to ensure fresh config resolution
        await e2e_limiter._repository.invalidate_config_cache()

        await e2e_limiter.create_entity("premium-user")
        await e2e_limiter.create_entity("free-user")

        # Set premium limits at entity level for "api" resource
        premium_limits = [
            Limit.per_minute("rpm", 1000),
            Limit.per_minute("tpm", 100000),
        ]
        await e2e_limiter.set_limits("premium-user", premium_limits, resource="api")

        # Set resource-level defaults as fallback (for free users)
        default_limits = [
            Limit.per_minute("rpm", 10),
            Limit.per_minute("tpm", 1000),
        ]
        await e2e_limiter.set_resource_defaults("api", default_limits)

        # Premium user uses entity-level limits (auto-resolved, no use_stored_limits needed)
        async with e2e_limiter.acquire(
            entity_id="premium-user",
            resource="api",
            limits=None,  # Let hierarchy resolve limits
            consume={"rpm": 1},
        ) as lease:
            # Consumed includes all limit types, even if 0
            assert lease.consumed["rpm"] == 1

        # Verify premium capacity uses entity-level limits
        premium_available = await e2e_limiter.available(
            entity_id="premium-user",
            resource="api",
        )
        assert premium_available["rpm"] > 900  # High limit (1000 - 1 = 999)

        # Free user falls back to resource-level defaults
        free_available = await e2e_limiter.available(
            entity_id="free-user",
            resource="api",
        )
        assert free_available["rpm"] == 10  # Default limit

    @pytest.mark.asyncio(loop_scope="class")
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
        # Should have consumed 250 tpm total (with tolerance for timing/refill)
        assert available["tpm"] < 10000 - 150  # Consumed at least ~150 tokens
        assert available["tpm"] > 10000 - 350  # But not more than ~350

    @pytest.mark.asyncio(loop_scope="class")
    async def test_repository_ping(self, e2e_limiter):
        """Test repository connectivity via ping."""
        assert await e2e_limiter._repository.ping() is True


class TestE2ELocalStackAggregatorWorkflow:
    """E2E tests for Lambda aggregator and usage snapshots."""

    @pytest_asyncio.fixture(scope="class", loop_scope="class")
    async def e2e_limiter_with_aggregator(self, shared_aggregator_stack, unique_name_class):
        """Namespace-scoped RateLimiter on the shared aggregator stack."""
        ns = f"aggr-{unique_name_class}"
        repo = await Repository.open(
            stack=shared_aggregator_stack.name,
            region=shared_aggregator_stack.region,
            endpoint_url=shared_aggregator_stack.endpoint_url,
        )
        await repo.register_namespace(ns)
        scoped = await repo.namespace(ns)
        limiter = RateLimiter(repository=scoped)
        yield limiter
        await repo.close()

    @pytest.mark.asyncio(loop_scope="class")
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

        # Verify bucket records were created (uses GSI3 for pre-shard bucket discovery)
        buckets = await repo.get_buckets("snapshot-user")
        assert len(buckets) > 0, "Bucket records should exist"


class TestE2ELocalStackErrorHandling:
    """E2E tests for error handling scenarios."""

    @pytest_asyncio.fixture(scope="class", loop_scope="class")
    async def e2e_limiter_minimal(self, shared_minimal_stack, unique_name_class):
        """Namespace-scoped RateLimiter on the shared minimal stack."""
        ns = f"err-{unique_name_class}"
        repo = await Repository.open(
            stack=shared_minimal_stack.name,
            region=shared_minimal_stack.region,
            endpoint_url=shared_minimal_stack.endpoint_url,
        )
        await repo.register_namespace(ns)
        scoped = await repo.namespace(ns)
        limiter = RateLimiter(repository=scoped)
        yield limiter
        await repo.close()

    @pytest.mark.asyncio(loop_scope="class")
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

    @pytest.mark.asyncio(loop_scope="class")
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

    @pytest.mark.asyncio(loop_scope="class")
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

    @pytest.mark.asyncio(loop_scope="class")
    async def test_acquire_recovers_after_refill_wait(self, e2e_limiter_minimal):
        """An exhausted bucket recovers after enough time passes (regression #428).

        Runs on the no-aggregator stack so the client refill-recovery slow path is
        what's exercised. On the aggregator stack the Lambda refills the bucket
        out-of-band and the client path never runs -- which is exactly why this bug
        (acquire raising RateLimitExceeded with retry_after=0.0 instead of
        refilling) reached production in v0.10.1 undetected.
        """
        # 100 tokens, refills the full bucket every second.
        limits = [Limit.custom("rpm", capacity=100, refill_amount=100, refill_period_seconds=1)]

        # Drain the bucket completely.
        async with e2e_limiter_minimal.acquire(
            entity_id="recover-user", resource="api", limits=limits, consume={"rpm": 100}
        ):
            pass

        # Immediately exhausted: the rejection must report a real wait, not 0.0.
        with pytest.raises(RateLimitExceeded) as exc_info:
            async with e2e_limiter_minimal.acquire(
                entity_id="recover-user", resource="api", limits=limits, consume={"rpm": 100}
            ):
                pass
        assert exc_info.value.retry_after_seconds > 0

        # After refilling, the same acquire must succeed (slow-path refill recovery).
        await asyncio.sleep(1.1)
        async with e2e_limiter_minimal.acquire(
            entity_id="recover-user", resource="api", limits=limits, consume={"rpm": 50}
        ) as lease:
            assert lease.consumed == {"rpm": 50}


class TestE2ECloudFormationStackVariations:
    """E2E tests for CloudFormation stack deployment variations."""

    @pytest.mark.asyncio
    async def test_cloudformation_full_stack_deployment(
        self, localstack_endpoint, full_stack_options, unique_name
    ):
        """Test full CloudFormation stack creation (with aggregator and alarms)."""
        repo = await (
            Repository.builder()
            .stack(unique_name)
            .region("us-east-1")
            .endpoint_url(localstack_endpoint)
            .stack_options(full_stack_options)
            .build()
        )
        limiter = RateLimiter(repository=repo)

        entity = await limiter.create_entity("cfn-full-entity", name="CFN Full Entity")
        assert entity.id == "cfn-full-entity"
        assert entity.name == "CFN Full Entity"

        await repo.delete_stack()

    @pytest.mark.asyncio
    async def test_cloudformation_aggregator_no_alarms(
        self, localstack_endpoint, aggregator_stack_options, unique_name
    ):
        """Test CloudFormation stack with aggregator but without alarms.

        This tests the edge case where EnableAggregator=true but EnableAlarms=false.
        The AggregatorDLQAlarmName output should not be created in this scenario.
        """
        repo = await (
            Repository.builder()
            .stack(unique_name)
            .region("us-east-1")
            .endpoint_url(localstack_endpoint)
            .stack_options(aggregator_stack_options)
            .build()
        )
        limiter = RateLimiter(repository=repo)

        entity = await limiter.create_entity("cfn-no-alarms-entity", name="CFN No Alarms Entity")
        assert entity.id == "cfn-no-alarms-entity"
        assert entity.name == "CFN No Alarms Entity"

        await repo.delete_stack()


class TestE2ERoleNaming:
    """E2E tests for IAM role naming (Issue #252, ADR-116)."""

    @pytest.fixture
    def cli_runner(self):
        """Create Click CLI runner."""
        return CliRunner()

    def test_role_naming_with_prefix_format(self, cli_runner, localstack_endpoint, unique_name):
        """Test deploying with role_name_format creates correctly named roles."""
        try:
            # Deploy with role_name_format
            result = cli_runner.invoke(
                cli,
                [
                    "deploy",
                    "--name",
                    unique_name,
                    "--endpoint-url",
                    localstack_endpoint,
                    "--region",
                    "us-east-1",
                    "--role-name-format",
                    "test-{}",
                    "--no-alarms",
                    "--wait",
                ],
            )
            assert result.exit_code == 0, f"Deploy failed: {result.output}"
            assert "Stack create complete" in result.output

            # Verify status command succeeds (stack is functional)
            status_result = cli_runner.invoke(
                cli,
                [
                    "status",
                    "--name",
                    unique_name,
                    "--endpoint-url",
                    localstack_endpoint,
                    "--region",
                    "us-east-1",
                ],
            )
            assert status_result.exit_code == 0, f"Status failed: {status_result.output}"
            assert "✓ Yes" in status_result.output  # Available check

        finally:
            # Cleanup
            cli_runner.invoke(
                cli,
                [
                    "delete",
                    "--name",
                    unique_name,
                    "--endpoint-url",
                    localstack_endpoint,
                    "--region",
                    "us-east-1",
                    "--yes",
                ],
            )

    def test_role_naming_with_suffix_format(self, cli_runner, localstack_endpoint, unique_name):
        """Test deploying with suffix role_name_format."""
        try:
            # Deploy with suffix format
            result = cli_runner.invoke(
                cli,
                [
                    "deploy",
                    "--name",
                    unique_name,
                    "--endpoint-url",
                    localstack_endpoint,
                    "--region",
                    "us-east-1",
                    "--role-name-format",
                    "{}-suffix",
                    "--no-alarms",
                    "--no-aggregator",  # Faster for this test
                    "--wait",
                ],
            )
            assert result.exit_code == 0, f"Deploy failed: {result.output}"

        finally:
            # Cleanup
            cli_runner.invoke(
                cli,
                [
                    "delete",
                    "--name",
                    unique_name,
                    "--endpoint-url",
                    localstack_endpoint,
                    "--region",
                    "us-east-1",
                    "--yes",
                ],
            )


class TestE2EDeletionProtection:
    """E2E tests for DynamoDB table deletion protection (Issue #273)."""

    @pytest.fixture
    def cli_runner(self):
        """Create Click CLI runner."""
        return CliRunner()

    def test_deploy_with_deletion_protection_enabled(
        self, cli_runner, localstack_endpoint, unique_name, dynamodb_client
    ):
        """Test deploying with --enable-deletion-protection sets table property.

        Note: LocalStack may not fully support DeletionProtectionEnabled.
        This test verifies the CLI correctly passes the parameter; the actual
        DynamoDB property assertion is conditional on LocalStack support.
        """
        try:
            # Deploy with deletion protection enabled
            result = cli_runner.invoke(
                cli,
                [
                    "deploy",
                    "--name",
                    unique_name,
                    "--endpoint-url",
                    localstack_endpoint,
                    "--region",
                    "us-east-1",
                    "--enable-deletion-protection",
                    "--no-alarms",
                    "--no-aggregator",
                    "--wait",
                ],
            )
            assert result.exit_code == 0, f"Deploy failed: {result.output}"
            assert "Stack create complete" in result.output
            assert "Deletion protection: enabled" in result.output

            # Verify table was created
            table_desc = dynamodb_client.describe_table(TableName=unique_name)
            assert table_desc["Table"]["TableStatus"] == "ACTIVE"

            # Check if LocalStack supports DeletionProtectionEnabled
            # LocalStack may not respect this CloudFormation property
            deletion_protected = table_desc["Table"].get("DeletionProtectionEnabled", False)
            if not deletion_protected:
                pytest.skip(
                    "LocalStack does not support DeletionProtectionEnabled; "
                    "CLI parameter was correctly passed (verified via output)"
                )

        finally:
            # Must disable deletion protection before deleting (if it was enabled)
            try:
                dynamodb_client.update_table(
                    TableName=unique_name,
                    DeletionProtectionEnabled=False,
                )
            except Exception:
                pass  # Table may not exist if deploy failed

            # Cleanup
            cli_runner.invoke(
                cli,
                [
                    "delete",
                    "--name",
                    unique_name,
                    "--endpoint-url",
                    localstack_endpoint,
                    "--region",
                    "us-east-1",
                    "--yes",
                ],
            )

    def test_deploy_without_deletion_protection(
        self, cli_runner, localstack_endpoint, unique_name, dynamodb_client
    ):
        """Test deploying without --enable-deletion-protection (default disabled)."""
        try:
            # Deploy without deletion protection (default)
            result = cli_runner.invoke(
                cli,
                [
                    "deploy",
                    "--name",
                    unique_name,
                    "--endpoint-url",
                    localstack_endpoint,
                    "--region",
                    "us-east-1",
                    "--no-alarms",
                    "--no-aggregator",
                    "--wait",
                ],
            )
            assert result.exit_code == 0, f"Deploy failed: {result.output}"
            assert "Deletion protection: disabled" in result.output

            # Verify table has deletion protection disabled
            table_desc = dynamodb_client.describe_table(TableName=unique_name)
            assert table_desc["Table"]["DeletionProtectionEnabled"] is False

        finally:
            # Cleanup
            cli_runner.invoke(
                cli,
                [
                    "delete",
                    "--name",
                    unique_name,
                    "--endpoint-url",
                    localstack_endpoint,
                    "--region",
                    "us-east-1",
                    "--yes",
                ],
            )


class TestE2EProvisionerReachesLiveBuckets:
    """A manifest apply must change what an already-created bucket enforces (#481).

    The bug is the seam between the provisioner and DynamoDB: ``_apply_set``
    was a bare ``put_item`` on the config item and nothing pushed the new
    params out to bucket items that already existed. Entity-level limits carry
    no TTL (deliberately, #271/#296), so such a bucket kept enforcing the
    numbers it was born with forever.

    A unit test with a mocked client cannot catch a wrong attribute name or a
    missing 1000x conversion (config items store whole tokens and seconds,
    bucket items millitokens and milliseconds); only a real acquire against a
    real table can.

    ``_handle_cli`` is invoked in-process, exactly as
    ``tests/integration/test_provisioner.py`` does -- the provisioner is sync
    boto3 and needs no deployed Lambda to exercise this path.
    """

    @pytest_asyncio.fixture(scope="class", loop_scope="class")
    async def provisioner_repo(self, shared_minimal_stack, unique_name_class):
        """Namespace-scoped Repository on the shared minimal stack.

        Minimal (no aggregator) on purpose: the aggregator's proactive refill
        writes to the same bucket items and would make the token assertions
        below racy.
        """
        ns = f"prov-{unique_name_class}"
        repo = await Repository.open(
            stack=shared_minimal_stack.name,
            region=shared_minimal_stack.region,
            endpoint_url=shared_minimal_stack.endpoint_url,
        )
        await repo.register_namespace(ns)
        scoped = await repo.namespace(ns)
        yield scoped
        await repo.close()

    @pytest.mark.asyncio(loop_scope="class")
    async def test_apply_changes_an_existing_bucket(self, provisioner_repo):
        from zae_limiter_provisioner.handler import _handle_cli

        repo = provisioner_repo
        limiter = RateLimiter(repository=repo)

        # 1. Create a bucket by acquiring against a generous entity limit.
        await repo.set_limits("user-1", [Limit.per_minute("rpm", 1000)], resource="gpt-4")
        async with limiter.acquire("user-1", "gpt-4", consume={"rpm": 1}):
            pass

        before = await repo.get_buckets("user-1", resource="gpt-4")
        rpm_before = next(b for b in before if b.limit_name == "rpm")
        assert rpm_before.capacity == 1000

        # 2. Apply a manifest that lowers it.
        result = _handle_cli(
            {
                "action": "apply",
                "table_name": repo.table_name,
                "namespace_id": repo._namespace_id,
                "manifest": {
                    "namespace": "default",
                    "entities": {
                        "user-1": {"resources": {"gpt-4": {"limits": {"rpm": {"capacity": 10}}}}}
                    },
                },
            },
            None,
        )
        assert result["status"] == "applied"
        assert result["errors"] == []

        # 3. The EXISTING bucket item must now carry the new capacity.
        #
        # The config cache is per-Repository and the provisioner wrote the
        # config item out of band, so evict before anything resolves limits.
        await repo.invalidate_config_cache()
        after = await repo.get_buckets("user-1", resource="gpt-4")
        rpm_after = next(b for b in after if b.limit_name == "rpm")
        assert rpm_after.capacity == 10, "manifest apply did not reach the live bucket"

        # 4. And it must actually be enforced.
        #
        # Enforcement is only observable once a refill clamps the balance down
        # to the new ceiling: `refill_bucket` does `min(capacity_milli, ...)`,
        # and the bucket still holds the ~999 tokens it was created with. The
        # default speculative fast path never refills -- it is a bare
        # `ADD tk -consumed` guarded by `tk >= consumed` -- so it would admit
        # 50 out of that stale balance no matter what `cp` says. The slow path
        # refills from the stored params, which is where the synced `cp` bites.
        # This is a property of the token bucket, not of the fix: lowering a
        # limit lowers the ceiling, it does not confiscate tokens already in
        # the bucket.
        await asyncio.sleep(0.1)  # ensure a non-zero refill tick, so the clamp runs
        slow_limiter = RateLimiter(repository=repo, speculative_writes=False)
        with pytest.raises(RateLimitExceeded):
            async with slow_limiter.acquire("user-1", "gpt-4", consume={"rpm": 50}):
                pass


# ---------------------------------------------------------------------------
# Scheduled limits (#222, design §8)
# ---------------------------------------------------------------------------
#
# Two groups, and the split is deliberate. Everything the *client* enforces runs
# with an injected clock: `Repository._now_ms()` (#430) drives the `rf` stamp,
# the `vu` comparison inside the speculative condition, the `ttl` stamp and its
# guard, and every refill computation, so a jumped clock is a real crossing
# rather than a simulated one. Only the cases that need the Lambda to observe a
# boundary itself wait on a real `*/2` schedule, because the aggregator reads
# `time.time()` inside a container these tests cannot reach — those live in
# `TestE2EScheduleWithTheAggregator` and are the only ones marked `slow`.
#
# Dates stay within a day of real time: DynamoDB's TTL reaper runs on real time
# regardless of the injected clock, so a `ttl` computed from an instant years in
# the past could be swept mid-test.

NY = ZoneInfo("America/New_York")


def _ny(s: str) -> int:
    """Epoch ms for a wall-clock instant in New York."""
    return int(datetime.fromisoformat(s).replace(tzinfo=NY).timestamp() * 1000)


# 2026-09-15 is a Tuesday. BUSINESS halves the limit from 09:00 through 17:59
# local on weekdays, so the window opens at 09:00 and closes at 18:00.
BUSINESS = (ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),)
NIGHT_DOUBLE = (ScheduleEntry(cron="* 0-6 * * *", tz="America/New_York", capacity=2000),)

BEFORE = _ny("2026-09-15 08:00")  # outside every window above
INSIDE = _ny("2026-09-15 10:00")  # inside BUSINESS
NIGHT = _ny("2026-09-16 03:00")  # inside NIGHT_DOUBLE
LATE = _ny("2026-09-15 23:00")  # one hour before the daily reset
AFTER_RESET = _ny("2026-09-16 00:30")


async def _raw_bucket_item(repo, entity_id, resource, shard=0):
    """The bucket item exactly as stored, bypassing every decode.

    `rf` and `vu` are item-level attributes that no public model exposes, and
    both are the subject of assertions below: `rf` because only a materialising
    pass moves it (so an unchanged `rf` *is* the proof a request stayed on the
    fast path), and `vu` because it is the gate that routes a request off it.
    """
    client = await repo._get_client()
    response = await client.get_item(
        TableName=repo.table_name,
        Key={
            "PK": {"S": schema.pk_bucket(repo._namespace_id, entity_id, resource, shard)},
            "SK": {"S": schema.sk_state()},
        },
    )
    return response.get("Item") or {}


async def _corrupt_config_sched(repo, entity_id, resource, limit_name, value):
    """Overwrite one limit's stored schedule on the entity config item.

    Out of band on purpose: no public API can write an unparseable schedule, and
    §6's whole subject is what the client does when it reads one anyway.
    """
    client = await repo._get_client()
    await client.update_item(
        TableName=repo.table_name,
        Key={
            "PK": {"S": schema.pk_entity(repo._namespace_id, entity_id)},
            "SK": {"S": schema.sk_config(resource)},
        },
        UpdateExpression="SET #a = :v",
        ExpressionAttributeNames={"#a": schema.limit_attr(limit_name, schema.LIMIT_FIELD_SCHED)},
        ExpressionAttributeValues={":v": {"S": value}},
    )


def _rpm(buckets):
    return next(b for b in buckets if b.limit_name == "rpm")


class TestE2EScheduleBoundaries:
    """Schedule enforcement against a real table, with an injected clock."""

    @pytest.fixture(scope="class")
    def sched_namespace(self, unique_name_class):
        """The namespace name, separately from the repo scoped to it.

        `Repository.namespace` is a coroutine *method*, so a scoped repo has no
        attribute spelling its own name that a test can read back; the sync
        smoke test below needs the name to open a second client on it.
        """
        return f"sched-{unique_name_class}"

    @pytest_asyncio.fixture(scope="class", loop_scope="class")
    async def sched_repo(self, shared_minimal_stack, sched_namespace):
        """Namespace-scoped Repository on the shared *minimal* stack.

        Minimal on purpose: the aggregator writes to the same bucket items and
        would make every token assertion below racy. The aggregator's own
        behaviour at a boundary is Group B.

        `config_cache_ttl=0` because the clock seam does not reach the config
        cache (see `_at`).
        """
        repo = await Repository.open(
            stack=shared_minimal_stack.name,
            region=shared_minimal_stack.region,
            endpoint_url=shared_minimal_stack.endpoint_url,
            config_cache_ttl=0,
        )
        await repo.register_namespace(sched_namespace)
        scoped = await repo.namespace(sched_namespace)
        yield scoped
        await repo.close()

    @staticmethod
    async def _at(repo, instant: int) -> None:
        """Move the injected clock and drop the config cache.

        The seam (#430) does not cover `config_cache.py`, which still reads
        `time.time()` — so without the second line every call after a jump
        resolves the limits cached *before* it, the schedule appears not to
        apply, and the obvious "fix" is to weaken an assertion.
        """
        repo._now_ms = lambda: instant
        await repo.invalidate_config_cache()

    @pytest.mark.asyncio(loop_scope="class")
    async def test_a_shrink_boundary_trims_a_full_bucket(self, sched_repo):
        """§8: the surplus must be unspendable, not a free burst.

        Fill to 1000 outside the window, cross into a 0.5x window, and the
        bucket must not admit 1000 — it holds at most 500. This is what
        replaces #469, seen end to end.
        """
        limiter = RateLimiter(repository=sched_repo)
        await self._at(sched_repo, BEFORE)
        await sched_repo.set_limits(
            "shrink-1",
            [Limit.per_minute("rpm", 1000).with_schedule(BUSINESS)],
            resource="gpt-4",
        )
        async with limiter.acquire("shrink-1", "gpt-4", consume={"rpm": 1}):
            pass

        await self._at(sched_repo, INSIDE)
        async with limiter.acquire("shrink-1", "gpt-4", consume={"rpm": 500}):
            pass
        with pytest.raises(RateLimitExceeded) as excinfo:
            async with limiter.acquire("shrink-1", "gpt-4", consume={"rpm": 1}):
                pass

        # #475: the status quotes the shard's share of the *scheduled* capacity.
        (violation,) = excinfo.value.violations
        assert violation.limit.capacity == 500

    @pytest.mark.asyncio(loop_scope="class")
    async def test_a_grow_boundary_makes_capacity_available(self, sched_repo):
        """§8. An absolute entry raising the ceiling to 2000 must be spendable
        promptly — one materialising pass, not a refill window's wait."""
        limiter = RateLimiter(repository=sched_repo)
        await self._at(sched_repo, BEFORE)
        await sched_repo.set_limits(
            "grow-1",
            [Limit.per_minute("rpm", 1000).with_schedule(NIGHT_DOUBLE)],
            resource="gpt-4",
        )
        async with limiter.acquire("grow-1", "gpt-4", consume={"rpm": 1000}):
            pass
        assert _rpm(await sched_repo.get_buckets("grow-1", resource="gpt-4")).tokens_milli == 0

        await self._at(sched_repo, NIGHT)
        async with limiter.acquire("grow-1", "gpt-4", consume={"rpm": 2000}) as lease:
            assert lease.consumed["rpm"] == 2000

    @pytest.mark.asyncio(loop_scope="class")
    async def test_a_future_vu_keeps_the_fast_path_and_reads_no_config(self, sched_repo):
        """§2.1's load-bearing claim, from the e2e side: inside a window and
        with `vu` still ahead, a request is one conditional UpdateItem.

        Asserted through the bucket item rather than a capacity counter (which
        is moto-only): `rf` must not move, because only a materialising pass
        stamps it.
        """
        limiter = RateLimiter(repository=sched_repo)
        await self._at(sched_repo, INSIDE)
        await sched_repo.set_limits(
            "fast-1",
            [Limit.per_minute("rpm", 1000).with_schedule(BUSINESS)],
            resource="gpt-4",
        )
        # The bucket does not exist yet, so `set_limits` fans out to nothing and
        # this first acquire is the create — its `vu` is a real boundary, not
        # the `vu = 0` the fan-out stamps (core plan Task 13). Asserting on the
        # acquire straight after a fan-out would observe the forced pass.
        async with limiter.acquire("fast-1", "gpt-4", consume={"rpm": 1}):
            pass
        before = await _raw_bucket_item(sched_repo, "fast-1", "gpt-4", shard=0)

        async with limiter.acquire("fast-1", "gpt-4", consume={"rpm": 1}):
            pass
        after = await _raw_bucket_item(sched_repo, "fast-1", "gpt-4", shard=0)

        assert after[schema.BUCKET_FIELD_RF] == before[schema.BUCKET_FIELD_RF]
        assert int(after[schema.BUCKET_FIELD_VU]["N"]) == _ny("2026-09-15 18:00")

    @pytest.mark.asyncio(loop_scope="class")
    async def test_a_boundary_crossed_while_a_lease_is_open(self, sched_repo):
        """§8, with #455: `adjust` still lands against the declared scope, and
        a limit the caller did not declare is still not adjustable — crossing a
        boundary mid-lease must not widen or narrow that."""
        limiter = RateLimiter(repository=sched_repo)
        await self._at(sched_repo, BEFORE)
        await sched_repo.set_limits(
            "lease-1",
            [
                Limit.per_minute("rpm", 1000).with_schedule(BUSINESS),
                Limit.per_minute("tpm", 100_000),
            ],
            resource="gpt-4",
        )

        async with limiter.acquire("lease-1", "gpt-4", consume={"rpm": 10}) as lease:
            await self._at(sched_repo, INSIDE)
            await lease.adjust(rpm=5)
            with pytest.warns(FutureWarning):
                await lease.adjust(tpm=50)

        buckets = {
            b.limit_name: b for b in await sched_repo.get_buckets("lease-1", resource="gpt-4")
        }
        assert buckets["rpm"].tokens_milli == (1000 - 15) * 1000
        assert buckets["tpm"].tokens_milli == 100_000 * 1000

    @pytest.mark.asyncio(loop_scope="class")
    async def test_concurrent_traffic_at_the_boundary_never_over_admits(self, sched_repo):
        """§8: exactly one materialisation wins, the losers take the retry path
        (`tk >= consumed`, which sees the winner's clamp), and the total
        admitted never exceeds the new ceiling.

        Twenty concurrent requests for 50 each against a 500 ceiling: at most
        ten may succeed.
        """
        limiter = RateLimiter(repository=sched_repo)
        await self._at(sched_repo, BEFORE)
        await sched_repo.set_limits(
            "race-1",
            [Limit.per_minute("rpm", 1000).with_schedule(BUSINESS)],
            resource="gpt-4",
        )
        async with limiter.acquire("race-1", "gpt-4", consume={"rpm": 1}):
            pass

        await self._at(sched_repo, INSIDE)

        async def one() -> bool:
            try:
                async with limiter.acquire("race-1", "gpt-4", consume={"rpm": 50}):
                    return True
            except RateLimitExceeded:
                return False

        results = await asyncio.gather(*[one() for _ in range(20)])
        assert sum(results) <= 10

        bucket = _rpm(await sched_repo.get_buckets("race-1", resource="gpt-4"))
        assert bucket.tokens_milli >= 0

    @pytest.mark.asyncio(loop_scope="class")
    async def test_cascade_with_different_schedules_on_child_and_parent(self, sched_repo):
        """§8: only the parent's boundary fires. The child keeps its full
        1000 and is admitted by its own bucket; the parent's 0.5x window is
        what rejects, and the status names the *parent*."""
        limiter = RateLimiter(repository=sched_repo)
        await self._at(sched_repo, BEFORE)
        await limiter.create_entity("casc-org")
        await limiter.create_entity("casc-key", parent_id="casc-org", cascade=True)
        await sched_repo.set_limits(
            "casc-org",
            [Limit.per_minute("rpm", 1000).with_schedule(BUSINESS)],
            resource="gpt-4",
        )
        await sched_repo.set_limits("casc-key", [Limit.per_minute("rpm", 1000)], resource="gpt-4")
        async with limiter.acquire("casc-key", "gpt-4", consume={"rpm": 1}):
            pass

        await self._at(sched_repo, INSIDE)
        with pytest.raises(RateLimitExceeded) as excinfo:
            async with limiter.acquire("casc-key", "gpt-4", consume={"rpm": 900}):
                pass
        assert {v.entity_id for v in excinfo.value.violations} == {"casc-org"}

    @pytest.mark.asyncio(loop_scope="class")
    async def test_every_shard_converges_on_its_share(self, sched_repo):
        """§8. Shares must sum to the scheduled ceiling, not to a multiple of
        it — scale first, then divide (Global Constraints).

        `select_shard` is the one place a shard is drawn and it re-picks at
        random on every call (ADR-134), so asserting that a given `acquire()`
        landed on a given shard is flaky by construction. It is pinned here for
        the duration of the test instead, which keeps the real acquire path —
        create, materialise, clamp — while making *which* shard each call
        exercises deterministic.
        """
        limiter = RateLimiter(repository=sched_repo)
        await self._at(sched_repo, BEFORE)
        await sched_repo.set_limits(
            "shard-1",
            [Limit.per_minute("rpm", 1000).with_schedule(BUSINESS)],
            resource="gpt-4",
        )
        async with limiter.acquire("shard-1", "gpt-4", consume={"rpm": 1}):
            pass
        await sched_repo.bump_shard_count("shard-1", "gpt-4", 1)

        real_select = sched_repo.select_shard

        def pin(target: int):
            def _select(entity_id, resource, shard_id=None, shard_count=None):
                chosen, count = real_select(entity_id, resource, shard_id, shard_count)
                return (chosen if shard_id is not None else target), count

            return _select

        try:
            # Shard 1 does not exist yet; the acquire that draws it creates it
            # with its share of the capacity in force at BEFORE (ADR-133).
            for shard in (0, 1):
                sched_repo.select_shard = pin(shard)
                async with limiter.acquire("shard-1", "gpt-4", consume={"rpm": 1}):
                    pass

            await self._at(sched_repo, INSIDE)
            for shard in (0, 1):
                sched_repo.select_shard = pin(shard)
                async with limiter.acquire("shard-1", "gpt-4", consume={"rpm": 1}):
                    pass
        finally:
            del sched_repo.select_shard

        # `get_buckets` with a resource reads one shard; the GSI3 form
        # discovers all of them.
        buckets = [b for b in await sched_repo.get_buckets("shard-1") if b.limit_name == "rpm"]
        assert len(buckets) == 2
        for b in buckets:
            assert b.shard_count == 2
            assert b.effective_capacity_milli(INSIDE) == 250_000  # (1_000_000 * 0.5) // 2
            assert b.tokens_milli <= 250_000
        assert sum(b.tokens_milli for b in buckets) <= 500_000

    @pytest.mark.asyncio(loop_scope="class")
    async def test_a_daily_quota_resets_in_one_lump(self, sched_repo):
        """§8: burn it, cross the edge, get it back at once, and `tc` keeps
        climbing across the boundary — the property #471's reset_bucket()
        destroyed and `.claude/rules/design-validation.md` exists to protect."""
        limiter = RateLimiter(repository=sched_repo)
        await self._at(sched_repo, LATE)
        await sched_repo.set_limits(
            "quota-1",
            [Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York")],
            resource="gpt-4",
        )
        async with limiter.acquire("quota-1", "gpt-4", consume={"rpd": 10_000}):
            pass
        with pytest.raises(RateLimitExceeded):
            async with limiter.acquire("quota-1", "gpt-4", consume={"rpd": 1}):
                pass

        await self._at(sched_repo, AFTER_RESET)
        async with limiter.acquire("quota-1", "gpt-4", consume={"rpd": 9_000}):
            pass

        bucket = next(
            b
            for b in await sched_repo.get_buckets("quota-1", resource="gpt-4")
            if b.limit_name == "rpd"
        )
        assert bucket.tokens_milli == 1_000_000
        assert bucket.total_consumed_milli == 19_000_000

    @pytest.mark.asyncio(loop_scope="class")
    async def test_an_idle_bucket_resets_on_wake_not_at_the_edge(self, sched_repo):
        """§8 and §9: nothing observes a bucket no one is using, so the reset
        lands on the first request after the edge. Asserted from both sides —
        the item is untouched at 00:30, and restored after the 09:00 request."""
        limiter = RateLimiter(repository=sched_repo)
        await self._at(sched_repo, LATE)
        await sched_repo.set_limits(
            "idle-1",
            [Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York")],
            resource="gpt-4",
        )
        async with limiter.acquire("idle-1", "gpt-4", consume={"rpd": 10_000}):
            pass

        await self._at(sched_repo, AFTER_RESET)
        idle = next(
            b
            for b in await sched_repo.get_buckets("idle-1", resource="gpt-4")
            if b.limit_name == "rpd"
        )
        assert idle.tokens_milli == 0  # the edge passed; nothing applied it

        await self._at(sched_repo, _ny("2026-09-16 09:00"))
        async with limiter.acquire("idle-1", "gpt-4", consume={"rpd": 1}):
            pass
        woken = next(
            b
            for b in await sched_repo.get_buckets("idle-1", resource="gpt-4")
            if b.limit_name == "rpd"
        )
        assert woken.tokens_milli == 9_999_000

    @pytest.mark.asyncio(loop_scope="class")
    async def test_check_availability_agrees_with_acquire_at_one_instant(self, sched_repo):
        """§7's reason for wiring the query surface: the display and the
        rejection must not describe the same bucket differently.

        The agreement assertion alone does not discriminate — with neither side
        reset-aware both report `0.0` and `approx(0.0, rel=0.01)` passes. The
        `approx(3600)` line is the one doing the work (#530): a quota has no
        drip at all, so the only finite answer is the wait to midnight.
        """
        limiter = RateLimiter(repository=sched_repo)
        await self._at(sched_repo, LATE)
        await sched_repo.set_limits(
            "agree-1",
            [Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York")],
            resource="gpt-4",
        )
        async with limiter.acquire("agree-1", "gpt-4", consume={"rpd": 10_000}):
            pass

        check = await limiter.check_availability("agree-1", "gpt-4", needed={"rpd": 5_000})
        with pytest.raises(RateLimitExceeded) as excinfo:
            async with limiter.acquire("agree-1", "gpt-4", consume={"rpd": 5_000}):
                pass

        displayed = check.status("rpd").retry_after_seconds
        rejected = excinfo.value.retry_after_seconds
        assert displayed == pytest.approx(rejected, rel=0.01)
        assert displayed == pytest.approx(3600, abs=5)  # at midnight, not "now" (#530)

    @pytest.mark.asyncio(loop_scope="class")
    async def test_a_corrupt_stored_schedule_honours_on_unavailable(self, sched_repo):
        """§8 and §6, both modes against a real table."""
        await self._at(sched_repo, INSIDE)
        await sched_repo.set_limits(
            "corrupt-e2e",
            [Limit.per_minute("rpm", 1000).with_schedule(BUSINESS)],
            resource="gpt-4",
        )
        await _corrupt_config_sched(sched_repo, "corrupt-e2e", "gpt-4", "rpm", "not-a-schedule")
        await sched_repo.invalidate_config_cache()

        slow = RateLimiter(repository=sched_repo, speculative_writes=False)
        with pytest.raises(RateLimiterUnavailable):
            async with slow.acquire(
                "corrupt-e2e", "gpt-4", consume={"rpm": 1}, on_unavailable=OnUnavailable.BLOCK
            ):
                pass

        async with slow.acquire(
            "corrupt-e2e", "gpt-4", consume={"rpm": 1}, on_unavailable=OnUnavailable.ALLOW
        ) as lease:
            assert lease.degraded is True

    @pytest.mark.asyncio(loop_scope="class")
    async def test_the_sync_client_enforces_the_same_boundary(self, sched_repo, sched_namespace):
        """§8's "plus the generated sync counterparts". SyncRepository reaches
        DynamoDB through boto3, not aioboto3, and nothing else in this file
        crosses a boundary on that client."""
        sync_repo = SyncRepository.open(
            sched_namespace,
            stack=sched_repo.stack_name,
            region=sched_repo.region,
            endpoint_url=sched_repo.endpoint_url,
            config_cache_ttl=0,
        )
        try:
            sync_repo._now_ms = lambda: BEFORE
            sync_repo.set_limits(
                "sync-1",
                [Limit.per_minute("rpm", 1000).with_schedule(BUSINESS)],
                resource="gpt-4",
            )
            limiter = SyncRateLimiter(repository=sync_repo)
            with limiter.acquire("sync-1", "gpt-4", consume={"rpm": 1}):
                pass

            sync_repo._now_ms = lambda: INSIDE
            sync_repo.invalidate_config_cache()
            with limiter.acquire("sync-1", "gpt-4", consume={"rpm": 499}):
                pass
            with pytest.raises(RateLimitExceeded):
                with limiter.acquire("sync-1", "gpt-4", consume={"rpm": 100}):
                    pass
        finally:
            sync_repo.close()


class TestE2EScheduleThroughTheProvisioner:
    """§8: a schedule applied through the manifest must reach live buckets.

    Modelled on TestE2EProvisionerReachesLiveBuckets: `_handle_cli` is invoked
    in-process, because the provisioner is sync boto3 and needs no deployed
    Lambda to exercise this path.
    """

    @pytest_asyncio.fixture(scope="class", loop_scope="class")
    async def prov_repo(self, shared_minimal_stack, unique_name_class):
        ns = f"schedprov-{unique_name_class}"
        repo = await Repository.open(
            stack=shared_minimal_stack.name,
            region=shared_minimal_stack.region,
            endpoint_url=shared_minimal_stack.endpoint_url,
            config_cache_ttl=0,
        )
        await repo.register_namespace(ns)
        scoped = await repo.namespace(ns)
        yield scoped
        await repo.close()

    @pytest.mark.asyncio(loop_scope="class")
    async def test_an_applied_schedule_reaches_an_existing_bucket(self, prov_repo):
        from zae_limiter_provisioner.handler import _handle_cli

        limiter = RateLimiter(repository=prov_repo)
        prov_repo._now_ms = lambda: BEFORE
        await prov_repo.set_limits("prov-1", [Limit.per_minute("rpm", 1000)], resource="gpt-4")
        async with limiter.acquire("prov-1", "gpt-4", consume={"rpm": 1}):
            pass

        result = _handle_cli(
            {
                "action": "apply",
                "table_name": prov_repo.table_name,
                "namespace_id": prov_repo._namespace_id,
                "manifest": {
                    "namespace": "default",
                    "entities": {
                        "prov-1": {
                            "resources": {
                                "gpt-4": {
                                    "limits": {
                                        "rpm": {
                                            "capacity": 1000,
                                            "schedule": [
                                                {
                                                    "cron": "* 9-17 * * MON-FRI",
                                                    "tz": "America/New_York",
                                                    "scale": 0.5,
                                                }
                                            ],
                                        }
                                    }
                                }
                            }
                        }
                    },
                },
            },
            None,
        )
        assert result["status"] == "applied"
        assert result["errors"] == []

        item = await _raw_bucket_item(prov_repo, "prov-1", "gpt-4", shard=0)
        assert item[schema.BUCKET_FIELD_SCHED]["S"] == "h9-17w1-5s500"
        assert item[schema.BUCKET_FIELD_SCHED_TZ]["S"] == "America/New_York"
        assert item[schema.BUCKET_FIELD_VU]["N"] == "0"

        # And it is enforced, not merely stored.
        prov_repo._now_ms = lambda: INSIDE
        await prov_repo.invalidate_config_cache()
        async with limiter.acquire("prov-1", "gpt-4", consume={"rpm": 499}):
            pass
        with pytest.raises(RateLimitExceeded):
            async with limiter.acquire("prov-1", "gpt-4", consume={"rpm": 100}):
                pass


class TestE2EScheduleWithTheAggregator:
    """§8: the same crossing with the aggregator running.

    Real waiting, because the Lambda reads its own clock inside a container
    this test cannot reach — which is also why these are the only cases that
    wait. Everything client-enforced is in TestE2EScheduleBoundaries with an
    injected clock.

    Only the two crossing tests wait, and only those carry `slow`. The `vu`
    re-stamp below needs the aggregator to see a boundary it has already
    crossed, which a fan-out hands it for free (`vu = 0`) — no waiting, and it
    is the test that found the early return this branch also fixes.
    """

    # `*/2` matches even minutes, so the state changes at every minute boundary
    # and a crossing is at most sixty seconds away.
    ALTERNATING = (ScheduleEntry(cron="*/2 * * * *", tz="UTC", scale=0.5),)

    @pytest_asyncio.fixture(scope="class", loop_scope="class")
    async def aggr_repo(self, shared_aggregator_stack, unique_name_class):
        ns = f"schedaggr-{unique_name_class}"
        repo = await Repository.open(
            stack=shared_aggregator_stack.name,
            region=shared_aggregator_stack.region,
            endpoint_url=shared_aggregator_stack.endpoint_url,
            config_cache_ttl=0,
        )
        await repo.register_namespace(ns)
        scoped = await repo.namespace(ns)
        yield scoped
        await repo.close()

    @staticmethod
    async def _sleep_to_the_next_minute_boundary() -> None:
        """Wait until `*/2` next changes state, plus a second of slack."""
        await asyncio.sleep(60 - (time.time() % 60) + 1)

    @staticmethod
    async def _poll_bucket(repo, entity_id, resource, predicate, timeout=60.0):
        """Poll the raw bucket item until `predicate` holds, or time out.

        A fixed sleep has to be sized for the worst LocalStack stream-to-Lambda
        latency on the slowest machine that will ever run this, which makes the
        common case slow and the rare case flaky anyway. Returns the last item
        seen either way, so the assertion that follows reports the real values.
        """
        deadline = time.monotonic() + timeout
        item = {}
        while time.monotonic() < deadline:
            item = await _raw_bucket_item(repo, entity_id, resource)
            if item and predicate(item):
                return item
            await asyncio.sleep(1.0)
        return item

    @pytest.mark.slow
    @pytest.mark.asyncio(loop_scope="class")
    async def test_the_aggregator_trims_to_the_scheduled_ceiling(self, aggr_repo):
        """The aggregator's whole job is keeping hot buckets off the slow path,
        so a shrink it does not apply is a shrink that never lands on the
        buckets that matter (§3.3). Drive traffic across a boundary and assert
        the stored balance never exceeds the scheduled share."""
        limiter = RateLimiter(repository=aggr_repo)
        await aggr_repo.set_limits(
            "aggr-sched",
            [Limit.per_minute("rpm", 1000).with_schedule(self.ALTERNATING)],
            resource="gpt-4",
        )
        async with limiter.acquire("aggr-sched", "gpt-4", consume={"rpm": 1}):
            pass

        await self._sleep_to_the_next_minute_boundary()
        for _ in range(20):
            try:
                async with limiter.acquire("aggr-sched", "gpt-4", consume={"rpm": 1}):
                    pass
            except RateLimitExceeded:
                pass
        await asyncio.sleep(15)  # stream + Lambda

        bucket = _rpm(await aggr_repo.get_buckets("aggr-sched", resource="gpt-4"))
        now_ms = aggr_repo._now_ms()
        assert bucket.tokens_milli <= bucket.effective_capacity_milli(now_ms)

    @pytest.mark.asyncio(loop_scope="class")
    async def test_the_aggregator_restamps_an_expired_vu(self, aggr_repo):
        """`vu = 0` after a fan-out must be replaced by a real boundary by
        whichever refiller gets there first, or the bucket is pinned to the
        slow path. Here that is the aggregator — deliberately nothing else:
        no client request follows the fan-out, because the first one would
        re-stamp `vu` itself and the assertion would pass with the Lambda
        switched off entirely.
        """
        limiter = RateLimiter(repository=aggr_repo)
        await aggr_repo.set_limits(
            "aggr-vu",
            [Limit.per_minute("rpm", 1000).with_schedule(self.ALTERNATING)],
            resource="gpt-4",
        )
        async with limiter.acquire("aggr-vu", "gpt-4", consume={"rpm": 1}):
            pass

        # Force `vu = 0` through the fan-out, and drop the ceiling far enough
        # that the aggregator's clamp is a guaranteed negative delta: it only
        # re-stamps `vu` in a write it was going to make anyway.
        await aggr_repo.set_limits(
            "aggr-vu",
            [Limit.per_minute("rpm", 10).with_schedule(self.ALTERNATING)],
            resource="gpt-4",
        )
        before = await _raw_bucket_item(aggr_repo, "aggr-vu", "gpt-4")
        assert int(before[schema.BUCKET_FIELD_VU]["N"]) == 0, "precondition: the fan-out expired vu"

        item = await self._poll_bucket(
            aggr_repo,
            "aggr-vu",
            "gpt-4",
            lambda i: int(i.get(schema.BUCKET_FIELD_VU, {}).get("N", "0")) > 0,
        )
        assert int(item[schema.BUCKET_FIELD_VU]["N"]) > aggr_repo._now_ms()

    @pytest.mark.slow
    @pytest.mark.asyncio(loop_scope="class")
    async def test_the_same_crossing_without_the_aggregator(
        self, shared_minimal_stack, unique_name_class
    ):
        """ADR-133: sharding and refill must work either way, so the assertion
        above must hold on a stack with no Lambda at all. The minimal-stack
        half of §8's with-and-without pair; the client-enforced cases in
        TestE2EScheduleBoundaries are the rest of it."""
        repo = await Repository.open(
            stack=shared_minimal_stack.name,
            region=shared_minimal_stack.region,
            endpoint_url=shared_minimal_stack.endpoint_url,
            config_cache_ttl=0,
        )
        try:
            ns = f"schednoaggr-{unique_name_class}"
            await repo.register_namespace(ns)
            scoped = await repo.namespace(ns)
            limiter = RateLimiter(repository=scoped)
            await scoped.set_limits(
                "noaggr-sched",
                [Limit.per_minute("rpm", 1000).with_schedule(self.ALTERNATING)],
                resource="gpt-4",
            )
            async with limiter.acquire("noaggr-sched", "gpt-4", consume={"rpm": 1}):
                pass

            await self._sleep_to_the_next_minute_boundary()
            for _ in range(20):
                try:
                    async with limiter.acquire("noaggr-sched", "gpt-4", consume={"rpm": 1}):
                        pass
                except RateLimitExceeded:
                    pass

            bucket = _rpm(await scoped.get_buckets("noaggr-sched", resource="gpt-4"))
            assert bucket.tokens_milli <= bucket.effective_capacity_milli(scoped._now_ms())
        finally:
            await repo.close()
