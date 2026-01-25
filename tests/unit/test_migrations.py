"""Tests for migrations module."""

import pytest

from zae_limiter.migrations import (
    Migration,
    apply_migrations,
    get_migrations,
    get_migrations_between,
    register_migration,
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
        # The v1_0_0 migration should already be registered
        migrations = get_migrations()
        if len(migrations) > 1:
            # Verify sorted order
            for i in range(len(migrations) - 1):
                assert migrations[i].version <= migrations[i + 1].version


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
        """Test that correct migrations returned for upgrade."""
        # v1_0_0 migration is registered by default
        result = get_migrations_between("0.9.0", "1.0.0")
        # Should include the v1_0_0 migration
        versions = [m.version for m in result]
        assert "1.0.0" in versions


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


class TestMigrationV110:
    """Tests for v1.1.0 schema flattening migration."""

    def test_v110_registered(self):
        """v1.1.0 migration should be registered."""
        migrations = get_migrations()
        versions = [m.version for m in migrations]
        assert "1.1.0" in versions

    def test_v110_between_100_and_200(self):
        """v1.1.0 migration should be included when upgrading from 1.0.0 to 2.0.0."""
        result = get_migrations_between("1.0.0", "2.0.0")
        versions = [m.version for m in result]
        assert "1.1.0" in versions

    def test_v110_not_reversible(self):
        """v1.1.0 migration should not be reversible."""
        migrations = get_migrations()
        v110 = next(m for m in migrations if m.version == "1.1.0")
        assert v110.reversible is False
        assert v110.rollback is None

    @pytest.mark.asyncio
    async def test_v110_flattens_entity(self, limiter):
        """v1.1.0 migration should flatten entity metadata records."""
        from zae_limiter import schema
        from zae_limiter.migrations.v1_1_0 import migrate_v1_1_0

        repository = limiter._repository
        client = await repository._get_client()

        # Insert entity in nested format
        await client.put_item(
            TableName=repository.table_name,
            Item={
                "PK": {"S": schema.pk_entity("test-entity")},
                "SK": {"S": schema.sk_meta()},
                "entity_id": {"S": "test-entity"},
                "data": {
                    "M": {
                        "name": {"S": "Test"},
                        "parent_id": {"NULL": True},
                        "metadata": {"M": {}},
                        "created_at": {"S": "2024-01-01T00:00:00Z"},
                    }
                },
            },
        )

        # Run migration
        await migrate_v1_1_0(repository)

        # Verify entity was flattened
        response = await client.get_item(
            TableName=repository.table_name,
            Key={
                "PK": {"S": schema.pk_entity("test-entity")},
                "SK": {"S": schema.sk_meta()},
            },
        )
        item = response["Item"]
        assert "data" not in item
        assert item["name"]["S"] == "Test"
        assert "NULL" in item["parent_id"]
        assert item["created_at"]["S"] == "2024-01-01T00:00:00Z"

    @pytest.mark.asyncio
    async def test_v110_flattens_version_record(self, limiter):
        """v1.1.0 migration should flatten version records."""
        from zae_limiter import schema
        from zae_limiter.migrations.v1_1_0 import migrate_v1_1_0

        repository = limiter._repository
        client = await repository._get_client()

        # Insert version record in nested format
        await client.put_item(
            TableName=repository.table_name,
            Item={
                "PK": {"S": schema.pk_system()},
                "SK": {"S": schema.sk_version()},
                "data": {
                    "M": {
                        "schema_version": {"S": "1.0.0"},
                        "lambda_version": {"NULL": True},
                        "client_min_version": {"S": "0.1.0"},
                        "updated_at": {"S": "2024-01-01T00:00:00Z"},
                        "updated_by": {"S": "test"},
                    }
                },
            },
        )

        await migrate_v1_1_0(repository)

        response = await client.get_item(
            TableName=repository.table_name,
            Key={
                "PK": {"S": schema.pk_system()},
                "SK": {"S": schema.sk_version()},
            },
        )
        item = response["Item"]
        assert "data" not in item
        assert item["schema_version"]["S"] == "1.0.0"
        assert item["updated_by"]["S"] == "test"

    @pytest.mark.asyncio
    async def test_v110_idempotent(self, limiter):
        """Running migration twice should be safe."""
        from zae_limiter import schema
        from zae_limiter.migrations.v1_1_0 import migrate_v1_1_0

        repository = limiter._repository
        client = await repository._get_client()

        # Insert entity in nested format
        await client.put_item(
            TableName=repository.table_name,
            Item={
                "PK": {"S": schema.pk_entity("idem-entity")},
                "SK": {"S": schema.sk_meta()},
                "entity_id": {"S": "idem-entity"},
                "data": {
                    "M": {
                        "name": {"S": "Idempotent"},
                        "parent_id": {"NULL": True},
                        "metadata": {"M": {}},
                        "created_at": {"S": "2024-01-01T00:00:00Z"},
                    }
                },
            },
        )

        # Run migration twice
        await migrate_v1_1_0(repository)
        await migrate_v1_1_0(repository)  # Should be no-op

        # Verify still correct
        entity = await repository.get_entity("idem-entity")
        assert entity is not None
        assert entity.name == "Idempotent"

    @pytest.mark.asyncio
    async def test_v110_skips_already_flat(self, limiter):
        """Already flat items should be skipped."""
        from zae_limiter.migrations.v1_1_0 import migrate_v1_1_0

        repository = limiter._repository

        # Create entity via normal API (writes flat format)
        await repository.create_entity("flat-entity", name="Already Flat")

        # Run migration — should skip this item
        await migrate_v1_1_0(repository)

        # Verify unchanged
        entity = await repository.get_entity("flat-entity")
        assert entity is not None
        assert entity.name == "Already Flat"
