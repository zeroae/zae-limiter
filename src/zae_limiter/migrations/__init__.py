"""
Migration framework for zae-limiter schema changes.

This module provides infrastructure for managing schema migrations
when upgrading between major versions of zae-limiter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from ..version import parse_version

if TYPE_CHECKING:
    from ..repository import Repository


class MigrationFunc(Protocol):
    """Protocol for migration functions."""

    async def __call__(self, repository: Repository) -> None:
        """Execute the migration."""
        ...


@dataclass
class Migration:
    """Represents a schema migration."""

    version: str  # Target schema version (e.g., "1.1.0")
    description: str  # Human-readable description
    reversible: bool  # Can this migration be rolled back?
    migrate: MigrationFunc  # Forward migration function
    rollback: MigrationFunc | None = None  # Rollback function (if reversible)


# Registry of all migrations, ordered by version
_MIGRATIONS: list[Migration] = []


def register_migration(migration: Migration) -> None:
    """Register a migration in the global registry."""
    _MIGRATIONS.append(migration)
    _MIGRATIONS.sort(key=lambda m: parse_version(m.version))


def get_migrations() -> list[Migration]:
    """Get all registered migrations."""
    return _MIGRATIONS.copy()


def get_migrations_between(from_version: str, to_version: str) -> list[Migration]:
    """
    Get migrations needed to upgrade from one version to another.

    Args:
        from_version: Current schema version
        to_version: Target schema version

    Returns:
        List of migrations to apply in order
    """
    from_v = parse_version(from_version)
    to_v = parse_version(to_version)

    if from_v >= to_v:
        return []

    migrations = []
    for migration in _MIGRATIONS:
        migration_v = parse_version(migration.version)
        if from_v < migration_v <= to_v:
            migrations.append(migration)

    return migrations


async def apply_migrations(
    repository: Repository,
    from_version: str,
    to_version: str,
) -> list[str]:
    """
    Apply all migrations between two versions.

    Args:
        repository: Repository instance
        from_version: Current schema version
        to_version: Target schema version

    Returns:
        List of applied migration versions

    Raises:
        Exception: If any migration fails
    """
    migrations = get_migrations_between(from_version, to_version)

    applied: list[str] = []
    for migration in migrations:
        try:
            await migration.migrate(repository)
            applied.append(migration.version)
        except Exception as e:
            raise RuntimeError(
                f"Migration to {migration.version} failed: {e}. Applied migrations: {applied}"
            ) from e

    return applied


# Import migrations to register them
from . import m_0_8_0_composite_limits as _  # noqa: F401, E402
from . import m_0_9_0_bucket_pk as __  # noqa: F401, E402
from . import m_0_10_0_lsi as ___  # noqa: F401, E402
