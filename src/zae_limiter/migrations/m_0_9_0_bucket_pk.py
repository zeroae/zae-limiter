"""
Migration 0.9.0: Bucket PK migration (GHSA-76rv-2r9v-c5m6).

Moves bucket items from entity-scoped partition keys to per-(entity, resource, shard)
partition keys for hot partition mitigation via write sharding.

Old scheme: PK={ns}/ENTITY#{id}, SK=#BUCKET#{resource}
New scheme: PK={ns}/BUCKET#{id}#{resource}#{shard}, SK=#STATE

This is a clean break migration:
- New code creates buckets at new PKs on first access
- Old bucket items at ENTITY# PKs are orphaned and ignored
- Old items will be cleaned up by DynamoDB TTL or a future cleanup script
- No data migration needed: buckets are ephemeral (token counts reset naturally)

Additionally, a reserved `wcu` (write capacity unit) infrastructure limit is
auto-injected on every bucket to track per-partition write pressure. GSI3
(KEYS_ONLY) enables bucket discovery by entity without impacting the hot path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import Migration, register_migration

if TYPE_CHECKING:
    from ..repository import Repository


async def migrate_to_0_9_0(repository: Repository) -> None:
    """
    No-op migration: new bucket PKs are created on first access.

    Old bucket items at ENTITY# partition keys are orphaned and will be
    cleaned up by TTL expiration. No data migration is required because
    buckets are ephemeral — token counts refill naturally.
    """
    # Intentionally empty: clean break migration


# Register the migration
register_migration(
    Migration(
        version="0.9.0",
        description="Bucket PK migration for write sharding (GHSA-76rv-2r9v-c5m6)",
        reversible=False,  # Clean break - old items orphaned
        migrate=migrate_to_0_9_0,
    )
)
