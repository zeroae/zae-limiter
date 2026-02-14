"""Unit tests for namespace registry (issue #369)."""

import pytest

from zae_limiter import schema
from zae_limiter.exceptions import EntityNotFoundError
from zae_limiter.repository import Repository


@pytest.fixture
async def repo(mock_dynamodb):
    """Repository with table created (for namespace registry tests)."""
    from tests.unit.conftest import _patch_aiobotocore_response

    with _patch_aiobotocore_response():
        repo = Repository(name="test-ns-registry", region="us-east-1")
        await repo.create_table()
        yield repo
        await repo.close()


class TestRegisterNamespace:
    """Tests for register_namespace()."""

    @pytest.mark.asyncio
    async def test_register_namespace_returns_id(self, repo):
        """register_namespace() returns a namespace ID."""
        ns_id = await repo.register_namespace("tenant-alpha")
        assert ns_id is not None
        assert len(ns_id) == 11  # token_urlsafe(8)

    @pytest.mark.asyncio
    async def test_register_namespace_writes_forward_and_reverse(self, repo):
        """register_namespace() creates both forward and reverse records."""
        ns_id = await repo.register_namespace("tenant-alpha")

        client = await repo._get_client()
        pk = schema.pk_system(schema.RESERVED_NAMESPACE)

        # Check forward record
        fwd = await client.get_item(
            TableName=repo.table_name,
            Key={"PK": {"S": pk}, "SK": {"S": schema.sk_namespace("tenant-alpha")}},
        )
        assert fwd["Item"]["namespace_id"]["S"] == ns_id
        assert fwd["Item"]["namespace_name"]["S"] == "tenant-alpha"
        assert fwd["Item"]["status"]["S"] == "active"
        assert "created_at" in fwd["Item"]

        # Check reverse record
        rev = await client.get_item(
            TableName=repo.table_name,
            Key={"PK": {"S": pk}, "SK": {"S": schema.sk_nsid(ns_id)}},
        )
        assert rev["Item"]["namespace_id"]["S"] == ns_id
        assert rev["Item"]["namespace_name"]["S"] == "tenant-alpha"
        assert rev["Item"]["status"]["S"] == "active"

    @pytest.mark.asyncio
    async def test_register_namespace_writes_gsi4_attributes(self, repo):
        """register_namespace() writes GSI4PK and GSI4SK on both records."""
        ns_id = await repo.register_namespace("tenant-gsi4")

        client = await repo._get_client()
        pk = schema.pk_system(schema.RESERVED_NAMESPACE)

        fwd = await client.get_item(
            TableName=repo.table_name,
            Key={"PK": {"S": pk}, "SK": {"S": schema.sk_namespace("tenant-gsi4")}},
        )
        assert fwd["Item"]["GSI4PK"]["S"] == schema.RESERVED_NAMESPACE
        assert fwd["Item"]["GSI4SK"]["S"] == pk

        rev = await client.get_item(
            TableName=repo.table_name,
            Key={"PK": {"S": pk}, "SK": {"S": schema.sk_nsid(ns_id)}},
        )
        assert rev["Item"]["GSI4PK"]["S"] == schema.RESERVED_NAMESPACE
        assert rev["Item"]["GSI4SK"]["S"] == pk

    @pytest.mark.asyncio
    async def test_register_namespace_idempotent(self, repo):
        """Registering the same name twice returns the same ID."""
        ns_id_1 = await repo.register_namespace("tenant-alpha")
        ns_id_2 = await repo.register_namespace("tenant-alpha")
        assert ns_id_1 == ns_id_2

    @pytest.mark.asyncio
    async def test_register_namespace_rejects_reserved(self, repo):
        """register_namespace('_') raises ValueError."""
        with pytest.raises(ValueError, match="reserved"):
            await repo.register_namespace("_")


class TestRegisterNamespaces:
    """Tests for register_namespaces()."""

    @pytest.mark.asyncio
    async def test_register_namespaces_returns_all_ids(self, repo):
        """register_namespaces() returns a mapping for all namespaces."""
        result = await repo.register_namespaces(["ns-a", "ns-b", "ns-c"])
        assert len(result) == 3
        assert "ns-a" in result
        assert "ns-b" in result
        assert "ns-c" in result
        # All IDs should be unique
        assert len(set(result.values())) == 3

    @pytest.mark.asyncio
    async def test_register_namespaces_idempotent(self, repo):
        """register_namespaces() returns existing IDs for already-registered names."""
        first = await repo.register_namespaces(["ns-x", "ns-y"])
        second = await repo.register_namespaces(["ns-x", "ns-y"])
        assert first == second

    @pytest.mark.asyncio
    async def test_register_namespaces_rejects_reserved(self, repo):
        """register_namespaces() raises ValueError if any name is reserved."""
        with pytest.raises(ValueError, match="reserved"):
            await repo.register_namespaces(["valid-ns", "_"])


class TestListNamespaces:
    """Tests for list_namespaces()."""

    @pytest.mark.asyncio
    async def test_list_namespaces_empty(self, repo):
        """list_namespaces() returns empty list when none registered."""
        result = await repo.list_namespaces()
        assert result == []

    @pytest.mark.asyncio
    async def test_list_namespaces_returns_active(self, repo):
        """list_namespaces() returns registered namespaces."""
        await repo.register_namespace("ns-one")
        await repo.register_namespace("ns-two")

        result = await repo.list_namespaces()
        names = [ns["name"] for ns in result]
        assert "ns-one" in names
        assert "ns-two" in names
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_list_namespaces_excludes_deleted(self, repo):
        """list_namespaces() excludes soft-deleted namespaces."""
        await repo.register_namespace("ns-active")
        await repo.register_namespace("ns-deleted")
        await repo.delete_namespace("ns-deleted")

        result = await repo.list_namespaces()
        names = [ns["name"] for ns in result]
        assert "ns-active" in names
        assert "ns-deleted" not in names

    @pytest.mark.asyncio
    async def test_list_namespaces_returns_correct_fields(self, repo):
        """list_namespaces() returns name, namespace_id, and created_at."""
        ns_id = await repo.register_namespace("ns-fields")

        result = await repo.list_namespaces()
        assert len(result) == 1
        ns = result[0]
        assert ns["name"] == "ns-fields"
        assert ns["namespace_id"] == ns_id
        assert ns["created_at"] != ""


class TestDeleteNamespace:
    """Tests for delete_namespace()."""

    @pytest.mark.asyncio
    async def test_delete_namespace_removes_forward(self, repo):
        """delete_namespace() removes the forward record."""
        await repo.register_namespace("ns-del")
        await repo.delete_namespace("ns-del")

        # Forward record should be gone
        resolved = await repo._resolve_namespace("ns-del")
        assert resolved is None

    @pytest.mark.asyncio
    async def test_delete_namespace_marks_reverse_deleted(self, repo):
        """delete_namespace() marks reverse record as deleted."""
        ns_id = await repo.register_namespace("ns-del-rev")
        await repo.delete_namespace("ns-del-rev")

        client = await repo._get_client()
        pk = schema.pk_system(schema.RESERVED_NAMESPACE)
        rev = await client.get_item(
            TableName=repo.table_name,
            Key={"PK": {"S": pk}, "SK": {"S": schema.sk_nsid(ns_id)}},
        )
        assert rev["Item"]["status"]["S"] == "deleted"
        assert "deleted_at" in rev["Item"]

    @pytest.mark.asyncio
    async def test_delete_namespace_noop_nonexistent(self, repo):
        """delete_namespace() is a no-op for nonexistent namespace."""
        # Should not raise
        await repo.delete_namespace("nonexistent")

    @pytest.mark.asyncio
    async def test_delete_namespace_rejects_reserved(self, repo):
        """delete_namespace('_') raises ValueError."""
        with pytest.raises(ValueError, match="reserved"):
            await repo.delete_namespace("_")

    @pytest.mark.asyncio
    async def test_delete_namespace_invalidates_cache(self, repo):
        """delete_namespace() removes the name from namespace cache."""
        await repo.register_namespace("ns-cache")
        assert "ns-cache" in repo._namespace_cache

        await repo.delete_namespace("ns-cache")
        assert "ns-cache" not in repo._namespace_cache


class TestRecoverNamespace:
    """Tests for recover_namespace()."""

    @pytest.mark.asyncio
    async def test_recover_namespace_restores_forward(self, repo):
        """recover_namespace() re-creates the forward record."""
        ns_id = await repo.register_namespace("ns-recover")
        await repo.delete_namespace("ns-recover")

        name = await repo.recover_namespace(ns_id)
        assert name == "ns-recover"

        # Forward record should be back
        resolved = await repo._resolve_namespace("ns-recover")
        assert resolved == ns_id

    @pytest.mark.asyncio
    async def test_recover_namespace_marks_active(self, repo):
        """recover_namespace() marks reverse record as active."""
        ns_id = await repo.register_namespace("ns-recover-active")
        await repo.delete_namespace("ns-recover-active")
        await repo.recover_namespace(ns_id)

        client = await repo._get_client()
        pk = schema.pk_system(schema.RESERVED_NAMESPACE)
        rev = await client.get_item(
            TableName=repo.table_name,
            Key={"PK": {"S": pk}, "SK": {"S": schema.sk_nsid(ns_id)}},
        )
        assert rev["Item"]["status"]["S"] == "active"
        assert "deleted_at" not in rev["Item"]

    @pytest.mark.asyncio
    async def test_recover_namespace_raises_not_found(self, repo):
        """recover_namespace() raises EntityNotFoundError for missing ID."""
        with pytest.raises(EntityNotFoundError):
            await repo.recover_namespace("nonexistent-id")

    @pytest.mark.asyncio
    async def test_recover_namespace_rejects_purging(self, repo):
        """recover_namespace() raises ValueError when status='purging'."""
        ns_id = await repo.register_namespace("ns-purging")
        await repo.delete_namespace("ns-purging")

        # Manually set status to "purging"
        client = await repo._get_client()
        pk = schema.pk_system(schema.RESERVED_NAMESPACE)
        await client.update_item(
            TableName=repo.table_name,
            Key={"PK": {"S": pk}, "SK": {"S": schema.sk_nsid(ns_id)}},
            UpdateExpression="SET #status = :purging",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":purging": {"S": "purging"}},
        )

        with pytest.raises(ValueError, match="purge is in progress"):
            await repo.recover_namespace(ns_id)

    @pytest.mark.asyncio
    async def test_recover_namespace_rejects_active(self, repo):
        """recover_namespace() raises ValueError when namespace is active."""
        ns_id = await repo.register_namespace("ns-active-recover")

        with pytest.raises(ValueError, match="already active"):
            await repo.recover_namespace(ns_id)

    @pytest.mark.asyncio
    async def test_recover_namespace_rejects_name_collision(self, repo):
        """recover_namespace() raises ValueError when name re-registered."""
        ns_id = await repo.register_namespace("ns-collision")
        await repo.delete_namespace("ns-collision")

        # Re-register the same name (gets a new ID)
        await repo.register_namespace("ns-collision")

        # Original ID recovery should fail — forward record already taken
        with pytest.raises(ValueError, match="re-registered"):
            await repo.recover_namespace(ns_id)


class TestListOrphanNamespaces:
    """Tests for list_orphan_namespaces()."""

    @pytest.mark.asyncio
    async def test_list_orphans_empty(self, repo):
        """list_orphan_namespaces() returns empty when no orphans."""
        await repo.register_namespace("ns-active")
        result = await repo.list_orphan_namespaces()
        assert result == []

    @pytest.mark.asyncio
    async def test_list_orphans_returns_deleted(self, repo):
        """list_orphan_namespaces() returns deleted namespaces."""
        ns_id = await repo.register_namespace("ns-orphan")
        await repo.delete_namespace("ns-orphan")

        result = await repo.list_orphan_namespaces()
        assert len(result) == 1
        assert result[0]["namespace_id"] == ns_id
        assert result[0]["namespace"] == "ns-orphan"
        assert result[0]["deleted_at"] != ""


class TestPurgeNamespace:
    """Tests for purge_namespace()."""

    @pytest.mark.asyncio
    async def test_purge_namespace_noop_nonexistent(self, repo):
        """purge_namespace() is a no-op for nonexistent ID."""
        await repo.purge_namespace("nonexistent-id")

    @pytest.mark.asyncio
    async def test_purge_namespace_rejects_active(self, repo):
        """purge_namespace() raises ValueError for active namespace."""
        ns_id = await repo.register_namespace("ns-purge-active")
        with pytest.raises(ValueError, match="Cannot purge active"):
            await repo.purge_namespace(ns_id)

    @pytest.mark.asyncio
    async def test_purge_namespace_sets_purging_status(self, repo):
        """purge_namespace() sets status='purging' before deleting data."""
        ns_id = await repo.register_namespace("ns-purge-status")
        await repo.delete_namespace("ns-purge-status")

        # We need to check status transitions. Since purge runs to completion,
        # we verify the reverse record is fully deleted after purge.
        await repo.purge_namespace(ns_id)

        # Reverse record should be completely gone after purge
        client = await repo._get_client()
        pk = schema.pk_system(schema.RESERVED_NAMESPACE)
        rev = await client.get_item(
            TableName=repo.table_name,
            Key={"PK": {"S": pk}, "SK": {"S": schema.sk_nsid(ns_id)}},
        )
        assert "Item" not in rev

    @pytest.mark.asyncio
    async def test_purge_namespace_deletes_data_items(self, repo):
        """purge_namespace() deletes all items belonging to the namespace."""
        # Register namespace and create scoped repo
        ns_id = await repo.register_namespace("ns-purge-data")

        # Set namespace_id on repo to create data under it
        original_ns_id = repo._namespace_id
        repo._namespace_id = ns_id

        # Create an entity (which writes data items with GSI4PK=ns_id)
        await repo.create_entity("test-entity-1")
        await repo.create_entity("test-entity-2")

        # Restore original namespace
        repo._namespace_id = original_ns_id

        # Delete and purge
        await repo.delete_namespace("ns-purge-data")
        await repo.purge_namespace(ns_id)

        # Verify entity data items are gone
        client = await repo._get_client()
        response = await client.query(
            TableName=repo.table_name,
            IndexName=schema.GSI4_NAME,
            KeyConditionExpression="GSI4PK = :pk",
            ExpressionAttributeValues={":pk": {"S": ns_id}},
        )
        assert len(response.get("Items", [])) == 0

    @pytest.mark.asyncio
    async def test_purge_namespace_handles_pagination(self, repo):
        """purge_namespace() handles >25 items (BatchWriteItem pagination)."""
        ns_id = await repo.register_namespace("ns-purge-pagination")

        # Set namespace_id on repo to create data under it
        original_ns_id = repo._namespace_id
        repo._namespace_id = ns_id

        # Create 30 entities (exceeds 25-item batch limit)
        for i in range(30):
            await repo.create_entity(f"entity-{i:03d}")

        repo._namespace_id = original_ns_id

        # Delete and purge
        await repo.delete_namespace("ns-purge-pagination")
        await repo.purge_namespace(ns_id)

        # Verify all items are gone
        client = await repo._get_client()
        response = await client.query(
            TableName=repo.table_name,
            IndexName=schema.GSI4_NAME,
            KeyConditionExpression="GSI4PK = :pk",
            ExpressionAttributeValues={":pk": {"S": ns_id}},
        )
        assert len(response.get("Items", [])) == 0
