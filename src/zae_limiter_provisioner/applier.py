"""Applies limit changes to DynamoDB.

Uses boto3 (sync) directly, like the aggregator. This module runs inside
Lambda where aioboto3 is not available.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import boto3

from zae_limiter.schema import (
    limit_attr,
    pk_entity,
    pk_resource,
    pk_system,
    sk_config,
)

from .differ import Change

logger = logging.getLogger(__name__)


@dataclass
class ApplyResult:
    """Result of applying changes."""

    created: int = 0
    updated: int = 0
    deleted: int = 0
    errors: list[str] = field(default_factory=list)


def _build_limit_item(
    pk: str,
    sk: str,
    namespace_id: str,
    limits: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a DynamoDB item for a config record with composite limit attributes."""
    item: dict[str, Any] = {
        "PK": {"S": pk},
        "SK": {"S": sk},
        "GSI4PK": {"S": namespace_id},
    }
    if extra:
        for k, v in extra.items():
            item[k] = v

    for name, decl in limits.items():
        item[limit_attr(name, "cp")] = {"N": str(decl["capacity"])}
        item[limit_attr(name, "bx")] = {"N": str(decl["burst"])}
        item[limit_attr(name, "ra")] = {"N": str(decl["refill_amount"])}
        item[limit_attr(name, "rp")] = {"N": str(decl["refill_period"])}

    return item


def apply_changes(
    changes: list[Change],
    table_name: str,
    namespace_id: str,
    client: Any | None = None,
) -> ApplyResult:
    """Apply a list of changes to DynamoDB.

    Args:
        changes: List of Change objects from the differ.
        table_name: DynamoDB table name.
        namespace_id: Opaque namespace ID (e.g., 'a7x3kq').
        client: Optional boto3 DynamoDB client (injected for testing).

    Returns:
        ApplyResult with counts and any errors.
    """
    result = ApplyResult()

    if not changes:
        return result

    if client is None:
        client = boto3.client("dynamodb")

    for change in changes:
        try:
            if change.action == "delete":
                _apply_delete(client, table_name, namespace_id, change)
                result.deleted += 1
            elif change.action in ("create", "update"):
                _apply_set(client, table_name, namespace_id, change)
                if change.action == "create":
                    result.created += 1
                else:
                    result.updated += 1
        except Exception as e:
            logger.warning("Failed to %s %s %s: %s", change.action, change.level, change.target, e)
            result.errors.append(f"{change.action} {change.level} {change.target}: {e}")

    return result


def _apply_set(
    client: Any,
    table_name: str,
    namespace_id: str,
    change: Change,
) -> None:
    """Apply a create or update change (PutItem)."""
    data = change.data or {}
    limits = data.get("limits", {})

    if change.level == "system":
        pk = pk_system(namespace_id)
        sk = sk_config()
        extra: dict[str, Any] = {}
        on_unavailable = data.get("on_unavailable")
        if on_unavailable is not None:
            extra["on_unavailable"] = {"S": on_unavailable}
        item = _build_limit_item(pk, sk, namespace_id, limits, extra)

    elif change.level == "resource":
        assert change.target is not None
        resource = change.target
        pk = pk_resource(namespace_id, resource)
        sk = sk_config()
        extra = {"resource": {"S": resource}}
        item = _build_limit_item(pk, sk, namespace_id, limits, extra)

    elif change.level == "entity":
        assert change.target is not None
        entity_id, resource = change.target.split("/", 1)
        pk = pk_entity(namespace_id, entity_id)
        sk = sk_config(resource)
        extra = {"entity_id": {"S": entity_id}, "resource": {"S": resource}}
        item = _build_limit_item(pk, sk, namespace_id, limits, extra)

    else:
        raise ValueError(f"Unknown level: {change.level}")

    client.put_item(TableName=table_name, Item=item)


def _apply_delete(
    client: Any,
    table_name: str,
    namespace_id: str,
    change: Change,
) -> None:
    """Apply a delete change (DeleteItem)."""
    if change.level == "system":
        pk = pk_system(namespace_id)
        sk = sk_config()

    elif change.level == "resource":
        assert change.target is not None
        resource = change.target
        pk = pk_resource(namespace_id, resource)
        sk = sk_config()

    elif change.level == "entity":
        assert change.target is not None
        entity_id, resource = change.target.split("/", 1)
        pk = pk_entity(namespace_id, entity_id)
        sk = sk_config(resource)

    else:
        raise ValueError(f"Unknown level: {change.level}")

    client.delete_item(TableName=table_name, Key={"PK": {"S": pk}, "SK": {"S": sk}})
