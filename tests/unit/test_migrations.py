"""Tests for migrations module."""

from unittest.mock import patch

import pytest

from zae_limiter import schema
from zae_limiter.migrations import (
    Migration,
    apply_migrations,
    get_migrations,
    get_migrations_between,
    register_migration,
)
from zae_limiter.migrations.m_0_8_0_composite_limits import (
    _migrate_entity_limits,
    _migrate_resource_limits,
    _migrate_system_limits,
    migrate_to_0_8_0,
)


class TestMigrationRegistry:
    """Tests for migration registration and retrieval."""

    def test_get_migrations_returns_copy(self):
        """Test that get_migrations returns a copy of the list."""
        migrations1 = get_migrations()
        migrations2 = get_migrations()
        # Should be different list objects
        assert migrations1 is not migrations2
        # But same content
        assert migrations1 == migrations2

    def test_register_migration_sorts_by_version(self):
        """Test that migrations are sorted by version after registration."""

        async def noop(repository):  # noqa: ARG001
            pass

        m1 = Migration(version="2.0.0", description="Second", reversible=False, migrate=noop)
        m2 = Migration(version="1.0.0", description="First", reversible=False, migrate=noop)

        register_migration(m1)
        register_migration(m2)

        try:
            migrations = get_migrations()
            versions = [m.version for m in migrations]
            assert versions.index("1.0.0") < versions.index("2.0.0")
        finally:
            from zae_limiter.migrations import _MIGRATIONS

            _MIGRATIONS[:] = [m for m in _MIGRATIONS if m.version not in ("1.0.0", "2.0.0")]


class TestGetMigrationsBetween:
    """Tests for get_migrations_between function."""

    def test_no_migrations_same_version(self):
        """Test that no migrations returned when versions are equal."""
        result = get_migrations_between("1.0.0", "1.0.0")
        assert result == []

    def test_no_migrations_downgrade(self):
        """Test that no migrations returned for downgrade."""
        result = get_migrations_between("2.0.0", "1.0.0")
        assert result == []

    def test_migrations_for_upgrade(self):
        """Test that no migrations returned when none are registered."""
        result = get_migrations_between("0.9.0", "1.0.0")
        assert result == []


class TestApplyMigrations:
    """Tests for apply_migrations function."""

    @pytest.mark.asyncio
    async def test_apply_migrations_empty_range(self, limiter):
        """Test applying migrations with empty range."""
        # Access repository through limiter
        repository = limiter._repository

        # Same version - no migrations
        applied = await apply_migrations(repository, "1.0.0", "1.0.0")
        assert applied == []

    @pytest.mark.asyncio
    async def test_apply_migrations_downgrade(self, limiter):
        """Test applying migrations for downgrade returns empty."""
        repository = limiter._repository

        # Downgrade - no migrations
        applied = await apply_migrations(repository, "2.0.0", "1.0.0")
        assert applied == []

    @pytest.mark.asyncio
    async def test_apply_migrations_success(self, limiter):
        """Test successful migration application."""
        repository = limiter._repository

        # Register a test migration
        async def test_migrate(repository):  # noqa: ARG001
            pass  # No-op migration for testing

        test_migration = Migration(
            version="99.0.0",  # High version to not conflict
            description="Test migration",
            reversible=False,
            migrate=test_migrate,
        )
        register_migration(test_migration)

        try:
            # Apply migrations from 1.0.0 to 99.0.0
            applied = await apply_migrations(repository, "1.0.0", "99.0.0")
            assert "99.0.0" in applied
        finally:
            # Clean up - remove test migration
            migrations = get_migrations()
            for m in migrations:
                if m.version == "99.0.0":
                    from zae_limiter.migrations import _MIGRATIONS

                    _MIGRATIONS.remove(m)
                    break

    @pytest.mark.asyncio
    async def test_apply_migrations_failure(self, limiter):
        """Test migration failure handling."""
        repository = limiter._repository

        # Register a failing migration
        async def failing_migrate(repository):  # noqa: ARG001
            raise ValueError("Migration failed intentionally")

        failing_migration = Migration(
            version="98.0.0",
            description="Failing migration",
            reversible=False,
            migrate=failing_migrate,
        )
        register_migration(failing_migration)

        try:
            # Apply should raise RuntimeError
            with pytest.raises(RuntimeError, match="Migration to 98.0.0 failed"):
                await apply_migrations(repository, "1.0.0", "98.0.0")
        finally:
            # Clean up
            from zae_limiter.migrations import _MIGRATIONS

            _MIGRATIONS[:] = [m for m in _MIGRATIONS if m.version != "98.0.0"]


class TestMigrationDataclass:
    """Tests for Migration dataclass."""

    def test_migration_creation(self):
        """Test creating a Migration instance."""

        async def noop(repository):  # noqa: ARG001
            pass

        migration = Migration(
            version="1.2.3",
            description="Test description",
            reversible=True,
            migrate=noop,
            rollback=noop,
        )

        assert migration.version == "1.2.3"
        assert migration.description == "Test description"
        assert migration.reversible is True
        assert migration.migrate is noop
        assert migration.rollback is noop

    def test_migration_without_rollback(self):
        """Test creating a non-reversible Migration."""

        async def noop(repository):  # noqa: ARG001
            pass

        migration = Migration(
            version="1.2.3",
            description="Non-reversible",
            reversible=False,
            migrate=noop,
        )

        assert migration.rollback is None


class TestMigration080CompositeLimits:
    """Tests for the 0.8.0 composite limits migration."""

    @pytest.mark.asyncio
    async def test_migrate_system_limits_no_items(self, limiter):
        """Test system migration with no old limit items."""
        repository = limiter._repository
        client = await repository._get_client()

        # Ensure no old limit items exist
        await _migrate_system_limits(client, repository.table_name, repository)

        # Should complete without error - nothing to migrate

    @pytest.mark.asyncio
    async def test_migrate_system_limits_single_limit(self, limiter):
        """Test system migration with a single old-format limit item."""
        repository = limiter._repository
        client = await repository._get_client()
        table = repository.table_name

        # Create old-format system limit item
        old_item = {
            "PK": {"S": schema.pk_system("default")},
            "SK": {"S": f"{schema.SK_LIMIT}rpm"},
            "limit_name": {"S": "rpm"},
            "capacity": {"N": "1000"},
            "burst": {"N": "1200"},
            "refill_amount": {"N": "1000"},
            "refill_period_seconds": {"N": "60"},
        }
        await client.put_item(TableName=table, Item=old_item)

        # Run migration
        await _migrate_system_limits(client, table, repository)

        # Verify composite item was created
        result = await client.get_item(
            TableName=table,
            Key={
                "PK": {"S": schema.pk_system("default")},
                "SK": {"S": schema.sk_config()},
            },
        )
        assert "Item" in result
        item = result["Item"]
        assert item.get("l_rpm_cp", {}).get("N") == "1200"
        assert item.get("l_rpm_ra", {}).get("N") == "1000"
        assert item.get("l_rpm_rp", {}).get("N") == "60"

        # Verify old item was deleted
        old_result = await client.get_item(
            TableName=table,
            Key={
                "PK": {"S": schema.pk_system("default")},
                "SK": {"S": f"{schema.SK_LIMIT}rpm"},
            },
        )
        assert "Item" not in old_result

    @pytest.mark.asyncio
    async def test_migrate_system_limits_preserves_on_unavailable(self, limiter):
        """Test system migration preserves existing on_unavailable config."""
        repository = limiter._repository
        client = await repository._get_client()
        table = repository.table_name

        # Create existing config with on_unavailable
        config_item = {
            "PK": {"S": schema.pk_system("default")},
            "SK": {"S": schema.sk_config()},
            "on_unavailable": {"S": "allow"},
            "config_version": {"N": "1"},
        }
        await client.put_item(TableName=table, Item=config_item)

        # Create old-format limit item
        old_item = {
            "PK": {"S": schema.pk_system("default")},
            "SK": {"S": f"{schema.SK_LIMIT}tpm"},
            "limit_name": {"S": "tpm"},
            "capacity": {"N": "100000"},
            "burst": {"N": "120000"},
            "refill_amount": {"N": "100000"},
            "refill_period_seconds": {"N": "60"},
        }
        await client.put_item(TableName=table, Item=old_item)

        # Run migration
        await _migrate_system_limits(client, table, repository)

        # Verify on_unavailable was preserved
        result = await client.get_item(
            TableName=table,
            Key={
                "PK": {"S": schema.pk_system("default")},
                "SK": {"S": schema.sk_config()},
            },
        )
        item = result["Item"]
        assert item.get("on_unavailable", {}).get("S") == "allow"
        assert item.get("l_tpm_cp", {}).get("N") == "120000"

    @pytest.mark.asyncio
    async def test_migrate_system_limits_multiple_limits(self, limiter):
        """Test system migration with multiple old-format limit items."""
        repository = limiter._repository
        client = await repository._get_client()
        table = repository.table_name

        # Create multiple old-format limit items
        for name, capacity in [("rpm", "1000"), ("tpm", "100000"), ("rpd", "10000")]:
            old_item = {
                "PK": {"S": schema.pk_system("default")},
                "SK": {"S": f"{schema.SK_LIMIT}{name}"},
                "limit_name": {"S": name},
                "capacity": {"N": capacity},
                "burst": {"N": capacity},
                "refill_amount": {"N": capacity},
                "refill_period_seconds": {"N": "60"},
            }
            await client.put_item(TableName=table, Item=old_item)

        # Run migration
        await _migrate_system_limits(client, table, repository)

        # Verify all limits in composite item
        result = await client.get_item(
            TableName=table,
            Key={
                "PK": {"S": schema.pk_system("default")},
                "SK": {"S": schema.sk_config()},
            },
        )
        item = result["Item"]
        assert item.get("l_rpm_cp", {}).get("N") == "1000"
        assert item.get("l_tpm_cp", {}).get("N") == "100000"
        assert item.get("l_rpd_cp", {}).get("N") == "10000"

    @pytest.mark.asyncio
    async def test_migrate_resource_limits_no_items(self, limiter):
        """Test resource migration with no old limit items."""
        repository = limiter._repository
        client = await repository._get_client()

        await _migrate_resource_limits(client, repository.table_name, repository)

        # Should complete without error

    @pytest.mark.asyncio
    async def test_migrate_resource_limits_single_resource(self, limiter):
        """Test resource migration with a single resource's limits."""
        repository = limiter._repository
        client = await repository._get_client()
        table = repository.table_name

        # Create resource registry
        registry_item = {
            "PK": {"S": schema.pk_system("default")},
            "SK": {"S": schema.sk_resources()},
            "resources": {"SS": ["gpt-4"]},
        }
        await client.put_item(TableName=table, Item=registry_item)

        # Create old-format resource limit item
        old_item = {
            "PK": {"S": schema.pk_resource("default", "gpt-4")},
            "SK": {"S": f"{schema.SK_LIMIT}rpm"},
            "limit_name": {"S": "rpm"},
            "capacity": {"N": "500"},
            "burst": {"N": "600"},
            "refill_amount": {"N": "500"},
            "refill_period_seconds": {"N": "60"},
        }
        await client.put_item(TableName=table, Item=old_item)

        # Run migration
        await _migrate_resource_limits(client, table, repository)

        # Verify composite item was created
        result = await client.get_item(
            TableName=table,
            Key={
                "PK": {"S": schema.pk_resource("default", "gpt-4")},
                "SK": {"S": schema.sk_config()},
            },
        )
        assert "Item" in result
        item = result["Item"]
        assert item.get("resource", {}).get("S") == "gpt-4"
        assert item.get("l_rpm_cp", {}).get("N") == "600"

        # Verify old item was deleted
        old_result = await client.get_item(
            TableName=table,
            Key={
                "PK": {"S": schema.pk_resource("default", "gpt-4")},
                "SK": {"S": f"{schema.SK_LIMIT}rpm"},
            },
        )
        assert "Item" not in old_result

    @pytest.mark.asyncio
    async def test_migrate_entity_limits_no_items(self, limiter):
        """Test entity migration with no old limit items."""
        repository = limiter._repository
        client = await repository._get_client()

        await _migrate_entity_limits(client, repository.table_name, repository)

        # Should complete without error

    @pytest.mark.asyncio
    async def test_migrate_entity_limits_single_entity(self, limiter):
        """Test entity migration with a single entity's limits."""
        repository = limiter._repository
        client = await repository._get_client()
        table = repository.table_name

        # Create old-format entity limit item
        old_item = {
            "PK": {"S": schema.pk_entity("default", "user-123")},
            "SK": {"S": f"{schema.SK_LIMIT}gpt-4#rpm"},
            "entity_id": {"S": "user-123"},
            "resource": {"S": "gpt-4"},
            "limit_name": {"S": "rpm"},
            "capacity": {"N": "100"},
            "burst": {"N": "120"},
            "refill_amount": {"N": "100"},
            "refill_period_seconds": {"N": "60"},
        }
        await client.put_item(TableName=table, Item=old_item)

        # Run migration
        await _migrate_entity_limits(client, table, repository)

        # Verify composite item was created
        result = await client.get_item(
            TableName=table,
            Key={
                "PK": {"S": schema.pk_entity("default", "user-123")},
                "SK": {"S": schema.sk_config("gpt-4")},
            },
        )
        assert "Item" in result
        item = result["Item"]
        assert item.get("entity_id", {}).get("S") == "user-123"
        assert item.get("resource", {}).get("S") == "gpt-4"
        assert item.get("l_rpm_cp", {}).get("N") == "120"

        # Verify old item was deleted
        old_result = await client.get_item(
            TableName=table,
            Key={
                "PK": {"S": schema.pk_entity("default", "user-123")},
                "SK": {"S": f"{schema.SK_LIMIT}gpt-4#rpm"},
            },
        )
        assert "Item" not in old_result

    @pytest.mark.asyncio
    async def test_migrate_entity_limits_multiple_resources(self, limiter):
        """Test entity migration groups limits by resource."""
        repository = limiter._repository
        client = await repository._get_client()
        table = repository.table_name

        # Create old-format entity limit items for multiple resources
        for resource in ["gpt-4", "claude-3"]:
            for name in ["rpm", "tpm"]:
                old_item = {
                    "PK": {"S": schema.pk_entity("default", "user-456")},
                    "SK": {"S": f"{schema.SK_LIMIT}{resource}#{name}"},
                    "entity_id": {"S": "user-456"},
                    "resource": {"S": resource},
                    "limit_name": {"S": name},
                    "capacity": {"N": "100"},
                    "burst": {"N": "120"},
                    "refill_amount": {"N": "100"},
                    "refill_period_seconds": {"N": "60"},
                }
                await client.put_item(TableName=table, Item=old_item)

        # Run migration
        await _migrate_entity_limits(client, table, repository)

        # Verify composite items were created for each resource
        for resource in ["gpt-4", "claude-3"]:
            result = await client.get_item(
                TableName=table,
                Key={
                    "PK": {"S": schema.pk_entity("default", "user-456")},
                    "SK": {"S": schema.sk_config(resource)},
                },
            )
            assert "Item" in result
            item = result["Item"]
            assert item.get("l_rpm_cp", {}).get("N") == "120"
            assert item.get("l_tpm_cp", {}).get("N") == "120"

    @pytest.mark.asyncio
    async def test_full_migration_all_levels(self, limiter):
        """Test full migration across all levels."""
        repository = limiter._repository
        client = await repository._get_client()
        table = repository.table_name

        # Create old-format items at all levels
        # System level
        await client.put_item(
            TableName=table,
            Item={
                "PK": {"S": schema.pk_system("default")},
                "SK": {"S": f"{schema.SK_LIMIT}rpm"},
                "limit_name": {"S": "rpm"},
                "capacity": {"N": "1000"},
                "burst": {"N": "1200"},
                "refill_amount": {"N": "1000"},
                "refill_period_seconds": {"N": "60"},
            },
        )

        # Resource level
        await client.put_item(
            TableName=table,
            Item={
                "PK": {"S": schema.pk_system("default")},
                "SK": {"S": schema.sk_resources()},
                "resources": {"SS": ["api"]},
            },
        )
        await client.put_item(
            TableName=table,
            Item={
                "PK": {"S": schema.pk_resource("default", "api")},
                "SK": {"S": f"{schema.SK_LIMIT}rpm"},
                "limit_name": {"S": "rpm"},
                "capacity": {"N": "500"},
                "burst": {"N": "600"},
                "refill_amount": {"N": "500"},
                "refill_period_seconds": {"N": "60"},
            },
        )

        # Entity level
        await client.put_item(
            TableName=table,
            Item={
                "PK": {"S": schema.pk_entity("default", "test-user")},
                "SK": {"S": f"{schema.SK_LIMIT}api#rpm"},
                "entity_id": {"S": "test-user"},
                "resource": {"S": "api"},
                "limit_name": {"S": "rpm"},
                "capacity": {"N": "100"},
                "burst": {"N": "120"},
                "refill_amount": {"N": "100"},
                "refill_period_seconds": {"N": "60"},
            },
        )

        # Run full migration
        await migrate_to_0_8_0(repository)

        # Verify system level
        result = await client.get_item(
            TableName=table,
            Key={"PK": {"S": schema.pk_system("default")}, "SK": {"S": schema.sk_config()}},
        )
        assert result["Item"].get("l_rpm_cp", {}).get("N") == "1200"

        # Verify resource level
        result = await client.get_item(
            TableName=table,
            Key={
                "PK": {"S": schema.pk_resource("default", "api")},
                "SK": {"S": schema.sk_config()},
            },
        )
        assert result["Item"].get("l_rpm_cp", {}).get("N") == "600"

        # Verify entity level
        result = await client.get_item(
            TableName=table,
            Key={
                "PK": {"S": schema.pk_entity("default", "test-user")},
                "SK": {"S": schema.sk_config("api")},
            },
        )
        assert result["Item"].get("l_rpm_cp", {}).get("N") == "120"

    @pytest.mark.asyncio
    async def test_migrate_system_limits_items_without_limit_name(self, limiter):
        """Test system migration skips items without limit_name attribute (line 93)."""
        repository = limiter._repository
        client = await repository._get_client()
        table = repository.table_name

        # Create old-format system limit item WITHOUT limit_name
        old_item = {
            "PK": {"S": schema.pk_system("default")},
            "SK": {"S": f"{schema.SK_LIMIT}rpm"},
            # Missing limit_name attribute
            "capacity": {"N": "1000"},
            "burst": {"N": "1200"},
            "refill_amount": {"N": "1000"},
            "refill_period_seconds": {"N": "60"},
        }
        await client.put_item(TableName=table, Item=old_item)

        # Run migration - should return early since no valid limits found
        await _migrate_system_limits(client, table, repository)

        # Verify no composite item was created
        result = await client.get_item(
            TableName=table,
            Key={
                "PK": {"S": schema.pk_system("default")},
                "SK": {"S": schema.sk_config()},
            },
        )
        # No composite item should be created when no valid limits
        assert "Item" not in result or "l_rpm_cp" not in result.get("Item", {})

    @pytest.mark.asyncio
    async def test_migrate_resource_limits_resource_without_items(self, limiter):
        """Test resource migration handles resources with no limit items (line 166)."""
        repository = limiter._repository
        client = await repository._get_client()
        table = repository.table_name

        # Create resource registry with a resource that has no limit items
        registry_item = {
            "PK": {"S": schema.pk_system("default")},
            "SK": {"S": schema.sk_resources()},
            "resources": {"SS": ["empty-resource"]},
        }
        await client.put_item(TableName=table, Item=registry_item)

        # Run migration - should continue past the empty resource
        await _migrate_resource_limits(client, table, repository)

        # Verify no composite item was created for empty resource
        result = await client.get_item(
            TableName=table,
            Key={
                "PK": {"S": schema.pk_resource("default", "empty-resource")},
                "SK": {"S": schema.sk_config()},
            },
        )
        assert "Item" not in result

    @pytest.mark.asyncio
    async def test_migrate_resource_limits_skips_existing_config(self, limiter):
        """Test resource migration skips existing config record (line 175)."""
        repository = limiter._repository
        client = await repository._get_client()
        table = repository.table_name

        # Create resource registry
        registry_item = {
            "PK": {"S": schema.pk_system("default")},
            "SK": {"S": schema.sk_resources()},
            "resources": {"SS": ["test-resource"]},
        }
        await client.put_item(TableName=table, Item=registry_item)

        # Create existing config record (should be skipped, not parsed as limit)
        existing_config = {
            "PK": {"S": schema.pk_resource("default", "test-resource")},
            "SK": {"S": schema.sk_config()},
            "config_version": {"N": "1"},
        }
        await client.put_item(TableName=table, Item=existing_config)

        # Create old-format limit item
        old_item = {
            "PK": {"S": schema.pk_resource("default", "test-resource")},
            "SK": {"S": f"{schema.SK_LIMIT}rpm"},
            "limit_name": {"S": "rpm"},
            "capacity": {"N": "500"},
            "burst": {"N": "600"},
            "refill_amount": {"N": "500"},
            "refill_period_seconds": {"N": "60"},
        }
        await client.put_item(TableName=table, Item=old_item)

        # Run migration
        await _migrate_resource_limits(client, table, repository)

        # Verify composite item was created with limit data
        result = await client.get_item(
            TableName=table,
            Key={
                "PK": {"S": schema.pk_resource("default", "test-resource")},
                "SK": {"S": schema.sk_config()},
            },
        )
        assert "Item" in result
        assert result["Item"].get("l_rpm_cp", {}).get("N") == "600"

    @pytest.mark.asyncio
    async def test_migrate_resource_limits_items_without_limit_name(self, limiter):
        """Test resource migration skips items without limit_name (line 193)."""
        repository = limiter._repository
        client = await repository._get_client()
        table = repository.table_name

        # Create resource registry
        registry_item = {
            "PK": {"S": schema.pk_system("default")},
            "SK": {"S": schema.sk_resources()},
            "resources": {"SS": ["no-name-resource"]},
        }
        await client.put_item(TableName=table, Item=registry_item)

        # Create old-format limit item WITHOUT limit_name
        old_item = {
            "PK": {"S": schema.pk_resource("default", "no-name-resource")},
            "SK": {"S": f"{schema.SK_LIMIT}rpm"},
            # Missing limit_name attribute
            "capacity": {"N": "500"},
            "burst": {"N": "600"},
            "refill_amount": {"N": "500"},
            "refill_period_seconds": {"N": "60"},
        }
        await client.put_item(TableName=table, Item=old_item)

        # Run migration - should continue since no valid limits
        await _migrate_resource_limits(client, table, repository)

        # Verify no composite item was created
        result = await client.get_item(
            TableName=table,
            Key={
                "PK": {"S": schema.pk_resource("default", "no-name-resource")},
                "SK": {"S": schema.sk_config()},
            },
        )
        assert "Item" not in result

    @pytest.mark.asyncio
    async def test_migrate_entity_limits_items_without_limit_name(self, limiter):
        """Test entity migration skips items without limit_name (line 287)."""
        repository = limiter._repository
        client = await repository._get_client()
        table = repository.table_name

        # Create old-format entity limit item WITHOUT limit_name
        old_item = {
            "PK": {"S": schema.pk_entity("default", "user-no-name")},
            "SK": {"S": f"{schema.SK_LIMIT}gpt-4#rpm"},
            "entity_id": {"S": "user-no-name"},
            "resource": {"S": "gpt-4"},
            # Missing limit_name attribute
            "capacity": {"N": "100"},
            "burst": {"N": "120"},
            "refill_amount": {"N": "100"},
            "refill_period_seconds": {"N": "60"},
        }
        await client.put_item(TableName=table, Item=old_item)

        # Run migration - should continue since no valid limits
        await _migrate_entity_limits(client, table, repository)

        # Verify no composite item was created
        result = await client.get_item(
            TableName=table,
            Key={
                "PK": {"S": schema.pk_entity("default", "user-no-name")},
                "SK": {"S": schema.sk_config("gpt-4")},
            },
        )
        assert "Item" not in result

    @pytest.mark.asyncio
    async def test_migrate_entity_limits_pagination(self, limiter):
        """Test entity migration handles paginated scan results (line 244)."""
        repository = limiter._repository
        real_client = await repository._get_client()
        table = repository.table_name

        # Create entity limit items that will be returned in paginated responses
        item1 = {
            "PK": {"S": schema.pk_entity("default", "paginated-user")},
            "SK": {"S": f"{schema.SK_LIMIT}api#rpm"},
            "entity_id": {"S": "paginated-user"},
            "resource": {"S": "api"},
            "limit_name": {"S": "rpm"},
            "capacity": {"N": "100"},
            "burst": {"N": "120"},
            "refill_amount": {"N": "100"},
            "refill_period_seconds": {"N": "60"},
        }
        await real_client.put_item(TableName=table, Item=item1)

        # Mock the scan to simulate pagination
        call_count = 0

        async def mock_scan(**_kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call: return item with pagination key
                return {
                    "Items": [item1],
                    "LastEvaluatedKey": {"PK": {"S": "dummy"}, "SK": {"S": "dummy"}},
                }
            else:
                # Second call: return empty, no more pages
                return {"Items": []}

        with patch.object(real_client, "scan", side_effect=mock_scan):
            await _migrate_entity_limits(real_client, table, repository)

        # Verify pagination was exercised (scan called twice)
        assert call_count == 2

        # Verify composite item was created
        result = await real_client.get_item(
            TableName=table,
            Key={
                "PK": {"S": schema.pk_entity("default", "paginated-user")},
                "SK": {"S": schema.sk_config("api")},
            },
        )
        assert "Item" in result
        assert result["Item"].get("l_rpm_cp", {}).get("N") == "120"
