"""
Migration 0.8.0: Composite Limit Config Items (ADR-114 for configs).

Consolidates N per-limit config items into 1 composite item per config level.
This migration handles:
- System limits: SYSTEM# / #LIMIT#{name} -> SYSTEM# / #CONFIG with l_* attributes
- Resource limits: RESOURCE#{res} / #LIMIT#{name} -> RESOURCE#{res} / #CONFIG with l_* attrs
- Entity limits: ENTITY#{id} / #LIMIT#{res}#{name} -> ENTITY#{id} / #CONFIG#{res} with l_* attrs

This is a hard cutover migration - old items are deleted after migration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .. import schema
from ..models import Limit
from . import Migration, register_migration

if TYPE_CHECKING:
    from ..repository import Repository


async def migrate_to_0_8_0(repository: Repository) -> None:
    """
    Migrate from per-limit items to composite config items.

    This migration:
    1. Queries all old #LIMIT# items at each level (system, resource, entity)
    2. Groups limits by config level
    3. Writes composite config items with l_* attributes
    4. Deletes old per-limit items
    """
    client = await repository._get_client()
    table = repository.table_name

    # 1. Migrate system limits
    await _migrate_system_limits(client, table, repository)

    # 2. Migrate resource limits
    await _migrate_resource_limits(client, table, repository)

    # 3. Migrate entity limits
    await _migrate_entity_limits(client, table, repository)


async def _migrate_system_limits(
    client: Any,
    table: str,
    repository: Repository,
) -> None:
    """Migrate system-level limits to composite format."""
    # Query all system limit items (SK begins_with #LIMIT#)
    response = await client.query(
        TableName=table,
        KeyConditionExpression="PK = :pk AND begins_with(SK, :sk_prefix)",
        ExpressionAttributeValues={
            ":pk": {"S": schema.pk_system()},
            ":sk_prefix": {"S": schema.SK_LIMIT},
        },
    )

    # Skip if no items or only config/version records
    items = response.get("Items", [])
    limit_items = [
        item
        for item in items
        if item.get("SK", {}).get("S", "")
        not in (schema.sk_config(), schema.sk_version(), schema.sk_resources())
        and item.get("SK", {}).get("S", "").startswith(schema.SK_LIMIT)
    ]

    if not limit_items:
        return

    # Parse limits from old items
    limits: list[Limit] = []
    for item in limit_items:
        limit_name = item.get("limit_name", {}).get("S", "")
        if limit_name:
            limits.append(
                Limit(
                    name=limit_name,
                    capacity=int(item.get("capacity", {}).get("N", "0")),
                    burst=int(item.get("burst", {}).get("N", "0")),
                    refill_amount=int(item.get("refill_amount", {}).get("N", "0")),
                    refill_period_seconds=int(item.get("refill_period_seconds", {}).get("N", "0")),
                )
            )

    if not limits:
        return

    # Check if there's an existing config record with on_unavailable
    existing_config = await client.get_item(
        TableName=table,
        Key={
            "PK": {"S": schema.pk_system()},
            "SK": {"S": schema.sk_config()},
        },
    )
    on_unavailable: str | None = None
    if existing_config.get("Item"):
        on_unavailable_attr = existing_config["Item"].get("on_unavailable", {})
        on_unavailable = on_unavailable_attr.get("S") if on_unavailable_attr else None

    # Build composite item
    composite_item: dict[str, Any] = {
        "PK": {"S": schema.pk_system()},
        "SK": {"S": schema.sk_config()},
        "config_version": {"N": "1"},
    }

    if on_unavailable is not None:
        composite_item["on_unavailable"] = {"S": on_unavailable}

    # Add l_* attributes
    repository._serialize_composite_limits(limits, composite_item)

    # Write composite item
    await client.put_item(TableName=table, Item=composite_item)

    # Delete old limit items
    delete_requests = [
        {"DeleteRequest": {"Key": {"PK": item["PK"], "SK": item["SK"]}}} for item in limit_items
    ]

    for i in range(0, len(delete_requests), 25):
        chunk = delete_requests[i : i + 25]
        await client.batch_write_item(RequestItems={table: chunk})


async def _migrate_resource_limits(
    client: Any,
    table: str,
    repository: Repository,
) -> None:
    """Migrate resource-level limits to composite format."""
    # First, get list of resources with defaults from the registry
    registry_response = await client.get_item(
        TableName=table,
        Key={
            "PK": {"S": schema.pk_system()},
            "SK": {"S": schema.sk_resources()},
        },
    )

    registry_item = registry_response.get("Item", {})
    resources = registry_item.get("resources", {}).get("SS", [])

    # Process each resource
    for resource in resources:
        # Query all limit items for this resource
        response = await client.query(
            TableName=table,
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk_prefix)",
            ExpressionAttributeValues={
                ":pk": {"S": schema.pk_resource(resource)},
                ":sk_prefix": {"S": schema.SK_LIMIT},
            },
        )

        items = response.get("Items", [])
        if not items:
            continue

        # Parse limits from old items (filter out config record if exists)
        limits: list[Limit] = []
        limit_items: list[dict[str, Any]] = []

        for item in items:
            sk = item.get("SK", {}).get("S", "")
            if sk == schema.sk_config():
                continue  # Skip existing config record

            limit_name = item.get("limit_name", {}).get("S", "")
            if limit_name:
                limits.append(
                    Limit(
                        name=limit_name,
                        capacity=int(item.get("capacity", {}).get("N", "0")),
                        burst=int(item.get("burst", {}).get("N", "0")),
                        refill_amount=int(item.get("refill_amount", {}).get("N", "0")),
                        refill_period_seconds=int(
                            item.get("refill_period_seconds", {}).get("N", "0")
                        ),
                    )
                )
                limit_items.append(item)

        if not limits:
            continue

        # Build composite item
        composite_item: dict[str, Any] = {
            "PK": {"S": schema.pk_resource(resource)},
            "SK": {"S": schema.sk_config()},
            "resource": {"S": resource},
            "config_version": {"N": "1"},
        }

        # Add l_* attributes
        repository._serialize_composite_limits(limits, composite_item)

        # Write composite item
        await client.put_item(TableName=table, Item=composite_item)

        # Delete old limit items
        delete_requests = [
            {"DeleteRequest": {"Key": {"PK": item["PK"], "SK": item["SK"]}}} for item in limit_items
        ]

        for i in range(0, len(delete_requests), 25):
            chunk = delete_requests[i : i + 25]
            await client.batch_write_item(RequestItems={table: chunk})


async def _migrate_entity_limits(
    client: Any,
    table: str,
    repository: Repository,
) -> None:
    """Migrate entity-level limits to composite format.

    This scans for ENTITY# partitions with #LIMIT# items and migrates them
    to #CONFIG#{resource} composite items.
    """
    # Scan for all entity limit items (this is expensive but necessary for migration)
    # In production, this would ideally be done in batches with checkpointing
    paginator_key = None
    all_limit_items: list[dict[str, Any]] = []

    while True:
        scan_params: dict[str, Any] = {
            "TableName": table,
            "FilterExpression": "begins_with(PK, :pk_prefix) AND begins_with(SK, :sk_prefix)",
            "ExpressionAttributeValues": {
                ":pk_prefix": {"S": schema.ENTITY_PREFIX},
                ":sk_prefix": {"S": schema.SK_LIMIT},
            },
        }
        if paginator_key:
            scan_params["ExclusiveStartKey"] = paginator_key

        response = await client.scan(**scan_params)
        all_limit_items.extend(response.get("Items", []))

        paginator_key = response.get("LastEvaluatedKey")
        if paginator_key is None:
            break

    if not all_limit_items:
        return

    # Group by entity_id and resource
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in all_limit_items:
        entity_id = item.get("entity_id", {}).get("S", "")
        resource = item.get("resource", {}).get("S", "")
        if entity_id and resource:
            key = (entity_id, resource)
            if key not in groups:
                groups[key] = []
            groups[key].append(item)

    # Process each group
    for (entity_id, resource), items in groups.items():
        # Parse limits from old items
        limits: list[Limit] = []
        for item in items:
            limit_name = item.get("limit_name", {}).get("S", "")
            if limit_name:
                limits.append(
                    Limit(
                        name=limit_name,
                        capacity=int(item.get("capacity", {}).get("N", "0")),
                        burst=int(item.get("burst", {}).get("N", "0")),
                        refill_amount=int(item.get("refill_amount", {}).get("N", "0")),
                        refill_period_seconds=int(
                            item.get("refill_period_seconds", {}).get("N", "0")
                        ),
                    )
                )

        if not limits:
            continue

        # Build composite item
        composite_item: dict[str, Any] = {
            "PK": {"S": schema.pk_entity(entity_id)},
            "SK": {"S": schema.sk_config(resource)},
            "entity_id": {"S": entity_id},
            "resource": {"S": resource},
            "config_version": {"N": "1"},
        }

        # Add l_* attributes
        repository._serialize_composite_limits(limits, composite_item)

        # Write composite item
        await client.put_item(TableName=table, Item=composite_item)

        # Delete old limit items
        delete_requests = [
            {"DeleteRequest": {"Key": {"PK": item["PK"], "SK": item["SK"]}}} for item in items
        ]

        for i in range(0, len(delete_requests), 25):
            chunk = delete_requests[i : i + 25]
            await client.batch_write_item(RequestItems={table: chunk})


# Register the migration
register_migration(
    Migration(
        version="0.8.0",
        description="Consolidate limit config items into composite format (ADR-114)",
        reversible=False,  # Hard cutover - old items are deleted
        migrate=migrate_to_0_8_0,
    )
)
