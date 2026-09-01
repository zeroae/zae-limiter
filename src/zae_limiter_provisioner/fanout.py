"""Eager bucket stamping for disabled resources/entities (ADR-125).

Mirrors Repository._fanout_* using sync boto3, because the provisioner runs
inside Lambda where aiobotocore is unavailable. Key construction is shared via
zae_limiter.schema, which is already vendored into the Lambda package.

Two passes: like Repository._fanout_resource / _fanout_entity, the discovery
query runs twice per fan-out call, with stamped PKs de-duplicated across both
passes. The second pass catches a bucket created by an acquire() that was
already in flight when the first pass's query ran -- without it, a bucket
created between the first pass's query and its stamp would never be visited
(ADR-125).

Limitation: unlike Repository._fanout_resource, this does not evaluate
per-entity overrides. The handler compensates by applying resource-level
changes before entity-level ones, so an entity carve-out re-stamps its own
buckets last.
"""

from __future__ import annotations

import logging
from typing import Any

from zae_limiter.schema import (
    BUCKET_FIELD_DISABLED,
    DEFAULT_RESOURCE,
    GSI2_NAME,
    GSI3_NAME,
    decode_disabled,
    gsi2_pk_resource,
    gsi3_pk_entity,
    pk_entity,
    pk_resource,
    sk_config,
    sk_state,
)

logger = logging.getLogger(__name__)


def stamp_bucket(client: Any, table_name: str, pk: str, disabled: bool) -> None:
    """Set or remove the disabled attribute on one bucket item."""
    kwargs: dict[str, Any] = {
        "TableName": table_name,
        "Key": {"PK": {"S": pk}, "SK": {"S": sk_state()}},
        "ExpressionAttributeNames": {"#disabled": BUCKET_FIELD_DISABLED},
        "ConditionExpression": "attribute_exists(PK)",
    }
    if disabled:
        kwargs["UpdateExpression"] = "SET #disabled = :true"
        kwargs["ExpressionAttributeValues"] = {":true": {"BOOL": True}}
    else:
        kwargs["UpdateExpression"] = "REMOVE #disabled"

    try:
        client.update_item(**kwargs)
    except client.exceptions.ConditionalCheckFailedException:
        logger.debug("Bucket %s vanished before stamping", pk)


def resolve_disabled(
    client: Any,
    table_name: str,
    namespace_id: str,
    entity_id: str,
    resource: str,
) -> bool:
    """Resolve the effective disabled state for an entity+resource (ADR-125).

    Sync boto3 mirror of ``Repository.resolve_disabled``: walks
    entity(resource) -> entity(_default_) -> resource and returns the first
    level that sets `disabled` explicitly, defaulting to ``False`` when
    nothing along the walk does. The entity(_default_) level is skipped when
    `resource` already IS `_default_`, exactly as the async walk does.

    Reads whatever `apply_changes` has already written to DynamoDB for this
    apply, so a change whose manifest data omits `disabled` still resolves to
    the level above it (including "nothing set", which is False) rather than
    being conflated with an explicit `False`.
    """
    levels: list[tuple[str, str]] = [
        (pk_entity(namespace_id, entity_id), sk_config(resource)),
    ]
    if resource != DEFAULT_RESOURCE:
        levels.append((pk_entity(namespace_id, entity_id), sk_config(DEFAULT_RESOURCE)))
    levels.append((pk_resource(namespace_id, resource), sk_config()))

    for pk, sk in levels:
        response = client.get_item(TableName=table_name, Key={"PK": {"S": pk}, "SK": {"S": sk}})
        item = response.get("Item")
        if item is None:
            continue
        value = decode_disabled(item)
        if value is not None:
            return value

    return False


def fanout_resource(
    client: Any, table_name: str, namespace_id: str, resource: str, disabled: bool
) -> int:
    """Stamp every bucket for a resource. Returns the number stamped."""
    return _fanout(
        client,
        table_name,
        index=GSI2_NAME,
        pk_name="GSI2PK",
        sk_name="GSI2SK",
        pk_value=gsi2_pk_resource(namespace_id, resource),
        sk_prefix="BUCKET#",
        disabled=disabled,
    )


def fanout_entity(
    client: Any,
    table_name: str,
    namespace_id: str,
    entity_id: str,
    resource: str | None,
    disabled: bool,
) -> int:
    """Stamp every bucket for an entity. Returns the number stamped."""
    return _fanout(
        client,
        table_name,
        index=GSI3_NAME,
        pk_name="GSI3PK",
        sk_name="GSI3SK",
        pk_value=gsi3_pk_entity(namespace_id, entity_id),
        sk_prefix=f"BUCKET#{resource}#" if resource else "BUCKET#",
        disabled=disabled,
    )


def _fanout(
    client: Any,
    table_name: str,
    index: str,
    pk_name: str,
    sk_name: str,
    pk_value: str,
    sk_prefix: str,
    disabled: bool,
) -> int:
    """Discover and stamp matching bucket items, across two passes.

    Runs the paginated discovery query twice (see module docstring), with
    `stamped` shared across both passes so a PK visited on pass one is never
    re-stamped on pass two.
    """
    stamped: set[str] = set()

    for _pass in range(2):
        start_key: dict[str, Any] | None = None

        while True:
            params: dict[str, Any] = {
                "TableName": table_name,
                "IndexName": index,
                "KeyConditionExpression": f"{pk_name} = :pk AND begins_with({sk_name}, :sk)",
                "ExpressionAttributeValues": {
                    ":pk": {"S": pk_value},
                    ":sk": {"S": sk_prefix},
                },
            }
            if start_key:
                params["ExclusiveStartKey"] = start_key
            response = client.query(**params)
            for item in response.get("Items", []):
                pk = item.get("PK", {}).get("S", "")
                if pk and pk not in stamped:
                    stamp_bucket(client, table_name, pk, disabled)
                    stamped.add(pk)
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                break

    return len(stamped)
