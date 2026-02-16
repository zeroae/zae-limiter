"""Unit tests for namespace scoping (repo.namespace())."""

import pytest

from zae_limiter.exceptions import NamespaceNotFoundError
from zae_limiter.repository import Repository


@pytest.fixture
async def repo(mock_dynamodb):
    """Repository with table created and default namespace registered."""
    repo = Repository(name="test-ns-scope", region="us-east-1", _skip_deprecation_warning=True)
    await repo.create_table()
    # Register default and a test namespace
    await repo._register_namespace("default")
    await repo._register_namespace("tenant-a")
    await repo._register_namespace("tenant-b")
    yield repo
    await repo.close()


class TestNamespaceScopedRepo:
    """Test repo.namespace() returns a properly scoped repository."""

    @pytest.mark.asyncio
    async def test_namespace_returns_scoped_repo(self, repo):
        """namespace() returns a Repository with the resolved namespace."""
        scoped = await repo.namespace("tenant-a")
        assert isinstance(scoped, Repository)
        assert scoped.namespace_name == "tenant-a"
        assert scoped.namespace_id != "default"
        assert len(scoped.namespace_id) == 11  # token_urlsafe(8)

    @pytest.mark.asyncio
    async def test_namespace_shares_client(self, repo):
        """Scoped repo shares the DynamoDB client with the parent."""
        # Ensure client is initialized
        await repo._get_client()
        scoped = await repo.namespace("tenant-a")
        assert scoped._client is repo._client
        assert scoped._session is repo._session

    @pytest.mark.asyncio
    async def test_namespace_shares_entity_cache(self, repo):
        """Scoped repo shares entity cache with parent."""
        scoped = await repo.namespace("tenant-a")
        assert scoped._entity_cache is repo._entity_cache

    @pytest.mark.asyncio
    async def test_namespace_shares_namespace_cache(self, repo):
        """Scoped repo shares namespace cache with parent."""
        scoped = await repo.namespace("tenant-a")
        assert scoped._namespace_cache is repo._namespace_cache

    @pytest.mark.asyncio
    async def test_namespace_caches_resolved_ids(self, repo):
        """Resolved namespace IDs are cached for subsequent calls."""
        scoped1 = await repo.namespace("tenant-a")
        scoped2 = await repo.namespace("tenant-a")
        assert scoped1.namespace_id == scoped2.namespace_id

    @pytest.mark.asyncio
    async def test_namespace_raises_not_found(self, repo):
        """namespace() raises NamespaceNotFoundError for unregistered names."""
        with pytest.raises(NamespaceNotFoundError) as exc_info:
            await repo.namespace("nonexistent")
        assert exc_info.value.namespace_name == "nonexistent"

    @pytest.mark.asyncio
    async def test_scoped_close_does_not_close_parent_client(self, repo):
        """close() on a scoped repo is a no-op."""
        await repo._get_client()
        scoped = await repo.namespace("tenant-a")
        await scoped.close()
        # Parent client should still be usable
        assert repo._client is not None
        # Verify by making a real DynamoDB call
        await repo.ping()

    @pytest.mark.asyncio
    async def test_scoped_repo_has_own_config_cache(self, repo):
        """Each scoped repo gets its own ConfigCache instance."""
        scoped_a = await repo.namespace("tenant-a")
        scoped_b = await repo.namespace("tenant-b")
        assert scoped_a._config_cache is not repo._config_cache
        assert scoped_a._config_cache is not scoped_b._config_cache
        assert scoped_a._config_cache.namespace_id == scoped_a.namespace_id
        assert scoped_b._config_cache.namespace_id == scoped_b.namespace_id

    @pytest.mark.asyncio
    async def test_scoped_repo_no_stack_options(self, repo):
        """Scoped repos don't manage infrastructure."""
        scoped = await repo.namespace("tenant-a")
        assert scoped._stack_options is None

    @pytest.mark.asyncio
    async def test_scoped_repo_preserves_table_name(self, repo):
        """Scoped repos use the same table."""
        scoped = await repo.namespace("tenant-a")
        assert scoped.table_name == repo.table_name
        assert scoped.stack_name == repo.stack_name

    @pytest.mark.asyncio
    async def test_scoped_repo_on_unavailable(self, repo):
        """namespace() with on_unavailable persists to system config."""
        scoped = await repo.namespace("tenant-a", on_unavailable="allow")
        _, on_unavailable = await scoped.get_system_defaults()
        assert on_unavailable == "allow"

    @pytest.mark.asyncio
    async def test_scoped_repo_custom_bucket_ttl(self, repo):
        """namespace() accepts bucket_ttl_multiplier override."""
        scoped = await repo.namespace("tenant-a", bucket_ttl_multiplier=14)
        assert scoped._bucket_ttl_refill_multiplier == 14

    @pytest.mark.asyncio
    async def test_scoped_repo_inherits_bucket_ttl(self, repo):
        """namespace() inherits parent's bucket_ttl_multiplier by default."""
        repo._bucket_ttl_refill_multiplier = 21
        scoped = await repo.namespace("tenant-a")
        assert scoped._bucket_ttl_refill_multiplier == 21


class TestNamespaceScopedOperations:
    """Test that entity operations use the scoped namespace."""

    @pytest.mark.asyncio
    async def test_entity_operations_scoped_to_namespace(self, repo):
        """Entities created in a scoped repo are namespace-isolated."""
        from zae_limiter import schema

        scoped_a = await repo.namespace("tenant-a")
        scoped_b = await repo.namespace("tenant-b")

        # Create entity in tenant-a
        await scoped_a.create_entity(entity_id="user-1", name="User 1")

        # Entity should be visible in tenant-a's namespace
        client = await repo._get_client()
        response = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_entity(scoped_a.namespace_id, "user-1")},
                "SK": {"S": schema.sk_meta()},
            },
        )
        assert "Item" in response

        # Entity should NOT be visible in tenant-b's namespace
        response = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_entity(scoped_b.namespace_id, "user-1")},
                "SK": {"S": schema.sk_meta()},
            },
        )
        assert "Item" not in response
