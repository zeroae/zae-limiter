"""End-to-end tests for resource and system config storage.

These tests verify the complete workflow of storing and using
rate limit configuration at system, resource, and entity levels in DynamoDB.

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

import warnings

import pytest
import pytest_asyncio  # type: ignore[import-untyped]
from click.testing import CliRunner

from zae_limiter import Limit, OnUnavailable, RateLimiter, Repository
from zae_limiter.cli import cli

pytestmark = [pytest.mark.integration, pytest.mark.e2e]


class TestE2EResourceConfigStorage:
    """E2E tests for resource-level config storage."""

    @pytest_asyncio.fixture(scope="class", loop_scope="class")
    async def e2e_limiter(self, shared_minimal_stack, unique_name_class):
        """Namespace-scoped limiter on shared minimal stack for config tests."""
        ns = f"res-{unique_name_class}"
        repo = Repository(
            name=shared_minimal_stack.name,
            endpoint_url=shared_minimal_stack.endpoint_url,
            region=shared_minimal_stack.region,
        )
        await repo.register_namespace(ns)
        scoped = await repo.namespace(ns)
        limiter = RateLimiter(repository=scoped)
        async with limiter:
            yield limiter
        await repo.close()

    @pytest.mark.asyncio(loop_scope="class")
    async def test_resource_config_crud(self, e2e_limiter):
        """
        Test resource config CRUD operations.

        Workflow:
        1. CREATE: Set defaults for a resource
        2. READ: Verify defaults are stored
        3. UPDATE: Replace defaults with new values
        4. DELETE: Remove defaults
        """
        # Step 1: CREATE
        limits = [
            Limit.per_minute("rpm", 100),
            Limit.per_minute("tpm", 10_000),
        ]
        await e2e_limiter.set_resource_defaults("gpt-4", limits)

        # Step 2: READ
        retrieved = await e2e_limiter.get_resource_defaults("gpt-4")
        assert len(retrieved) == 2
        names = {limit.name for limit in retrieved}
        assert names == {"rpm", "tpm"}

        # Verify capacity values
        rpm_limit = next(lim for lim in retrieved if lim.name == "rpm")
        assert rpm_limit.capacity == 100

        # Step 3: UPDATE (replace)
        new_limits = [Limit.per_minute("tpm", 20_000)]
        await e2e_limiter.set_resource_defaults("gpt-4", new_limits)

        updated = await e2e_limiter.get_resource_defaults("gpt-4")
        assert len(updated) == 1
        assert updated[0].name == "tpm"
        assert updated[0].capacity == 20_000

        # Step 4: DELETE
        await e2e_limiter.delete_resource_defaults("gpt-4")

        deleted = await e2e_limiter.get_resource_defaults("gpt-4")
        assert len(deleted) == 0

    @pytest.mark.asyncio(loop_scope="class")
    async def test_resource_config_isolation(self, e2e_limiter):
        """
        Test that resource configs are isolated from each other.

        Workflow:
        1. Set different defaults for different resources
        2. Verify each resource has its own config
        3. Delete one resource config
        4. Verify other resource unaffected
        """
        # Step 1: Set different defaults
        await e2e_limiter.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 100)])
        await e2e_limiter.set_resource_defaults("claude-3", [Limit.per_minute("rpm", 200)])

        # Step 2: Verify isolation
        gpt4_defaults = await e2e_limiter.get_resource_defaults("gpt-4")
        claude_defaults = await e2e_limiter.get_resource_defaults("claude-3")

        assert gpt4_defaults[0].capacity == 100
        assert claude_defaults[0].capacity == 200

        # Step 3: Delete one config
        await e2e_limiter.delete_resource_defaults("gpt-4")

        # Step 4: Verify other unaffected
        claude_defaults_after = await e2e_limiter.get_resource_defaults("claude-3")
        assert claude_defaults_after[0].capacity == 200

        # Cleanup
        await e2e_limiter.delete_resource_defaults("claude-3")

    @pytest.mark.asyncio(loop_scope="class")
    async def test_resource_config_audit_trail(self, e2e_limiter):
        """
        Verify resource config changes use $RESOURCE:{name} audit entity (ADR-106).

        Workflow:
        1. Set resource defaults
        2. Query audit events for $RESOURCE:{resource}
        3. Verify audit events are recorded with correct entity_id
        """
        # Step 1: Set resource defaults
        await e2e_limiter.set_resource_defaults(
            "audit-test-resource",
            [Limit.per_minute("rpm", 100)],
        )

        # Step 2: Query audit events using special entity_id pattern
        events = await e2e_limiter.get_audit_events(
            entity_id="$RESOURCE:audit-test-resource",
            limit=10,
        )

        # Step 3: Verify audit events exist
        assert len(events) > 0, "Expected audit events for resource config change"
        assert events[0].action == "limits_set"

        # Cleanup
        await e2e_limiter.delete_resource_defaults("audit-test-resource")

    @pytest.mark.asyncio(loop_scope="class")
    async def test_list_resources_with_defaults(self, e2e_limiter):
        """
        Test listing resources with configured defaults.

        Workflow:
        1. Verify initially empty
        2. Add defaults for multiple resources
        3. Verify listing returns all
        4. Delete and verify list updates
        """
        # Step 1: Initially should be empty (or cleanup from previous tests)
        initial = await e2e_limiter.list_resources_with_defaults()

        # Step 2: Add defaults for multiple resources
        await e2e_limiter.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 100)])
        await e2e_limiter.set_resource_defaults("claude-3", [Limit.per_minute("rpm", 200)])
        await e2e_limiter.set_resource_defaults("gemini-pro", [Limit.per_minute("rpm", 150)])

        # Step 3: Verify listing
        resources = await e2e_limiter.list_resources_with_defaults()
        assert "gpt-4" in resources
        assert "claude-3" in resources
        assert "gemini-pro" in resources
        assert len(resources) >= len(initial) + 3

        # Step 4: Delete one and verify
        await e2e_limiter.delete_resource_defaults("gemini-pro")
        updated = await e2e_limiter.list_resources_with_defaults()
        assert "gemini-pro" not in updated
        assert "gpt-4" in updated

        # Cleanup
        await e2e_limiter.delete_resource_defaults("gpt-4")
        await e2e_limiter.delete_resource_defaults("claude-3")


class TestE2ESystemConfigStorage:
    """E2E tests for system-level config storage."""

    @pytest_asyncio.fixture(scope="class", loop_scope="class")
    async def e2e_limiter(self, shared_minimal_stack, unique_name_class):
        """Namespace-scoped limiter on shared minimal stack for system config tests."""
        ns = f"sys-{unique_name_class}"
        repo = Repository(
            name=shared_minimal_stack.name,
            endpoint_url=shared_minimal_stack.endpoint_url,
            region=shared_minimal_stack.region,
        )
        await repo.register_namespace(ns)
        scoped = await repo.namespace(ns)
        limiter = RateLimiter(repository=scoped)
        async with limiter:
            yield limiter
        await repo.close()

    @pytest.mark.asyncio(loop_scope="class")
    async def test_system_config_crud(self, e2e_limiter):
        """
        Test system config CRUD operations.

        Workflow:
        1. CREATE: Set system-wide defaults
        2. READ: Verify defaults are stored
        3. UPDATE: Replace with new values
        4. DELETE: Remove defaults
        """
        # Step 1: CREATE
        limits = [
            Limit.per_minute("rpm", 50),
            Limit.per_minute("tpm", 5_000),
        ]
        await e2e_limiter.set_system_defaults(limits)

        # Step 2: READ
        retrieved, on_unavailable = await e2e_limiter.get_system_defaults()
        assert len(retrieved) == 2
        names = {limit.name for limit in retrieved}
        assert names == {"rpm", "tpm"}

        # Verify capacity values
        rpm_limit = next(lim for lim in retrieved if lim.name == "rpm")
        assert rpm_limit.capacity == 50

        # Step 3: UPDATE (replace)
        new_limits = [Limit.per_minute("tpm", 10_000)]
        await e2e_limiter.set_system_defaults(new_limits)

        updated, _ = await e2e_limiter.get_system_defaults()
        assert len(updated) == 1
        assert updated[0].name == "tpm"
        assert updated[0].capacity == 10_000

        # Step 4: DELETE
        await e2e_limiter.delete_system_defaults()

        deleted, _ = await e2e_limiter.get_system_defaults()
        assert len(deleted) == 0

    @pytest.mark.asyncio(loop_scope="class")
    async def test_system_config_with_on_unavailable(self, e2e_limiter):
        """
        Test system config with on_unavailable setting.

        Workflow:
        1. Set system defaults with on_unavailable=ALLOW
        2. Verify on_unavailable is stored
        3. Update to on_unavailable=BLOCK
        4. Cleanup
        """
        # Step 1: Set with on_unavailable
        limits = [Limit.per_minute("rpm", 50)]
        await e2e_limiter.set_system_defaults(limits, on_unavailable=OnUnavailable.ALLOW)

        # Step 2: Verify
        retrieved, on_unavailable = await e2e_limiter.get_system_defaults()
        assert len(retrieved) == 1
        assert on_unavailable == OnUnavailable.ALLOW

        # Step 3: Update to BLOCK
        await e2e_limiter.set_system_defaults(limits, on_unavailable=OnUnavailable.BLOCK)
        _, on_unavailable = await e2e_limiter.get_system_defaults()
        assert on_unavailable == OnUnavailable.BLOCK

        # Step 4: Cleanup
        await e2e_limiter.delete_system_defaults()

    @pytest.mark.asyncio(loop_scope="class")
    async def test_system_config_audit_trail(self, e2e_limiter):
        """
        Verify system config changes use $SYSTEM audit entity (ADR-106).

        Workflow:
        1. Set system defaults
        2. Query audit events for $SYSTEM
        3. Verify audit events are recorded with correct entity_id
        """
        # Step 1: Set system defaults
        await e2e_limiter.set_system_defaults(
            limits=[Limit.per_minute("rpm", 100)],
            on_unavailable=OnUnavailable.ALLOW,
        )

        # Step 2: Query audit events using special entity_id
        events = await e2e_limiter.get_audit_events(
            entity_id="$SYSTEM",
            limit=10,
        )

        # Step 3: Verify audit events exist
        assert len(events) > 0, "Expected audit events for system config change"
        assert events[0].action == "limits_set"

        # Cleanup
        await e2e_limiter.delete_system_defaults()


class TestE2EConfigCLIWorkflow:
    """E2E tests for config CLI commands.

    Note: CLI tests use synchronous fixtures because Click's CliRunner
    internally uses asyncio.run(), which cannot be called from within
    an already-running event loop.
    """

    @pytest.fixture
    def cli_runner(self):
        """Create a CLI runner."""
        return CliRunner()

    @pytest.fixture(scope="class")
    def e2e_limiter(self, localstack_endpoint, unique_name_class, minimal_stack_options):
        """Class-scoped sync limiter with own stack for CLI tests.

        CLI tests deploy their own stack because CLI commands operate on the
        default namespace and don't support --namespace for all operations.
        """
        from zae_limiter import SyncRateLimiter
        from zae_limiter.sync_repository import SyncRepository

        name = f"{unique_name_class}-cli"
        repo = SyncRepository(
            name=name,
            endpoint_url=localstack_endpoint,
            region="us-east-1",
            stack_options=minimal_stack_options,
        )
        limiter = SyncRateLimiter(repository=repo)

        with limiter:
            yield limiter

        try:
            repo.delete_stack()
        except Exception as e:
            warnings.warn(f"Stack cleanup failed: {e}", ResourceWarning, stacklevel=2)

    def test_resource_config_cli_workflow(self, e2e_limiter, cli_runner, localstack_endpoint):
        """
        Test resource config CLI workflow.

        Workflow:
        1. Set resource defaults via CLI
        2. Get and verify via CLI
        3. List resources via CLI
        4. Delete via CLI
        """
        # Get the limiter name for CLI commands
        short_name = e2e_limiter._repository.stack_name

        # Step 1: Set resource defaults
        result = cli_runner.invoke(
            cli,
            [
                "resource",
                "set-defaults",
                "gpt-4",
                "--name",
                short_name,
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
        assert result.exit_code == 0, f"CLI failed: {result.output}"
        assert "Set 2 default(s) for resource 'gpt-4'" in result.output

        # Step 2: Get and verify
        result = cli_runner.invoke(
            cli,
            [
                "resource",
                "get-defaults",
                "gpt-4",
                "--name",
                short_name,
                "--endpoint-url",
                localstack_endpoint,
                "--region",
                "us-east-1",
            ],
        )
        assert result.exit_code == 0, f"CLI failed: {result.output}"
        assert "rpm" in result.output
        assert "tpm" in result.output

        # Step 3: List resources
        result = cli_runner.invoke(
            cli,
            [
                "resource",
                "list",
                "--name",
                short_name,
                "--endpoint-url",
                localstack_endpoint,
                "--region",
                "us-east-1",
            ],
        )
        assert result.exit_code == 0, f"CLI failed: {result.output}"
        assert "gpt-4" in result.output

        # Step 4: Delete
        result = cli_runner.invoke(
            cli,
            [
                "resource",
                "delete-defaults",
                "gpt-4",
                "--name",
                short_name,
                "--endpoint-url",
                localstack_endpoint,
                "--region",
                "us-east-1",
                "--yes",
            ],
        )
        assert result.exit_code == 0, f"CLI failed: {result.output}"
        assert "Deleted defaults for resource 'gpt-4'" in result.output

    def test_system_config_cli_workflow(self, e2e_limiter, cli_runner, localstack_endpoint):
        """
        Test system config CLI workflow.

        Workflow:
        1. Set system defaults via CLI
        2. Get and verify via CLI
        3. Delete via CLI
        """
        # Get the limiter name for CLI commands
        short_name = e2e_limiter._repository.stack_name

        # Step 1: Set system defaults
        result = cli_runner.invoke(
            cli,
            [
                "system",
                "set-defaults",
                "--name",
                short_name,
                "--endpoint-url",
                localstack_endpoint,
                "--region",
                "us-east-1",
                "-l",
                "rpm:50",
                "-l",
                "tpm:5000",
                "--on-unavailable",
                "allow",
            ],
        )
        assert result.exit_code == 0, f"CLI failed: {result.output}"
        assert "Set 2 system-wide default(s)" in result.output
        assert "on_unavailable: allow" in result.output

        # Step 2: Get and verify
        result = cli_runner.invoke(
            cli,
            [
                "system",
                "get-defaults",
                "--name",
                short_name,
                "--endpoint-url",
                localstack_endpoint,
                "--region",
                "us-east-1",
            ],
        )
        assert result.exit_code == 0, f"CLI failed: {result.output}"
        assert "System-wide defaults" in result.output
        assert "rpm" in result.output
        assert "on_unavailable: allow" in result.output

        # Step 3: Delete
        result = cli_runner.invoke(
            cli,
            [
                "system",
                "delete-defaults",
                "--name",
                short_name,
                "--endpoint-url",
                localstack_endpoint,
                "--region",
                "us-east-1",
                "--yes",
            ],
        )
        assert result.exit_code == 0, f"CLI failed: {result.output}"
        assert "Deleted all system-wide defaults" in result.output

    def test_entity_config_cli_workflow(self, e2e_limiter, cli_runner, localstack_endpoint):
        """
        Test entity config CLI workflow.

        Workflow:
        1. Set entity limits via CLI
        2. Get and verify via CLI
        3. Delete via CLI
        """
        # Get the limiter name for CLI commands
        short_name = e2e_limiter._repository.stack_name

        # Step 1: Set entity limits
        result = cli_runner.invoke(
            cli,
            [
                "entity",
                "set-limits",
                "user-123",
                "--resource",
                "gpt-4",
                "--name",
                short_name,
                "--endpoint-url",
                localstack_endpoint,
                "--region",
                "us-east-1",
                "-l",
                "rpm:1000",
                "-l",
                "tpm:100000",
            ],
        )
        assert result.exit_code == 0, f"CLI failed: {result.output}"
        assert "Set 2 limit(s) for entity 'user-123'" in result.output
        assert "gpt-4" in result.output

        # Step 2: Get and verify
        result = cli_runner.invoke(
            cli,
            [
                "entity",
                "get-limits",
                "user-123",
                "--resource",
                "gpt-4",
                "--name",
                short_name,
                "--endpoint-url",
                localstack_endpoint,
                "--region",
                "us-east-1",
            ],
        )
        assert result.exit_code == 0, f"CLI failed: {result.output}"
        assert "rpm" in result.output
        assert "tpm" in result.output

        # Step 3: Delete
        result = cli_runner.invoke(
            cli,
            [
                "entity",
                "delete-limits",
                "user-123",
                "--resource",
                "gpt-4",
                "--name",
                short_name,
                "--endpoint-url",
                localstack_endpoint,
                "--region",
                "us-east-1",
                "--yes",
            ],
        )
        assert result.exit_code == 0, f"CLI failed: {result.output}"
        assert "Deleted limits for entity 'user-123'" in result.output


class TestE2ESyncConfigStorage:
    """E2E tests for sync limiter config storage."""

    @pytest.fixture(scope="class")
    def sync_localstack_limiter(self, shared_minimal_stack, unique_name_class):
        """Namespace-scoped sync limiter on shared minimal stack for config tests."""
        from zae_limiter import SyncRateLimiter
        from zae_limiter.sync_repository import SyncRepository

        ns = f"sync-{unique_name_class}"
        repo = SyncRepository(
            name=shared_minimal_stack.name,
            endpoint_url=shared_minimal_stack.endpoint_url,
            region=shared_minimal_stack.region,
        )
        repo.register_namespace(ns)
        scoped = repo.namespace(ns)
        limiter = SyncRateLimiter(repository=scoped)
        with limiter:
            yield limiter
        repo.close()

    def test_sync_resource_config_workflow(self, sync_localstack_limiter):
        """
        Test resource config with SyncRateLimiter.

        Workflow:
        1. Set resource defaults
        2. Get resource defaults
        3. List resources
        4. Delete resource defaults
        """
        limiter = sync_localstack_limiter

        # Step 1: Set resource defaults
        limits = [Limit.per_minute("rpm", 100), Limit.per_minute("tpm", 10_000)]
        limiter.set_resource_defaults("gpt-4", limits)

        # Step 2: Get resource defaults
        retrieved = limiter.get_resource_defaults("gpt-4")
        assert len(retrieved) == 2
        names = {limit.name for limit in retrieved}
        assert names == {"rpm", "tpm"}

        # Step 3: List resources
        resources = limiter.list_resources_with_defaults()
        assert "gpt-4" in resources

        # Step 4: Delete resource defaults
        limiter.delete_resource_defaults("gpt-4")
        deleted = limiter.get_resource_defaults("gpt-4")
        assert len(deleted) == 0

    def test_sync_system_config_workflow(self, sync_localstack_limiter):
        """
        Test system config with SyncRateLimiter.

        Workflow:
        1. Set system defaults
        2. Get system defaults
        3. Verify on_unavailable
        4. Delete system defaults
        """
        limiter = sync_localstack_limiter

        # Step 1: Set system defaults with on_unavailable
        limits = [Limit.per_minute("rpm", 50)]
        limiter.set_system_defaults(limits, on_unavailable=OnUnavailable.ALLOW)

        # Step 2: Get system defaults
        retrieved, on_unavailable = limiter.get_system_defaults()
        assert len(retrieved) == 1
        assert retrieved[0].name == "rpm"

        # Step 3: Verify on_unavailable
        assert on_unavailable == OnUnavailable.ALLOW

        # Step 4: Delete system defaults
        limiter.delete_system_defaults()
        deleted, on_unavailable = limiter.get_system_defaults()
        assert len(deleted) == 0
        assert on_unavailable is None
