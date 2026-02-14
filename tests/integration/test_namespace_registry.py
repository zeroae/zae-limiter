"""Integration tests for namespace registry using LocalStack.

Runs against a real DynamoDB instance (LocalStack) to verify namespace
registry operations: register, list, delete, recover, purge.

To run:
    export AWS_ENDPOINT_URL=http://localhost:4566
    export AWS_ACCESS_KEY_ID=test
    export AWS_SECRET_ACCESS_KEY=test
    export AWS_DEFAULT_REGION=us-east-1
    pytest tests/integration/test_namespace_registry.py -v
"""

import pytest

from zae_limiter import schema
from zae_limiter.exceptions import EntityNotFoundError
from zae_limiter.repository import Repository

pytestmark = pytest.mark.integration


@pytest.fixture
async def localstack_repo(localstack_endpoint, unique_name):
    """Repository connected to LocalStack (direct table creation)."""
    repo = Repository(
        name=unique_name,
        endpoint_url=localstack_endpoint,
        region="us-east-1",
    )
    await repo.create_table()
    yield repo
    try:
        await repo.delete_table()
    except Exception:
        pass
    await repo.close()


class TestRegisterNamespace:
    """Integration tests for register_namespace()."""

    @pytest.mark.asyncio
    async def test_register_and_resolve(self, localstack_repo):
        """Register a namespace and resolve it back."""
        ns_id = await localstack_repo.register_namespace("tenant-alpha")
        assert ns_id is not None
        assert len(ns_id) == 11  # token_urlsafe(8)

        resolved = await localstack_repo._resolve_namespace("tenant-alpha")
        assert resolved == ns_id

    @pytest.mark.asyncio
    async def test_register_idempotent(self, localstack_repo):
        """Registering the same name twice returns the same ID."""
        ns_id_1 = await localstack_repo.register_namespace("tenant-beta")
        ns_id_2 = await localstack_repo.register_namespace("tenant-beta")
        assert ns_id_1 == ns_id_2

    @pytest.mark.asyncio
    async def test_register_writes_gsi4(self, localstack_repo):
        """Forward and reverse records have GSI4 attributes."""
        ns_id = await localstack_repo.register_namespace("tenant-gsi4")

        client = await localstack_repo._get_client()
        pk = schema.pk_system(schema.RESERVED_NAMESPACE)

        fwd = await client.get_item(
            TableName=localstack_repo.table_name,
            Key={"PK": {"S": pk}, "SK": {"S": schema.sk_namespace("tenant-gsi4")}},
        )
        assert fwd["Item"]["GSI4PK"]["S"] == schema.RESERVED_NAMESPACE
        assert fwd["Item"]["GSI4SK"]["S"] == pk

        rev = await client.get_item(
            TableName=localstack_repo.table_name,
            Key={"PK": {"S": pk}, "SK": {"S": schema.sk_nsid(ns_id)}},
        )
        assert rev["Item"]["GSI4PK"]["S"] == schema.RESERVED_NAMESPACE
        assert rev["Item"]["GSI4SK"]["S"] == pk

    @pytest.mark.asyncio
    async def test_register_rejects_reserved(self, localstack_repo):
        """Cannot register the reserved namespace '_'."""
        with pytest.raises(ValueError, match="reserved"):
            await localstack_repo.register_namespace("_")


class TestRegisterNamespaces:
    """Integration tests for register_namespaces()."""

    @pytest.mark.asyncio
    async def test_bulk_register(self, localstack_repo):
        """Bulk-register multiple namespaces."""
        result = await localstack_repo.register_namespaces(["ns-a", "ns-b", "ns-c"])
        assert len(result) == 3
        assert len(set(result.values())) == 3  # All IDs unique

    @pytest.mark.asyncio
    async def test_bulk_register_idempotent(self, localstack_repo):
        """Bulk-registering same names twice returns same IDs."""
        first = await localstack_repo.register_namespaces(["ns-x", "ns-y"])
        second = await localstack_repo.register_namespaces(["ns-x", "ns-y"])
        assert first == second


class TestListNamespaces:
    """Integration tests for list_namespaces()."""

    @pytest.mark.asyncio
    async def test_list_empty(self, localstack_repo):
        """Empty table returns empty list."""
        result = await localstack_repo.list_namespaces()
        assert result == []

    @pytest.mark.asyncio
    async def test_list_returns_registered(self, localstack_repo):
        """list_namespaces() returns registered namespaces."""
        ns_id_1 = await localstack_repo.register_namespace("ns-one")
        ns_id_2 = await localstack_repo.register_namespace("ns-two")

        result = await localstack_repo.list_namespaces()
        names = {ns["name"] for ns in result}
        ids = {ns["namespace_id"] for ns in result}

        assert names == {"ns-one", "ns-two"}
        assert ids == {ns_id_1, ns_id_2}
        for ns in result:
            assert ns["created_at"] != ""

    @pytest.mark.asyncio
    async def test_list_excludes_deleted(self, localstack_repo):
        """list_namespaces() excludes soft-deleted namespaces."""
        await localstack_repo.register_namespace("ns-active")
        await localstack_repo.register_namespace("ns-deleted")
        await localstack_repo.delete_namespace("ns-deleted")

        result = await localstack_repo.list_namespaces()
        names = [ns["name"] for ns in result]
        assert "ns-active" in names
        assert "ns-deleted" not in names


class TestDeleteNamespace:
    """Integration tests for delete_namespace()."""

    @pytest.mark.asyncio
    async def test_delete_removes_forward(self, localstack_repo):
        """Soft-delete removes forward record."""
        await localstack_repo.register_namespace("ns-del")
        await localstack_repo.delete_namespace("ns-del")

        resolved = await localstack_repo._resolve_namespace("ns-del")
        assert resolved is None

    @pytest.mark.asyncio
    async def test_delete_marks_reverse_deleted(self, localstack_repo):
        """Soft-delete marks reverse record as deleted."""
        ns_id = await localstack_repo.register_namespace("ns-del-rev")
        await localstack_repo.delete_namespace("ns-del-rev")

        client = await localstack_repo._get_client()
        pk = schema.pk_system(schema.RESERVED_NAMESPACE)
        rev = await client.get_item(
            TableName=localstack_repo.table_name,
            Key={"PK": {"S": pk}, "SK": {"S": schema.sk_nsid(ns_id)}},
        )
        assert rev["Item"]["status"]["S"] == "deleted"
        assert "deleted_at" in rev["Item"]

    @pytest.mark.asyncio
    async def test_delete_noop_nonexistent(self, localstack_repo):
        """delete_namespace() is a no-op for nonexistent namespace."""
        await localstack_repo.delete_namespace("nonexistent")  # Should not raise


class TestRecoverNamespace:
    """Integration tests for recover_namespace()."""

    @pytest.mark.asyncio
    async def test_recover_restores(self, localstack_repo):
        """recover_namespace() restores a deleted namespace."""
        ns_id = await localstack_repo.register_namespace("ns-recover")
        await localstack_repo.delete_namespace("ns-recover")

        name = await localstack_repo.recover_namespace(ns_id)
        assert name == "ns-recover"

        resolved = await localstack_repo._resolve_namespace("ns-recover")
        assert resolved == ns_id

    @pytest.mark.asyncio
    async def test_recover_marks_active(self, localstack_repo):
        """recover_namespace() sets reverse record status to active."""
        ns_id = await localstack_repo.register_namespace("ns-recover-active")
        await localstack_repo.delete_namespace("ns-recover-active")
        await localstack_repo.recover_namespace(ns_id)

        client = await localstack_repo._get_client()
        pk = schema.pk_system(schema.RESERVED_NAMESPACE)
        rev = await client.get_item(
            TableName=localstack_repo.table_name,
            Key={"PK": {"S": pk}, "SK": {"S": schema.sk_nsid(ns_id)}},
        )
        assert rev["Item"]["status"]["S"] == "active"
        assert "deleted_at" not in rev["Item"]

    @pytest.mark.asyncio
    async def test_recover_raises_not_found(self, localstack_repo):
        """recover_namespace() raises EntityNotFoundError for missing ID."""
        with pytest.raises(EntityNotFoundError):
            await localstack_repo.recover_namespace("nonexistent-id")

    @pytest.mark.asyncio
    async def test_recover_rejects_active(self, localstack_repo):
        """recover_namespace() raises ValueError for active namespace."""
        ns_id = await localstack_repo.register_namespace("ns-active-recover")
        with pytest.raises(ValueError, match="already active"):
            await localstack_repo.recover_namespace(ns_id)

    @pytest.mark.asyncio
    async def test_recover_rejects_name_collision(self, localstack_repo):
        """recover_namespace() raises ValueError when name re-registered."""
        ns_id = await localstack_repo.register_namespace("ns-collision")
        await localstack_repo.delete_namespace("ns-collision")
        await localstack_repo.register_namespace("ns-collision")

        with pytest.raises(ValueError, match="re-registered"):
            await localstack_repo.recover_namespace(ns_id)


class TestListOrphanNamespaces:
    """Integration tests for list_orphan_namespaces()."""

    @pytest.mark.asyncio
    async def test_orphans_empty(self, localstack_repo):
        """No orphans when all namespaces are active."""
        await localstack_repo.register_namespace("ns-active")
        result = await localstack_repo.list_orphan_namespaces()
        assert result == []

    @pytest.mark.asyncio
    async def test_orphans_returns_deleted(self, localstack_repo):
        """list_orphan_namespaces() returns deleted namespaces."""
        ns_id = await localstack_repo.register_namespace("ns-orphan")
        await localstack_repo.delete_namespace("ns-orphan")

        result = await localstack_repo.list_orphan_namespaces()
        assert len(result) == 1
        assert result[0]["namespace_id"] == ns_id
        assert result[0]["namespace"] == "ns-orphan"
        assert result[0]["deleted_at"] != ""


class TestPurgeNamespace:
    """Integration tests for purge_namespace()."""

    @pytest.mark.asyncio
    async def test_purge_noop_nonexistent(self, localstack_repo):
        """purge_namespace() is a no-op for nonexistent ID."""
        await localstack_repo.purge_namespace("nonexistent-id")

    @pytest.mark.asyncio
    async def test_purge_rejects_active(self, localstack_repo):
        """purge_namespace() raises ValueError for active namespace."""
        ns_id = await localstack_repo.register_namespace("ns-purge-active")
        with pytest.raises(ValueError, match="Cannot purge active"):
            await localstack_repo.purge_namespace(ns_id)

    @pytest.mark.asyncio
    async def test_purge_removes_reverse_record(self, localstack_repo):
        """purge_namespace() removes the reverse record entirely."""
        ns_id = await localstack_repo.register_namespace("ns-purge-rev")
        await localstack_repo.delete_namespace("ns-purge-rev")
        await localstack_repo.purge_namespace(ns_id)

        client = await localstack_repo._get_client()
        pk = schema.pk_system(schema.RESERVED_NAMESPACE)
        rev = await client.get_item(
            TableName=localstack_repo.table_name,
            Key={"PK": {"S": pk}, "SK": {"S": schema.sk_nsid(ns_id)}},
        )
        assert "Item" not in rev

    @pytest.mark.asyncio
    async def test_purge_deletes_data_items(self, localstack_repo):
        """purge_namespace() deletes all items scoped to the namespace."""
        ns_id = await localstack_repo.register_namespace("ns-purge-data")

        # Create entities under the namespace
        original_ns_id = localstack_repo._namespace_id
        localstack_repo._namespace_id = ns_id
        await localstack_repo.create_entity("test-entity-1")
        await localstack_repo.create_entity("test-entity-2")
        localstack_repo._namespace_id = original_ns_id

        # Delete and purge
        await localstack_repo.delete_namespace("ns-purge-data")
        await localstack_repo.purge_namespace(ns_id)

        # Verify data items are gone via GSI4
        client = await localstack_repo._get_client()
        response = await client.query(
            TableName=localstack_repo.table_name,
            IndexName=schema.GSI4_NAME,
            KeyConditionExpression="GSI4PK = :pk",
            ExpressionAttributeValues={":pk": {"S": ns_id}},
        )
        assert len(response.get("Items", [])) == 0
