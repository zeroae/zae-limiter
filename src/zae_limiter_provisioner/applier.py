"""Applies limit changes to DynamoDB.

Uses boto3 (sync) directly, like the aggregator. This module runs inside
Lambda where aiobotocore is not available.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import boto3
from botocore.exceptions import ClientError

from zae_limiter.exceptions import VersionMismatchError
from zae_limiter.schedule import encode, encode_reset
from zae_limiter.schema import (
    CONFIG_FIELD_CASCADE,
    CONFIG_FIELD_DISABLED,
    CONFIG_FIELD_SCHED_TZ,
    LIMIT_FIELD_RSA,
    LIMIT_FIELD_RSCHED,
    LIMIT_FIELD_SCHED,
    LIMIT_FIELD_SOFT,
    RESERVED_NAMESPACE,
    config_limit_names,
    decode_cascade,
    encode_cascade,
    encode_disabled,
    limit_attr,
    pk_entity,
    pk_resource,
    pk_system,
    sk_config,
    sk_version,
)
from zae_limiter.version import (
    MIN_READER_VERSION_FOR_CASCADE_POLICY,
    MIN_READER_VERSION_FOR_NON_ENFORCING,
    MIN_READER_VERSION_FOR_RESET_AFTER,
    cascade_policy_refusal,
    non_enforcing_refusal,
    ratcheted_client_min_version,
    reads_reset_after,
    reset_after_refusal,
)

from .differ import Change
from .manifest import entries_from_manifest

try:
    # The provisioner zip carries no full `zae_limiter/__init__.py`, so the
    # build's version comes from the vendored `_version.py` (#638), with the
    # same fallback the package itself uses when hatch-vcs wrote none.
    from zae_limiter._version import __version__
except ImportError:  # pragma: no cover - only a source tree without hatch-vcs
    __version__ = "0.0.0+unknown"

logger = logging.getLogger(__name__)


@dataclass
class ApplyResult:
    """Result of applying changes."""

    created: int = 0
    updated: int = 0
    deleted: int = 0
    errors: list[str] = field(default_factory=list)
    # (level, target) of every change whose stored cascade policy it changed
    # (ADR-146). Only these need their buckets restamped.
    cascade_changed: list[tuple[str, str]] = field(default_factory=list)
    # (level, target, names) of every system- or resource-level change whose
    # limits' soft-ness it changed (#467). Those levels reach buckets only at
    # TTL otherwise, so these are restamped by a change-only fan-out.
    soft_changed: list[tuple[str, str | None, list[str]]] = field(default_factory=list)


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
        # #640: a limit carrying `reset_after_seconds` is stored under `w_`, the
        # prefix a reader predating ADR-139 does not scan — mirroring
        # `Repository._serialize_composite_limits`. Every field of the limit
        # moves with it; `schema.config_limit_names` reads either.
        windowed = decl.get("reset_after_seconds") is not None

        def attr(field: str, name: str = name, windowed: bool = windowed) -> str:
            return limit_attr(name, field, windowed=windowed)

        item[attr("cp")] = {"N": str(decl["capacity"])}
        item[attr("ra")] = {"N": str(decl["refill_amount"])}
        item[attr("rp")] = {"N": str(decl["refill_period"])}
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
                item[attr(attr_field)] = {"S": encoder(entries)[0]}

        # ADR-139: a duration-window quota's window length, in seconds. Same
        # storage rule as cp/ra/rp — written only when the decl carries it,
        # and config items are full-replace `PutItem`s, so a limit converted
        # back to a drip loses `rsa` simply by omitting it here, no REMOVE
        # needed.
        reset_after_seconds = decl.get("reset_after_seconds")
        if reset_after_seconds is not None:
            item[attr(LIMIT_FIELD_RSA)] = {"N": str(reset_after_seconds)}
        # #467: written only for a soft limit, like the client does.
        if decl.get("soft"):
            item[attr(LIMIT_FIELD_SOFT)] = {"BOOL": True}

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


# Mirrors `Repository._CLIENT_MIN_RATCHET_ATTEMPTS`.
_CLIENT_MIN_RATCHET_ATTEMPTS = 3


def require_reset_after_readers(
    changes: list[Change],
    table_name: str,
    client: Any | None = None,
) -> None:
    """Refuse an apply that stores a ``reset_after`` limit the stack cannot read (#638 A).

    Boto3 mirror of ``Repository._require_reset_after_readers``: called before
    ``apply_changes`` writes anything, so a refusal leaves the table exactly as
    it was. Free unless a create/update change declares ``reset_after``; then
    one strongly consistent ``GetItem`` of the version record, and — the first
    time only — one conditional ``UpdateItem`` raising ``client_min_version``
    (never lowering it).

    Mostly a guard for a hand-deployed provisioner: the provisioner that parses
    ``reset_after`` is normally deployed by the same step that deploys a
    ``reset_after``-aware aggregator and stamps ``lambda_version``.

    Raises:
        VersionMismatchError: the version record is missing, or its
            ``lambda_version`` predates ``reset_after``. Out of the Lambda
            handler this is a CloudFormation FAILED, or an ``errorMessage`` the
            ``limits apply`` CLI prints before exiting 1.
    """
    declares_reset_after = any(
        decl.get("reset_after_seconds") is not None
        for change in changes
        if change.action in ("create", "update")
        for decl in ((change.data or {}).get("limits") or {}).values()
    )
    if not declares_reset_after:
        return
    _require_readers(table_name, client, MIN_READER_VERSION_FOR_RESET_AFTER, reset_after_refusal)


def require_cascade_policy_readers(
    changes: list[Change],
    table_name: str,
    client: Any | None = None,
) -> None:
    """Refuse an apply that sets a cascade policy the stack cannot keep (ADR-146).

    The ``require_reset_after_readers`` contract, for the cascade policy: free
    unless a create/update change declares ``cascade``, then the same version
    check and ``client_min_version`` ratchet, against 0.16.0. Mirrors
    ``Repository._require_cascade_policy_readers``.

    Raises:
        VersionMismatchError: the record is missing, or its ``lambda_version``
            is unknown or predates the cascade policy.
    """
    declares_cascade = any(
        (change.data or {}).get("cascade") is not None
        for change in changes
        if change.action in ("create", "update")
    )
    if not declares_cascade:
        return
    _require_readers(
        table_name, client, MIN_READER_VERSION_FOR_CASCADE_POLICY, cascade_policy_refusal
    )


def require_non_enforcing_readers(
    changes: list[Change],
    table_name: str,
    client: Any | None = None,
) -> None:
    """Refuse an apply that stores a soft limit or a bypass the stack cannot keep.

    The ``require_cascade_policy_readers`` contract for ADR-151 (#467, #311):
    free unless a create/update change declares a ``soft`` limit or
    ``disabled: bypass``, then the version check and ``client_min_version``
    ratchet against 0.17.0. Mirrors ``Repository._require_non_enforcing_readers``.
    """
    declares = any(
        (change.data or {}).get("disabled") == "bypass"
        or any(decl.get("soft") for decl in ((change.data or {}).get("limits") or {}).values())
        for change in changes
        if change.action in ("create", "update")
    )
    if not declares:
        return
    _require_readers(
        table_name, client, MIN_READER_VERSION_FOR_NON_ENFORCING, non_enforcing_refusal
    )


def _require_readers(
    table_name: str,
    client: Any | None,
    minimum: str,
    refusal: Callable[[bool, str | None], tuple[str, bool]],
) -> None:
    """The version gate shared by ``reset_after`` and the cascade policy."""
    if client is None:
        client = boto3.client("dynamodb")

    key = {"PK": {"S": pk_system(RESERVED_NAMESPACE)}, "SK": {"S": sk_version()}}
    last_error: ClientError | None = None
    for _ in range(_CLIENT_MIN_RATCHET_ATTEMPTS):
        item = client.get_item(TableName=table_name, Key=key, ConsistentRead=True).get("Item")
        lambda_version = (item or {}).get("lambda_version", {}).get("S")
        if not item or not reads_reset_after(lambda_version, __version__, minimum):
            message, can_auto_update = refusal(bool(item), lambda_version)
            raise VersionMismatchError(
                client_version=__version__,
                schema_version=(item or {}).get("schema_version", {}).get("S", "unknown"),
                lambda_version=lambda_version,
                message=message,
                can_auto_update=can_auto_update,
            )
        stored_min = item.get("client_min_version", {}).get("S")
        new_min = ratcheted_client_min_version(stored_min, __version__, minimum)
        if new_min is None:
            return
        condition = (
            "client_min_version = :stored"
            if stored_min is not None
            else "attribute_not_exists(client_min_version)"
        )
        values: dict[str, Any] = {":new": {"S": new_min}}
        if stored_min is not None:
            values[":stored"] = {"S": stored_min}
        try:
            client.update_item(
                TableName=table_name,
                Key=key,
                UpdateExpression="SET client_min_version = :new",
                ConditionExpression=f"attribute_exists(PK) AND {condition}",
                ExpressionAttributeValues=values,
            )
            return
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            last_error = e
    assert last_error is not None
    raise last_error


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
            # The write's own ALL_OLD image says what the policy was, at no
            # extra read: only a level whose policy actually changed has its
            # buckets restamped (ADR-146), so a routine apply writes no more.
            if change.action == "delete":
                old = _apply_delete(client, table_name, namespace_id, change)
                _note_cascade_change(result, change, old, None)
                _note_soft_change(result, change, old)
                result.deleted += 1
            elif change.action in ("create", "update"):
                old = _apply_set(client, table_name, namespace_id, change)
                _note_cascade_change(result, change, old, (change.data or {}).get("cascade"))
                _note_soft_change(result, change, old)
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
) -> dict[str, Any]:
    """Apply a create or update change (PutItem); return the replaced item, or {}."""
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
        _add_flags(extra, data)
        item = _build_limit_item(pk, sk, namespace_id, limits, extra)

    elif change.level == "entity":
        assert change.target is not None
        entity_id, resource = change.target.split("/", 1)
        pk = pk_entity(namespace_id, entity_id)
        sk = sk_config(resource)
        extra = {"entity_id": {"S": entity_id}, "resource": {"S": resource}}
        _add_flags(extra, data)
        item = _build_limit_item(pk, sk, namespace_id, limits, extra)

    else:
        raise ValueError(f"Unknown level: {change.level}")

    response = client.put_item(TableName=table_name, Item=item, ReturnValues="ALL_OLD")
    return _old_image(response)


def _add_flags(extra: dict[str, Any], data: dict[str, Any]) -> None:
    """The tri-state `disabled` (ADR-125) and `cascade` (ADR-146) flags, when declared.

    Written only when the manifest declares them: the item is a full-replace
    `PutItem`, so leaving a flag out is how the manifest clears it.
    """
    disabled_attr = encode_disabled(data.get("disabled"))
    if disabled_attr is not None:
        extra[CONFIG_FIELD_DISABLED] = disabled_attr
    cascade_attr = encode_cascade(data.get("cascade"))
    if cascade_attr is not None:
        extra[CONFIG_FIELD_CASCADE] = cascade_attr


def _note_cascade_change(
    result: ApplyResult, change: Change, old: dict[str, Any], new: bool | None
) -> None:
    """Record a resource or entity change that changed its stored cascade policy."""
    if change.target is not None and decode_cascade(old) != new:
        result.cascade_changed.append((change.level, change.target))


def _soft_names(item: dict[str, Any]) -> set[str]:
    """The limits a stored config item marks soft (#467)."""
    try:
        stored = config_limit_names(item)
    except ValueError:
        # An unreadable old image errs toward restamping everything it names.
        return {name for name in item if name.endswith(f"_{LIMIT_FIELD_SOFT}")}
    return {
        name
        for name, windowed in stored.items()
        if item.get(limit_attr(name, LIMIT_FIELD_SOFT, windowed=windowed), {}).get("BOOL")
    }


def _note_soft_change(result: ApplyResult, change: Change, old: dict[str, Any]) -> None:
    """Record a system- or resource-level change that changed any limit's soft-ness.

    Entity levels need none: the param sync restamps their buckets on every
    apply. The write's own ``ALL_OLD`` image is the old side, at no extra read.
    """
    if change.level not in ("system", "resource"):
        return
    new = (
        {
            name
            for name, decl in ((change.data or {}).get("limits") or {}).items()
            if decl.get("soft")
        }
        if change.action != "delete"
        else set()
    )
    changed = _soft_names(old) ^ new
    if changed:
        result.soft_changed.append((change.level, change.target, sorted(changed)))


def _old_image(response: Any) -> dict[str, Any]:
    """The `ALL_OLD` image a write returned, or {} when there was none."""
    old = response.get("Attributes") if isinstance(response, dict) else None
    return old if isinstance(old, dict) else {}


def _apply_delete(
    client: Any,
    table_name: str,
    namespace_id: str,
    change: Change,
) -> dict[str, Any]:
    """Apply a delete change (DeleteItem); return the deleted item, or {}."""
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

    response = client.delete_item(
        TableName=table_name, Key={"PK": {"S": pk}, "SK": {"S": sk}}, ReturnValues="ALL_OLD"
    )
    return _old_image(response)
