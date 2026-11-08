"""Re-expressing fill instants under an entity-level limit change (ADR-155, #820).

The entity-level parameter sync (``Repository._sync_bucket_params`` and the
provisioner's ``bucket_sync``) rewrites a bucket's ``cp`` / ``ra`` / ``rp`` and
schedules. A fill instant is scaled by the refill amount its parameters put in
force, so overwriting them blind would reinterpret every stored fill instant:
a halved ``ra`` reads a fill instant twice as far ahead, decades into the
future. On a shard storing fill instants the sync therefore writes, in the same
``UpdateItem``, each fill instant re-expressed at the sync's instant from the
parameters it was read under to the ones the sync writes, keeping the balance
or trimming it to the new share, and is pinned on what it read (form 3).

Pure: the item-level half of that, shared by both writers so they cannot drift
(the parity of #549 / #777 / #797). Imports only ``schema``, ``schedule`` and
``fill_instant``, all three vendored into the provisioner's zip.
"""

from __future__ import annotations

import re
from typing import Any

from . import fill_instant, schedule, schema

_SET_PART = re.compile(r"^\s*(#[A-Za-z0-9_]+)\s*=\s*(:[A-Za-z0-9_]+)\s*$")


def apply_update(
    item: dict[str, Any],
    update_expr: str,
    names: dict[str, str],
    values: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """``item`` as a parameter-sync plan would leave it.

    The plan is ``SET #a = :v, ... [REMOVE #b, ...]`` with every path an alias
    and every value a placeholder, which is the only shape the sync builds; a
    part of any other shape is refused rather than guessed at.
    """
    projected = dict(item)
    body = update_expr.strip()
    removes = ""
    if " REMOVE " in body:
        body, removes = body.split(" REMOVE ", 1)
    if not body.startswith("SET "):
        raise ValueError(f"not a parameter-sync plan: {update_expr!r}")
    for part in body[len("SET ") :].split(","):
        match = _SET_PART.match(part)
        if match is None:
            raise ValueError(f"not a parameter-sync SET: {part!r}")
        projected[names[match.group(1)]] = values[match.group(2)]
    for alias in removes.split(","):
        alias = alias.strip()
        if alias:
            projected.pop(names[alias], None)
    return projected


def _number(item: dict[str, Any], attr: str) -> int | None:
    raw = item.get(attr, {}).get("N")
    return int(raw) if raw is not None else None


def _schedule(item: dict[str, Any], name: str) -> tuple[schedule.ScheduleEntry, ...]:
    """One limit's parameter schedule on ``item``: its override, else the item default."""
    if name == schema.WCU_LIMIT_NAME:
        return ()
    override = item.get(schema.bucket_attr(name, schema.BUCKET_FIELD_SCHED), {}).get("S")
    if override == schema.BUCKET_SCHED_NONE:
        return ()
    compact = override or item.get(schema.BUCKET_FIELD_SCHED, {}).get("S")
    if not compact:
        return ()
    tz = item.get(schema.BUCKET_FIELD_SCHED_TZ, {}).get("S") or "UTC"
    return schedule.decode(compact, tz)


def _expressed_now(
    item: dict[str, Any], name: str, now_ms: int
) -> tuple[int, fill_instant.ShardParams] | None:
    """A fill instant on ``item`` re-expressed at ``now_ms``, and the parameters then in force."""
    fi = _number(item, schema.bucket_attr(name, schema.BUCKET_FIELD_FI))
    cp = _number(item, schema.bucket_attr(name, schema.BUCKET_FIELD_CP))
    ra = _number(item, schema.bucket_attr(name, schema.BUCKET_FIELD_RA))
    rp = _number(item, schema.bucket_attr(name, schema.BUCKET_FIELD_RP))
    xi = _number(item, schema.BUCKET_FIELD_XI)
    if fi is None or cp is None or ra is None or rp is None or xi is None or ra <= 0:
        return None
    shards = 1 if name == schema.WCU_LIMIT_NAME else (_number(item, "shard_count") or 1)
    sched = _schedule(item, name)
    z = fi
    if sched and now_ms > xi:
        z, _last = fill_instant.re_express_across(fi, xi, now_ms, cp, ra, rp, sched, shards)
    return z, fill_instant.params_at(max(now_ms, xi), cp, ra, rp, sched, shards)


def reexpression(
    item: dict[str, Any],
    update_expr: str,
    names: dict[str, str],
    values: dict[str, dict[str, Any]],
    now_ms: int,
) -> tuple[str, dict[str, str], dict[str, dict[str, Any]], str] | None:
    """The plan for one shard, extended to re-express its fill instants (ADR-155).

    Returns ``(update_expr, names, values, condition)``, or None when ``item``
    stores no fill instant (the plan applies as it is). Each fill instant the
    plan does not remove is ``SET`` to its re-expression at ``now_ms`` from
    the parameters read to the ones the plan writes, and ``xi`` to ``now_ms``.
    The condition pins every fill instant, ``cp`` / ``ra`` / ``rp`` it was read
    under and ``xi`` as read: a write landing in between fails it, and the
    caller retries from that failure's image. Positional tokens (#634):
    ``#yf{k}`` / ``:yf{k}`` / ``:yb{k}`` and ``#yc`` / ``#ya`` / ``#yr`` with
    their values, and ``#yxi`` / ``:yxi`` / ``:yxir``.
    """
    xi = _number(item, schema.BUCKET_FIELD_XI)
    if xi is None:
        return None
    after = apply_update(item, update_expr, names, values)
    fill_names = sorted(
        attr[len(schema.BUCKET_ATTR_PREFIX) : -len(schema.BUCKET_FIELD_FI) - 1]
        for attr in item
        if attr.startswith(schema.BUCKET_ATTR_PREFIX)
        and attr.endswith(f"_{schema.BUCKET_FIELD_FI}")
    )
    names = dict(names)
    values = dict(values)
    sets: list[str] = []
    conditions = ["attribute_exists(PK)"]
    k = 0
    for name in fill_names:
        fi_attr = schema.bucket_attr(name, schema.BUCKET_FIELD_FI)
        if fi_attr not in after:
            continue  # removed by the plan: stale, or now a quota
        before = _expressed_now(item, name, now_ms)
        new_cp = _number(after, schema.bucket_attr(name, schema.BUCKET_FIELD_CP))
        new_ra = _number(after, schema.bucket_attr(name, schema.BUCKET_FIELD_RA))
        new_rp = _number(after, schema.bucket_attr(name, schema.BUCKET_FIELD_RP))
        if before is None or new_cp is None or new_ra is None or new_rp is None or new_ra <= 0:
            continue  # nothing to express it against; the next slow pass decides
        z, old = before
        shards = 1 if name == schema.WCU_LIMIT_NAME else (_number(after, "shard_count") or 1)
        new = fill_instant.params_at(now_ms, new_cp, new_ra, new_rp, _schedule(after, name), shards)
        names[f"#yf{k}"] = fi_attr
        values[f":yf{k}"] = {"N": str(fill_instant.re_express(z, now_ms, old, new))}
        values[f":yb{k}"] = {"N": item[fi_attr]["N"]}
        sets.append(f"#yf{k} = :yf{k}")
        conditions.append(f"#yf{k} = :yb{k}")
        for token, field in (
            ("yc", schema.BUCKET_FIELD_CP),
            ("ya", schema.BUCKET_FIELD_RA),
            ("yr", schema.BUCKET_FIELD_RP),
        ):
            attr = schema.bucket_attr(name, field)
            names[f"#{token}{k}"] = attr
            values[f":{token}{k}"] = item[attr]
            conditions.append(f"#{token}{k} = :{token}{k}")
        k += 1
    names["#yxi"] = schema.BUCKET_FIELD_XI
    values[":yxi"] = {"N": str(now_ms)}
    values[":yxir"] = {"N": str(xi)}
    sets.append("#yxi = :yxi")
    conditions.append("#yxi = :yxir")
    body, _, removes = update_expr.partition(" REMOVE ")
    extended = f"{body}, {', '.join(sets)}"
    if removes:
        extended += f" REMOVE {removes}"
    return extended, names, values, " AND ".join(conditions)
