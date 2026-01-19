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
