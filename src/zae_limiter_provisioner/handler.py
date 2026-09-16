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
    RESERVED_NAMESPACE,
    pk_system,
    sk_namespace,
    sk_provisioner,
)

from .applier import ApplyResult, apply_changes
from .bucket_sync import DEFAULT_TTL_MULTIPLIER, resolve_effective_limits, sync_bucket_params
from .differ import Change, compute_diff
from .fanout import fanout_entity, fanout_resource, resolve_disabled
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

    manifest = LimitsManifest.from_dict(manifest_data)
    previous = _read_provisioner_state(table_name, namespace_id)
    changes = compute_diff(manifest, previous)

    change_dicts = [{"action": c.action, "level": c.level, "target": c.target} for c in changes]

    if action == "plan":
        return {"status": "planned", "changes": change_dicts}

    # Apply
    result = _apply_and_record(manifest, changes, table_name, namespace_id)

    return {
        "status": "applied",
        "changes": change_dicts,
        "created": result.created,
        "updated": result.updated,
        "deleted": result.deleted,
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
    changes = compute_diff(manifest, previous)

    result = _apply_and_record(manifest, changes, table_name, namespace_id)

    return {
        "physical_resource_id": physical_resource_id,
        "status": "applied",
        "changes": [{"action": c.action, "level": c.level, "target": c.target} for c in changes],
        "created": result.created,
        "updated": result.updated,
        "deleted": result.deleted,
        "errors": result.errors,
    }


def _apply_and_record(
    manifest: LimitsManifest,
    changes: list[Change],
    table_name: str,
    namespace_id: str,
) -> ApplyResult:
    """Commit the config writes, fan out, then record the managed state (#563).

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
    result = apply_changes(changes, table_name, namespace_id)
    result.errors.extend(_fanout_disabled_changes(table_name, namespace_id, changes))
    result.errors.extend(_sync_bucket_param_changes(table_name, namespace_id, changes))

    manifest_hash = hashlib.sha256(
        json.dumps(manifest.to_dict(), sort_keys=True).encode()
    ).hexdigest()

    new_state = manifest.managed_set()
    new_state["last_applied"] = datetime.now(UTC).isoformat()
    new_state["applied_hash"] = f"sha256:{manifest_hash}"
    _write_provisioner_state(table_name, namespace_id, new_state)

    return result


def _fanout_disabled_changes(
    table_name: str,
    namespace_id: str,
    changes: list[Change],
) -> list[str]:
    """Eagerly stamp bucket items for every resource/entity change (ADR-125).

    Runs AFTER `apply_changes`, so every level it resolves already reflects
    this apply's writes and deletes.

    Fans out unconditionally for every create/update at the resource and
    entity levels — NOT only when the change's data happens to carry a
    `disabled` key. `ResourceDecl.to_dict()` / `EntityResourceDecl.to_dict()`
    omit `disabled` entirely when it is `None` ("inherit"), so an operator
    deleting a `disabled: true` line and re-applying produces a change with
    no `disabled` key at all. Filtering on key presence would skip that
    change's fan-out, leaving existing buckets stamped `disabled: true`
    forever (until TTL) even though the config item correctly lost the
    attribute.

    For a resource-level change, the effective value is simply the
    resource's own (possibly absent -> False) `disabled` value — there is no
    level above resource in the walk (system-level disable is out of scope
    per ADR-125), mirroring ``Repository._set_resource_disabled``'s
    ``disabled=bool(value)``. ``fanout_resource`` then re-resolves each
    bucket's entity and skips the ones with their own overriding value, so a
    per-entity carve-out made out of band survives an apply that merely
    re-asserts the resource's unchanged state (``differ.py`` emits a change
    for every manifest resource on every apply).

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

    Returns a list of human-readable failures, one per change that could not be
    stamped, rather than raising on the first (#563). Guarding is per change so
    one unreadable config item cannot abandon every *other* change's fan-out —
    the direct analogue of ``FanoutIncomplete.stamped`` reporting how far the
    async fan-out got. See ``_apply_and_record``.
    """
    candidates = [c for c in changes if c.level in ("resource", "entity") and c.target]
    if not candidates:
        return []

    errors: list[str] = []
    client = boto3.client("dynamodb")
    for change in sorted(candidates, key=lambda c: 0 if c.level == "resource" else 1):
        data = change.data or {}
        try:
            if change.level == "resource" and change.target:
                disabled = bool(data.get("disabled"))
                fanout_resource(client, table_name, namespace_id, change.target, disabled)
            elif change.level == "entity" and change.target:
                entity_id, resource = change.target.split("/", 1)
                disabled = resolve_disabled(client, table_name, namespace_id, entity_id, resource)
                fanout_resource_arg = None if resource == DEFAULT_RESOURCE else resource
                fanout_entity(
                    client, table_name, namespace_id, entity_id, fanout_resource_arg, disabled
                )
        except Exception as e:
            logger.warning("disable fan-out failed for %s %s: %s", change.level, change.target, e)
            errors.append(f"disable fan-out {change.level} {change.target}: {e}")
    return errors


def _sync_bucket_param_changes(
    table_name: str,
    namespace_id: str,
    changes: list[Change],
) -> list[str]:
    """Push entity-level limit changes out to existing bucket items (#481).

    Runs AFTER ``apply_changes``, so ``resolve_effective_limits`` sees the
    config this apply has already written — the same ordering contract
    ``_fanout_disabled_changes`` relies on.

    **Entity level only.** Resource and system defaults deliberately never
    touch buckets: a bucket on defaults carries a TTL and is recreated with
    current params when it expires (#271, #296).

    Returns a list of human-readable failures, one per entity change that could
    not be synced, rather than raising on the first (#563). ``_decode_limits``
    raises on a stored compact schedule this provisioner cannot read (PR #549,
    deliberately — silently dropping the limit was the bug class that change
    removed), and a config item written by a newer client is a realistic source
    of one: design §4.1 ships no version marker (#515). Left unguarded, that
    ``ValueError`` escaped past ``_write_provisioner_state``. See
    ``_apply_and_record``.
    """
    client = boto3.client("dynamodb")
    now_ms = int(time.time() * 1000)
    errors: list[str] = []

    for change in changes:
        if change.level != "entity" or change.target is None:
            continue
        entity_id, resource = change.target.split("/", 1)
        declared = (change.data or {}).get("limits", {})

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
            errors.append(f"bucket param sync entity {change.target}: {e}")
    return errors


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


def _coerce_bool(value: Any, where: str) -> bool:
    """Coerce a CloudFormation-delivered `Disabled` value to a real ``bool``.

    A case-insensitive allowlist that **raises** on anything else, rather than
    falling through to ``bool(value)``. Case matters concretely: an author
    writing ``Disabled: "True"`` (quoted, so YAML keeps it a string) would
    otherwise arrive as ``'True'`` and be truthy by accident rather than by
    the allowlist. ``'1'``, ``'yes'``, ``''`` and every other spelling are
    rejected: this is the ADR-125 kill switch, and guessing wrong either
    disables a tenant or re-admits one that was meant to stay out.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise ValueError(
        f"{where} must be true or false, got {value!r}. CloudFormation delivers "
        f"every property as a string, so this boundary accepts only 'true'/'false' "
        f"(any case) or a real boolean — anything else is rejected rather than "
        f"guessed, because `disabled` is tri-state and a wrong guess silently "
        f"disables or re-admits a tenant (ADR-125)."
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
                resource_entry["disabled"] = _coerce_bool(
                    cfn_resource["Disabled"], f"Resources.{resource_name}.Disabled"
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
                    entity_resource_entry["disabled"] = _coerce_bool(
                        cfn_res["Disabled"], f"{prefix}.Disabled"
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
        }

    managed_entities: dict[str, list[str]] = {}
    raw_entities = item.get("managed_entities", {}).get("M", {})
    for entity_id, resources_attr in raw_entities.items():
        managed_entities[entity_id] = [r["S"] for r in resources_attr.get("L", [])]

    return {
        "managed_system": item.get("managed_system", {}).get("BOOL", False),
        "managed_resources": [r["S"] for r in item.get("managed_resources", {}).get("L", [])],
        "managed_entities": managed_entities,
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
    }
    client.put_item(TableName=table_name, Item=item)


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
