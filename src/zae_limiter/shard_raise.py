"""Raising the shard count of a bucket holding a dripping limit (ADR-156, #821).

ADR-133 created a shard N>0 at a full share on first use and doubled
``shard_count`` without touching the shards that already existed, a transient
of up to 1.5x the limit that ADR-153 makes a defect (the 2026-11-06 review
admitted 1252, 1402 and 1902 against 1000). A raise from ``S`` to ``2S``
shards of a bucket holding a dripping limit instead:

1. stamps a short-lived marker (``rx``, a deadline) on every shard, refused by
   every write that moves a fill instant, ``xi`` or the shard count while it
   is live (``schema.raise_guard``), so the hot writers cannot break the
   raise's pins. The marker write's ``ALL_NEW`` image is the raise's strongly
   consistent read of that shard;
2. commits one transaction pinned on what it read (:func:`plan_raise`), with
   ADR-150's owner check: each existing shard ``i`` re-expressed at the new
   count keeping ``min(b_i, C/2S)``, and shard ``i + S`` created as a copy of
   it holding ``max(0, b_i − C/2S)`` of each dripping limit (proofs §8).

The total balance is conserved exactly. A shard missing while others exist is
created empty, adding nothing; every new shard's ``wcu`` starts full, because
that budget belongs to the partition. A quota is not copied: its new shard
seeds it on its first slow pass, funded by a move off the sibling covering its
slot (ADR-145), and each legacy quota on an existing shard has its grant size
frozen in the same write. A writer the marker does not hold off (a ``disabled``
or owner stamp fan-out) is pinned instead, so a stamp landing in between
cancels the raise rather than being copied stale.

Pure: works on wire-format items, so the client (``Repository.raise_shards``)
and the aggregator (``processor.raise_bucket_shards``) send the same writes.
Vendored into the aggregator's zip with ``fill_sync`` and ``fill_instant``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import fill_instant, fill_sync, schedule, schema

# Item-level attributes copied onto a new shard that a writer the marker does
# not hold off can change: pinned on every existing shard, so the copy is exact.
_PINNED = (
    schema.BUCKET_FIELD_DISABLED,
    schema.BUCKET_FIELD_BYPASS,
    "cascade",
    "parent_id",
    schema.BUCKET_FIELD_PGEN,
    schema.BUCKET_FIELD_PINC,
    schema.BUCKET_FIELD_SCHED,
    schema.BUCKET_FIELD_SCHED_TZ,
)

# Every per-limit attribute a quota can carry, removed from a new shard.
_QUOTA_FIELDS = (
    schema.BUCKET_FIELD_TK,
    schema.BUCKET_FIELD_FI,
    schema.BUCKET_FIELD_CP,
    schema.BUCKET_FIELD_RA,
    schema.BUCKET_FIELD_RP,
    schema.BUCKET_FIELD_TC,
    schema.BUCKET_FIELD_SCHED,
    schema.BUCKET_FIELD_RSCHED,
    schema.BUCKET_FIELD_WS,
    schema.BUCKET_FIELD_RSA,
    schema.BUCKET_FIELD_WA,
    schema.BUCKET_FIELD_WTC,
    schema.BUCKET_FIELD_GC,
    schema.BUCKET_FIELD_TU,
    schema.BUCKET_FIELD_RN,
    schema.BUCKET_FIELD_SOFT,
)

_WCU_PARAMS = fill_instant.ShardParams(
    schema.WCU_LIMIT_CAPACITY * 1000,
    schema.WCU_LIMIT_REFILL_AMOUNT * 1000,
    schema.WCU_LIMIT_REFILL_PERIOD_SECONDS * 1000,
    1,
)


def _n(value: int) -> dict[str, str]:
    return {"N": str(value)}


def _number(item: dict[str, Any], attr: str) -> int | None:
    raw = item.get(attr, {}).get("N")
    return int(raw) if raw is not None else None


def stored_count(item: dict[str, Any]) -> int:
    """An item's stored ``shard_count``; absent or corrupt (< 1) reads as 1."""
    return max(1, _number(item, "shard_count") or 1)


def marker_write(deadline_ms: int, now_ms: int) -> dict[str, Any]:
    """The ``UpdateItem`` arguments stamping a raise marker with ``deadline_ms``.

    Refused while another raise's marker is live; send it with
    ``ReturnValues="ALL_NEW"`` (the raise's read of the shard) and
    ``ReturnValuesOnConditionCheckFailure="ALL_OLD"`` (a missing shard returns
    no image, a busy one its marker).
    """
    return {
        "UpdateExpression": "SET #rx = :rxd",
        "ConditionExpression": (
            "attribute_exists(PK) AND (attribute_not_exists(#rx) OR #rx <= :rxt)"
        ),
        "ExpressionAttributeNames": {"#rx": schema.BUCKET_FIELD_RAISE},
        "ExpressionAttributeValues": {":rxd": _n(deadline_ms), ":rxt": _n(now_ms)},
    }


def release_write(deadline_ms: int) -> dict[str, Any]:
    """The ``UpdateItem`` arguments removing this raise's own marker after it gave up."""
    return {
        "UpdateExpression": "REMOVE #rx",
        "ConditionExpression": "#rx = :rxd",
        "ExpressionAttributeNames": {"#rx": schema.BUCKET_FIELD_RAISE},
        "ExpressionAttributeValues": {":rxd": _n(deadline_ms)},
    }


def limit_names(item: dict[str, Any]) -> list[str]:
    """Every limit on ``item``: a ``tk`` or ``fi`` beside its ``cp``, as the reader finds them."""
    names: set[str] = set()
    prefix = schema.BUCKET_ATTR_PREFIX
    for attr in item:
        if not attr.startswith(prefix):
            continue
        for field in (schema.BUCKET_FIELD_TK, schema.BUCKET_FIELD_FI):
            suffix = f"_{field}"
            if attr.endswith(suffix):
                name = attr[len(prefix) : -len(suffix)]
                if name and schema.bucket_attr(name, schema.BUCKET_FIELD_CP) in item:
                    names.add(name)
    return sorted(names)


def _resets(item: dict[str, Any], name: str) -> bool:
    """Whether ``name`` on ``item`` carries a reset: its own window or reset schedule,
    or the item's default reset schedule unless the #541 marker blocks it."""
    if schema.bucket_attr(name, schema.BUCKET_FIELD_RSA) in item:
        return True
    own = item.get(schema.bucket_attr(name, schema.BUCKET_FIELD_RSCHED), {}).get("S")
    if own == schema.BUCKET_SCHED_NONE:
        return False
    return bool(own or item.get(schema.BUCKET_FIELD_RSCHED, {}).get("S"))


def _drips(item: dict[str, Any], name: str) -> bool:
    """A dripping limit (ADR-137): a positive rate and no reset, as the aggregator reads it."""
    ra = _number(item, schema.bucket_attr(name, schema.BUCKET_FIELD_RA))
    rp = _number(item, schema.bucket_attr(name, schema.BUCKET_FIELD_RP))
    return (
        ra is not None
        and ra > 0
        and rp is not None
        and rp > 0
        and (name == schema.WCU_LIMIT_NAME or not _resets(item, name))
    )


def holds_dripping(item: dict[str, Any] | None) -> bool:
    """Whether ``item`` holds a dripping user limit: its bucket is raised by :func:`plan_raise`."""
    if not item:
        return False
    return any(name != schema.WCU_LIMIT_NAME and _drips(item, name) for name in limit_names(item))


def legacy_quota_sizes(item: dict[str, Any], skip: set[str], now_ms: int) -> list[tuple[str, int]]:
    """``(name, grant_count)`` for every legacy quota ``item`` carries outside ``skip``.

    A quota is read off the stored shape: a zero refill rate beside a reset —
    the limit's own ``b_{name}_rsa`` or ``b_{name}_rsched``, or the item-level
    ``rsched`` unless the #541 ``BUCKET_SCHED_NONE`` marker blocks it. Only one
    with no ``gc`` (ADR-145 design §9) is returned, sized by the R7 rule
    against its schedule-effective capacity: the stored count, halved until the
    share covers what the shard holds. An undecodable schedule sizes against
    the base capacity.
    """
    tz = item.get(schema.BUCKET_FIELD_SCHED_TZ, {}).get("S", "UTC")
    sizes: list[tuple[str, int]] = []
    prefix, suffix = schema.BUCKET_ATTR_PREFIX, f"_{schema.BUCKET_FIELD_TK}"
    for attr in item:
        if not (attr.startswith(prefix) and attr.endswith(suffix)):
            continue
        name = attr[len(prefix) : -len(suffix)]

        def field(key: str, kind: str, name: str = name) -> str | None:
            value = item.get(schema.bucket_attr(name, key), {}).get(kind)
            return None if value is None else str(value)

        if (
            not name
            or name == schema.WCU_LIMIT_NAME
            or name in skip
            or schema.bucket_attr(name, schema.BUCKET_FIELD_GC) in item
            or int(field(schema.BUCKET_FIELD_RA, "N") or "0") != 0
        ):
            continue
        cp = field(schema.BUCKET_FIELD_CP, "N")
        if not _resets(item, name) or cp is None:
            continue
        own_sched = field(schema.BUCKET_FIELD_SCHED, "S")
        compact = (
            None
            if own_sched == schema.BUCKET_SCHED_NONE
            else own_sched or item.get(schema.BUCKET_FIELD_SCHED, {}).get("S")
        )
        try:
            sched = schedule.decode(compact, tz) if compact else ()
        except ValueError:
            sched = ()
        rp = int(field(schema.BUCKET_FIELD_RP, "N") or "1000")
        capacity = schedule.effective_params(int(cp), 0, rp, sched, now_ms)[0]
        grant_count = stored_count(item)
        tokens = int(item[attr]["N"])
        while grant_count > 1 and tokens > capacity // grant_count:
            grant_count //= 2
        sizes.append((name, grant_count))
    return sizes


def _expressed_at(item: dict[str, Any]) -> int:
    """The instant an existing shard's dripping limits are re-expressed and split at.

    Its clock reading may lag the instant they were last expressed (``xi``, or
    ``rf`` for one still stored as ``tk``); a fill instant is never expressed
    backwards.
    """
    return max(_number(item, schema.BUCKET_FIELD_XI) or 0, _number(item, "rf") or 0)


def _fill_at(
    item: dict[str, Any], name: str, at_ms: int, shards: int
) -> tuple[int, fill_instant.ShardParams] | None:
    """One dripping limit's fill instant at ``at_ms`` and the parameters then in force.

    Read as a fill instant, or converted from ``tk``/``rf`` exactly (#817), at
    the item's stored count, re-expressed across every schedule boundary since
    it was expressed (ADR-155), then at ``shards`` when the item's count lags
    it (trimmed to the larger count's share).
    """
    cp = _number(item, schema.bucket_attr(name, schema.BUCKET_FIELD_CP))
    ra = _number(item, schema.bucket_attr(name, schema.BUCKET_FIELD_RA))
    rp = _number(item, schema.bucket_attr(name, schema.BUCKET_FIELD_RP))
    if cp is None or ra is None or rp is None or cp <= 0 or ra <= 0 or rp <= 0:
        return None
    own = 1 if name == schema.WCU_LIMIT_NAME else stored_count(item)
    sched = fill_sync.limit_schedule(item, name)
    fi = _number(item, schema.bucket_attr(name, schema.BUCKET_FIELD_FI))
    if fi is not None:
        z = fi
        expressed = _number(item, schema.BUCKET_FIELD_XI) or _number(item, "rf") or at_ms
    else:
        tk = _number(item, schema.bucket_attr(name, schema.BUCKET_FIELD_TK))
        if tk is None:
            return None
        expressed = _number(item, "rf") or at_ms
        z = fill_instant.from_tokens(
            tk, expressed, fill_instant.params_at(expressed, cp, ra, rp, sched, own)
        )
    if sched and at_ms > expressed:
        z, _last = fill_instant.re_express_across(z, expressed, at_ms, cp, ra, rp, sched, own)
    params = fill_instant.params_at(at_ms, cp, ra, rp, sched, own)
    if own != shards:
        target = fill_instant.params_at(at_ms, cp, ra, rp, sched, shards)
        z, params = fill_instant.re_express(z, at_ms, params, target), target
    return z, params


@dataclass(frozen=True)
class RaisePlan:
    """One raise's transaction, wire format, ready for ``TransactWriteItems``."""

    old_count: int
    new_count: int
    transact_items: list[dict[str, Any]]


def _key(namespace_id: str, entity_id: str, resource: str, shard: int) -> dict[str, Any]:
    return {
        "PK": {"S": schema.pk_bucket(namespace_id, entity_id, resource, shard)},
        "SK": {"S": schema.sk_state()},
    }


def _new_shard(
    parent: dict[str, Any],
    namespace_id: str,
    entity_id: str,
    resource: str,
    shard: int,
    new_count: int,
    expressed_ms: int,
    fills: dict[str, int],
) -> dict[str, Any]:
    """Shard ``shard`` as a copy of ``parent``: its stamps and schedules, ``fills``, no quota."""
    item = {
        attr: value
        for attr, value in parent.items()
        if attr not in (schema.BUCKET_FIELD_RAISE, schema.BUCKET_FIELD_XI)
    }
    item["PK"] = {"S": schema.pk_bucket(namespace_id, entity_id, resource, shard)}
    item["GSI2SK"] = {"S": schema.gsi2_sk_bucket(entity_id, shard)}
    item["GSI3SK"] = {"S": schema.gsi3_sk_bucket(resource, shard)}
    item["GSI4SK"] = {"S": schema.gsi4_sk_bucket(entity_id, resource, shard)}
    item["shard_count"] = _n(new_count)
    item[schema.BUCKET_FIELD_XI] = _n(expressed_ms)
    for name in limit_names(parent):
        if name not in fills:
            # A quota (or a limit nothing can express): its new shard seeds it
            # on its first slow pass, funded as ADR-145 funds a created shard.
            for field in _QUOTA_FIELDS:
                item.pop(schema.bucket_attr(name, field), None)
    # The partition's own budget, at its own parameters: the fill instant
    # below is expressed against them, whatever the parent stores.
    for field, value in (
        (schema.BUCKET_FIELD_CP, _WCU_PARAMS.capacity_milli),
        (schema.BUCKET_FIELD_RA, _WCU_PARAMS.refill_amount_milli),
        (schema.BUCKET_FIELD_RP, _WCU_PARAMS.refill_period_ms),
    ):
        item[schema.bucket_attr(schema.WCU_LIMIT_NAME, field)] = _n(value)
    for name, z in fills.items():
        item.pop(schema.bucket_attr(name, schema.BUCKET_FIELD_TK), None)
        item[schema.bucket_attr(name, schema.BUCKET_FIELD_FI)] = _n(z)
        # The new shard's usage counter starts at 0: the aggregator counts a
        # MODIFY's delta from it, and the parent keeps everything it counted.
        item[schema.bucket_attr(name, schema.BUCKET_FIELD_TC)] = _n(0)
    return item


def plan_raise(
    table_name: str,
    namespace_id: str,
    entity_id: str,
    resource: str,
    images: dict[int, dict[str, Any] | None],
    old_count: int,
    now_ms: int,
    deadline_ms: int,
) -> RaisePlan | None:
    """The transaction raising a bucket from ``old_count`` to ``2 × old_count`` shards.

    ``images`` holds every slot below ``old_count``: the ``ALL_NEW`` image of
    this raise's marker write (which it pins on, ``rx = deadline_ms``), or
    None for a slot with no item. Returns None, writing nothing, when no shard
    exists, a shard already stores a larger count, the shards disagree on
    their owner (a move's fan-out in progress, ADR-150), or no shard holds a
    dripping user limit.

    Each existing shard ``i`` is one ``Update``: every dripping limit
    re-expressed at the instant it is split at and split (``fill_instant.
    split``), a ``tk`` converted, ``shard_count`` raised, ``xi`` stamped, the
    marker removed, each legacy quota's grant size frozen; pinned on the
    marker, each fill instant (or ``tk``) and ``cp`` / ``ra`` / ``rp`` /
    ``b_{n}_sched`` / ``b_{n}_soft`` it read, ``xi``, ``shard_count`` and the
    item stamps in ``_PINNED``. Shard ``i + S`` is a ``Put`` copying it with
    each dripping limit's moved balance. A missing slot below ``S`` and its
    split are created empty from the lowest existing shard. Every new shard's
    ``wcu`` starts full. One ``ConditionCheck`` pins the owner's META on the
    shards' ``pgen`` / ``pinc`` (``schema.owner_meta_condition``). Positional
    tokens (#634): ``#rx`` / ``:rxd``, ``#sc`` / ``:sc`` / ``:scr``, ``#xi`` /
    ``:xi`` / ``:xir``, ``#u{k}`` / ``:u{k}``, ``#l{f}{k}`` / ``:l{f}{k}`` per
    dripping limit, ``#lg{m}`` / ``:lg{m}`` per frozen quota.
    """
    existing = {shard: item for shard, item in images.items() if item}
    if not existing or any(stored_count(item) > old_count for item in existing.values()):
        return None
    owners = {
        (
            item.get(schema.BUCKET_FIELD_PGEN, {}).get("N"),
            item.get(schema.BUCKET_FIELD_PINC, {}).get("S"),
        )
        for item in existing.values()
    }
    if len(owners) != 1 or not any(holds_dripping(item) for item in existing.values()):
        return None
    new_count = old_count * 2
    template = existing[min(existing)]
    items: list[dict[str, Any]] = []
    puts: list[dict[str, Any]] = []

    def put(item: dict[str, Any]) -> None:
        puts.append(
            {
                "Put": {
                    "TableName": table_name,
                    "Item": item,
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            }
        )

    def empty_fills(at_ms: int) -> dict[str, int]:
        fills: dict[str, int] = {}
        for name in limit_names(template):
            if name == schema.WCU_LIMIT_NAME:
                fills[name] = fill_instant.created_full(at_ms, _WCU_PARAMS)
                continue
            if not _drips(template, name):
                continue
            filled = _fill_at(template, name, at_ms, new_count)
            if filled is not None:
                fills[name] = fill_instant.created_empty(at_ms, filled[1])
        return fills

    for shard in range(old_count):
        item = existing.get(shard)
        if item is None:
            at_ms = max(now_ms, _expressed_at(template))
            fills = empty_fills(at_ms)
            for slot in (shard, shard + old_count):
                put(
                    _new_shard(
                        template, namespace_id, entity_id, resource, slot, new_count, at_ms, fills
                    )
                )
            continue
        at_ms = max(now_ms, _expressed_at(item))
        names = {
            "#rx": schema.BUCKET_FIELD_RAISE,
            "#sc": "shard_count",
            "#xi": schema.BUCKET_FIELD_XI,
        }
        values: dict[str, Any] = {":rxd": _n(deadline_ms), ":sc": _n(new_count), ":xi": _n(at_ms)}
        sets = ["#sc = :sc", "#xi = :xi"]
        removes = ["#rx"]
        conditions = ["attribute_exists(PK)", "#rx = :rxd"]
        if "shard_count" in item:
            values[":scr"] = item["shard_count"]
            conditions.append("#sc = :scr")
        else:
            conditions.append("attribute_not_exists(#sc)")
        if schema.BUCKET_FIELD_XI in item:
            values[":xir"] = item[schema.BUCKET_FIELD_XI]
            conditions.append("#xi = :xir")
        else:
            conditions.append("attribute_not_exists(#xi)")
        for k, attr in enumerate(_PINNED):
            names[f"#u{k}"] = attr
            if attr in item:
                values[f":u{k}"] = item[attr]
                conditions.append(f"#u{k} = :u{k}")
            else:
                conditions.append(f"attribute_not_exists(#u{k})")
        moved: dict[str, int] = {}
        for k, name in enumerate(n for n in limit_names(item) if _drips(item, n)):
            is_wcu = name == schema.WCU_LIMIT_NAME
            filled = _fill_at(item, name, at_ms, 1 if is_wcu else old_count)
            if filled is None:
                continue
            z, params = filled
            fi_attr = schema.bucket_attr(name, schema.BUCKET_FIELD_FI)
            names[f"#lf{k}"] = fi_attr
            if is_wcu:
                # The partition's own budget: never split. Each new shard's
                # starts full; this shard's is only converted from `tk`.
                moved[name] = fill_instant.created_full(at_ms, _WCU_PARAMS)
                kept = z
            else:
                _new, kept, moved[name] = fill_instant.split(z, at_ms, params)
            stored_fi = item.get(fi_attr)
            if stored_fi is not None:
                values[f":lb{k}"] = stored_fi
                conditions.append(f"#lf{k} = :lb{k}")
            else:
                tk_attr = schema.bucket_attr(name, schema.BUCKET_FIELD_TK)
                names[f"#lt{k}"] = tk_attr
                values[f":lt{k}"] = item[tk_attr]
                conditions.append(f"attribute_not_exists(#lf{k}) AND #lt{k} = :lt{k}")
                removes.append(f"#lt{k}")
            if not is_wcu or stored_fi is None:
                values[f":lf{k}"] = _n(kept)
                sets.append(f"#lf{k} = :lf{k}")
            for token, field in (
                ("lc", schema.BUCKET_FIELD_CP),
                ("la", schema.BUCKET_FIELD_RA),
                ("lr", schema.BUCKET_FIELD_RP),
                ("lh", schema.BUCKET_FIELD_SCHED),
                ("ls", schema.BUCKET_FIELD_SOFT),
            ):
                attr = schema.bucket_attr(name, field)
                names[f"#{token}{k}"] = attr
                if attr in item:
                    values[f":{token}{k}"] = item[attr]
                    conditions.append(f"#{token}{k} = :{token}{k}")
                else:
                    conditions.append(f"attribute_not_exists(#{token}{k})")
        for m, (name, grant_count) in enumerate(legacy_quota_sizes(item, set(), now_ms)):
            # ADR-145 R5/R6: raising a legacy quota's count would shrink the
            # grant it is read at, and a slot it still holds tokens for would be
            # granted again. Its current grant keeps the size it was read at.
            names[f"#lg{m}"] = schema.bucket_attr(name, schema.BUCKET_FIELD_GC)
            values[f":lg{m}"] = _n(grant_count)
            sets.append(f"#lg{m} = if_not_exists(#lg{m}, :lg{m})")
        items.append(
            {
                "Update": {
                    "TableName": table_name,
                    "Key": _key(namespace_id, entity_id, resource, shard),
                    "UpdateExpression": f"SET {', '.join(sets)} REMOVE {', '.join(removes)}",
                    "ConditionExpression": " AND ".join(conditions),
                    "ExpressionAttributeNames": names,
                    "ExpressionAttributeValues": values,
                }
            }
        )
        put(
            _new_shard(
                item, namespace_id, entity_id, resource, shard + old_count, new_count, at_ms, moved
            )
        )

    pgen_raw, pinc = owners.pop()
    pgen = int(pgen_raw) if pgen_raw is not None else None
    expression, owner_names, owner_values = schema.owner_meta_condition(
        exists=True if pgen is not None or pinc is not None else None, pgen=pgen, pinc=pinc
    )
    check: dict[str, Any] = {
        "TableName": table_name,
        "Key": {
            "PK": {"S": schema.pk_entity(namespace_id, entity_id)},
            "SK": {"S": schema.sk_meta()},
        },
        "ConditionExpression": expression,
    }
    if owner_names:
        check["ExpressionAttributeNames"] = owner_names
    if owner_values:
        check["ExpressionAttributeValues"] = {
            token: _n(value) if isinstance(value, int) else {"S": value}
            for token, value in owner_values.items()
        }
    return RaisePlan(old_count, new_count, items + puts + [{"ConditionCheck": check}])
