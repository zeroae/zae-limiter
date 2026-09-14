"""Sync bucket static params when manifest-applied limits change (issue #481).

Sync boto3 mirror of ``Repository._sync_bucket_params``, because the
provisioner runs inside Lambda where aiobotocore is unavailable. Key
construction is shared via ``zae_limiter.schema``, already vendored into the
Lambda package.

Scope is deliberately **entity-level only**. ``set_resource_defaults`` and
``set_system_defaults`` never touch buckets: a bucket running on defaults
carries a TTL and is recreated with current params when it expires (#271,
#296). Only ``set_limits`` and ``reconcile_bucket_to_defaults`` fan out on the
async side, and this mirror inherits that rule rather than widening it.

Units: config items store whole tokens and seconds; bucket items store
millitokens and milliseconds. Everything crossing over is multiplied by 1000.
"""

from __future__ import annotations

import logging
from typing import Any

from zae_limiter.models import Limit
from zae_limiter.schema import (
    BUCKET_FIELD_CP,
    BUCKET_FIELD_RA,
    BUCKET_FIELD_RP,
    BUCKET_FIELD_TC,
    BUCKET_FIELD_TK,
    DEFAULT_RESOURCE,
    GSI3_NAME,
    LIMIT_FIELD_CP,
    LIMIT_FIELD_RA,
    LIMIT_FIELD_RP,
    bucket_attr,
    calculate_bucket_ttl_seconds,
    calculate_ttl,
    gsi3_pk_entity,
    parse_limit_attr,
    pk_entity,
    pk_resource,
    pk_system,
    sk_config,
    sk_state,
)

logger = logging.getLogger(__name__)

_MANIFEST_KEY = {
    LIMIT_FIELD_CP: "capacity",
    LIMIT_FIELD_RA: "refill_amount",
    LIMIT_FIELD_RP: "refill_period",
}

# Fields removed for a limit that no longer exists in the effective config.
# BUCKET_FIELD_RF is deliberately absent: it is shared across every limit in a
# composite bucket and carries the optimistic lock.
_STALE_FIELDS = (
    BUCKET_FIELD_TK,
    BUCKET_FIELD_CP,
    BUCKET_FIELD_RA,
    BUCKET_FIELD_RP,
    BUCKET_FIELD_TC,
)


def build_bucket_param_update(
    limits: dict[str, dict[str, int]],
    ttl_multiplier: int | None,
    stale_limit_names: set[str] | None,
    now_ms: int,
) -> tuple[str, dict[str, str], dict[str, dict[str, str]]]:
    """Build the SET/REMOVE UpdateExpression for one bucket shard.

    Args:
        limits: Manifest-shaped limits, ``{name: {capacity, refill_amount,
            refill_period}}``, in whole tokens and seconds.
        ttl_multiplier: None leaves ``ttl`` alone; 0 REMOVEs it (entity has
            custom limits, so the bucket must persist); >0 SETs it.
        stale_limit_names: Limit names to strip from the bucket entirely.
        now_ms: Current time, epoch milliseconds.

    Returns:
        ``(update_expr, expr_names, expr_values)``.
    """
    set_parts: list[str] = []
    remove_parts: list[str] = []
    expr_names: dict[str, str] = {}
    expr_values: dict[str, dict[str, str]] = {}

    # Numeric indices for expression names: limit names may contain hyphens,
    # dots, slashes and colons, none of which are legal in an alias.
    for i, (name, decl) in enumerate(limits.items()):
        for alias, field, value in (
            (f"#cp{i}", BUCKET_FIELD_CP, decl["capacity"] * 1000),
            (f"#ra{i}", BUCKET_FIELD_RA, decl["refill_amount"] * 1000),
            (f"#rp{i}", BUCKET_FIELD_RP, decl["refill_period"] * 1000),
        ):
            set_parts.append(f"{alias} = :{alias[1:]}")
            expr_names[alias] = bucket_attr(name, field)
            expr_values[f":{alias[1:]}"] = {"N": str(value)}

    if ttl_multiplier is not None:
        expr_names["#ttl"] = "ttl"
        if ttl_multiplier > 0:
            ttl_seconds = calculate_bucket_ttl_seconds(
                [
                    Limit(
                        name=n,
                        capacity=d["capacity"],
                        refill_amount=d["refill_amount"],
                        refill_period_seconds=d["refill_period"],
                    )
                    for n, d in limits.items()
                ],
                ttl_multiplier,
            )
            if ttl_seconds is not None:
                set_parts.append("#ttl = :ttl_val")
                expr_values[":ttl_val"] = {"N": str(calculate_ttl(now_ms, ttl_seconds))}
        else:
            remove_parts.append("#ttl")

    # A monotonic counter, not the stale name, because limit names may contain
    # characters that are illegal in an expression attribute alias.
    for i, stale_name in enumerate(sorted(stale_limit_names or ())):
        for j, field in enumerate(_STALE_FIELDS):
            alias = f"#stale{i}_{j}"
            expr_names[alias] = bucket_attr(stale_name, field)
            remove_parts.append(alias)

    update_expr = f"SET {', '.join(set_parts)}"
    if remove_parts:
        update_expr += f" REMOVE {', '.join(remove_parts)}"
    return update_expr, expr_names, expr_values


def _update_one_shard(
    client: Any,
    table_name: str,
    pk: str,
    update_expr: str,
    expr_names: dict[str, str],
    expr_values: dict[str, dict[str, str]],
) -> bool:
    """Apply one shard's param update. Returns False if the shard vanished."""
    try:
        client.update_item(
            TableName=table_name,
            Key={"PK": {"S": pk}, "SK": {"S": sk_state()}},
            UpdateExpression=update_expr,
            ConditionExpression="attribute_exists(PK)",
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values,
        )
        return True
    except client.exceptions.ConditionalCheckFailedException:
        # Bucket does not exist yet, or no longer does: TTL can expire a shard
        # between discovery and this write. A bucket created later is created
        # with the current params, so there is nothing to reconcile.
        logger.debug("Bucket %s vanished before param sync", pk)
        return False


def sync_bucket_params(
    client: Any,
    table_name: str,
    namespace_id: str,
    entity_id: str,
    resource: str,
    limits: dict[str, dict[str, int]],
    ttl_multiplier: int | None,
    stale_limit_names: set[str] | None,
    now_ms: int,
) -> int:
    """Push changed limit params to every shard of one entity+resource bucket.

    Two discovery passes, exactly like the ADR-125 disable fan-out in
    ``fanout.py``: the second catches a bucket created by an ``acquire()``
    already in flight when the first pass's query ran. Mitigates, but does not
    eliminate, that race.

    Returns the number of shards actually written.
    """
    if not limits:
        return 0

    update_expr, expr_names, expr_values = build_bucket_param_update(
        limits, ttl_multiplier, stale_limit_names, now_ms
    )

    synced: set[str] = set()
    written = 0
    for _pass in range(2):
        start_key: dict[str, Any] | None = None
        while True:
            params: dict[str, Any] = {
                "TableName": table_name,
                "IndexName": GSI3_NAME,
                "KeyConditionExpression": "GSI3PK = :pk AND begins_with(GSI3SK, :sk)",
                "ExpressionAttributeValues": {
                    ":pk": {"S": gsi3_pk_entity(namespace_id, entity_id)},
                    ":sk": {"S": f"BUCKET#{resource}#"},
                },
            }
            if start_key:
                params["ExclusiveStartKey"] = start_key
            response = client.query(**params)
            for item in response.get("Items", []):
                pk = item.get("PK", {}).get("S", "")
                if pk and pk not in synced:
                    synced.add(pk)
                    if _update_one_shard(
                        client, table_name, pk, update_expr, expr_names, expr_values
                    ):
                        written += 1
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                break
    return written


def _decode_limits(item: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Decode composite ``l_{name}_{field}`` attributes into manifest shape.

    A limit missing any of cp/ra/rp is malformed and is skipped rather than
    given a synthesised default, which would silently invent a limit.
    """
    partial: dict[str, dict[str, int]] = {}
    for attr, value in item.items():
        parsed = parse_limit_attr(attr)
        if parsed is None:
            continue
        name, field = parsed
        key = _MANIFEST_KEY.get(field)
        if key is None:
            continue
        partial.setdefault(name, {})[key] = int(value["N"])
    return {
        name: decl
        for name, decl in partial.items()
        if decl.keys() == {"capacity", "refill_amount", "refill_period"}
    }


def resolve_effective_limits(
    client: Any,
    table_name: str,
    namespace_id: str,
    entity_id: str,
    resource: str,
) -> dict[str, dict[str, int]]:
    """Effective limits after an entity's per-resource config is deleted.

    Walks entity(`_default_`) -> resource -> system and returns the first
    level that defines any limits, mirroring the precedence in ADR-100 minus
    the entity(resource) level the caller has just removed. The
    entity(`_default_`) level is skipped when `resource` already IS
    `_default_`, exactly as ``fanout.resolve_disabled`` does.
    """
    levels: list[tuple[str, str]] = []
    if resource != DEFAULT_RESOURCE:
        levels.append((pk_entity(namespace_id, entity_id), sk_config(DEFAULT_RESOURCE)))
    levels.append((pk_resource(namespace_id, resource), sk_config()))
    levels.append((pk_system(namespace_id), sk_config()))

    for pk, sk in levels:
        response = client.get_item(TableName=table_name, Key={"PK": {"S": pk}, "SK": {"S": sk}})
        item = response.get("Item")
        if not item:
            continue
        limits = _decode_limits(item)
        if limits:
            return limits
    return {}
