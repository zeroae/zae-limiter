"""End-to-end tests for namespace management using LocalStack.

Tests the full multi-tenant namespace lifecycle:
1. Register namespaces, create entities, verify isolation
2. Delete namespace, purge data, verify other namespaces unaffected
3. CLI namespace commands end-to-end

To run these tests locally:
    # Start LocalStack
    zae-limiter local up

    # Set environment variables and run tests
    export AWS_ENDPOINT_URL=http://localhost:4566
    export AWS_ACCESS_KEY_ID=test
    export AWS_SECRET_ACCESS_KEY=test
    export AWS_DEFAULT_REGION=us-east-1
    pytest tests/e2e/test_namespace.py -v
"""

import re

import pytest
import pytest_asyncio
from click.testing import CliRunner

from zae_limiter import (
    Limit,
    RateLimiter,
    Repository,
    schema,
)
from zae_limiter.cli import cli
from zae_limiter.sync_repository import SyncRepository

pytestmark = [pytest.mark.integration, pytest.mark.e2e]


class TestE2ENamespaceMultiTenantLifecycle:
    """E2E multi-tenant lifecycle: register, isolate, delete, purge.

    Covers AC #1: register ns-a and ns-b, create entities in each,
    verify isolation, delete ns-a, verify cleanup, verify ns-b unaffected.
    """

    @pytest_asyncio.fixture(scope="class", loop_scope="class")
    async def multi_tenant_repos(self, shared_minimal_stack, unique_name_class):
        """Create scoped repos for unique ns-a and ns-b on shared stack."""
        suffix = unique_name_class
        repo = await Repository.connect(
            shared_minimal_stack.name,
            shared_minimal_stack.region,
            endpoint_url=shared_minimal_stack.endpoint_url,
        )

        # Register test namespaces with unique suffix
        ns_a_name = f"ns-a-{suffix}"
        ns_b_name = f"ns-b-{suffix}"
        ns_a_id = await repo.register_namespace(ns_a_name)
        ns_b_id = await repo.register_namespace(ns_b_name)

        repo_a = await repo.namespace(ns_a_name)
        repo_b = await repo.namespace(ns_b_name)

        yield {
            "repo": repo,
            "repo_a": repo_a,
            "repo_b": repo_b,
            "ns_a_id": ns_a_id,
            "ns_b_id": ns_b_id,
            "ns_a_name": ns_a_name,
            "ns_b_name": ns_b_name,
        }

        await repo.close()

    @pytest.mark.asyncio(loop_scope="class")
    async def test_register_and_list_namespaces(self, multi_tenant_repos):
        """Registered namespaces appear in list."""
        repo = multi_tenant_repos["repo"]
        ns_a_name = multi_tenant_repos["ns_a_name"]
        ns_b_name = multi_tenant_repos["ns_b_name"]

        namespaces = await repo.list_namespaces()
        names = {ns["name"] for ns in namespaces}

        # "default" is auto-registered by builder, plus our test namespaces
        assert "default" in names
        assert ns_a_name in names
        assert ns_b_name in names

    @pytest.mark.asyncio(loop_scope="class")
    async def test_create_entities_in_namespaces(self, multi_tenant_repos):
        """Create entities in each namespace independently."""
        repo_a = multi_tenant_repos["repo_a"]
        repo_b = multi_tenant_repos["repo_b"]

        entity_a = await repo_a.create_entity("user-1", name="Alpha User")
        entity_b = await repo_b.create_entity("user-1", name="Beta User")

        assert entity_a.id == "user-1"
        assert entity_b.id == "user-1"

    @pytest.mark.asyncio(loop_scope="class")
    async def test_entity_isolation(self, multi_tenant_repos):
        """Entity created in ns-a is invisible from ns-b and vice versa."""
        repo_a = multi_tenant_repos["repo_a"]
        repo_b = multi_tenant_repos["repo_b"]

        # Create distinct entities in each namespace
        await repo_a.create_entity("alpha-only", name="Alpha Only")
        await repo_b.create_entity("beta-only", name="Beta Only")

        # alpha-only visible in ns-a, invisible in ns-b
        assert (await repo_a.get_entity("alpha-only")) is not None
        assert (await repo_b.get_entity("alpha-only")) is None

        # beta-only visible in ns-b, invisible in ns-a
        assert (await repo_b.get_entity("beta-only")) is not None
        assert (await repo_a.get_entity("beta-only")) is None

    @pytest.mark.asyncio(loop_scope="class")
    async def test_rate_limiting_isolation(self, multi_tenant_repos):
        """Token consumption in ns-a does not affect ns-b capacity."""
        repo_a = multi_tenant_repos["repo_a"]
        repo_b = multi_tenant_repos["repo_b"]

        limits = [Limit.per_minute("rpm", 100)]

        # Ensure entities exist
        if await repo_a.get_entity("rate-user") is None:
            await repo_a.create_entity("rate-user")
        if await repo_b.get_entity("rate-user") is None:
            await repo_b.create_entity("rate-user")

        limiter_a = RateLimiter(repository=repo_a)
        limiter_b = RateLimiter(repository=repo_b)

        # Consume 90 of 100 rpm in ns-a
        async with limiter_a.acquire(
            entity_id="rate-user",
            resource="api",
            limits=limits,
            consume={"rpm": 90},
        ):
            pass

        # ns-b should still have full capacity — 90 would fail if shared
        async with limiter_b.acquire(
            entity_id="rate-user",
            resource="api",
            limits=limits,
            consume={"rpm": 90},
        ):
            pass  # Should succeed — namespaces are isolated

    @pytest.mark.asyncio(loop_scope="class")
    async def test_config_isolation(self, multi_tenant_repos):
        """System defaults in ns-a do not bleed into ns-b."""
        repo_a = multi_tenant_repos["repo_a"]
        repo_b = multi_tenant_repos["repo_b"]

        # Set system defaults in ns-a only
        await repo_a.set_system_defaults(
            limits=[Limit.per_minute("rpm", 500)],
            on_unavailable="allow",
        )

        # ns-b should have no system defaults
        limits_b, on_unavailable_b = await repo_b.get_system_defaults()
        assert limits_b == []
        assert on_unavailable_b is None

        # ns-a should have the configured defaults
        limits_a, on_unavailable_a = await repo_a.get_system_defaults()
        assert len(limits_a) == 1
        assert limits_a[0].name == "rpm"
        assert on_unavailable_a == "allow"

    @pytest.mark.asyncio(loop_scope="class")
    async def test_delete_namespace_a(self, multi_tenant_repos):
        """Soft-deleting ns-a removes it from list but keeps ns-b."""
        repo = multi_tenant_repos["repo"]
        ns_a_name = multi_tenant_repos["ns_a_name"]
        ns_b_name = multi_tenant_repos["ns_b_name"]

        await repo.delete_namespace(ns_a_name)

        namespaces = await repo.list_namespaces()
        names = {ns["name"] for ns in namespaces}
        assert ns_a_name not in names
        assert ns_b_name in names

    @pytest.mark.asyncio(loop_scope="class")
    async def test_namespace_a_data_cleaned_after_purge(self, multi_tenant_repos):
        """Purging ns-a removes all data items via GSI4."""
        repo = multi_tenant_repos["repo"]
        ns_a_id = multi_tenant_repos["ns_a_id"]

        await repo.purge_namespace(ns_a_id)

        # Verify no items remain for the namespace via GSI4
        client = await repo._get_client()
        response = await client.query(
            TableName=repo.table_name,
            IndexName=schema.GSI4_NAME,
            KeyConditionExpression="GSI4PK = :pk",
            ExpressionAttributeValues={":pk": {"S": ns_a_id}},
        )
        assert len(response.get("Items", [])) == 0

    @pytest.mark.asyncio(loop_scope="class")
    async def test_namespace_b_unaffected_after_purge(self, multi_tenant_repos):
        """ns-b entities and rate limiting still work after ns-a purge."""
        repo_b = multi_tenant_repos["repo_b"]

        # Entities created earlier in ns-b should still exist
        entity = await repo_b.get_entity("user-1")
        assert entity is not None
        assert entity.name == "Beta User"

        entity = await repo_b.get_entity("beta-only")
        assert entity is not None

        # Rate limiting in ns-b should still work
        limits = [Limit.per_minute("rpm", 100)]
        if await repo_b.get_entity("post-purge-user") is None:
            await repo_b.create_entity("post-purge-user")

        limiter_b = RateLimiter(repository=repo_b)
        async with limiter_b.acquire(
            entity_id="post-purge-user",
            resource="api",
            limits=limits,
            consume={"rpm": 1},
        ) as lease:
            assert lease.consumed == {"rpm": 1}


class TestE2ENamespaceCLIWorkflow:
    """E2E test of CLI namespace commands.

    Covers AC #2: namespace register, namespace list, entity create
    --namespace, namespace delete end-to-end via CliRunner.

    Uses SyncRateLimiter because CLI uses asyncio.run() internally,
    which conflicts with pytest-asyncio's event loop.
    """

    @pytest.fixture
    def cli_runner(self):
        """Create Click CLI runner."""
        return CliRunner()

    def _cli_args(self, endpoint, name, *args):
        """Build common CLI args with --name and --endpoint-url."""
        return [
            *args,
            "--name",
            name,
            "--endpoint-url",
            endpoint,
            "--region",
            "us-east-1",
        ]

    def test_namespace_cli_lifecycle(self, cli_runner, localstack_endpoint, unique_name):
        """Complete CLI namespace lifecycle.

        Steps:
        1. Deploy stack
        2. Register namespaces ns-a and ns-b
        3. List namespaces — verify both shown
        4. Show ns-a details
        5. Create entity in ns-a via SyncRateLimiter, set limits via CLI
        6. Get limits via CLI — verify output
        7. Delete ns-a
        8. List — verify ns-a gone, ns-b present
        9. Orphans — verify ns-a shown
        10. Recover ns-a
        11. List — verify ns-a restored
        12. Delete stack
        """
        stack_name = unique_name

        try:
            # Step 1: Deploy stack via CLI
            result = cli_runner.invoke(
                cli,
                [
                    "deploy",
                    "--name",
                    stack_name,
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

            # Step 2: Register namespaces
            result = cli_runner.invoke(
                cli,
                [
                    "namespace",
                    "register",
                    "ns-a",
                    "ns-b",
                    *self._cli_args(localstack_endpoint, stack_name),
                ],
            )
            assert result.exit_code == 0, f"Register failed: {result.output}"
            assert "Registered 2 namespace(s)" in result.output
            assert "ns-a" in result.output
            assert "ns-b" in result.output

            # Step 3: List namespaces
            result = cli_runner.invoke(
                cli,
                [
                    "namespace",
                    "list",
                    *self._cli_args(localstack_endpoint, stack_name),
                ],
            )
            assert result.exit_code == 0, f"List failed: {result.output}"
            assert "ns-a" in result.output
            assert "ns-b" in result.output

            # Step 4: Show ns-a details
            result = cli_runner.invoke(
                cli,
                [
                    "namespace",
                    "show",
                    "ns-a",
                    *self._cli_args(localstack_endpoint, stack_name),
                ],
            )
            assert result.exit_code == 0, f"Show failed: {result.output}"
            assert "Namespace:    ns-a" in result.output
            assert "Namespace ID:" in result.output
            assert "Status:       active" in result.output

            # Extract ns-a ID for later recovery
            ns_a_id_match = re.search(r"Namespace ID: (\S+)", result.output)
            assert ns_a_id_match, f"Could not extract namespace ID from: {result.output}"
            ns_a_id = ns_a_id_match.group(1)

            # Step 5: Create entity in ns-a and set limits via CLI
            # Use builder (not connect) because CLI deploy doesn't register "default" namespace
            repo = SyncRepository.builder(
                stack_name, "us-east-1", endpoint_url=localstack_endpoint
            ).build()
            try:
                repo_a = repo.namespace("ns-a")
                repo_a.create_entity("cli-user", name="CLI User")
            finally:
                repo.close()

            result = cli_runner.invoke(
                cli,
                [
                    "entity",
                    "set-limits",
                    "cli-user",
                    "--resource",
                    "api",
                    "-l",
                    "rpm:1000",
                    "--namespace",
                    "ns-a",
                    *self._cli_args(localstack_endpoint, stack_name),
                ],
            )
            assert result.exit_code == 0, f"Set limits failed: {result.output}"

            # Step 6: Get limits via CLI
            result = cli_runner.invoke(
                cli,
                [
                    "entity",
                    "get-limits",
                    "cli-user",
                    "--resource",
                    "api",
                    "--namespace",
                    "ns-a",
                    *self._cli_args(localstack_endpoint, stack_name),
                ],
            )
            assert result.exit_code == 0, f"Get limits failed: {result.output}"
            assert "rpm" in result.output

            # Step 7: Delete ns-a
            result = cli_runner.invoke(
                cli,
                [
                    "namespace",
                    "delete",
                    "ns-a",
                    "--yes",
                    *self._cli_args(localstack_endpoint, stack_name),
                ],
            )
            assert result.exit_code == 0, f"Delete failed: {result.output}"
            assert "deleted" in result.output.lower()

            # Step 8: List — ns-a gone, ns-b present
            result = cli_runner.invoke(
                cli,
                [
                    "namespace",
                    "list",
                    *self._cli_args(localstack_endpoint, stack_name),
                ],
            )
            assert result.exit_code == 0, f"List failed: {result.output}"
            assert "ns-b" in result.output
            # ns-a should not appear in active namespace list
            # Split by lines and check "ns-a" doesn't appear as a namespace name
            # (it may appear in the "Total:" line or elsewhere, but not as a row)
            lines = result.output.strip().split("\n")
            ns_a_in_table = any(
                "ns-a" in line and "ns-b" not in line
                for line in lines
                if not line.startswith("Total:")
            )
            assert not ns_a_in_table, f"ns-a still in list: {result.output}"

            # Step 9: Orphans — ns-a shown
            result = cli_runner.invoke(
                cli,
                [
                    "namespace",
                    "orphans",
                    *self._cli_args(localstack_endpoint, stack_name),
                ],
            )
            assert result.exit_code == 0, f"Orphans failed: {result.output}"
            assert ns_a_id in result.output

            # Step 10: Recover ns-a
            result = cli_runner.invoke(
                cli,
                [
                    "namespace",
                    "recover",
                    ns_a_id,
                    *self._cli_args(localstack_endpoint, stack_name),
                ],
            )
            assert result.exit_code == 0, f"Recover failed: {result.output}"
            assert "Recovered" in result.output

            # Step 11: List — ns-a restored
            result = cli_runner.invoke(
                cli,
                [
                    "namespace",
                    "list",
                    *self._cli_args(localstack_endpoint, stack_name),
                ],
            )
            assert result.exit_code == 0, f"List failed: {result.output}"
            assert "ns-a" in result.output
            assert "ns-b" in result.output

        finally:
            # Step 12: Delete stack
            cli_runner.invoke(
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
