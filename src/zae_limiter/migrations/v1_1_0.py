"""
Migration: v1.1.0 (Flatten DynamoDB schema)

Promotes nested data.M fields to flat top-level attributes for all record
types: entities, buckets, limits, audit events, and version records.

This migration is optional for operators who want immediate consistency.
The v1.1.0 code reads both flat and nested formats (dual-format
deserialization), so records will be lazily migrated on next write even
without running this migration.

Schema changes:
- Entity metadata: name, parent_id, metadata, created_at → top-level
- Bucket state: resource, limit_name, tokens_milli, etc. → top-level
- Entity limits: resource, limit_name, capacity, etc. → top-level
- Audit events: event_id, timestamp, action, etc. → top-level
- Version record: schema_version, lambda_version, etc. → top-level

Already flat (unchanged): system/resource config, usage snapshots.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from . import Migration, register_migration

if TYPE_CHECKING:
    from ..repository import Repository

logger = logging.getLogger(__name__)


async def migrate_v1_1_0(repository: Repository) -> None:
    """
    Flatten all nested data.M records to top-level attributes.

    Scans the table for items with a ``data`` attribute and promotes
    nested fields to the top level using a single UpdateExpression per
    item (SET each field, then REMOVE data).

    Safe to run multiple times: items already flattened (no ``data``
    attribute) are skipped.
    """
    client = await repository._get_client()
    table_name = repository.table_name

    # Scan for items that still have the nested 'data' attribute
    paginator_kwargs: dict[str, Any] = {
        "TableName": table_name,
        "FilterExpression": "attribute_exists(#data)",
        "ExpressionAttributeNames": {"#data": "data"},
        "ProjectionExpression": "PK, SK, #data",
    }

    migrated_count = 0
    scanned_count = 0
    exclusive_start_key: dict[str, Any] | None = None

    while True:
        if exclusive_start_key:
            paginator_kwargs["ExclusiveStartKey"] = exclusive_start_key

        response = await client.scan(**paginator_kwargs)
        items = response.get("Items", [])
        scanned_count += response.get("ScannedCount", 0)

        for item in items:
            pk = item.get("PK", {}).get("S", "")
            sk = item.get("SK", {}).get("S", "")
            data_map = item.get("data", {}).get("M", {})

            if not data_map:
                continue

            # Build SET expressions for each field in data.M
            set_parts: list[str] = []
            attr_names: dict[str, str] = {"#data": "data"}
            attr_values: dict[str, Any] = {}

            for i, (field_name, field_value) in enumerate(data_map.items()):
                alias = f"#f{i}"
                value_alias = f":v{i}"
                attr_names[alias] = field_name
                attr_values[value_alias] = field_value
                set_parts.append(f"{alias} = {value_alias}")

            update_expr = f"SET {', '.join(set_parts)} REMOVE #data"

            try:
                await client.update_item(
                    TableName=table_name,
                    Key={"PK": item["PK"], "SK": item["SK"]},
                    UpdateExpression=update_expr,
                    ExpressionAttributeNames=attr_names,
                    ExpressionAttributeValues=attr_values,
                    ConditionExpression="attribute_exists(#data)",
                )
                migrated_count += 1
            except client.exceptions.ConditionalCheckFailedException:
                # Already migrated by concurrent process — skip
                pass
            except Exception:
                logger.warning(
                    "Failed to flatten item PK=%s SK=%s",
                    pk,
                    sk,
                    exc_info=True,
                )
                raise

        exclusive_start_key = response.get("LastEvaluatedKey")
        if not exclusive_start_key:
            break

    logger.info(
        "Migration v1.1.0 complete: flattened %d items (scanned %d)",
        migrated_count,
        scanned_count,
    )


# Register the migration
register_migration(
    Migration(
        version="1.1.0",
        description="Flatten nested data.M records to top-level attributes",
        reversible=False,
        migrate=migrate_v1_1_0,
        rollback=None,
    )
)
