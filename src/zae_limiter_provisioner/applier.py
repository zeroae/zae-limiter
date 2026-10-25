"""Applies limit changes to DynamoDB.

Uses boto3 (sync) directly, like the aggregator. This module runs inside
Lambda where aiobotocore is not available.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
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
    decode_disabled,
    encode_cascade,
    encode_disabled,
    limit_attr,
    parse_limit_attr,
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
    # Resource and entity changes whose stored tri-state `disabled` it changed
    # (ADR-125). Only these need the disable fan-out.
    disabled_changed: list[Change] = field(default_factory=list)
    # Entity changes whose stored limits it changed (#481). Only these need
    # their buckets' params synced.
    limits_changed: list[Change] = field(default_factory=list)
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


# Concurrent `GetItem`s for the pre-read in `drop_unchanged`. Below botocore's
# default connection pool of 10, so the threads never wait on a connection.
_READ_WORKERS = 8


def drop_unchanged(
    changes: list[Change],
    table_name: str,
    namespace_id: str,
    client: Any | None = None,
    *,
    stored_out: dict[int, dict[str, Any]] | None = None,
) -> list[Change]:
    """The changes that would actually change the table.

    ``compute_diff`` emits a create or update for **every** level the manifest
    declares, because the ``#PROVISIONER`` record holds only which levels are
    managed, not what they hold. Applied as is, a one-line edit to a manifest
    of a thousand entities rewrote a thousand config items and ran a thousand
    bucket fan-outs, one call at a time.

    This reads each create/update's live config item (strongly consistent, so
    an edit made just before cannot be missed) and drops the change when the
    item it would write is **identical** to the one stored. That write would be
    a no-op ``PutItem``, so skipping it cannot change what the table holds, and
    comparing against the live item rather than the previous manifest keeps the
    apply reconciling drift: a level changed out of band (``entity set-limits``)
    differs from the manifest's item and is rewritten as before.

    ``GetItem`` rather than ``BatchGetItem`` because the provisioner role grants
    only the former: a code-only Lambda upgrade must not need a stack update.
    The reads run on a small thread pool (boto3 clients are thread-safe).

    Deletes are always kept. A change whose item cannot be built (two
    timezones on one item) or whose read fails is kept too, so
    ``apply_changes`` reports it or writes it as before: nothing is dropped
    without the stored item proving it redundant.

    ``stored_out``, when given, receives the live item read for each change
    (keyed by ``id(change)``, ``{}`` when absent), so the caller can tell
    which fan-outs a kept change will need before writing it
    (``fanouts_needed``). A change with no entry was not read.
    """
    candidates: list[tuple[int, tuple[str, str], dict[str, Any]]] = []
    for i, change in enumerate(changes):
        if change.action not in ("create", "update"):
            continue
        try:
            pk, sk, item = _build_change_item(namespace_id, change)
        except Exception:
            continue
        candidates.append((i, (pk, sk), item))
    if not candidates:
        return list(changes)

    if client is None:
        client = boto3.client("dynamodb")

    def _read(key: tuple[str, str]) -> dict[str, Any] | None:
        try:
            response = client.get_item(
                TableName=table_name,
                Key={"PK": {"S": key[0]}, "SK": {"S": key[1]}},
                ConsistentRead=True,
            )
        except Exception as e:
            logger.warning("Could not read %s %s, writing it: %s", key[0], key[1], e)
            return None
        item = response.get("Item") if isinstance(response, dict) else None
        # Absent is {}: a create, whose old side is known to be empty.
        return item if isinstance(item, dict) else {}

    workers = min(_READ_WORKERS, len(candidates))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        stored = list(pool.map(_read, [key for _i, key, _item in candidates]))

    unchanged = {
        i
        for (i, _key, item), live in zip(candidates, stored, strict=True)
        if live is not None and _normalized(live) == _normalized(item)
    }
    if stored_out is not None:
        for (i, _key, _item), live in zip(candidates, stored, strict=True):
            if live is not None:
                stored_out[id(changes[i])] = live
    return [change for i, change in enumerate(changes) if i not in unchanged]


def _normalized(value: Any) -> Any:
    """An attribute value with every number compared as a number.

    DynamoDB stores a number canonically, so a manifest's ``capacity: 1000.0``
    is written as ``"1000.0"`` and read back as ``"1000"``. Compared as strings
    the entry would differ on every apply: correct, but rewritten and fanned
    out each time.
    """
    if isinstance(value, dict):
        if set(value) == {"N"} and isinstance(value["N"], str):
            try:
                return ("N", Decimal(value["N"]))
            except InvalidOperation:
                return value
        return {k: _normalized(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalized(v) for v in value]
    return value


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
            # The write's own ALL_OLD image says what the level held, at no
            # extra read: only a level whose policy, `disabled` or limits
            # actually changed has its buckets touched (ADR-146, ADR-125,
            # #481), so a routine apply writes no more.
            if change.action == "delete":
                old = _apply_delete(client, table_name, namespace_id, change)
                _note_changes(result, change, old, {})
                _note_soft_change(result, change, old)
                result.deleted += 1
            elif change.action in ("create", "update"):
                old, new = _apply_set(client, table_name, namespace_id, change)
                _note_changes(result, change, old, new)
                _note_soft_change(result, change, old)
                if change.action == "create":
                    result.created += 1
                else:
                    result.updated += 1
        except Exception as e:
            logger.warning("Failed to %s %s %s: %s", change.action, change.level, change.target, e)
            result.errors.append(f"{change.action} {change.level} {change.target}: {e}")

    return result


def _config_key(namespace_id: str, change: Change) -> tuple[str, str]:
    """The (PK, SK) of the config item a change writes or deletes."""
    if change.level == "system":
        return pk_system(namespace_id), sk_config()
    if change.level == "resource":
        assert change.target is not None
        return pk_resource(namespace_id, change.target), sk_config()
    if change.level == "entity":
        assert change.target is not None
        entity_id, resource = change.target.split("/", 1)
        return pk_entity(namespace_id, entity_id), sk_config(resource)
    raise ValueError(f"Unknown level: {change.level}")


def _build_change_item(namespace_id: str, change: Change) -> tuple[str, str, dict[str, Any]]:
    """The full config item a create or update writes, with its key."""
    data = change.data or {}
    limits = data.get("limits", {})
    pk, sk = _config_key(namespace_id, change)

    extra: dict[str, Any] = {}
    if change.level == "system":
        on_unavailable = data.get("on_unavailable")
        if on_unavailable is not None:
            extra["on_unavailable"] = {"S": on_unavailable}
    elif change.level == "resource":
        assert change.target is not None
        extra["resource"] = {"S": change.target}
        _add_flags(extra, data)
    else:
        assert change.target is not None
        entity_id, resource = change.target.split("/", 1)
        extra["entity_id"] = {"S": entity_id}
        extra["resource"] = {"S": resource}
        _add_flags(extra, data)
    return pk, sk, _build_limit_item(pk, sk, namespace_id, limits, extra)


def _apply_set(
    client: Any,
    table_name: str,
    namespace_id: str,
    change: Change,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply a create or update change (PutItem); return (replaced item or {}, new item)."""
    _pk, _sk, item = _build_change_item(namespace_id, change)
    response = client.put_item(TableName=table_name, Item=item, ReturnValues="ALL_OLD")
    return _old_image(response), item


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


def _note_changes(
    result: ApplyResult, change: Change, old: dict[str, Any], new: dict[str, Any]
) -> None:
    """Record which bucket fan-outs a resource or entity write makes necessary.

    Each fan-out derives bucket state from one part of the config, so it is
    needed only when that part of this level changed: the resolution walks
    for `disabled` and `cascade` take the first level that sets a value, and
    a level whose own value is unchanged cannot change the answer. A level
    above or below that did change is its own change, with its own fan-out.
    """
    kinds = fanouts_for(change, old, new)
    if "cascade" in kinds:
        assert change.target is not None
        result.cascade_changed.append((change.level, change.target))
    if "disabled" in kinds:
        result.disabled_changed.append(change)
    if "sync" in kinds:
        result.limits_changed.append(change)


def fanouts_for(change: Change, old: dict[str, Any], new: dict[str, Any]) -> set[str]:
    """The bucket fan-outs (``disabled``, ``cascade``, ``sync``) a write from ``old`` to
    ``new`` makes necessary. System-level writes need none of the three."""
    if change.target is None:
        return set()
    kinds: set[str] = set()
    if decode_cascade(old) != decode_cascade(new):
        kinds.add("cascade")
    if decode_disabled(old) != decode_disabled(new):
        kinds.add("disabled")
    if change.level == "entity" and _limit_attrs(old) != _limit_attrs(new):
        kinds.add("sync")
    return kinds


def fanouts_needed(namespace_id: str, change: Change, stored: dict[str, Any] | None) -> set[str]:
    """The fan-outs a change will need, predicted before it is written.

    From the live item ``drop_unchanged`` read (``stored``); with none — a
    delete, an unread or unbuildable item — every fan-out the level can have,
    since the old side is unknown. The write's own ``ALL_OLD`` image remains
    the authority (``apply_changes``): this only decides what to record ahead.
    """
    if change.target is None:
        return set()
    every = {"disabled", "cascade"} | ({"sync"} if change.level == "entity" else set())
    if stored is None or change.action not in ("create", "update"):
        return every
    try:
        _pk, _sk, new = _build_change_item(namespace_id, change)
    except Exception:
        return every
    return fanouts_for(change, stored, new)


def _limit_attrs(item: dict[str, Any]) -> dict[str, Any]:
    """The attributes of a config item that say what its limits are."""
    return {
        name: value
        for name, value in item.items()
        if name == CONFIG_FIELD_SCHED_TZ or parse_limit_attr(name) is not None
    }


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

    Entity levels need none: the param sync restamps their buckets whenever
    the level's limits changed, and soft-ness is one of them. The write's own
    ``ALL_OLD`` image is the old side, at no extra read.
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
    pk, sk = _config_key(namespace_id, change)
    response = client.delete_item(
        TableName=table_name, Key={"PK": {"S": pk}, "SK": {"S": sk}}, ReturnValues="ALL_OLD"
    )
    return _old_image(response)
