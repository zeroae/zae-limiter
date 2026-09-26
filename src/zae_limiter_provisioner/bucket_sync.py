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
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from zae_limiter.models import Limit
from zae_limiter.schedule import ScheduleEntry, decode, decode_reset, encode, encode_reset
from zae_limiter.schema import (
    BUCKET_FIELD_CP,
    BUCKET_FIELD_RA,
    BUCKET_FIELD_RP,
    BUCKET_FIELD_RSA,
    BUCKET_FIELD_RSCHED,
    BUCKET_FIELD_SCHED,
    BUCKET_FIELD_SCHED_TZ,
    BUCKET_FIELD_TC,
    BUCKET_FIELD_TK,
    BUCKET_FIELD_VU,
    BUCKET_SCHED_NONE,
    CONFIG_FIELD_SCHED_TZ,
    DEFAULT_RESOURCE,
    GSI3_NAME,
    LIMIT_FIELD_CP,
    LIMIT_FIELD_RA,
    LIMIT_FIELD_RP,
    LIMIT_FIELD_RSA,
    LIMIT_FIELD_RSCHED,
    LIMIT_FIELD_SCHED,
    bucket_attr,
    calculate_bucket_ttl_seconds,
    calculate_ttl,
    gsi3_pk_entity,
    parse_bucket_pk,
    parse_limit_attr,
    pk_entity,
    pk_resource,
    pk_system,
    sk_config,
    sk_state,
)

from .manifest import entries_from_manifest

logger = logging.getLogger(__name__)

# `Repository._bucket_ttl_refill_multiplier`'s default. A bucket running on
# resource/system defaults expires after max_time_to_fill * this (#271, #296).
# The provisioner has no Repository to read it from and no knob of its own.
DEFAULT_TTL_MULTIPLIER = 7

# Levels that mean "this entity has custom limits", so its bucket must persist.
_ENTITY_LEVELS = ("entity", "entity_default")

# Config attribute -> manifest key. The numeric trio and the two compact
# schedule strings decode differently, so the split is kept explicit rather
# than inferred from the DynamoDB type tag.
_MANIFEST_NUMERIC_KEY = {
    LIMIT_FIELD_CP: "capacity",
    LIMIT_FIELD_RA: "refill_amount",
    LIMIT_FIELD_RP: "refill_period",
}
_MANIFEST_SCHEDULE_KEY = {
    LIMIT_FIELD_SCHED: "schedule",
    LIMIT_FIELD_RSCHED: "reset_schedule",
}
# The duration-window length (ADR-139) decodes the same way as the numeric
# trio above (a plain "N" attribute), but it is optional — a limit carries it
# only when it is a session quota — so it is kept out of
# `_MANIFEST_NUMERIC_KEY` / `_REQUIRED_MANIFEST_KEYS`: folding it in there
# would make every decoded limit need a `reset_after`, rejecting every
# ordinary rate limit as malformed.
_MANIFEST_OPTIONAL_NUMERIC_KEY = {
    LIMIT_FIELD_RSA: "reset_after",
}

# A limit missing any of these is malformed and is dropped rather than given a
# synthesised default. Deliberately a REQUIRED-keys test (`<=`) and not the
# exact-set equality it replaced: the decoded shape gained `schedule` and
# `reset_schedule` in #222, and an equality check turns every future widening
# into a silent, exception-free disappearance of the widened limit — which is
# indistinguishable from the operator having deleted it by the time
# `_resolved_plan` acts on it.
_REQUIRED_MANIFEST_KEYS = frozenset(_MANIFEST_NUMERIC_KEY.values())

# Fields removed for a limit that no longer exists in the effective config.
# BUCKET_FIELD_RF is deliberately absent: it is shared across every limit in a
# composite bucket and carries the optimistic lock.
_STALE_FIELDS = (
    BUCKET_FIELD_TK,
    BUCKET_FIELD_CP,
    BUCKET_FIELD_RA,
    BUCKET_FIELD_RP,
    BUCKET_FIELD_TC,
    # A dropped limit's own schedule overrides go with it, matching the async
    # path. A limit deleted through a manifest may have carried a schedule —
    # from the manifest itself since #543, or from the Python API — and left
    # behind they re-attach the moment a limit of that name is configured
    # again. That is worse for `rsched` than for `sched`: an orphan reset
    # restores a balance on a calendar nobody configured.
    BUCKET_FIELD_SCHED,
    BUCKET_FIELD_RSCHED,
)


def _encode_one_tuple(
    named_schedules: list[tuple[str, tuple[ScheduleEntry, ...]]],
    encoder: Callable[[tuple[ScheduleEntry, ...]], tuple[str, str | None]],
) -> tuple[str, dict[str, str]] | None:
    """§4.1's item-level default plus per-limit overrides, for one tuple.

    Sync mirror of ``Repository._encode_one_tuple``. The two must not diverge:
    a manifest apply and a ``set_limits()`` call write the same attributes on
    the same items, and a different choice of item-level default here would
    silently re-scale whichever limits inherit it.

    A limit with **no** schedule of this kind gets the explicit
    ``BUCKET_SCHED_NONE`` override (#541), exactly as the async encoder does.
    Absence still means "inherit the item default"; it just no longer *also*
    means "unscheduled". Diverging here would give one item two inheritance
    rules depending on which writer touched it last.
    """
    scheduled = [(name, encoder(sched)[0]) for name, sched in named_schedules if sched]
    if not scheduled:
        return None
    default_compact = scheduled[0][1]
    encodings = dict(scheduled)
    overrides = {}
    for name, _sched in named_schedules:
        compact = encodings.get(name, BUCKET_SCHED_NONE)
        if compact != default_compact:
            overrides[name] = compact
    return default_compact, overrides


def _encode_item_schedules(
    named: list[tuple[str, tuple[ScheduleEntry, ...], tuple[ScheduleEntry, ...]]],
) -> tuple[str, tuple[str, dict[str, str]] | None, tuple[str, dict[str, str]] | None] | None:
    """Both tuples and the one timezone they share, for a whole bucket item.

    Sync mirror of ``Repository._encode_item_schedules``. Resolved together,
    not by two independent calls, because ``sched_tz`` is a single item-level
    attribute: one call would SET it while the other REMOVEd it in the same
    expression, which is the #488 ``ValidationException``.

    Returns ``None`` when nothing on the item is scheduled at all.

    Raises:
        ValueError: the scheduled limits disagree on a timezone. Silently
            keeping the first limit's zone would reinterpret the second
            limit's cron in the wrong one.
    """
    zones = {entry.tz for _name, sched, reset in named for entry in (*sched, *reset)}
    if len(zones) > 1:
        raise ValueError(
            f"all scheduled limits on one bucket item must share a timezone, got "
            f"{sorted(zones)}. The timezone is stored once per item as `sched_tz`, "
            f"not per limit."
        )
    if not zones:
        return None
    param = _encode_one_tuple([(name, sched) for name, sched, _reset in named], encode)
    reset_part = _encode_one_tuple([(name, reset) for name, _sched, reset in named], encode_reset)
    return zones.pop(), param, reset_part


def build_bucket_param_update(
    limits: dict[str, dict[str, Any]],
    ttl_multiplier: int | None,
    stale_limit_names: set[str] | None,
    now_ms: int,
) -> tuple[str, dict[str, str], dict[str, dict[str, str]]]:
    """Build the SET/REMOVE UpdateExpression for one bucket shard.

    Args:
        limits: Manifest-shaped limits, ``{name: {capacity, refill_amount,
            refill_period}}``, in whole tokens and seconds, optionally with
            ``schedule`` / ``reset_schedule`` entries (#222) or a
            ``reset_after`` duration-window length in seconds (ADR-139).
        ttl_multiplier: None leaves ``ttl`` alone; 0 REMOVEs it (entity has
            custom limits, so the bucket must persist); >0 SETs it.
        stale_limit_names: Limit names to strip from the bucket entirely.
        now_ms: Current time, epoch milliseconds.

    Returns:
        ``(update_expr, expr_names, expr_values)``.

    Raises:
        ValueError: two scheduled limits disagree on a timezone.
    """
    set_parts: list[str] = []
    remove_parts: list[str] = []
    expr_names: dict[str, str] = {}
    expr_values: dict[str, dict[str, str]] = {}

    # Numeric indices for expression names: limit names may contain hyphens,
    # dots, slashes and colons, none of which are legal in an alias.
    names = list(limits)
    for i, (name, decl) in enumerate(limits.items()):
        for alias, field, value in (
            (f"#cp{i}", BUCKET_FIELD_CP, decl["capacity"] * 1000),
            (f"#ra{i}", BUCKET_FIELD_RA, decl["refill_amount"] * 1000),
            (f"#rp{i}", BUCKET_FIELD_RP, decl["refill_period"] * 1000),
        ):
            set_parts.append(f"{alias} = :{alias[1:]}")
            expr_names[alias] = bucket_attr(name, field)
            expr_values[f":{alias[1:]}"] = {"N": str(value)}

        # Duration window length in seconds (ADR-139), mirroring
        # `Repository._build_bucket_param_update`. SET where this limit has
        # one, REMOVE where it does not — absence means "no window", full
        # stop, so this needs no BUCKET_SCHED_NONE analogue (#541): there is
        # no item-level default to inherit. A `rsa` left behind on a limit
        # converted back to a drip would keep the item reconstructing as a
        # quota forever.
        #
        # `ws` (window start) is deliberately NOT written here, for the same
        # reason the async path never writes it: a manifest apply is not a
        # rollover, and stamping it would restart every caller's window on
        # an unrelated edit. The `vu = 0` this write already stamps
        # unconditionally (below) forces the one materialising pass that
        # anchors a first window or leaves an existing one alone.
        rsa_alias = f"#rsa{i}"
        expr_names[rsa_alias] = bucket_attr(name, BUCKET_FIELD_RSA)
        reset_after = decl.get("reset_after")
        if reset_after is not None:
            set_parts.append(f"{rsa_alias} = :{rsa_alias[1:]}")
            expr_values[f":{rsa_alias[1:]}"] = {"N": str(reset_after)}
        else:
            remove_parts.append(rsa_alias)

    # Re-stamp both schedules (#222 §2.2, §3.6). Manifests learned to express
    # schedules in #543, which is what lifts this mirror's old exemption: a
    # bucket left holding a superseded `sched` is refilled toward a ceiling the
    # operator has already changed, and one left holding a superseded `rsched`
    # keeps resetting on a calendar nobody asked for any more. Override, not
    # merge — including when the manifest carries no schedule at all, since
    # that is how a schedule is *removed*.
    parsed = [
        (
            name,
            entries_from_manifest(d.get("schedule"), reset=False),
            entries_from_manifest(d.get("reset_schedule"), reset=True),
        )
        for name, d in limits.items()
    ]
    encoded = _encode_item_schedules(parsed)
    # `sched_tz` is shared by both tuples, so it is decided once, from whether
    # *anything* on the item is scheduled. Deciding it inside the parameter
    # branch would REMOVE it for a quota carrying only a reset — and the stored
    # `rsched` would then decode as UTC forever — or SET and REMOVE it in one
    # expression (#488).
    expr_names["#sched_tz"] = BUCKET_FIELD_SCHED_TZ
    if encoded is None:
        param, reset_part = None, None
        remove_parts.append("#sched_tz")
    else:
        tz, param, reset_part = encoded
        set_parts.append("#sched_tz = :sched_tz")
        expr_values[":sched_tz"] = {"S": tz}

    # Per-limit overrides are SET where a limit differs from the item default
    # and REMOVEd everywhere else — including on the scheduled branch. Absence
    # means "inherit the item default", so a limit that used to carry its own
    # schedule and now shares the default keeps enforcing the superseded one
    # forever unless its override is stripped. A limit that now has *no*
    # schedule gets `BUCKET_SCHED_NONE` SET instead (#541) — removing its
    # override would make it inherit the default. Each alias lands in exactly
    # one of the two lists, never both (#488).
    for prefix, field, part in (
        ("sched", BUCKET_FIELD_SCHED, param),
        ("rsched", BUCKET_FIELD_RSCHED, reset_part),
    ):
        item_alias = f"#{prefix}"
        expr_names[item_alias] = field
        if part is None:
            overrides: dict[str, str] = {}
            remove_parts.append(item_alias)
        else:
            default_compact, overrides = part
            set_parts.append(f"{item_alias} = :{prefix}")
            expr_values[f":{prefix}"] = {"S": default_compact}
        for i, name in enumerate(names):
            alias = f"#l{prefix}{i}"
            expr_names[alias] = bucket_attr(name, field)
            compact = overrides.get(name)
            if compact is None:
                remove_parts.append(alias)
            else:
                set_parts.append(f"{alias} = :l{prefix}{i}")
                expr_values[f":l{prefix}{i}"] = {"S": compact}

    # Mirrors `Repository._build_bucket_param_update`: `vu = 0` on EVERY
    # fan-out, scheduled or not, forcing exactly one materialising pass that
    # clamps a surplus over a lowered ceiling before the fast path (a pure ADD
    # with no cap maths) can spend it. A manifest apply that shrinks a
    # capacity has precisely #469's exposure, and `differ.py` re-asserts every
    # manifest resource on every apply, so the mirror needs this as much as
    # the async path does. `#vu` is SET, so it must never join `remove_parts`
    # (#488).
    set_parts.append("#vu = :vu_zero")
    expr_names["#vu"] = BUCKET_FIELD_VU
    expr_values[":vu_zero"] = {"N": "0"}

    if ttl_multiplier is not None:
        expr_names["#ttl"] = "ttl"
        if ttl_multiplier > 0:
            # The schedules are carried into the rebuilt `Limit` because
            # ADR-137 rejects a zero `refill_amount` that has no
            # `reset_schedule` and no `reset_after` — a manifest quota
            # round-trips to exactly that, and so does a session quota
            # (ADR-139), so dropping either reset here raises before the TTL
            # is ever computed. (The resource/system case then divides by
            # the zero rate, which is #532 and is fixed in `schema`, not
            # here.)
            ttl_seconds = calculate_bucket_ttl_seconds(
                [
                    Limit(
                        name=n,
                        capacity=limits[n]["capacity"],
                        refill_amount=limits[n]["refill_amount"],
                        refill_period_seconds=limits[n]["refill_period"],
                        schedule=sched,
                        reset_schedule=reset_sched,
                        reset_after=(
                            timedelta(seconds=limits[n]["reset_after"])
                            if limits[n].get("reset_after") is not None
                            else None
                        ),
                    )
                    for n, sched, reset_sched in parsed
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


def _resolved_plan(
    client: Any,
    table_name: str,
    namespace_id: str,
    entity_id: str,
    bucket_resource: str,
    directive_limits: dict[str, dict[str, Any]],
    stale_limit_names: set[str] | None,
    now_ms: int,
) -> tuple[str, dict[str, str], dict[str, dict[str, str]]] | None:
    """Build one bucket's update from the limits resolved for ITS resource.

    Used only under the entity-wide (``_default_``) scope, and mirroring
    ``Repository._resolved_bucket_param_update``. The caller's limits are a
    directive for the level the manifest wrote, and a resource with its own
    entity config outranks that level; writing the caller's limits everywhere
    would clobber the more specific config with the less specific one (#487).

    TTL follows the level that answered (entity persists, resource/system
    expires), and the caller's stale names are intersected with the
    resolution: a name the bucket's own level still declares must not be SET
    and REMOVEd in one expression, and a directive limit absent from this
    resource's resolution is stale here even though the caller did not name it.

    Returns None when nothing resolves — there is no correct value to write,
    so the bucket is left alone.
    """
    resolved, level = resolve_bucket_limits(
        client, table_name, namespace_id, entity_id, bucket_resource
    )
    if not resolved:
        return None
    stale = (set(stale_limit_names or ()) | set(directive_limits)) - set(resolved)
    multiplier = 0 if level in _ENTITY_LEVELS else DEFAULT_TTL_MULTIPLIER
    return build_bucket_param_update(resolved, multiplier, stale or None, now_ms)


def sync_bucket_params(
    client: Any,
    table_name: str,
    namespace_id: str,
    entity_id: str,
    resource: str,
    limits: dict[str, dict[str, Any]],
    ttl_multiplier: int | None,
    stale_limit_names: set[str] | None,
    now_ms: int,
) -> int:
    """Push changed limit params to every shard of one entity's buckets.

    Two discovery passes, exactly like the ADR-125 disable fan-out in
    ``fanout.py``: the second catches a bucket created by an ``acquire()``
    already in flight when the first pass's query ran. Mitigates, but does not
    eliminate, that race.

    ``_default_`` is the entity-WIDE config scope, not a resource, so it is
    translated to an unscoped discovery exactly as ``handler.py`` translates it
    for ``fanout_entity``. Forwarding it built the prefix ``BUCKET#_default_#``,
    which no bucket item can ever carry, so the query matched zero items and
    the whole sync was a silent no-op (#487). Under that scope each discovered
    bucket is written from the limits resolved for its OWN resource — see
    ``_resolved_plan`` — memoized per distinct resource.

    Returns the number of shards actually written.
    """
    if not limits:
        return 0

    unscoped = resource == DEFAULT_RESOURCE
    plans: dict[str, tuple[str, dict[str, str], dict[str, dict[str, str]]] | None] = {}
    if not unscoped:
        plans[resource] = build_bucket_param_update(
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
                    ":sk": {"S": "BUCKET#" if unscoped else f"BUCKET#{resource}#"},
                },
            }
            if start_key:
                params["ExclusiveStartKey"] = start_key
            response = client.query(**params)
            for item in response.get("Items", []):
                pk = item.get("PK", {}).get("S", "")
                if not pk or pk in synced:
                    continue
                synced.add(pk)
                _ns, _eid, bucket_resource, _shard = parse_bucket_pk(pk)
                if bucket_resource not in plans:
                    plans[bucket_resource] = _resolved_plan(
                        client,
                        table_name,
                        namespace_id,
                        entity_id,
                        bucket_resource,
                        limits,
                        stale_limit_names,
                        now_ms,
                    )
                plan = plans[bucket_resource]
                if plan is None:
                    continue
                if _update_one_shard(client, table_name, pk, *plan):
                    written += 1
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                break
    return written


def _decode_limits(item: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Decode composite ``l_{name}_{field}`` attributes into manifest shape.

    A limit missing any of cp/ra/rp is malformed and is skipped rather than
    given a synthesised default, which would silently invent a limit. A limit
    carrying *more* than those — a schedule, a reset schedule, or a field a
    newer writer added — is kept: the filter tests for the required keys, not
    for an exact set, because a limit dropped here is indistinguishable
    downstream from one the operator deleted and `_resolved_plan` would unstamp
    it from the bucket enforcing it.

    The timezone is hoisted to one item-level ``sched_tz`` (#222 §4.1), so it
    is read once here and applied to every entry. A corrupt compact string
    raises out of ``decode`` rather than being skipped, for the same reason:
    silence here removes a limit.
    """
    sched_tz = item.get(CONFIG_FIELD_SCHED_TZ, {}).get("S") or "UTC"
    partial: dict[str, dict[str, Any]] = {}
    for attr, value in item.items():
        parsed = parse_limit_attr(attr)
        if parsed is None:
            continue
        name, field = parsed
        if field in _MANIFEST_NUMERIC_KEY:
            partial.setdefault(name, {})[_MANIFEST_NUMERIC_KEY[field]] = int(value["N"])
        elif field in _MANIFEST_OPTIONAL_NUMERIC_KEY:
            partial.setdefault(name, {})[_MANIFEST_OPTIONAL_NUMERIC_KEY[field]] = int(value["N"])
        elif field in _MANIFEST_SCHEDULE_KEY:
            decoder = decode_reset if field == LIMIT_FIELD_RSCHED else decode
            partial.setdefault(name, {})[_MANIFEST_SCHEDULE_KEY[field]] = decoder(
                value["S"], sched_tz
            )
    return {name: decl for name, decl in partial.items() if _REQUIRED_MANIFEST_KEYS <= decl.keys()}


def _walk(
    client: Any,
    table_name: str,
    levels: list[tuple[str, str, str]],
) -> tuple[dict[str, dict[str, Any]], str | None]:
    """Return the first level that defines any limits, and which one it was."""
    for level, pk, sk in levels:
        response = client.get_item(TableName=table_name, Key={"PK": {"S": pk}, "SK": {"S": sk}})
        item = response.get("Item")
        if not item:
            continue
        limits = _decode_limits(item)
        if limits:
            return limits, level
    return {}, None


def _fallback_levels(
    namespace_id: str, entity_id: str, resource: str
) -> list[tuple[str, str, str]]:
    """entity(`_default_`) -> resource -> system, in precedence order.

    The entity(`_default_`) level is skipped when `resource` already IS
    `_default_`, exactly as ``fanout.resolve_disabled`` does.
    """
    levels: list[tuple[str, str, str]] = []
    if resource != DEFAULT_RESOURCE:
        levels.append(
            ("entity_default", pk_entity(namespace_id, entity_id), sk_config(DEFAULT_RESOURCE))
        )
    levels.append(("resource", pk_resource(namespace_id, resource), sk_config()))
    levels.append(("system", pk_system(namespace_id), sk_config()))
    return levels


def resolve_effective_limits(
    client: Any,
    table_name: str,
    namespace_id: str,
    entity_id: str,
    resource: str,
) -> dict[str, dict[str, Any]]:
    """Effective limits after an entity's per-resource config is deleted.

    Walks entity(`_default_`) -> resource -> system and returns the first
    level that defines any limits, mirroring the precedence in ADR-100 minus
    the entity(resource) level the caller has just removed.
    """
    limits, _level = _walk(client, table_name, _fallback_levels(namespace_id, entity_id, resource))
    return limits


def resolve_bucket_limits(
    client: Any,
    table_name: str,
    namespace_id: str,
    entity_id: str,
    resource: str,
) -> tuple[dict[str, dict[str, Any]], str | None]:
    """Full-precedence walk for one existing bucket, and the level that won.

    entity(resource) -> entity(`_default_`) -> resource -> system, the whole
    ADR-100 hierarchy. Unlike ``resolve_effective_limits`` this keeps the
    entity(resource) level, because the entity-wide fan-out is asking "what
    applies to this bucket right now", not "what applies now that the caller's
    own level is gone". The level is returned because it decides the bucket's
    TTL (#271, #296).
    """
    levels: list[tuple[str, str, str]] = [
        ("entity", pk_entity(namespace_id, entity_id), sk_config(resource))
    ]
    levels.extend(_fallback_levels(namespace_id, entity_id, resource))
    return _walk(client, table_name, levels)
