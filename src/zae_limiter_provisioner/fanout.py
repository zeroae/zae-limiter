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

Both fan-outs re-resolve per bucket, mirroring the async Repository:
fanout_resource skips buckets whose entity has its own overriding value, and
the unscoped (`resource=None`) form of fanout_entity stamps each bucket with
its own resource's resolved value. The handler still applies resource-level
changes before entity-level ones so that within a single apply the entity
level is written before anything reads it back.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from typing import Any

from zae_limiter.schema import (
    BUCKET_FIELD_DISABLED,
    DEFAULT_RESOURCE,
    GSI2_NAME,
    GSI3_NAME,
    decode_cascade,
    decode_disabled,
    gsi2_pk_resource,
    gsi3_pk_entity,
    parse_bucket_pk,
    pk_entity,
    pk_resource,
    sk_config,
    sk_meta,
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
    """Stamp every bucket for a resource, honoring per-entity overrides.

    Mirrors ``Repository._fanout_resource``: an entity whose own config
    resolves to a different value than the resource-level directive is
    skipped, which is what makes an entity-level `disabled: false` a
    carve-out from a disabled resource (ADR-125). Without this check every
    apply would clobber out-of-band per-entity state on manifest-managed
    resources, because `differ.py` emits a change for every resource in the
    manifest on every apply whether or not anything changed.

    Returns the number of buckets stamped.
    """
    effective_by_entity: dict[str, bool] = {}

    def _decide(pk: str) -> bool | None:
        _ns, entity_id, _res, _shard = parse_bucket_pk(pk)
        if entity_id not in effective_by_entity:
            effective_by_entity[entity_id] = resolve_disabled(
                client, table_name, namespace_id, entity_id, resource
            )
        if effective_by_entity[entity_id] != disabled:
            # This entity overrides the resource-level value; leave it alone.
            return None
        return disabled

    return _fanout(
        client,
        table_name,
        index=GSI2_NAME,
        pk_name="GSI2PK",
        sk_name="GSI2SK",
        pk_value=gsi2_pk_resource(namespace_id, resource),
        sk_prefix="BUCKET#",
        disabled=disabled,
        decide=_decide,
    )


def fanout_entity(
    client: Any,
    table_name: str,
    namespace_id: str,
    entity_id: str,
    resource: str | None,
    disabled: bool,
) -> int:
    """Stamp every bucket for an entity. Returns the number stamped.

    When `resource` is None, this is applying the entity's `_default_`
    directive across every resource the entity has a bucket for. A
    resource-specific override for this same entity (its own per-resource
    config, or the resource's own `disabled` config) can outrank that
    `_default_` directive in `resolve_disabled`'s walk, exactly as an
    entity's own override outranks a resource-level fan-out in
    `fanout_resource`. Each discovered bucket's own resource is therefore
    re-resolved via `resolve_disabled` and stamped with its OWN resolved
    value, ignoring `disabled` (ADR-125), mirroring
    `Repository._fanout_entity`. Skipping the buckets whose resolution
    disagrees would be wrong for a *clear*: once the entity's `_default_`
    value is gone, a resource whose own config says the opposite becomes the
    deciding level and its buckets must be restamped to that new value. When
    scoped to one resource, the caller's directive is unambiguous for every
    discovered bucket, so all buckets are stamped with `disabled` directly.
    """
    effective_by_resource: dict[str, bool] = {}

    def _decide(pk: str) -> bool | None:
        if resource is not None:
            return disabled
        _ns, _eid, bucket_resource, _shard = parse_bucket_pk(pk)
        if bucket_resource not in effective_by_resource:
            effective_by_resource[bucket_resource] = resolve_disabled(
                client, table_name, namespace_id, entity_id, bucket_resource
            )
        return effective_by_resource[bucket_resource]

    return _fanout(
        client,
        table_name,
        index=GSI3_NAME,
        pk_name="GSI3PK",
        sk_name="GSI3SK",
        pk_value=gsi3_pk_entity(namespace_id, entity_id),
        sk_prefix=f"BUCKET#{resource}#" if resource else "BUCKET#",
        disabled=disabled,
        decide=_decide,
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
    decide: Callable[[str], bool | None] | None = None,
) -> int:
    """Discover and stamp matching bucket items, across two passes.

    Runs the paginated discovery query twice (see module docstring), with
    `stamped` shared across both passes so a PK visited on pass one is never
    re-stamped on pass two. `decide`, when given, chooses the value to stamp
    on each newly-discovered PK, or returns None to leave it alone; a PK it
    declines is NOT added to `stamped`, so it is re-considered (cheaply,
    since the caller's resolutions are memoized) on a later pass or page.
    """
    stamped: set[str] = set()

    for _pass in range(2):
        for pk in _query_bucket_pks(
            client, table_name, index, pk_name, sk_name, pk_value, sk_prefix
        ):
            if pk not in stamped:
                value = disabled if decide is None else decide(pk)
                if value is None:
                    continue
                stamp_bucket(client, table_name, pk, value)
                stamped.add(pk)

    return len(stamped)


def _query_bucket_pks(
    client: Any,
    table_name: str,
    index: str,
    pk_name: str,
    sk_name: str,
    pk_value: str,
    sk_prefix: str,
) -> Iterator[str]:
    """Every bucket PK one paginated discovery query returns."""
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
            if pk:
                yield pk
        start_key = response.get("LastEvaluatedKey")
        if not start_key:
            return


# --- ADR-146: the cascade policy --------------------------------------------


def resolve_cascade(
    client: Any,
    table_name: str,
    namespace_id: str,
    entity_id: str,
    resource: str,
) -> bool | None:
    """The cascade policy the ADR-125 walk resolves, or None when no level sets one.

    Sync mirror of ``Repository.resolve_access``'s cascade half (ADR-146).
    None means the entity's own META ``cascade`` decides. Strongly consistent:
    the fan-out calls it right after the apply wrote the policy, and an
    eventually consistent read can return the old one, which would then be
    stamped on every bucket (pre-release review).
    """
    for pk, sk in _walk_keys(namespace_id, entity_id, resource):
        item = client.get_item(
            TableName=table_name,
            Key={"PK": {"S": pk}, "SK": {"S": sk}},
            ConsistentRead=True,
        ).get("Item")
        if item is not None:
            value = decode_cascade(item)
            if value is not None:
                return value
    return None


def _walk_keys(namespace_id: str, entity_id: str, resource: str) -> list[tuple[str, str]]:
    """entity(resource) -> entity(_default_) -> resource, as `resolve_disabled` walks."""
    levels = [(pk_entity(namespace_id, entity_id), sk_config(resource))]
    if resource != DEFAULT_RESOURCE:
        levels.append((pk_entity(namespace_id, entity_id), sk_config(DEFAULT_RESOURCE)))
    levels.append((pk_resource(namespace_id, resource), sk_config()))
    return levels


def _owner(
    client: Any, table_name: str, namespace_id: str, entity_id: str
) -> tuple[bool, str | None] | None:
    """An entity's META ``(cascade, parent_id)``, or None when it has no META.

    Strongly consistent, like ``resolve_cascade``.
    """
    item = client.get_item(
        TableName=table_name,
        Key={"PK": {"S": pk_entity(namespace_id, entity_id)}, "SK": {"S": sk_meta()}},
        ConsistentRead=True,
    ).get("Item")
    if item is None:
        return None
    return item.get("cascade", {}).get("BOOL", False), item.get("parent_id", {}).get("S")


def stamp_bucket_cascade(
    client: Any, table_name: str, pk: str, cascade: bool, parent_id: str | None
) -> None:
    """Write one bucket's effective cascade policy and owner `parent_id` (ADR-146).

    Mirrors ``Repository._stamp_bucket_cascade``.
    """
    values: dict[str, Any] = {":c": {"BOOL": cascade}}
    if parent_id is not None:
        update = "SET #c = :c, #p = :p"
        values[":p"] = {"S": parent_id}
    else:
        update = "SET #c = :c REMOVE #p"
    try:
        client.update_item(
            TableName=table_name,
            Key={"PK": {"S": pk}, "SK": {"S": sk_state()}},
            UpdateExpression=update,
            ConditionExpression="attribute_exists(PK)",
            ExpressionAttributeNames={"#c": "cascade", "#p": "parent_id"},
            ExpressionAttributeValues=values,
        )
    except client.exceptions.ConditionalCheckFailedException:
        logger.debug("Bucket %s vanished before stamping", pk)


def fanout_cascade(
    client: Any,
    table_name: str,
    namespace_id: str,
    *,
    resource: str | None = None,
    entity_id: str | None = None,
) -> int:
    """Restamp every bucket a cascade-policy change can reach (ADR-146).

    Mirrors ``Repository._fanout_cascade``: scoped to a resource (GSI2) or an
    entity (GSI3; ``resource`` None or ``_default_`` means every resource of
    it), two discovery passes, and every bucket stamped with the policy
    resolved for its own entity and resource, falling back to the entity's
    META ``cascade``, plus the owner's ``parent_id``. A bucket whose entity
    has no META is skipped.

    Returns the number of buckets stamped.
    """
    scope = None if resource == DEFAULT_RESOURCE else resource
    if entity_id is not None:
        query = (GSI3_NAME, "GSI3PK", "GSI3SK", gsi3_pk_entity(namespace_id, entity_id))
        prefix = f"BUCKET#{scope}#" if scope else "BUCKET#"
    else:
        assert scope is not None
        query = (GSI2_NAME, "GSI2PK", "GSI2SK", gsi2_pk_resource(namespace_id, scope))
        prefix = "BUCKET#"

    owners: dict[str, tuple[bool, str | None] | None] = {}
    targets: dict[tuple[str, str], tuple[bool, str | None] | None] = {}
    stamped: set[str] = set()
    for _pass in range(2):
        for pk in _query_bucket_pks(client, table_name, *query, prefix):
            if pk in stamped:
                continue
            _ns, eid, bucket_resource, _shard = parse_bucket_pk(pk)
            key = (eid, bucket_resource)
            if key not in targets:
                if eid not in owners:
                    owners[eid] = _owner(client, table_name, namespace_id, eid)
                owner = owners[eid]
                if owner is None:
                    targets[key] = None
                else:
                    meta_cascade, parent_id = owner
                    policy = resolve_cascade(client, table_name, namespace_id, eid, bucket_resource)
                    effective = bool(parent_id) and (meta_cascade if policy is None else policy)
                    targets[key] = (effective, parent_id)
            target = targets[key]
            if target is None:
                continue
            stamp_bucket_cascade(client, table_name, pk, *target)
            stamped.add(pk)
    return len(stamped)
