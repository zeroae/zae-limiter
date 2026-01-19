"""End-to-end tests for resource and system config storage.

These tests verify the complete workflow of storing and using
rate limit configuration at resource and system levels in DynamoDB.

To run these tests locally:
    # Start LocalStack (from project root)
    docker compose up -d

    # Set environment variables and run tests
    export AWS_ENDPOINT_URL=http://localhost:4566
    export AWS_ACCESS_KEY_ID=test
    export AWS_SECRET_ACCESS_KEY=test
    export AWS_DEFAULT_REGION=us-east-1
    pytest tests/e2e/test_config_storage.py -v

Note: These tests use minimal stack options (no aggregator) for faster execution.
"""

import pytest
import pytest_asyncio  # type: ignore[import-untyped]
from click.testing import CliRunner

from zae_limiter import Limit, RateLimiter
from zae_limiter.cli import cli

pytestmark = [pytest.mark.integration, pytest.mark.e2e]


class TestE2EResourceConfigStorage:
    """E2E tests for resource-level config storage."""

    @pytest_asyncio.fixture(scope="class", loop_scope="class")
    async def e2e_limiter(self, localstack_endpoint, unique_name_class, minimal_stack_options):
        """Class-scoped limiter with minimal stack for config tests."""
        limiter = RateLimiter(
            name=unique_name_class,
            endpoint_url=localstack_endpoint,
            region="us-east-1",
            stack_options=minimal_stack_options,
        )

        async with limiter:
            yield limiter

        try:
            await limiter.delete_stack()
        except Exception as e:
            print(f"Warning: Stack cleanup failed: {e}")

    @pytest.mark.asyncio(loop_scope="class")
    async def test_resource_config_crud(self, e2e_limiter):
        """
        Test resource config CRUD operations.

        Workflow:
        1. CREATE: Set limits for a resource
        2. READ: Verify limits are stored
        3. UPDATE: Replace limits with new values
        4. DELETE: Remove limits
        """
        # Step 1: CREATE
        limits = [
            Limit.per_minute("rpm", 100),
            Limit.per_minute("tpm", 10_000),
        ]
        await e2e_limiter.set_resource_limits("gpt-4", limits)

        # Step 2: READ
        retrieved = await e2e_limiter.get_resource_limits("gpt-4")
        assert len(retrieved) == 2
        names = {limit.name for limit in retrieved}
        assert names == {"rpm", "tpm"}

        # Verify capacity values
        rpm_limit = next(l for l in retrieved if l.name == "rpm")
        assert rpm_limit.capacity == 100

        # Step 3: UPDATE (replace)
        new_limits = [Limit.per_minute("tpm", 20_000)]
        await e2e_limiter.set_resource_limits("gpt-4", new_limits)

        updated = await e2e_limiter.get_resource_limits("gpt-4")
        assert len(updated) == 1
        assert updated[0].name == "tpm"
        assert updated[0].capacity == 20_000

        # Step 4: DELETE
        await e2e_limiter.delete_resource_limits("gpt-4")

        deleted = await e2e_limiter.get_resource_limits("gpt-4")
        assert len(deleted) == 0

    @pytest.mark.asyncio(loop_scope="class")
    async def test_resource_config_isolation(self, e2e_limiter):
        """
        Test that resource configs are isolated from each other.

        Workflow:
        1. Set different limits for different resources
        2. Verify each resource has its own config
        3. Delete one resource config
        4. Verify other resource unaffected
        """
        # Step 1: Set different limits
        await e2e_limiter.set_resource_limits("gpt-4", [Limit.per_minute("rpm", 100)])
        await e2e_limiter.set_resource_limits("claude-3", [Limit.per_minute("rpm", 200)])

        # Step 2: Verify isolation
        gpt4_limits = await e2e_limiter.get_resource_limits("gpt-4")
        claude_limits = await e2e_limiter.get_resource_limits("claude-3")

        assert gpt4_limits[0].capacity == 100
        assert claude_limits[0].capacity == 200

        # Step 3: Delete one config
        await e2e_limiter.delete_resource_limits("gpt-4")

        # Step 4: Verify other unaffected
        claude_limits_after = await e2e_limiter.get_resource_limits("claude-3")
        assert claude_limits_after[0].capacity == 200

        # Cleanup
        await e2e_limiter.delete_resource_limits("claude-3")

    @pytest.mark.asyncio(loop_scope="class")
    async def test_list_resources_with_limits(self, e2e_limiter):
        """
        Test listing resources with configured limits.

        Workflow:
        1. Verify initially empty
        2. Add limits for multiple resources
        3. Verify listing returns all
        4. Delete and verify list updates
        """
        # Step 1: Initially should be empty (or cleanup from previous tests)
        initial = await e2e_limiter.list_resources_with_limits()

        # Step 2: Add limits for multiple resources
        await e2e_limiter.set_resource_limits("gpt-4", [Limit.per_minute("rpm", 100)])
        await e2e_limiter.set_resource_limits("claude-3", [Limit.per_minute("rpm", 200)])
        await e2e_limiter.set_resource_limits("gemini-pro", [Limit.per_minute("rpm", 150)])

        # Step 3: Verify listing
        resources = await e2e_limiter.list_resources_with_limits()
        assert "gpt-4" in resources
        assert "claude-3" in resources
        assert "gemini-pro" in resources
        assert len(resources) >= len(initial) + 3

        # Step 4: Delete one and verify
        await e2e_limiter.delete_resource_limits("gemini-pro")
        updated = await e2e_limiter.list_resources_with_limits()
        assert "gemini-pro" not in updated
        assert "gpt-4" in updated

        # Cleanup
        await e2e_limiter.delete_resource_limits("gpt-4")
        await e2e_limiter.delete_resource_limits("claude-3")


class TestE2ESystemConfigStorage:
    """E2E tests for system-level config storage."""

    @pytest_asyncio.fixture(scope="class", loop_scope="class")
    async def e2e_limiter(self, localstack_endpoint, unique_name_class, minimal_stack_options):
        """Class-scoped limiter with minimal stack for config tests."""
        # Use different name suffix to avoid conflicts with other test class
        name = f"{unique_name_class}-sys"
        limiter = RateLimiter(
            name=name,
            endpoint_url=localstack_endpoint,
            region="us-east-1",
            stack_options=minimal_stack_options,
        )

        async with limiter:
            yield limiter

        try:
            await limiter.delete_stack()
        except Exception as e:
            print(f"Warning: Stack cleanup failed: {e}")

    @pytest.mark.asyncio(loop_scope="class")
    async def test_system_config_crud(self, e2e_limiter):
        """
        Test system config CRUD operations.

        Workflow:
        1. CREATE: Set system defaults for a resource
        2. READ: Verify defaults are stored
        3. UPDATE: Replace with new values
        4. DELETE: Remove defaults
        """
        # Step 1: CREATE
        limits = [
            Limit.per_minute("rpm", 50),
            Limit.per_minute("tpm", 5_000),
        ]
        await e2e_limiter.set_system_limits("gpt-4", limits)

        # Step 2: READ
        retrieved = await e2e_limiter.get_system_limits("gpt-4")
        assert len(retrieved) == 2
        names = {limit.name for limit in retrieved}
        assert names == {"rpm", "tpm"}

        # Verify capacity values
        rpm_limit = next(l for l in retrieved if l.name == "rpm")
        assert rpm_limit.capacity == 50

        # Step 3: UPDATE (replace)
        new_limits = [Limit.per_minute("tpm", 10_000)]
        await e2e_limiter.set_system_limits("gpt-4", new_limits)

        updated = await e2e_limiter.get_system_limits("gpt-4")
        assert len(updated) == 1
        assert updated[0].name == "tpm"
        assert updated[0].capacity == 10_000

        # Step 4: DELETE
        await e2e_limiter.delete_system_limits("gpt-4")

        deleted = await e2e_limiter.get_system_limits("gpt-4")
        assert len(deleted) == 0

    @pytest.mark.asyncio(loop_scope="class")
    async def test_system_config_isolation(self, e2e_limiter):
        """
        Test that system configs are resource-isolated.

        Workflow:
        1. Set different system defaults for different resources
        2. Verify each resource has its own defaults
        """
        # Step 1: Set different defaults
        await e2e_limiter.set_system_limits("gpt-4", [Limit.per_minute("rpm", 50)])
        await e2e_limiter.set_system_limits("claude-3", [Limit.per_minute("rpm", 100)])

        # Step 2: Verify isolation
        gpt4_limits = await e2e_limiter.get_system_limits("gpt-4")
        claude_limits = await e2e_limiter.get_system_limits("claude-3")

        assert gpt4_limits[0].capacity == 50
        assert claude_limits[0].capacity == 100

        # Cleanup
        await e2e_limiter.delete_system_limits("gpt-4")
        await e2e_limiter.delete_system_limits("claude-3")

    @pytest.mark.asyncio(loop_scope="class")
    async def test_list_system_resources(self, e2e_limiter):
        """
        Test listing resources with system defaults.

        Workflow:
        1. Add system defaults for multiple resources
        2. Verify listing returns all
        3. Delete one and verify list updates
        """
        # Step 1: Add system defaults
        await e2e_limiter.set_system_limits("gpt-4", [Limit.per_minute("rpm", 50)])
        await e2e_limiter.set_system_limits("claude-3", [Limit.per_minute("rpm", 100)])

        # Step 2: Verify listing
        resources = await e2e_limiter.list_system_resources_with_limits()
        assert "gpt-4" in resources
        assert "claude-3" in resources

        # Step 3: Delete one and verify
        await e2e_limiter.delete_system_limits("gpt-4")
        updated = await e2e_limiter.list_system_resources_with_limits()
        assert "gpt-4" not in updated
        assert "claude-3" in updated

        # Cleanup
        await e2e_limiter.delete_system_limits("claude-3")


class TestE2EConfigCLIWorkflow:
    """E2E tests for CLI config commands."""

    @pytest.fixture
    def cli_runner(self):
        """Create Click CLI runner."""
        return CliRunner()

    def test_resource_config_cli_workflow(
        self, cli_runner, localstack_endpoint, unique_name, minimal_stack_options
    ):
        """
        Complete resource config workflow using CLI commands.

        Workflow:
        1. Deploy stack via CLI
        2. Set resource limits via CLI
        3. Get resource limits via CLI
        4. List resources via CLI
        5. Delete resource limits via CLI
        6. Clean up stack
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

            # Step 2: Set resource limits via CLI
            result = cli_runner.invoke(
                cli,
                [
                    "resource",
                    "set",
                    "gpt-4",
                    "--name",
                    unique_name,
                    "--endpoint-url",
                    localstack_endpoint,
                    "--region",
                    "us-east-1",
                    "-l",
                    "rpm:100",
                    "-l",
                    "tpm:10000",
                ],
            )
            assert result.exit_code == 0, f"Resource set failed: {result.output}"
            assert "Set 2 limit(s)" in result.output

            # Step 3: Get resource limits via CLI
            result = cli_runner.invoke(
                cli,
                [
                    "resource",
                    "get",
                    "gpt-4",
                    "--name",
                    unique_name,
                    "--endpoint-url",
                    localstack_endpoint,
                    "--region",
                    "us-east-1",
                ],
            )
            assert result.exit_code == 0, f"Resource get failed: {result.output}"
            assert "rpm" in result.output
            assert "tpm" in result.output

            # Step 4: List resources via CLI
            result = cli_runner.invoke(
                cli,
                [
                    "resource",
                    "list",
                    "--name",
                    unique_name,
                    "--endpoint-url",
                    localstack_endpoint,
                    "--region",
                    "us-east-1",
                ],
            )
            assert result.exit_code == 0, f"Resource list failed: {result.output}"
            assert "gpt-4" in result.output

            # Step 5: Delete resource limits via CLI
            result = cli_runner.invoke(
                cli,
                [
                    "resource",
                    "delete",
                    "gpt-4",
                    "--name",
                    unique_name,
                    "--endpoint-url",
                    localstack_endpoint,
                    "--region",
                    "us-east-1",
                    "--yes",
                ],
            )
            assert result.exit_code == 0, f"Resource delete failed: {result.output}"
            assert "Deleted" in result.output

        finally:
            # Cleanup: Delete stack via CLI
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

    def test_system_config_cli_workflow(
        self, cli_runner, localstack_endpoint, unique_name, minimal_stack_options
    ):
        """
        Complete system config workflow using CLI commands.

        Workflow:
        1. Deploy stack via CLI
        2. Set system defaults via CLI
        3. Get system defaults via CLI
        4. List system resources via CLI
        5. Delete system defaults via CLI
        6. Clean up stack
        """
        # Use different name to avoid conflicts
        name = f"{unique_name}-syscli"

        try:
            # Step 1: Deploy stack via CLI
            result = cli_runner.invoke(
                cli,
                [
                    "deploy",
                    "--name",
                    name,
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

            # Step 2: Set system defaults via CLI
            result = cli_runner.invoke(
                cli,
                [
                    "system",
                    "set-defaults",
                    "gpt-4",
                    "--name",
                    name,
                    "--endpoint-url",
                    localstack_endpoint,
                    "--region",
                    "us-east-1",
                    "-l",
                    "rpm:50",
                    "-l",
                    "tpm:5000",
                ],
            )
            assert result.exit_code == 0, f"System set-defaults failed: {result.output}"
            assert "Set 2 system default(s)" in result.output

            # Step 3: Get system defaults via CLI
            result = cli_runner.invoke(
                cli,
                [
                    "system",
                    "get-defaults",
                    "gpt-4",
                    "--name",
                    name,
                    "--endpoint-url",
                    localstack_endpoint,
                    "--region",
                    "us-east-1",
                ],
            )
            assert result.exit_code == 0, f"System get-defaults failed: {result.output}"
            assert "rpm" in result.output
            assert "tpm" in result.output

            # Step 4: List system resources via CLI
            result = cli_runner.invoke(
                cli,
                [
                    "system",
                    "list-resources",
                    "--name",
                    name,
                    "--endpoint-url",
                    localstack_endpoint,
                    "--region",
                    "us-east-1",
                ],
            )
            assert result.exit_code == 0, f"System list-resources failed: {result.output}"
            assert "gpt-4" in result.output

            # Step 5: Delete system defaults via CLI
            result = cli_runner.invoke(
                cli,
                [
                    "system",
                    "delete-defaults",
                    "gpt-4",
                    "--name",
                    name,
                    "--endpoint-url",
                    localstack_endpoint,
                    "--region",
                    "us-east-1",
                    "--yes",
                ],
            )
            assert result.exit_code == 0, f"System delete-defaults failed: {result.output}"
            assert "Deleted" in result.output

        finally:
            # Cleanup: Delete stack via CLI
            cli_runner.invoke(
                cli,
                [
                    "delete",
                    "--name",
                    name,
                    "--endpoint-url",
                    localstack_endpoint,
                    "--region",
                    "us-east-1",
                    "--yes",
                ],
            )


class TestE2ESyncConfigStorage:
    """E2E tests for sync limiter config storage."""

    def test_sync_resource_config_workflow(
        self, sync_localstack_limiter
    ):
        """
        Test resource config with SyncRateLimiter.

        Workflow:
        1. Set resource limits
        2. Get resource limits
        3. List resources
        4. Delete resource limits
        """
        limiter = sync_localstack_limiter

        # Step 1: Set resource limits
        limits = [Limit.per_minute("rpm", 100)]
        limiter.set_resource_limits("gpt-4", limits)

        # Step 2: Get resource limits
        retrieved = limiter.get_resource_limits("gpt-4")
        assert len(retrieved) == 1
        assert retrieved[0].capacity == 100

        # Step 3: List resources
        resources = limiter.list_resources_with_limits()
        assert "gpt-4" in resources

        # Step 4: Delete resource limits
        limiter.delete_resource_limits("gpt-4")
        deleted = limiter.get_resource_limits("gpt-4")
        assert len(deleted) == 0

    def test_sync_system_config_workflow(
        self, sync_localstack_limiter
    ):
        """
        Test system config with SyncRateLimiter.

        Workflow:
        1. Set system limits
        2. Get system limits
        3. List system resources
        4. Delete system limits
        """
        limiter = sync_localstack_limiter

        # Step 1: Set system limits
        limits = [Limit.per_minute("rpm", 50)]
        limiter.set_system_limits("gpt-4", limits)

        # Step 2: Get system limits
        retrieved = limiter.get_system_limits("gpt-4")
        assert len(retrieved) == 1
        assert retrieved[0].capacity == 50

        # Step 3: List system resources
        resources = limiter.list_system_resources_with_limits()
        assert "gpt-4" in resources

        # Step 4: Delete system limits
        limiter.delete_system_limits("gpt-4")
        deleted = limiter.get_system_limits("gpt-4")
        assert len(deleted) == 0
