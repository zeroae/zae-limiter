"""
Migration 0.10.0: Local Secondary Indexes (ADR-123).

Defines 5 LSI slots on the DynamoDB table with alternating projections
(odd=ALL, even=KEYS_ONLY). LSIs can only be added at table creation time,
so this migration is a no-op for existing tables.

Operators upgrading from 0.9.0 must recreate the table to gain LSI support.
New deployments automatically include the LSIs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import Migration, register_migration

if TYPE_CHECKING:
    from ..repository import Repository


async def migrate_to_0_10_0(repository: Repository) -> None:
    """
    No-op migration: LSIs cannot be added to existing tables.

    Tables created with schema 0.10.0+ include LSI1-LSI5 at creation time.
    Existing tables continue to work without LSIs — queries that would use
    LSIs fall back to client-side filtering.
    """
    # Intentionally empty: LSIs require table recreation


# Register the migration
register_migration(
    Migration(
        version="0.10.0",
        description="Local Secondary Indexes (ADR-123) - requires table recreation",
        reversible=False,  # Table recreation required
        migrate=migrate_to_0_10_0,
    )
)
