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

from zae_limiter.models import Limit
from zae_limiter.schema import (
    BUCKET_FIELD_CP,
    BUCKET_FIELD_RA,
    BUCKET_FIELD_RP,
    BUCKET_FIELD_TC,
    BUCKET_FIELD_TK,
    bucket_attr,
    calculate_bucket_ttl_seconds,
    calculate_ttl,
)

logger = logging.getLogger(__name__)

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
