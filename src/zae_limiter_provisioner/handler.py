"""Lambda handler for declarative limits provisioner.

Handles two event types:
1. CLI invocations (action, manifest, table_name, namespace_id)
2. CloudFormation custom resource events (RequestType, ResourceProperties)
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import time
import urllib.request
from datetime import UTC, datetime
from typing import Any

import boto3

from zae_limiter.schema import (
    DEFAULT_RESOURCE,
    DISABLED_BYPASS,
    RESERVED_NAMESPACE,
    pk_system,
    sk_namespace,
    sk_provisioner,
)

from .applier import (
    ApplyResult,
    apply_changes,
    drop_unchanged,
    require_cascade_policy_readers,
    require_non_enforcing_readers,
    require_reset_after_readers,
)
from .bucket_sync import DEFAULT_TTL_MULTIPLIER, resolve_effective_limits, sync_bucket_params
from .differ import Change, compute_diff
from .fanout import (
    fanout_cascade,
    fanout_entity,
    fanout_resource,
    fanout_soft,
    resolve_disabled,
)
from .manifest import LimitsManifest

logger = logging.getLogger(__name__)

TABLE_NAME = os.environ.get("TABLE_NAME", "rate-limits")


def on_event(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point."""
    if "RequestType" in event:
        return _handle_cfn_with_response(event, context)
    return _handle_cli(event, context)


def _handle_cfn_with_response(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Handle CFN custom resource event with response protocol.

    Wraps _handle_cfn to send SUCCESS/FAILED response to CloudFormation's
    pre-signed ResponseURL. Without this, CloudFormation will hang waiting
    for a response and eventually time out.
    """
    physical_resource_id = event.get("PhysicalResourceId", "")
    try:
        result = _handle_cfn(event, context)
        physical_resource_id = result.pop("physical_resource_id", physical_resource_id)
        if "ResponseURL" in event:
            _send_cfn_response(
                event,
                context,
                "SUCCESS",
                data=result,
                physical_resource_id=physical_resource_id,
            )
        return result
    except Exception as e:
        logger.exception("CFN handler failed")
        if "ResponseURL" in event:
            _send_cfn_response(
                event,
                context,
                "FAILED",
                reason=str(e),
                physical_resource_id=physical_resource_id or "failed",
            )
        raise


def _handle_cli(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Handle CLI invocation (plan or apply)."""
    action = event.get("action", "plan")
    table_name = event.get("table_name", TABLE_NAME)
    namespace_id = event.get("namespace_id", "")
    manifest_data = event.get("manifest", {})

    # `full` (#739 review): skip the change-only filter and fan out every
    # declared level, as every apply did before. The escape hatch for a bucket
    # a fan-out's discovery query missed (see `_apply_and_record`).
    manifest = LimitsManifest.from_dict(manifest_data)
    previous = _read_provisioner_state(table_name, namespace_id)
    # An apply cut short with too many levels to list ahead owes a full one.
    full = event.get("full") is True or (action != "plan" and previous.get("full_pending", False))
    declared, changes = _plan_changes(manifest, previous, table_name, namespace_id, full=full)
    unchanged = len(declared) - len(changes)

    change_dicts = [{"action": c.action, "level": c.level, "target": c.target} for c in changes]

    if action == "plan":
        return {"status": "planned", "changes": change_dicts, "unchanged": unchanged}

    # Apply
    result = _apply_and_record(
        manifest,
        changes,
        table_name,
        namespace_id,
        previous.get("cascade_pending", []),
        previous.get("fanout_pending", []),
        declared=declared,
        full=full,
    )

    return {
        "status": "applied",
        "changes": change_dicts,
        "created": result.created,
        "updated": result.updated,
        "deleted": result.deleted,
        "unchanged": unchanged,
        "errors": result.errors,
    }


def _handle_cfn(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Handle CloudFormation custom resource event."""
    request_type = event["RequestType"]
    properties = event.get("ResourceProperties", {})

    table_name = properties.get("TableName", TABLE_NAME)
    namespace_id = properties.get("NamespaceId", "")
    if not namespace_id:
        namespace_name = properties.get("Namespace", "default")
        namespace_id = _resolve_namespace_id(table_name, namespace_name)

    physical_resource_id = f"{table_name}/{namespace_id}/limits-provisioner"

    if request_type == "Delete":
        # Empty manifest deletes all managed items
        manifest_data: dict[str, Any] = {"namespace": properties.get("Namespace", "deleted")}
    else:
        # Create or Update: convert CFN properties to manifest format
        manifest_data = _cfn_properties_to_manifest(properties)

    manifest = LimitsManifest.from_dict(manifest_data)
    previous = _read_provisioner_state(table_name, namespace_id)
    full = previous.get("full_pending", False)
    declared, changes = _plan_changes(manifest, previous, table_name, namespace_id, full=full)
    unchanged = len(declared) - len(changes)

    result = _apply_and_record(
        manifest,
        changes,
        table_name,
        namespace_id,
        previous.get("cascade_pending", []),
        previous.get("fanout_pending", []),
        declared=declared,
        full=full,
    )

    return {
        "physical_resource_id": physical_resource_id,
        "status": "applied",
        "changes": [{"action": c.action, "level": c.level, "target": c.target} for c in changes],
        "created": result.created,
        "updated": result.updated,
        "deleted": result.deleted,
        "unchanged": unchanged,
        "errors": result.errors,
    }


def _plan_changes(
    manifest: LimitsManifest,
    previous: dict[str, Any],
    table_name: str,
    namespace_id: str,
    *,
    full: bool = False,
) -> tuple[list[Change], list[Change]]:
    """``(declared, changes)``: every level the manifest touches, and those an apply writes.

    ``compute_diff`` alone lists every declared level as a create or update;
    ``drop_unchanged`` removes the ones whose stored config item already
    matches, so ``plan``, ``diff`` and ``apply`` all report — and ``apply``
    only writes and fans out — what actually differs from the table. With
    ``full`` nothing is dropped.
    """
    declared = compute_diff(manifest, previous)
    if full:
        return declared, declared
    return declared, drop_unchanged(declared, table_name, namespace_id)


def _apply_and_record(
    manifest: LimitsManifest,
    changes: list[Change],
    table_name: str,
    namespace_id: str,
    cascade_pending: list[tuple[str, str]] | None = None,
    fanout_pending: list[dict[str, str]] | None = None,
    *,
    declared: list[Change] | None = None,
    full: bool = False,
) -> ApplyResult:
    """Commit the config writes, fan out, then record the managed state (#563).

    Every fan-out is **change-only**: it runs for a level only when that
    level's write changed what it fans out — the stored cascade policy, the
    stored `disabled`, or (entity level) the stored limits — read off each
    write's own ``ALL_OLD`` image (``ApplyResult``). Unchanged levels never
    reach here at all (``_plan_changes``). So an apply costs what changed, not
    what the manifest declares. ``full`` fans out every change regardless.

    ``declared`` is every level the manifest touches, before ``drop_unchanged``
    (defaults to ``changes``). The version gates read it, so an unchanged
    ``reset_after`` or ``cascade`` entry is still checked against the deployed
    Lambdas; and every declared level with an explicit ``disabled`` is
    re-asserted on each apply (``_reasserted_disabled``).

    ``cascade_pending`` and ``fanout_pending`` are the levels whose fan-out
    has not been seen to finish: being change-only, re-running the same apply
    would otherwise see no change and never reconcile. They are retried here
    with this apply's own changes, and whatever still fails is recorded for
    the next one. ``cascade_pending`` keeps its own list because records
    written before ``fanout_pending`` existed carry it.

    **The pending lists are written ahead** (#739 review). Before the first
    config write, every level about to change is added to both lists on the
    ``#PROVISIONER`` record; the final record write replaces them with only
    what failed. A Lambda killed or timed out between the config writes and
    the fan-outs therefore leaves a record the next apply retries — without
    it, that apply would find the config already matching, drop the level,
    and never fan it out (a disabled entity admitted forever). An apply
    changing more than ``_MAX_PENDING_AHEAD`` levels (a first apply, or
    ``full``) records ``full_pending`` instead of listing them, which keeps the
    record far below DynamoDB's 400 KB item limit; the next apply then runs in
    full.

    The single place both entry points run an apply, so the ordering contract
    below cannot drift between the CFN and CLI paths again — which is exactly
    how #563 came to exist in two copies.

    **A post-commit failure reports partial progress; it never abandons the
    record.** ``apply_changes`` commits the config writes first, so by the time
    either fan-out runs, "fail cleanly" is no longer on the menu — something is
    already in the table. An exception escaping a fan-out used to propagate
    past ``_write_provisioner_state``, leaving the ``#PROVISIONER`` record
    describing the *previous* apply while the table held this one's config, and
    (on the CFN path) telling CloudFormation FAILED so it rolled back a stack
    whose configuration had already been applied.

    Both fan-outs therefore return their failures rather than raising them, and
    they are appended to ``ApplyResult.errors`` — the mechanism ``apply_changes``
    already uses for a failed *config write*, which is strictly the more severe
    failure of the two. The CLI prints them and exits 1; the CFN response
    carries them in ``Data`` under a SUCCESS status.

    This mirrors the contract of :class:`zae_limiter.exceptions.FanoutIncomplete`
    on the async side — config committed first, progress reported, every write
    idempotent so re-running the same apply reconciles the rest — without
    adopting its delivery mechanism. Out of a Lambda handler an exception *is* a
    CloudFormation FAILED, which is the outcome #563 exists to remove. See the
    PR body for the full rationale.

    ``_write_provisioner_state`` is deliberately left unguarded: it is the last
    step, there is nothing after it to salvage, and a DynamoDB failure there is
    a genuine infrastructure failure for which a FAILED response and a retry are
    the right answer.
    """
    if declared is None:
        declared = changes
    pending = list(fanout_pending or [])
    pending_cascade = list(cascade_pending or [])

    # Before anything is written (#638): a refusal raises, which is a clean
    # CloudFormation FAILED / CLI error because the table is untouched. Gated
    # on the declared set, not only the changed one (#739 review).
    require_reset_after_readers(declared, table_name)
    require_cascade_policy_readers(declared, table_name)
    require_non_enforcing_readers(declared, table_name)

    fanned = [c for c in changes if c.level in ("resource", "entity") and c.target]
    if len(fanned) > _MAX_PENDING_AHEAD:
        _write_pending_ahead(table_name, namespace_id, pending, pending_cascade, full=True)
    elif fanned:
        _write_pending_ahead(
            table_name,
            namespace_id,
            [
                *pending,
                *(_pending_entry("disabled", c) for c in fanned),
                *(_pending_entry("sync", c) for c in fanned if c.level == "entity"),
            ],
            [*pending_cascade, *((c.level, c.target or "") for c in fanned)],
        )

    result = apply_changes(changes, table_name, namespace_id)
    if full:
        result.disabled_changed = list(fanned)
        result.limits_changed = [c for c in fanned if c.level == "entity"]
        result.cascade_changed = [(c.level, c.target or "") for c in fanned]

    disabled_targets = _with_pending(manifest, result.disabled_changed, pending, "disabled")
    reasserted = _reasserted_disabled(manifest, disabled_targets)
    disabled_errors, disabled_failed = _fanout_disabled_changes(
        table_name, namespace_id, [*disabled_targets, *reasserted], reasserted=reasserted
    )
    result.errors.extend(disabled_errors)
    result.errors.extend(_fanout_soft_changes(table_name, result.soft_changed, namespace_id))
    cascade_targets: list[tuple[str, str]] = list(
        dict.fromkeys([*pending_cascade, *result.cascade_changed])
    )
    cascade_errors, cascade_failed = _fanout_cascade_changes(
        table_name, namespace_id, cascade_targets
    )
    result.errors.extend(cascade_errors)
    sync_errors, sync_failed = _sync_bucket_param_changes(
        table_name,
        namespace_id,
        _with_pending(manifest, result.limits_changed, pending, "sync"),
    )
    result.errors.extend(sync_errors)

    manifest_hash = hashlib.sha256(
        json.dumps(manifest.to_dict(), sort_keys=True).encode()
    ).hexdigest()

    new_state = manifest.managed_set()
    new_state["last_applied"] = datetime.now(UTC).isoformat()
    new_state["applied_hash"] = f"sha256:{manifest_hash}"
    new_state["cascade_pending"] = cascade_failed
    new_state["fanout_pending"] = [
        *(_pending_entry("disabled", c) for c in disabled_failed),
        *(_pending_entry("sync", c) for c in sync_failed),
    ]
    _write_provisioner_state(table_name, namespace_id, new_state)

    return result


def _pending_entry(kind: str, change: Change) -> dict[str, str]:
    """A fan-out not yet seen to finish, as the ``#PROVISIONER`` record stores it."""
    return {"kind": kind, "level": change.level, "target": change.target or ""}


def _reasserted_disabled(manifest: LimitsManifest, fanned: list[Change]) -> list[Change]:
    """Every declared level with an explicit ``disabled``, not already being fanned out.

    The ADR-125 fan-out has a documented race: an ``acquire()`` in flight when
    the discovery query runs can create a bucket the query misses, and that
    bucket keeps the old stamp. Before the change-only apply every apply
    re-ran the fan-out for every declared level, which repaired such a bucket
    on the next apply; re-asserting the levels that actually set ``disabled``
    keeps that repair (#739 review). Those are the kill switches and
    carve-outs, normally a handful, not the whole manifest. A resource-level
    re-assert skips buckets already carrying the right stamp
    (``fanout_resource(skip_matching=True)``, read off GSI2's projection), so
    it costs the discovery query and nothing else when nothing was missed.
    """
    seen = {(c.level, c.target) for c in fanned}
    reasserted: list[Change] = []
    for name, resource in manifest.resources.items():
        if resource.disabled is not None and ("resource", name) not in seen:
            reasserted.append(Change("update", "resource", name, resource.to_dict()))
    for entity_id, entity in manifest.entities.items():
        for name, decl in entity.resources.items():
            target = f"{entity_id}/{name}"
            if decl.disabled is not None and ("entity", target) not in seen:
                reasserted.append(Change("update", "entity", target, decl.to_dict()))
    return reasserted


# Levels an apply may list ahead in the #PROVISIONER record before it records
# `full_pending` instead. At ~190 bytes a level (three entries), 100 levels is
# ~19 KB: far from the 400 KB item limit even beside a large managed set.
_MAX_PENDING_AHEAD = 100


def _write_pending_ahead(
    table_name: str,
    namespace_id: str,
    fanout_pending: list[dict[str, str]],
    cascade_pending: list[tuple[str, str]],
    *,
    full: bool = False,
) -> None:
    """Record the fan-outs an apply is about to owe, before it owes them.

    Touches only the two pending lists and ``full_pending`` (and the GSI4 key,
    so a record created here is still found by ``purge_namespace``); the
    managed set is written with the final record, which also clears
    ``full_pending``. Entries are de-duplicated, preserving order.
    """
    fanout = list({tuple(sorted(e.items())): e for e in fanout_pending}.values())
    cascade = list(dict.fromkeys(cascade_pending))
    client = boto3.client("dynamodb")
    client.update_item(
        TableName=table_name,
        Key={"PK": {"S": pk_system(namespace_id)}, "SK": {"S": sk_provisioner()}},
        UpdateExpression=(
            "SET GSI4PK = :ns, fanout_pending = :fp, cascade_pending = :cp, full_pending = :full"
        ),
        ExpressionAttributeValues={
            ":ns": {"S": namespace_id},
            ":fp": _encode_fanout_pending(fanout),
            ":cp": _encode_cascade_pending(cascade),
            ":full": {"BOOL": full},
        },
    )


def _with_pending(
    manifest: LimitsManifest,
    changed: list[Change],
    pending: list[dict[str, str]],
    kind: str,
) -> list[Change]:
    """This apply's changes for one fan-out, plus the ones an earlier apply left.

    A pending level is retried against what the manifest now says about it: its
    current declaration when it still declares one, otherwise a delete — both
    fan-outs then resolve what applies from the table, exactly as they do for a
    delete this apply made. A level this apply changed is fanned out once, from
    this apply's change.
    """
    seen = {(c.level, c.target) for c in changed}
    retried: list[Change] = []
    for entry in pending:
        if entry.get("kind") != kind:
            continue
        level, target = entry["level"], entry["target"]
        if (level, target) in seen:
            continue
        seen.add((level, target))
        retried.append(_manifest_change(manifest, level, target))
    return [*retried, *changed]


def _manifest_change(manifest: LimitsManifest, level: str, target: str) -> Change:
    """The change re-asserting what the manifest declares for one level."""
    if level == "resource" and target in manifest.resources:
        return Change("update", level, target, manifest.resources[target].to_dict())
    if level == "entity":
        entity_id, resource = target.split("/", 1)
        entity = manifest.entities.get(entity_id)
        if entity is not None and resource in entity.resources:
            return Change("update", level, target, entity.resources[resource].to_dict())
    return Change("delete", level, target)


def _fanout_soft_changes(
    table_name: str,
    changed: list[tuple[str, str | None, list[str]]],
    namespace_id: str,
) -> list[str]:
    """Restamp buckets for every system or resource level whose soft-ness changed (#467).

    Change-only, from each write's own ``ALL_OLD`` image (``ApplyResult.soft_changed``),
    so a routine apply writes no bucket. Mirrors ``Repository._fanout_soft``;
    failures are returned, like the disable fan-out's.

    Unlike the disable fan-out it is not written ahead into ``fanout_pending``,
    and a failure is not retried: a soft stamp carries a deadline (ADR-151
    decision 8). A stamp a lost fan-out failed to remove waives its limit for at
    most ``STAMP_TTL_SECONDS``, and a stamp it failed to set is set by the next
    slow pass that reads the config fresh. A missing ``disabled`` stamp has no
    such deadline, which is why that fan-out is written ahead.
    """
    errors: list[str] = []
    if not changed:
        return errors
    client = boto3.client("dynamodb")
    for level, target, names in changed:
        try:
            fanout_soft(
                client,
                table_name,
                namespace_id,
                target if level == "resource" else None,
                set(names),
            )
        except Exception as e:
            logger.warning("soft fan-out failed for %s %s: %s", level, target, e)
            errors.append(f"soft fan-out {level} {target}: {e}")
    return errors


def _fanout_disabled_changes(
    table_name: str,
    namespace_id: str,
    changes: list[Change],
    *,
    reasserted: list[Change] | None = None,
) -> tuple[list[str], list[Change]]:
    """Eagerly stamp bucket items for every resource/entity change given (ADR-125).

    Runs AFTER `apply_changes`, so every level it resolves already reflects
    this apply's writes and deletes.

    The caller passes the changes whose stored `disabled` changed
    (`ApplyResult.disabled_changed`, compared off each write's `ALL_OLD`
    image), plus any whose fan-out failed on an earlier apply. That is NOT
    the same as "the change's data carries a `disabled` key":
    `ResourceDecl.to_dict()` / `EntityResourceDecl.to_dict()` omit `disabled`
    entirely when it is `None` ("inherit"), so an operator deleting a
    `disabled: true` line and re-applying produces a change with no
    `disabled` key at all — and the old image's explicit `true` against the
    new item's absence is exactly a change. Filtering on key presence would
    skip that change's fan-out, leaving existing buckets stamped
    `disabled: true` forever (until TTL) even though the config item
    correctly lost the attribute.

    For a resource-level change, the effective value is simply the
    resource's own (possibly absent -> False) `disabled` value — there is no
    level above resource in the walk (system-level disable is out of scope
    per ADR-125), mirroring ``Repository._set_resource_disabled``'s
    ``disabled=bool(value)``. ``fanout_resource`` then re-resolves each
    bucket's entity and skips the ones with their own overriding value, so a
    per-entity carve-out made out of band survives an apply that changes the
    resource's `disabled`.

    For an entity-level change, the effective value is NOT `data.get(
    "disabled", False)` — that would be wrong whenever the entity's own
    value is absent, since absent at entity level means "inherit from
    resource", which may legitimately be `True`. Instead this resolves the
    same entity(resource) -> entity(_default_) -> resource walk
    ``Repository.resolve_disabled`` uses, reading the config items
    `apply_changes` has already written for this apply (see
    ``fanout.resolve_disabled``).

    Resource-level changes are applied before entity-level ones so that the
    entity level is written before anything resolves it back.

    Deletes fan out too. Removing a managed config item removes the level
    that was deciding `disabled`, which changes the resolution in either
    direction — dropping a carve-out re-disables an entity, dropping a
    disabled resource's config re-enables it — so the stamps have to follow.
    Since the delete has already been applied, `Change.data` being `None`
    is exactly right for a resource-level delete's effective value (`False`,
    with no level above resource), and the entity-level branch re-resolves
    from DynamoDB and so sees the post-delete state. This mirrors
    ``Repository.delete_limits()`` / ``delete_resource_defaults()``, which
    also re-run their fan-out against the post-delete resolution (ADR-125).

    An entity-level change whose resource is the `_default_` sentinel is an
    entity-wide directive (applies across every resource the entity has a
    bucket for). `_default_` is only ever a config SK, never a real bucket
    resource — a bucket is always keyed by its actual resource name — so it
    is translated to `resource=None` (unscoped) before calling
    `fanout_entity`, which then discovers every one of the entity's buckets
    across all resources and re-resolves each bucket's own effective value
    so a per-resource override still wins over the entity-wide directive
    (see `fanout.fanout_entity`'s docstring). Passing the literal string
    `"_default_"` through to `fanout_entity` instead would silently match
    zero real buckets (`GSI3SK begins_with "BUCKET#_default_#"`) and leave
    every existing bucket un-stamped despite the config being written
    correctly and the apply reporting success.

    Returns ``(errors, failed)``: a human-readable failure and the change for
    each one that could not be stamped, rather than raising on the first
    (#563). ``failed`` is recorded so the next apply retries it. Guarding is
    per change so one unreadable config item cannot abandon every *other*
    change's fan-out — the direct analogue of ``FanoutIncomplete.stamped``
    reporting how far the async fan-out got. See ``_apply_and_record``.
    """
    candidates = [c for c in changes if c.level in ("resource", "entity") and c.target]
    if not candidates:
        return [], []

    errors: list[str] = []
    failed: list[Change] = []
    reasserted_ids = {id(c) for c in reasserted or ()}
    client = boto3.client("dynamodb")
    for change in sorted(candidates, key=lambda c: 0 if c.level == "resource" else 1):
        data = change.data or {}
        try:
            disabled: bool | str
            if change.level == "resource" and change.target:
                value = data.get("disabled")
                disabled = value if value == DISABLED_BYPASS else bool(value)
                fanout_resource(
                    client,
                    table_name,
                    namespace_id,
                    change.target,
                    disabled,
                    skip_matching=id(change) in reasserted_ids,
                )
            elif change.level == "entity" and change.target:
                entity_id, resource = change.target.split("/", 1)
                disabled = resolve_disabled(client, table_name, namespace_id, entity_id, resource)
                fanout_resource_arg = None if resource == DEFAULT_RESOURCE else resource
                fanout_entity(
                    client, table_name, namespace_id, entity_id, fanout_resource_arg, disabled
                )
        except Exception as e:
            logger.warning("disable fan-out failed for %s %s: %s", change.level, change.target, e)
            errors.append(
                f"disable fan-out {change.level} {change.target}: {e} (retried on the next apply)"
            )
            failed.append(change)
    return errors, failed


def _fanout_cascade_changes(
    table_name: str,
    namespace_id: str,
    changed: list[tuple[str, str]],
) -> tuple[list[str], list[tuple[str, str]]]:
    """Restamp buckets for every level whose cascade policy changed (ADR-146).

    Unlike `disabled`, only the levels `apply_changes` saw change — from each
    write's own ALL_OLD image — plus any an earlier apply failed to restamp are
    fanned out, so a routine apply that re-asserts an unchanged policy writes
    no bucket. Every bucket reached is stamped from its own resolution
    (`fanout.fanout_cascade`), so an entity override survives a resource-level
    change, and the order of the changes does not matter.

    Returns ``(errors, failed)``: failures are reported, not raised, as for
    `disabled` (#563), and ``failed`` is recorded so the next apply retries
    them — every write is idempotent, so re-running reconciles the rest.
    """
    if not changed:
        return [], []
    errors: list[str] = []
    failed: list[tuple[str, str]] = []
    client = boto3.client("dynamodb")
    for level, target in changed:
        try:
            if level == "resource":
                fanout_cascade(client, table_name, namespace_id, resource=target)
            else:
                entity_id, resource = target.split("/", 1)
                fanout_cascade(
                    client, table_name, namespace_id, entity_id=entity_id, resource=resource
                )
        except Exception as e:
            logger.warning("cascade fan-out failed for %s %s: %s", level, target, e)
            errors.append(f"cascade fan-out {level} {target}: {e} (retried on the next apply)")
            failed.append((level, target))
    return errors, failed


def _sync_bucket_param_changes(
    table_name: str,
    namespace_id: str,
    changes: list[Change],
) -> tuple[list[str], list[Change]]:
    """Push entity-level limit changes out to existing bucket items (#481).

    Runs AFTER ``apply_changes``, so ``resolve_effective_limits`` sees the
    config this apply has already written — the same ordering contract
    ``_fanout_disabled_changes`` relies on. The caller passes the changes whose
    stored limits changed (``ApplyResult.limits_changed``) plus any whose sync
    failed on an earlier apply.

    **Entity level only.** Resource and system defaults deliberately never
    touch buckets: a bucket on defaults carries a TTL and is recreated with
    current params when it expires (#271, #296).

    Returns ``(errors, failed)``: a human-readable failure and the change for
    each entity change that could not be synced, rather than raising on the
    first (#563); ``failed`` is recorded so the next apply retries it. ``_decode_limits``
    raises on a stored compact schedule this provisioner cannot read (PR #549,
    deliberately — silently dropping the limit was the bug class that change
    removed), and a config item written by a newer client is a realistic source
    of one: design §4.1 ships no version marker (#515). Left unguarded, that
    ``ValueError`` escaped past ``_write_provisioner_state``. See
    ``_apply_and_record``.
    """
    client = boto3.client("dynamodb")
    errors: list[str] = []
    failed: list[Change] = []

    for change in changes:
        if change.level != "entity" or change.target is None:
            continue
        entity_id, resource = change.target.split("/", 1)
        declared = (change.data or {}).get("limits", {})
        # Per change, just before its resolution: a soft stamp's deadline
        # counts from the read behind it, and one instant for a whole apply
        # wrote stamps already expired on a long one (ADR-151 decision 8,
        # review E4).
        now_ms = int(time.time() * 1000)

        limits: dict[str, Any]
        stale_limit_names: set[str] | None
        try:
            if change.action == "delete":
                # Reconcile to whatever now applies, and strip the limits that
                # the deleted config had but the new effective config does not.
                effective = resolve_effective_limits(
                    client, table_name, namespace_id, entity_id, resource
                )
                if not effective:
                    continue
                stale = set(declared) - set(effective)
                limits, ttl_multiplier = effective, DEFAULT_TTL_MULTIPLIER
                stale_limit_names = stale or None
            else:
                if not declared:
                    continue
                limits, ttl_multiplier = declared, 0
                stale_limit_names = None

            sync_bucket_params(
                client=client,
                table_name=table_name,
                namespace_id=namespace_id,
                entity_id=entity_id,
                resource=resource,
                limits=limits,
                ttl_multiplier=ttl_multiplier,
                stale_limit_names=stale_limit_names,
                now_ms=now_ms,
            )
        except Exception as e:
            logger.warning("bucket param sync failed for entity %s: %s", change.target, e)
            errors.append(
                f"bucket param sync entity {change.target}: {e} (retried on the next apply)"
            )
            failed.append(change)
    return errors, failed


# ---------------------------------------------------------------------------
# CloudFormation scalar coercion (#554)
# ---------------------------------------------------------------------------
#
# CloudFormation stringifies EVERY scalar in a custom resource's
# `ResourceProperties` before delivering them to the Lambda. Measured against a
# real `aws cloudformation deploy` (see #554): `Disabled: false` arrives as
# `'false'`, `Capacity: 1000` as `'1000'`, `Scale: 0.5` as `'0.5'`, and
# `!Ref` of a `Type: Number` parameter as `'1000'`. Structure survives — lists
# stay lists and maps stay maps — only the leaves are stringified. Quoting in
# the template changes nothing: `false` and `"false"` are byte-identical on
# arrival, so quoting cannot be used as a signal.
#
# Untreated, that broke two ways:
#   * `bool('false')` is `True`, so an ADR-125 carve-out (`Disabled: false`)
#     SILENTLY disabled the entity it was meant to re-admit — but only for an
#     entry carrying no numeric fields, which is exactly what a carve-out looks
#     like (`{Disabled: false}` with no limits of its own).
#   * `'1000' <= 0` is a `TypeError`, so any entry with numeric fields aborted
#     the whole apply loudly.
#
# Coercion is **type-directed**, dispatching on the target field, never on what
# the value looks like: `Cron` and `Tz` are legitimately strings while `Scale`
# and `Capacity` are not, so a generic "looks numeric => int" pass would corrupt
# cron expressions. AWS's own RPDK `recast.py` dispatches on declared type hints
# for the same reason (and exists at all because a registry resource type does
# not escape this — aws-cloudformation/cloudformation-cli#435).
#
# Every coercer is **idempotent**: an already-native value passes through
# unchanged. Re-invokes can hand back values another pass already converted, and
# should AWS ever deliver real types, this boundary keeps working rather than
# breaking on the fix.
#
# `OldResourceProperties` (present on Update, and stringified the same way) is
# deliberately NOT read anywhere in this package. If drift/diff logic is ever
# added, it MUST run the old properties through this same coercion — comparing
# coerced-new against stringified-old would report a change in every field on
# every re-apply.

# Returned by a numeric coercer for the empty string, meaning "treat the
# property as absent". This mirrors the RPDK's rule for numeric targets and the
# CloudFormation `Parameter: {Default: ""}` + optional-property idiom. It is
# deliberately NOT extended to `Disabled`: that field is tri-state and
# safety-critical, so an unrecognised spelling must fail loudly rather than be
# guessed into "inherit".
_ABSENT: Any = object()


def _coerce_disabled(value: Any, where: str) -> bool | str:
    """Coerce a CloudFormation ``Disabled`` value: a boolean, or ``bypass`` (#311).

    ``bypass`` is accepted in any case and normalised; every other value goes
    through ``_coerce_bool``'s strict allowlist.
    """
    if isinstance(value, str) and value.lower() == DISABLED_BYPASS:
        return DISABLED_BYPASS
    return _coerce_bool(value, where)


def _coerce_bool(value: Any, where: str) -> bool:
    """Coerce a CloudFormation-delivered `Disabled` or `Cascade` value to a real ``bool``.

    A case-insensitive allowlist that **raises** on anything else, rather than
    falling through to ``bool(value)``. Case matters concretely: an author
    writing ``Disabled: "True"`` (quoted, so YAML keeps it a string) would
    otherwise arrive as ``'True'`` and be truthy by accident rather than by
    the allowlist. ``'1'``, ``'yes'``, ``''`` and every other spelling are
    rejected: both properties are tri-state switches (ADR-125, ADR-146), and
    guessing wrong silently disables or re-admits a tenant, or turns its
    cascade to a parent on or off.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise ValueError(
        f"{where} must be true or false, got {value!r}. CloudFormation delivers "
        f"every property as a string, so this boundary accepts only 'true'/'false' "
        f"(any case) or a real boolean — anything else is rejected rather than "
        f"guessed, because the property is tri-state and a wrong guess silently "
        f"changes who is admitted or charged."
    )


def _coerce_int(value: Any, where: str) -> Any:
    """Coerce a CloudFormation-delivered numeric property to ``int``.

    ``bool`` is rejected even though it is an ``int`` subclass in Python: a
    ``Capacity`` of ``true`` is a mistake, not the number 1. The empty string
    yields :data:`_ABSENT` so the caller can drop an optional property, which
    is how a CloudFormation ``Default: ""`` parameter spells "not set".
    """
    if isinstance(value, bool):
        raise ValueError(f"{where} must be a whole number, got boolean {value!r}.")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        if value == "":
            return _ABSENT
        try:
            return int(value)
        except ValueError:
            pass
    raise ValueError(
        f"{where} must be a whole number, got {value!r}. CloudFormation delivers "
        f"every property as a string; this boundary parses it back, but only for a "
        f"value that is actually an integer."
    )


def _coerce_float(value: Any, where: str) -> Any:
    """Coerce a CloudFormation-delivered ``Scale`` to ``float``.

    Non-finite values are rejected here rather than downstream:
    ``ScheduleEntry.__post_init__`` validates ``scale`` with ``scale <= 0``,
    and every comparison against NaN is ``False``, so a ``Scale: NaN`` would
    pass validation and then poison the effective-parameter arithmetic.
    """
    if isinstance(value, bool):
        raise ValueError(f"{where} must be a number, got boolean {value!r}.")
    if isinstance(value, int | float):
        parsed = float(value)
    elif isinstance(value, str):
        if value == "":
            return _ABSENT
        try:
            parsed = float(value)
        except ValueError:
            raise ValueError(
                f"{where} must be a number, got {value!r}. CloudFormation delivers "
                f"every property as a string; this boundary parses it back, but only "
                f"for a value that is actually a number."
            ) from None
    else:
        raise ValueError(f"{where} must be a number, got {value!r}.")
    if not math.isfinite(parsed):
        raise ValueError(
            f"{where} must be a finite number, got {value!r}. NaN and infinity are "
            f"rejected here because `scale <= 0` — the downstream validation — is "
            f"False for NaN, so one would pass validation and then poison every "
            f"effective-parameter calculation."
        )
    return parsed


def _coerce_str(value: Any, where: str) -> str:
    """Pass through a CloudFormation-delivered string property.

    CloudFormation already delivers these as ``str``, so this is normally the
    identity. It exists so the dispatch table names a type for *every* field
    (the point of a type-directed boundary), and so a non-string reaching it —
    only possible from a direct Lambda invoke — fails with a message naming the
    property rather than as an ``AttributeError`` inside ``parse_cron``.
    """
    if isinstance(value, str):
        return value
    raise ValueError(f"{where} must be a string, got {value!r}.")


def _cfn_properties_to_manifest(properties: dict[str, Any]) -> dict[str, Any]:
    """Convert CloudFormation ResourceProperties to manifest dict format.

    CFN uses PascalCase keys; manifest uses snake_case. Scalars arrive as
    strings and are coerced back per-field on the way through (#554).
    """
    manifest: dict[str, Any] = {"namespace": properties.get("Namespace", "default")}

    if "System" in properties:
        system: dict[str, Any] = {}
        cfn_system = properties["System"]
        if "OnUnavailable" in cfn_system:
            system["on_unavailable"] = _coerce_str(
                cfn_system["OnUnavailable"], "System.OnUnavailable"
            )
        if "Limits" in cfn_system:
            system["limits"] = _cfn_limits_to_manifest(cfn_system["Limits"], where="System.Limits")
        if "Cascade" in cfn_system:
            # Carried through so the manifest rejects it with its reason
            # (ADR-146: no system-level policy) rather than dropping it here.
            system["cascade"] = _coerce_bool(cfn_system["Cascade"], "System.Cascade")
        if "Disabled" in cfn_system:
            # Same for `Disabled` (#693): ADR-125 has no system-level disable.
            system["disabled"] = _coerce_bool(cfn_system["Disabled"], "System.Disabled")
        manifest["system"] = system

    if "Resources" in properties:
        resources = {}
        for resource_name, cfn_resource in properties["Resources"].items():
            resource_entry: dict[str, Any] = {
                "limits": _cfn_limits_to_manifest(
                    cfn_resource.get("Limits", {}),
                    where=f"Resources.{resource_name}.Limits",
                )
            }
            # Tri-state: only set "disabled" when "Disabled" is present in the CFN
            # properties. An explicit False must survive (it's the carve-out value);
            # an absent key must NOT be coerced to False, or every apply would
            # re-enable anything the operator previously disabled out-of-band.
            # `_coerce_bool` is therefore applied INSIDE this branch: it converts a
            # present value, it never invents one (#554).
            if "Disabled" in cfn_resource:
                resource_entry["disabled"] = _coerce_disabled(
                    cfn_resource["Disabled"], f"Resources.{resource_name}.Disabled"
                )
            # The cascade policy (ADR-146), tri-state by the same rule.
            if "Cascade" in cfn_resource:
                resource_entry["cascade"] = _coerce_bool(
                    cfn_resource["Cascade"], f"Resources.{resource_name}.Cascade"
                )
            resources[resource_name] = resource_entry
        manifest["resources"] = resources

    if "Entities" in properties:
        entities = {}
        for entity_id, cfn_entity in properties["Entities"].items():
            entity_resources = {}
            for resource_name, cfn_res in cfn_entity.get("Resources", {}).items():
                prefix = f"Entities.{entity_id}.Resources.{resource_name}"
                entity_resource_entry: dict[str, Any] = {
                    "limits": _cfn_limits_to_manifest(
                        cfn_res.get("Limits", {}), where=f"{prefix}.Limits"
                    )
                }
                if "Disabled" in cfn_res:
                    entity_resource_entry["disabled"] = _coerce_disabled(
                        cfn_res["Disabled"], f"{prefix}.Disabled"
                    )
                if "Cascade" in cfn_res:
                    entity_resource_entry["cascade"] = _coerce_bool(
                        cfn_res["Cascade"], f"{prefix}.Cascade"
                    )
                entity_resources[resource_name] = entity_resource_entry
            entities[entity_id] = {"resources": entity_resources}
        manifest["entities"] = entities

    return manifest


# CloudFormation schedule property -> manifest schedule-entry field (#222).
#
# The exact inverse of `zae_limiter.limits_cli._SCHEDULE_KEYS`; the two cannot
# share a module because the provisioner Lambda zip carries only a four-file
# `zae_limiter` stub and so can never import `limits_cli`. A unit test pins them
# as inverses. The snake_case column is `manifest._ENTRY_FIELDS`, whose
# allowlist is strict — producing a key outside it, or the right key in the
# wrong case, fails the whole apply.
_CFN_SCHEDULE_KEYS: dict[str, str] = {
    "Cron": "cron",
    "Tz": "tz",
    "Scale": "scale",
    "Capacity": "capacity",
    "RefillAmount": "refill_amount",
    "RefillPeriodSeconds": "refill_period_seconds",
}

# The target type of each schedule property, for the #554 coercion. Keyed
# identically to `_CFN_SCHEDULE_KEYS` — a unit test pins the two key sets equal,
# so a seventh schedule property cannot be added without also declaring its
# type. `Cron` and `Tz` are strings while `Scale` and the three absolutes are
# not, which is precisely why the coercion dispatches on the field rather than
# sniffing the value: `"0 9 * * 1-5"` must stay a cron expression.
_CFN_SCHEDULE_COERCERS: dict[str, Any] = {
    "Cron": _coerce_str,
    "Tz": _coerce_str,
    "Scale": _coerce_float,
    "Capacity": _coerce_int,
    "RefillAmount": _coerce_int,
    "RefillPeriodSeconds": _coerce_int,
}

# Limit-level CFN property -> (manifest key, coercer). Note `RefillPeriod`,
# which is *not* the schedule entry's `RefillPeriodSeconds`. `Capacity` is
# handled separately because `LimitDecl` requires it.
_CFN_LIMIT_OPTIONAL_KEYS: dict[str, tuple[str, Any]] = {
    "RefillAmount": ("refill_amount", _coerce_int),
    "RefillPeriod": ("refill_period", _coerce_int),
    # ADR-139: a duration-window quota's window length. CloudFormation
    # delivers every property as a string (#554); `_coerce_int` parses it
    # back and rejects a bool and a non-integral value (#569). The manifest
    # key mirrors `LimitDecl.reset_after_seconds`.
    "ResetAfterSeconds": ("reset_after_seconds", _coerce_int),
    # #467: a soft limit. Delivered as a string like every property (#554).
    "Soft": ("soft", _coerce_bool),
}


def _cfn_schedule_to_manifest(entries: Any, *, where: str) -> Any:
    """Convert CFN schedule entries back to manifest snake_case.

    Table-driven, so an unrecognised *property* is dropped rather than forwarded
    in some guessed spelling: `manifest._parse_entries` rejects any key outside
    its six-field allowlist, and failing the operator's whole apply over a key
    this function invented would be the worse outcome.

    A malformed *shape* is the opposite case and is passed through untouched —
    a `Schedule` that is a bare string, or a list holding something other than
    mappings, reaches `_parse_entries`, which names the offending entry and
    fails the custom resource. Swallowing it here would apply the limit with
    its schedule silently missing, which is the one failure nothing downstream
    could detect.

    Each recognised property is coerced to its declared type on the way through
    (#554): CloudFormation delivers `Scale: 0.5` as `'0.5'`, and `_parse_entries`
    catches only `ValueError`, so the resulting `'<=' not supported between str
    and int` `TypeError` would escape unwrapped and fail the stack with a
    message naming no field at all.
    """
    if not isinstance(entries, list):
        return entries
    converted: list[Any] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            converted.append(entry)
            continue
        out: dict[str, Any] = {}
        for pascal, snake in _CFN_SCHEDULE_KEYS.items():
            if pascal not in entry:
                continue
            value = _CFN_SCHEDULE_COERCERS[pascal](entry[pascal], f"{where}[{i}].{pascal}")
            if value is not _ABSENT:
                out[snake] = value
        converted.append(out)
    return converted


def _cfn_limits_to_manifest(cfn_limits: dict[str, Any], *, where: str = "Limits") -> dict[str, Any]:
    """Convert CFN PascalCase limits to manifest snake_case.

    ``where`` is a dotted path prefix used only to build error messages, so a
    coercion failure (#554) names the limit an operator has to go and fix
    rather than just the property that was wrong.
    """
    result = {}
    for name, cfn_limit in cfn_limits.items():
        limit: dict[str, Any] = {}
        capacity = _coerce_int(cfn_limit["Capacity"], f"{where}.{name}.Capacity")
        if capacity is _ABSENT:
            # Empty means "absent" for an optional property, but `capacity` is
            # the one field `LimitDecl` requires; dropping it would surface as a
            # KeyError naming the snake_case key the operator never wrote.
            raise ValueError(
                f"{where}.{name}.Capacity is required and must not be empty. "
                "Every other limit property has a documented default; capacity "
                "is the allowance itself."
            )
        limit["capacity"] = capacity
        for prop, (key, coerce) in _CFN_LIMIT_OPTIONAL_KEYS.items():
            if prop in cfn_limit:
                value = coerce(cfn_limit[prop], f"{where}.{name}.{prop}")
                if value is not _ABSENT:
                    limit[key] = value
        # Set only when non-empty, matching both the generator's emission rule
        # and `LimitDecl.to_dict()`: an empty list must not be invented as a
        # key, or the manifest this path builds would differ from the one
        # `limits apply` sends for the same intent.
        for prop, key in (("Schedule", "schedule"), ("ResetSchedule", "reset_schedule")):
            if prop in cfn_limit:
                converted = _cfn_schedule_to_manifest(
                    cfn_limit[prop], where=f"{where}.{name}.{prop}"
                )
                if converted:
                    limit[key] = converted
        result[name] = limit
    return result


def _read_provisioner_state(table_name: str, namespace_id: str) -> dict[str, Any]:
    """Read the #PROVISIONER state record from DynamoDB."""
    client = boto3.client("dynamodb")
    result = client.get_item(
        TableName=table_name,
        Key={
            "PK": {"S": pk_system(namespace_id)},
            "SK": {"S": sk_provisioner()},
        },
    )
    item = result.get("Item")
    if not item:
        return {
            "managed_system": False,
            "managed_resources": [],
            "managed_entities": {},
            "cascade_pending": [],
            "fanout_pending": [],
            "full_pending": False,
        }

    managed_entities: dict[str, list[str]] = {}
    raw_entities = item.get("managed_entities", {}).get("M", {})
    for entity_id, resources_attr in raw_entities.items():
        managed_entities[entity_id] = [r["S"] for r in resources_attr.get("L", [])]

    return {
        "managed_system": item.get("managed_system", {}).get("BOOL", False),
        "managed_resources": [r["S"] for r in item.get("managed_resources", {}).get("L", [])],
        "managed_entities": managed_entities,
        "cascade_pending": [
            (entry["M"]["level"]["S"], entry["M"]["target"]["S"])
            for entry in item.get("cascade_pending", {}).get("L", [])
        ],
        "fanout_pending": [
            {field: entry["M"][field]["S"] for field in ("kind", "level", "target")}
            for entry in item.get("fanout_pending", {}).get("L", [])
        ],
        "full_pending": item.get("full_pending", {}).get("BOOL", False),
    }


def _write_provisioner_state(
    table_name: str,
    namespace_id: str,
    state: dict[str, Any],
) -> None:
    """Write the #PROVISIONER state record to DynamoDB."""
    client = boto3.client("dynamodb")
    item: dict[str, Any] = {
        "PK": {"S": pk_system(namespace_id)},
        "SK": {"S": sk_provisioner()},
        "GSI4PK": {"S": namespace_id},
        "managed_system": {"BOOL": state.get("managed_system", False)},
        "managed_resources": {"L": [{"S": r} for r in state.get("managed_resources", [])]},
        "managed_entities": {
            "M": {
                eid: {"L": [{"S": r} for r in resources]}
                for eid, resources in state.get("managed_entities", {}).items()
            }
        },
        "last_applied": {"S": state.get("last_applied", "")},
        "applied_hash": {"S": state.get("applied_hash", "")},
        # Levels whose cascade fan-out failed (ADR-146): retried by the next
        # apply, which would otherwise see no change and restamp nothing.
        "cascade_pending": _encode_cascade_pending(state.get("cascade_pending", [])),
        # Disable fan-outs and bucket param syncs that failed: retried by the
        # next apply, which (being change-only) would otherwise skip them.
        "fanout_pending": _encode_fanout_pending(state.get("fanout_pending", [])),
    }
    client.put_item(TableName=table_name, Item=item)


def _encode_cascade_pending(pending: list[tuple[str, str]]) -> dict[str, Any]:
    return {
        "L": [{"M": {"level": {"S": level}, "target": {"S": target}}} for level, target in pending]
    }


def _encode_fanout_pending(pending: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "L": [
            {"M": {field: {"S": entry[field]} for field in ("kind", "level", "target")}}
            for entry in pending
        ]
    }


def _resolve_namespace_id(table_name: str, namespace_name: str) -> str:
    """Resolve namespace name to opaque ID via DynamoDB lookup.

    Args:
        table_name: DynamoDB table name.
        namespace_name: Human-readable namespace name (e.g., "default").

    Returns:
        Opaque namespace ID string.

    Raises:
        ValueError: If the namespace is not registered.
    """
    client = boto3.client("dynamodb")
    result = client.get_item(
        TableName=table_name,
        Key={
            "PK": {"S": pk_system(RESERVED_NAMESPACE)},
            "SK": {"S": sk_namespace(namespace_name)},
        },
    )
    item = result.get("Item")
    if not item:
        raise ValueError(f"Namespace '{namespace_name}' not found in table '{table_name}'")
    return item["namespace_id"]["S"]


def _send_cfn_response(
    event: dict[str, Any],
    context: Any,
    status: str,
    *,
    data: dict[str, Any] | None = None,
    physical_resource_id: str | None = None,
    reason: str | None = None,
) -> None:
    """Send response to CloudFormation pre-signed URL.

    CloudFormation custom resources require a PUT to the ResponseURL
    with status information. Without this, the stack operation hangs
    until timeout (default 1 hour).
    """
    response_url = event["ResponseURL"]

    log_stream = getattr(context, "log_stream_name", "unknown") if context else "unknown"

    response_body = {
        "Status": status,
        "Reason": reason or f"See CloudWatch Log Stream: {log_stream}",
        "PhysicalResourceId": physical_resource_id or log_stream,
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "Data": data or {},
    }

    body = json.dumps(response_body).encode()
    req = urllib.request.Request(
        response_url,
        data=body,
        headers={"Content-Type": "", "Content-Length": str(len(body))},
        method="PUT",
    )
    urllib.request.urlopen(req)
