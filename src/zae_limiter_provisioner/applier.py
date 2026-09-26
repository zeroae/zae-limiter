"""Applies limit changes to DynamoDB.

Uses boto3 (sync) directly, like the aggregator. This module runs inside
Lambda where aiobotocore is not available.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import boto3

from zae_limiter.schedule import encode, encode_reset
from zae_limiter.schema import (
    CONFIG_FIELD_DISABLED,
    CONFIG_FIELD_SCHED_TZ,
    LIMIT_FIELD_RSA,
    LIMIT_FIELD_RSCHED,
    LIMIT_FIELD_SCHED,
    limit_attr,
    pk_entity,
    pk_resource,
    pk_system,
    sk_config,
)

from .differ import Change
from .manifest import entries_from_manifest

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

    # Raises before anything is written, so a rejected item is never
    # half-serialized — mirroring `Repository._serialize_composite_limits`.
    hoisted_tz = _hoisted_timezone(limits)

    for name, decl in limits.items():
        item[limit_attr(name, "cp")] = {"N": str(decl["capacity"])}
        item[limit_attr(name, "ra")] = {"N": str(decl["refill_amount"])}
        item[limit_attr(name, "rp")] = {"N": str(decl["refill_period"])}
        # #222: manifests learned to express schedules in #543, and without
        # this leg the schedule reached the bucket fan-out but never the config
        # item — so it survived only until a bucket expired and was recreated
        # from config, unscheduled and silently. Written only when declared:
        # config items are full-replace `PutItem`, so absence is both "no
        # schedule" and how one is removed.
        for attr_field, entries, encoder in (
            (LIMIT_FIELD_SCHED, entries_from_manifest(decl.get("schedule"), reset=False), encode),
            (
                LIMIT_FIELD_RSCHED,
                entries_from_manifest(decl.get("reset_schedule"), reset=True),
                encode_reset,
            ),
        ):
            if entries:
                item[limit_attr(name, attr_field)] = {"S": encoder(entries)[0]}

        # ADR-139: a duration-window quota's window length, in seconds. Same
        # storage rule as cp/ra/rp — written only when the decl carries it,
        # and config items are full-replace `PutItem`s, so a limit converted
        # back to a drip loses `rsa` simply by omitting it here, no REMOVE
        # needed.
        reset_after_seconds = decl.get("reset_after_seconds")
        if reset_after_seconds is not None:
            item[limit_attr(name, LIMIT_FIELD_RSA)] = {"N": str(reset_after_seconds)}

    if hoisted_tz is not None:
        item[CONFIG_FIELD_SCHED_TZ] = {"S": hoisted_tz}

    return item


def _hoisted_timezone(limits: dict[str, Any]) -> str | None:
    """The one timezone every schedule on this config item shares (#222 §4.1).

    ``sched_tz`` is a single item-level attribute covering **both** tuples, so
    an item cannot carry two. Silently keeping the first limit's zone would
    reinterpret the second limit's cron in the wrong one — a New York daily
    quota read as UTC resets at 19:00 local, forever, with no error anywhere.

    Returns None when nothing on the item is scheduled.

    Raises:
        ValueError: the scheduled limits disagree. ``apply_changes`` records it
            against that change rather than writing a wrong item.
    """
    zones = {
        entry.tz
        for decl in limits.values()
        for entry in (
            *entries_from_manifest(decl.get("schedule"), reset=False),
            *entries_from_manifest(decl.get("reset_schedule"), reset=True),
        )
    }
    if len(zones) > 1:
        raise ValueError(
            f"all scheduled limits on one config item must share a timezone, got "
            f"{sorted(zones)}. The timezone is stored once per item as `sched_tz`, "
            f"not per limit."
        )
    return zones.pop() if zones else None


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
        disabled = data.get("disabled")
        if disabled is not None:
            extra[CONFIG_FIELD_DISABLED] = {"BOOL": bool(disabled)}
        item = _build_limit_item(pk, sk, namespace_id, limits, extra)

    elif change.level == "entity":
        assert change.target is not None
        entity_id, resource = change.target.split("/", 1)
        pk = pk_entity(namespace_id, entity_id)
        sk = sk_config(resource)
        extra = {"entity_id": {"S": entity_id}, "resource": {"S": resource}}
        disabled = data.get("disabled")
        if disabled is not None:
            extra[CONFIG_FIELD_DISABLED] = {"BOOL": bool(disabled)}
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
