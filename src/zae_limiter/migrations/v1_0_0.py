"""
Migration: v1.0.0 (Initial schema)

This is the baseline migration that represents the initial schema version.
It does not perform any actual migration - it simply documents the initial
schema structure.

Schema v1.0.0 includes:
- DynamoDB table with PAY_PER_REQUEST billing
- Primary key: PK (partition) + SK (sort)
- GSI1: Parent -> Children lookups
- GSI2: Resource aggregation
- TTL on 'ttl' attribute
- Streams enabled with NEW_AND_OLD_IMAGES

Key patterns:
- Entity metadata: PK=ENTITY#{id}, SK=#META
- Buckets: PK=ENTITY#{id}, SK=#BUCKET#{resource}#{limit_name}
- Limits: PK=ENTITY#{id}, SK=#LIMIT#{resource}#{limit_name}
- Usage: PK=ENTITY#{id}, SK=#USAGE#{resource}#{window_key}
- Version: PK=SYSTEM#, SK=#VERSION
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import Migration, register_migration

if TYPE_CHECKING:
    from ..repository import Repository


async def migrate_v1_0_0(repository: Repository) -> None:
    """
    Baseline migration - no action needed.

    This migration represents the initial schema version.
    It exists to document the baseline and serve as a reference
    for future migrations.
    """
    # No migration needed - this is the initial schema
    pass


# Register the baseline migration
register_migration(
    Migration(
        version="1.0.0",
        description="Initial schema (baseline)",
        reversible=False,
        migrate=migrate_v1_0_0,
        rollback=None,
    )
)
