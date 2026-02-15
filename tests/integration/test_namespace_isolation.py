"""Integration tests for namespace isolation using LocalStack.

Verifies that entities created in one namespace are invisible from
another namespace, using RepositoryBuilder with different namespaces.

To run:
    export AWS_ENDPOINT_URL=http://localhost:4566
    export AWS_ACCESS_KEY_ID=test
    export AWS_SECRET_ACCESS_KEY=test
    export AWS_DEFAULT_REGION=us-east-1
    pytest tests/integration/test_namespace_isolation.py -v
"""

import pytest

from zae_limiter import Limit, RateLimiter
from zae_limiter.repository import Repository

pytestmark = pytest.mark.integration


@pytest.fixture
async def base_repo(localstack_endpoint, unique_name):
    """Repository with table and two registered namespaces."""
    repo = Repository(
        name=unique_name,
        endpoint_url=localstack_endpoint,
        region="us-east-1",
    )
    await repo.create_table()
    await repo._register_namespace("default")
    await repo._register_namespace("tenant-alpha")
    await repo._register_namespace("tenant-beta")
    yield repo
    try:
        await repo.delete_table()
    except Exception:
        pass
    await repo.close()


class TestNamespaceIsolation:
    """Verify that entities in one namespace are invisible from another."""

    @pytest.mark.asyncio
    async def test_entity_invisible_across_namespaces(self, base_repo):
        """Entity created in namespace A is not found in namespace B."""
        repo_a = await base_repo.namespace("tenant-alpha")
        repo_b = await base_repo.namespace("tenant-beta")

        # Create entity in tenant-alpha
        await repo_a.create_entity(entity_id="user-1", name="Alpha User")

        # Entity exists in tenant-alpha
        entity = await repo_a.get_entity("user-1")
        assert entity is not None
        assert entity.name == "Alpha User"

        # Entity does NOT exist in tenant-beta
        entity = await repo_b.get_entity("user-1")
        assert entity is None

    @pytest.mark.asyncio
    async def test_same_entity_id_different_namespaces(self, base_repo):
        """Same entity_id in different namespaces are independent."""
        repo_a = await base_repo.namespace("tenant-alpha")
        repo_b = await base_repo.namespace("tenant-beta")

        # Create entity with same ID in both namespaces
        await repo_a.create_entity(entity_id="shared-id", name="Alpha Entity")
        await repo_b.create_entity(entity_id="shared-id", name="Beta Entity")

        # Each namespace sees its own entity
        entity_a = await repo_a.get_entity("shared-id")
        entity_b = await repo_b.get_entity("shared-id")

        assert entity_a is not None
        assert entity_b is not None
        assert entity_a.name == "Alpha Entity"
        assert entity_b.name == "Beta Entity"

    @pytest.mark.asyncio
    async def test_buckets_isolated_across_namespaces(self, base_repo):
        """Buckets are namespace-scoped — acquire in A doesn't affect B."""
        repo_a = await base_repo.namespace("tenant-alpha")
        repo_b = await base_repo.namespace("tenant-beta")

        limits = [Limit.per_minute("rpm", 100)]

        # Create entities and buckets in both namespaces
        await repo_a.create_entity("user-1")
        await repo_b.create_entity("user-1")

        limiter_a = RateLimiter(repository=repo_a)
        limiter_b = RateLimiter(repository=repo_b)

        # Consume most tokens in tenant-alpha (90 of 100 rpm)
        async with limiter_a.acquire(
            entity_id="user-1",
            resource="api",
            limits=limits,
            consume={"rpm": 90},
        ):
            pass

        # Tenant-beta should still have full capacity (no consumption)
        # This acquire of 90 would fail if namespaces shared buckets
        async with limiter_b.acquire(
            entity_id="user-1",
            resource="api",
            limits=limits,
            consume={"rpm": 90},
        ):
            pass  # Should succeed — namespaces are isolated

    @pytest.mark.asyncio
    async def test_config_isolated_across_namespaces(self, base_repo):
        """System defaults are namespace-scoped."""
        repo_a = await base_repo.namespace("tenant-alpha")
        repo_b = await base_repo.namespace("tenant-beta")

        # Set system defaults in tenant-alpha
        await repo_a.set_system_defaults(
            limits=[Limit.per_minute("rpm", 500)],
            on_unavailable="allow",
        )

        # Tenant-beta should have no system defaults
        limits_b, on_unavailable_b = await repo_b.get_system_defaults()
        assert limits_b == []
        assert on_unavailable_b is None

        # Tenant-alpha should have the configured defaults
        limits_a, on_unavailable_a = await repo_a.get_system_defaults()
        assert len(limits_a) == 1
        assert limits_a[0].name == "rpm"
        assert on_unavailable_a == "allow"

    @pytest.mark.asyncio
    async def test_delete_entity_scoped_to_namespace(self, base_repo):
        """Deleting entity in namespace A does not affect namespace B."""
        repo_a = await base_repo.namespace("tenant-alpha")
        repo_b = await base_repo.namespace("tenant-beta")

        await repo_a.create_entity("user-del", name="Alpha Del")
        await repo_b.create_entity("user-del", name="Beta Del")

        # Delete in tenant-alpha
        await repo_a.delete_entity("user-del")

        # Gone from alpha
        assert await repo_a.get_entity("user-del") is None

        # Still exists in beta
        entity_b = await repo_b.get_entity("user-del")
        assert entity_b is not None
        assert entity_b.name == "Beta Del"


class TestNamespaceIsolationViaBuilder:
    """Verify namespace isolation using RepositoryBuilder."""

    @pytest.mark.asyncio
    async def test_builder_creates_isolated_namespaces(self, localstack_endpoint, unique_name):
        """RepositoryBuilder with different namespaces produces isolated repos."""
        # First build with default namespace to create infrastructure
        # and register the additional namespaces
        repo_default = await (
            Repository.builder(unique_name, "us-east-1", endpoint_url=localstack_endpoint)
            .enable_aggregator(False)
            .enable_alarms(False)
            .build()
        )
        await repo_default.register_namespace("tenant-alpha")
        await repo_default.register_namespace("tenant-beta")

        # Build repo for tenant-alpha
        repo_a = await (
            Repository.builder(unique_name, "us-east-1", endpoint_url=localstack_endpoint)
            .namespace("tenant-alpha")
            .build()
        )

        # Build repo for tenant-beta (reuses existing stack)
        repo_b = await (
            Repository.builder(unique_name, "us-east-1", endpoint_url=localstack_endpoint)
            .namespace("tenant-beta")
            .build()
        )

        try:
            # Create entity in tenant-alpha
            await repo_a.create_entity("builder-user", name="Alpha Builder User")

            # Visible in alpha
            entity_a = await repo_a.get_entity("builder-user")
            assert entity_a is not None
            assert entity_a.name == "Alpha Builder User"

            # Invisible in beta
            entity_b = await repo_b.get_entity("builder-user")
            assert entity_b is None
        finally:
            try:
                await repo_default.delete_stack()
            except Exception as e:
                print(f"Warning: Stack cleanup failed: {e}")
            await repo_default.close()
            await repo_a.close()
            await repo_b.close()
