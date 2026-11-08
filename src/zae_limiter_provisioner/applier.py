"""Applies limit changes to DynamoDB.

Uses boto3 (sync) directly, like the aggregator. This module runs inside
Lambda where aiobotocore is not available.
"""

from __future__ import annotations

import logging
import random
import threading
import time
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
    gsi3_pk_entity_config,
    gsi3_sk_entity,
    limit_attr,
    parse_limit_attr,
    pk_entity,
    pk_resource,
    pk_system,
    sk_config,
    sk_entity_config_resources,
    sk_resources,
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
from .workers import WORKERS

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
    # Pending fan-out items left after the apply, its own failures and those
    # still leased to another (possibly dead) apply included (#739 re-review 2
    # #4). Set by the handler once the apply has finished.
    pending: int = 0


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
        # Both GSI4 keys, or the item is left out of the index and
        # `purge_namespace` never finds it. GSI4SK = PK, as the client writes.
        "GSI4PK": {"S": namespace_id},
        "GSI4SK": {"S": pk},
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


# Concurrent `GetItem`s for the pre-read in `drop_unchanged`, and concurrent
# config writes in `apply_changes` (`workers.WORKERS`).
_READ_WORKERS = WORKERS
_WRITE_WORKERS = WORKERS


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

    Deletes are always kept, and read like the rest. A change whose item cannot be built (two
    timezones on one item) or whose read fails is kept too, so
    ``apply_changes`` reports it or writes it as before: nothing is dropped
    without the stored item proving it redundant.

    ``stored_out``, when given, receives the live item read for each change
    (keyed by ``id(change)``, ``{}`` when absent), so the caller can tell
    which fan-outs a kept change will need before writing it
    (``fanouts_needed``). A change with no entry was not read.
    """
    # A delete is read too, never dropped: its stored item says which fan-outs
    # removing it needs (`fanouts_needed`), so it does not run all of them.
    candidates: list[tuple[int, tuple[str, str], dict[str, Any] | None]] = []
    for i, change in enumerate(changes):
        if change.action == "delete":
            candidates.append((i, config_key(namespace_id, change), None))
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
        if item is not None and live is not None and _normalized(live) == _normalized(item)
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
    stored: dict[int, dict[str, Any]] | None = None,
) -> ApplyResult:
    """Apply a list of changes to DynamoDB.

    Args:
        changes: List of Change objects from the differ.
        table_name: DynamoDB table name.
        namespace_id: Opaque namespace ID (e.g., 'a7x3kq').
        client: Optional boto3 DynamoDB client (injected for testing).
        stored: The live items ``drop_unchanged`` read (``stored_out``), keyed by
            ``id(change)``. An entity write needs the item it replaces to keep
            the registry count exact and reads it when it has no entry here.

    Returns:
        ApplyResult with counts and any errors.
    """
    result = ApplyResult()

    if not changes:
        return result

    if client is None:
        client = boto3.client("dynamodb")

    def _write(change: Change) -> tuple[dict[str, Any], dict[str, Any]] | Exception | None:
        # The write's own ALL_OLD image says what the level held, at no
        # extra read: only a level whose policy, `disabled` or limits
        # actually changed has its buckets touched (ADR-146, ADR-125,
        # #481), so a routine apply writes no more.
        try:
            known_old = None if stored is None else stored.get(id(change))
            if change.action == "delete":
                return _apply_delete(client, table_name, namespace_id, change, known_old), {}
            if change.action in ("create", "update"):
                return _apply_set(client, table_name, namespace_id, change, known_old)
            return None
        except Exception as e:
            return e

    # Every change writes its own config item, and nothing reads them back
    # until every write has returned (the fan-outs run after), so the writes
    # go out `_WRITE_WORKERS` at a time. The one shared item, the entity
    # registry, is updated only by counted transactions, which serialize
    # themselves (`_counted_transact`). The results are recorded in change
    # order, as a serial loop recorded them.
    if len(changes) == 1:
        outcomes = [_write(changes[0])]
    else:
        with ThreadPoolExecutor(max_workers=min(_WRITE_WORKERS, len(changes))) as pool:
            outcomes = list(pool.map(_write, changes))

    for change, outcome in zip(changes, outcomes, strict=True):
        if outcome is None:
            continue
        # A failed write, or a failure reading what it changed, is one error for
        # this change; the rest of the apply carries on, as a serial loop did.
        try:
            if isinstance(outcome, Exception):
                raise outcome
            old, new = outcome
            _note_changes(result, change, old, new)
            _note_soft_change(result, change, old)
            if change.action == "delete":
                result.deleted += 1
            elif change.action == "create":
                result.created += 1
            else:
                result.updated += 1
        except Exception as e:
            logger.warning("Failed to %s %s %s: %s", change.action, change.level, change.target, e)
            result.errors.append(f"{change.action} {change.level} {change.target}: {e}")

    return result


def config_key(namespace_id: str, change: Change) -> tuple[str, str]:
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
    pk, sk = config_key(namespace_id, change)

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
        # The sparse GSI3 entry `Repository.set_limits` writes, which is what
        # `list_entities_with_custom_limits` (`entity list --with-custom-limits`)
        # queries. Its presence also marks the item as counted in
        # #ENTITY_CONFIG_RESOURCES (`_apply_entity_set`).
        extra[GSI3_PK] = {"S": gsi3_pk_entity_config(namespace_id, resource)}
        extra[GSI3_SK] = {"S": gsi3_sk_entity(entity_id)}
        _add_flags(extra, data)
    return pk, sk, _build_limit_item(pk, sk, namespace_id, limits, extra)


# Entity config items carry these, the resource registry its `resources` set.
GSI3_PK = "GSI3PK"
GSI3_SK = "GSI3SK"
_REGISTRY_RESOURCES = "resources"
# A counted write that keeps losing its condition to concurrent writers.
_COUNTED_WRITE_ATTEMPTS = 3

# Every counted transaction updates the namespace's one `#ENTITY_CONFIG_RESOURCES`
# item, and DynamoDB cancels concurrent transactions on one item
# (`TransactionConflict`). An apply's config writes run on a thread pool
# (`apply_changes`), so its counted transactions take this lock and run one at
# a time; plain writes, including every update of an item already counted, do
# not. On AWS, 8 unserialized writers had 684 of 1190 transactions cancelled
# and 134 of 640 entity creates give up.
_COUNTED_TRANSACTION_LOCK = threading.Lock()
# A conflict with another writer (a concurrent apply, or the API), or a
# throttled transaction (botocore does not retry a cancellation), is retried
# with full jitter before it counts as a lost condition.
_CONFLICT_RETRIES = 5
_CONFLICT_BACKOFF_BASE_S = 0.025
_CONFLICT_BACKOFF_CAP_S = 0.4


def resource_registry_update(namespace_id: str, resource: str, *, add: bool) -> dict[str, Any]:
    """The `UpdateItem` arguments that add a resource to, or drop it from, ``#RESOURCES``.

    The registry `resource list` reads (`Repository.list_resources_with_defaults`),
    kept exactly as `set_resource_defaults` / `delete_resource_defaults` keep it.
    A String Set: adding a member twice or deleting an absent one is a no-op,
    so neither needs a create-vs-update guard.
    """
    key = {"PK": {"S": pk_system(namespace_id)}, "SK": {"S": sk_resources()}}
    member = {"SS": [resource]}
    if not add:
        return {
            "Key": key,
            "UpdateExpression": "DELETE #rs :r0",
            "ExpressionAttributeNames": {"#rs": _REGISTRY_RESOURCES},
            "ExpressionAttributeValues": {":r0": member},
        }
    return {
        "Key": key,
        "UpdateExpression": "SET #g4p = if_not_exists(#g4p, :g4p), "
        "#g4s = if_not_exists(#g4s, :g4s) ADD #rs :r0",
        "ExpressionAttributeNames": {
            "#rs": _REGISTRY_RESOURCES,
            "#g4p": "GSI4PK",
            "#g4s": "GSI4SK",
        },
        "ExpressionAttributeValues": {
            ":r0": member,
            ":g4p": {"S": namespace_id},
            ":g4s": {"S": pk_system(namespace_id)},
        },
    }


def entity_registry_update(namespace_id: str, resource: str, delta: int) -> dict[str, Any]:
    """The `UpdateItem` arguments that move a resource's ``#ENTITY_CONFIG_RESOURCES`` count.

    The wide-column ref count `entity list-resources` reads
    (`Repository.list_resources_with_entity_configs`, which lists counts > 0):
    one attribute per resource, named by it. The name is only ever an alias
    value, never expression text, since a resource name may carry `.` or `:`.
    """
    return {
        "Key": {"PK": {"S": pk_system(namespace_id)}, "SK": {"S": sk_entity_config_resources()}},
        "UpdateExpression": "SET #g4p = if_not_exists(#g4p, :g4p), "
        "#g4s = if_not_exists(#g4s, :g4s) ADD #r0 :d0",
        "ExpressionAttributeNames": {"#r0": resource, "#g4p": "GSI4PK", "#g4s": "GSI4SK"},
        "ExpressionAttributeValues": {
            ":d0": {"N": str(delta)},
            ":g4p": {"S": namespace_id},
            ":g4s": {"S": pk_system(namespace_id)},
        },
    }


def entity_registry_cleanup(namespace_id: str, resource: str) -> dict[str, Any]:
    """The `UpdateItem` arguments that drop a resource whose count reached zero.

    Mirrors `Repository._cleanup_entity_config_registry`; conditional, so a
    concurrent increment keeps the attribute.
    """
    return {
        "Key": {"PK": {"S": pk_system(namespace_id)}, "SK": {"S": sk_entity_config_resources()}},
        "UpdateExpression": "REMOVE #r0",
        "ConditionExpression": "#r0 <= :z0",
        "ExpressionAttributeNames": {"#r0": resource},
        "ExpressionAttributeValues": {":z0": {"N": "0"}},
    }


def _apply_set(
    client: Any,
    table_name: str,
    namespace_id: str,
    change: Change,
    known_old: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply a create or update change (PutItem); return (replaced item or {}, new item).

    Keeps the registries the CLI's list commands read in step, as the
    `Repository` setters do: a resource level joins ``#RESOURCES``, an entity
    level is counted in ``#ENTITY_CONFIG_RESOURCES`` (`_apply_entity_set`).
    """
    if change.level == "entity":
        return _apply_entity_set(client, table_name, namespace_id, change, known_old)
    _pk, _sk, item = _build_change_item(namespace_id, change)
    if change.level == "resource":
        assert change.target is not None
        # Registered before the config write: a run that stops in between
        # lists a resource with no defaults yet, never hides one that has them.
        client.update_item(
            TableName=table_name, **resource_registry_update(namespace_id, change.target, add=True)
        )
    response = client.put_item(TableName=table_name, Item=item, ReturnValues="ALL_OLD")
    return _old_image(response), item


def _read_config(client: Any, table_name: str, pk: str, sk: str) -> dict[str, Any]:
    """The live config item, strongly consistent; {} when absent."""
    response = client.get_item(
        TableName=table_name, Key={"PK": {"S": pk}, "SK": {"S": sk}}, ConsistentRead=True
    )
    item = response.get("Item") if isinstance(response, dict) else None
    return item if isinstance(item, dict) else {}


def _is_retryable_cancellation(error: ClientError) -> bool:
    """A transaction cancelled by a conflict or a throttle, not by a condition."""
    if error.response.get("Error", {}).get("Code") != "TransactionCanceledException":
        return False
    reasons = {r.get("Code") for r in error.response.get("CancellationReasons", [])}
    return bool(reasons & {"TransactionConflict", "ThrottlingError"}) and (
        "ConditionalCheckFailed" not in reasons
    )


def _counted_transact(
    client: Any,
    transact_items: list[dict[str, Any]],
    then: Callable[[], None] | None = None,
) -> None:
    """Run a counted transaction: one at a time per apply, conflicts retried with jitter.

    A conflict or throttle still there after `_CONFLICT_RETRIES` is raised,
    and the caller treats a conflict as a lost condition (`_lost_condition`):
    re-read, try again. ``then`` runs under the same lock once the
    transaction lands (the registry tidy after a delete, which writes the same
    item and would otherwise collide with the apply's next transaction).
    """
    with _COUNTED_TRANSACTION_LOCK:
        for attempt in range(_CONFLICT_RETRIES + 1):
            try:
                client.transact_write_items(TransactItems=transact_items)
                break
            except ClientError as e:
                if attempt == _CONFLICT_RETRIES or not _is_retryable_cancellation(e):
                    raise
                cap = min(_CONFLICT_BACKOFF_CAP_S, _CONFLICT_BACKOFF_BASE_S * 2**attempt)
                time.sleep(random.uniform(0, cap))
        if then is not None:
            then()


def _lost_condition(error: ClientError) -> bool:
    """A counted write lost to a concurrent one: re-read and try again.

    A plain write whose condition failed, or a transaction cancelled by its
    first item's condition or by a conflict on either item.
    """
    code = error.response.get("Error", {}).get("Code")
    if code == "ConditionalCheckFailedException":
        return True
    if code != "TransactionCanceledException":
        return False
    reasons = [r.get("Code") for r in error.response.get("CancellationReasons", [])]
    return bool(reasons) and (
        reasons[0] == "ConditionalCheckFailed" or "TransactionConflict" in reasons
    )


def _apply_entity_set(
    client: Any,
    table_name: str,
    namespace_id: str,
    change: Change,
    known_old: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Write an entity config item and keep its resource's ref count exact.

    **An entity config item is counted in ``#ENTITY_CONFIG_RESOURCES`` exactly
    when it carries ``GSI3PK``**: every writer that adds one to the count gives
    the item its GSI3 entry in the same transaction (`Repository.set_limits`,
    the disable/cascade stubs, and this), and a provisioner predating this
    wrote neither. So the write that gives an item ``GSI3PK`` takes +1 with it,
    atomically, under ``attribute_not_exists(GSI3PK)`` — a create, or the
    first rewrite of such an older item, which is how an apply repairs one.
    An item already carrying it is replaced under ``attribute_exists(GSI3PK)``
    with no count.

    ``known_old`` is the item `drop_unchanged` read, when it read one. Without
    it the conditional ``PutItem`` goes first — the common case, an item
    already counted, costs what it always did — and only a lost condition
    reads the item (the transaction returns no old image) and takes the
    counting path. A lost condition there (a concurrent create or delete)
    re-reads and tries again.
    """
    pk, sk, item = _build_change_item(namespace_id, change)
    resource = item["resource"]["S"]
    names = {"#g3": GSI3_PK}
    old = known_old
    for _attempt in range(_COUNTED_WRITE_ATTEMPTS):
        try:
            if old is None or GSI3_PK in old:
                response = client.put_item(
                    TableName=table_name,
                    Item=item,
                    ReturnValues="ALL_OLD",
                    ConditionExpression="attribute_exists(#g3)",
                    ExpressionAttributeNames=names,
                )
                return _old_image(response), item
            _counted_transact(
                client,
                [
                    {
                        "Put": {
                            "TableName": table_name,
                            "Item": item,
                            "ConditionExpression": "attribute_not_exists(#g3)",
                            "ExpressionAttributeNames": names,
                        }
                    },
                    {
                        "Update": {
                            "TableName": table_name,
                            **entity_registry_update(namespace_id, resource, 1),
                        }
                    },
                ],
            )
            return old, item
        except ClientError as e:
            if not _lost_condition(e):
                raise
            old = _read_config(client, table_name, pk, sk)
    raise RuntimeError(
        f"entity config {change.target} kept changing under the write; "
        "nothing was written, re-run the apply"
    )


def _apply_entity_delete(
    client: Any,
    table_name: str,
    namespace_id: str,
    change: Change,
    known_old: dict[str, Any] | None,
) -> dict[str, Any]:
    """Delete an entity config item, taking its count with it when it was counted.

    The inverse of `_apply_entity_set`: an item carrying ``GSI3PK`` is deleted
    with −1 in one transaction, then the resource is dropped from the registry
    once its count is zero; an item without it (written by an older
    provisioner, never counted) is deleted alone, and so is an absent one (a
    no-op). Returns the deleted item.

    Without ``known_old`` the plain conditional delete goes first, and only a
    lost condition (the item is counted) reads it and takes the transaction.
    """
    assert change.target is not None
    resource = change.target.split("/", 1)[1]
    pk, sk = config_key(namespace_id, change)
    key = {"PK": {"S": pk}, "SK": {"S": sk}}
    names = {"#g3": GSI3_PK}
    old = known_old
    for _attempt in range(_COUNTED_WRITE_ATTEMPTS):
        try:
            if old is None or GSI3_PK not in old:
                response = client.delete_item(
                    TableName=table_name,
                    Key=key,
                    ReturnValues="ALL_OLD",
                    ConditionExpression="attribute_not_exists(#g3)",
                    ExpressionAttributeNames=names,
                )
                return _old_image(response)
            _counted_transact(
                client,
                [
                    {
                        "Delete": {
                            "TableName": table_name,
                            "Key": key,
                            "ConditionExpression": "attribute_exists(#g3)",
                            "ExpressionAttributeNames": names,
                        }
                    },
                    {
                        "Update": {
                            "TableName": table_name,
                            **entity_registry_update(namespace_id, resource, -1),
                        }
                    },
                ],
                then=lambda: _cleanup_entity_registry(client, table_name, namespace_id, resource),
            )
            return old
        except ClientError as e:
            if not _lost_condition(e):
                raise
            old = _read_config(client, table_name, pk, sk)
    raise RuntimeError(
        f"entity config {change.target} kept changing under the delete; "
        "nothing was deleted, re-run the apply"
    )


def _cleanup_entity_registry(
    client: Any, table_name: str, namespace_id: str, resource: str
) -> None:
    """Drop a resource whose entity count reached zero; best effort.

    A count left at zero is invisible to the list (it shows counts > 0), so a
    failure here, of any kind, is logged rather than reported against a delete
    that landed.
    """
    try:
        client.update_item(TableName=table_name, **entity_registry_cleanup(namespace_id, resource))
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            logger.warning("Could not tidy the entity config registry for %s: %s", resource, e)
    except Exception as e:
        logger.warning("Could not tidy the entity config registry for %s: %s", resource, e)


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
    """The bucket fan-outs (``disabled``, ``cascade``, ``soft``, ``sync``) a write from
    ``old`` to ``new`` makes necessary.

    ``soft`` is the system- and resource-level soft restamp (#467): entity
    levels carry soft-ness in their limits, which the sync restamps.
    """
    kinds: set[str] = set()
    if change.level in ("system", "resource") and _soft_names(old) != _soft_names(new):
        kinds.add("soft")
    # Any other change to a default level reaches active buckets too (ADR-153,
    # #823): left to their TTL it never reaches one used once per TTL period.
    if change.level in ("system", "resource") and _param_attrs(old) != _param_attrs(new):
        kinds.add("sync")
    if change.target is None:
        return kinds
    if decode_cascade(old) != decode_cascade(new):
        kinds.add("cascade")
    if decode_disabled(old) != decode_disabled(new):
        kinds.add("disabled")
    if change.level == "entity" and _limit_attrs(old) != _limit_attrs(new):
        kinds.add("sync")
    return kinds


def fanouts_needed(namespace_id: str, change: Change, stored: dict[str, Any] | None) -> set[str]:
    """The fan-outs a change will need, predicted before it is written.

    From the live item ``drop_unchanged`` read (``stored``), for a delete as
    for a create or update; with none — an unread or unbuildable item — every
    fan-out the level can have, since the old side is unknown. The prediction
    stands even when the write's own ``ALL_OLD`` image disagrees: a re-sent
    write's image already shows the new state (#739 review, finding 2).
    """
    every = {
        "system": {"soft", "sync"},
        "resource": {"disabled", "cascade", "soft", "sync"},
        "entity": {"disabled", "cascade", "sync"},
    }.get(change.level, set())
    if stored is None:
        return every
    if change.action == "delete":
        # The level becomes absent: it needs what removing its stored item
        # changes (#739 review), not every fan-out.
        return fanouts_for(change, stored, {})
    try:
        _pk, _sk, new = _build_change_item(namespace_id, change)
    except Exception:
        return every
    return fanouts_for(change, stored, new)


def full_system_fanouts(
    namespace_id: str, change: Change, stored: dict[str, Any] | None
) -> set[str]:
    """The fan-outs a **full** apply owes the system level.

    Only the soft restamp, and only when soft-ness is in play (#739 re-review 2
    #5): the level holds a soft limit, or this apply gives it one. Owed on
    every full apply, it paged the namespace's whole GSI4 partition and wrote
    a stamp per bucket even with nothing soft anywhere. A stamp that a soft
    limit removed earlier left behind was that removal's own fan-out, owed as a
    change when it happened, and expires on its own within the stamp TTL.
    With the level unread, it is owed, as every fan-out is.
    """
    if stored is None:
        return {"soft"}
    try:
        _pk, _sk, new = _build_change_item(namespace_id, change)
    except Exception:
        return {"soft"}
    return {"soft"} if _soft_names(stored) or _soft_names(new) else set()


def _limit_attrs(item: dict[str, Any]) -> dict[str, Any]:
    """The attributes of a config item that say what its limits are."""
    return {
        name: value
        for name, value in item.items()
        if name == CONFIG_FIELD_SCHED_TZ or parse_limit_attr(name) is not None
    }


def _param_attrs(item: dict[str, Any]) -> dict[str, Any]:
    """A config item's limit attributes but their soft-ness (#823): the ``soft``
    fan-out answers for that."""
    return {
        name: value
        for name, value in _limit_attrs(item).items()
        if (parse_limit_attr(name) or ("", ""))[1] != "soft"
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
    known_old: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply a delete change (DeleteItem); return the deleted item, or {}.

    Takes the level out of the registries the CLI's list commands read, as
    `delete_resource_defaults` / `delete_limits` do (`_apply_entity_delete`).
    """
    if change.level == "entity":
        return _apply_entity_delete(client, table_name, namespace_id, change, known_old)
    pk, sk = config_key(namespace_id, change)
    response = client.delete_item(
        TableName=table_name, Key={"PK": {"S": pk}, "SK": {"S": sk}}, ReturnValues="ALL_OLD"
    )
    if change.level == "resource":
        assert change.target is not None
        # After the delete: a run that stops in between leaves the resource
        # listed with no defaults, never its defaults unlisted.
        client.update_item(
            TableName=table_name,
            **resource_registry_update(namespace_id, change.target, add=False),
        )
    return _old_image(response)
