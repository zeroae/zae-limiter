"""Reading a limit's balance off a raw bucket item, in whichever form it is stored.

Since #821 every dripping limit (the reserved ``wcu`` included) is stored as a
fill instant ``b_{name}_fi`` beside the item's ``xi`` (ADR-154), so a test can
no longer read its balance off ``b_{name}_tk``. A quota keeps ``tk``. These go
through the repository's own reader, so both forms read exactly as the limiter
reads them. Plain sync code: the generated sync tests use them unchanged.
"""

from typing import Any

from zae_limiter import fill_instant, schema
from zae_limiter.bucket import balance_milli_at
from zae_limiter.models import BucketState


def item_state(repo: Any, item: dict[str, Any], limit_name: str) -> BucketState:
    """``limit_name``'s state on ``item``, as the slow path reads it."""
    for state in repo._deserialize_composite_bucket(item):
        if state.limit_name == limit_name:
            return state
    raise KeyError(f"{limit_name!r} is not on the item")


def balance_milli(repo: Any, item: dict[str, Any], limit_name: str, now_ms: int) -> int:
    """``limit_name``'s balance on ``item`` at ``now_ms``, millitokens, read exactly.

    A fill instant is projected to ``now_ms``; a quota's (or a legacy item's)
    ``tk`` is refilled from ``rf`` by its stored rate, which for a quota is 0.
    """
    return balance_milli_at(item_state(repo, item, limit_name), now_ms)


def stored_fill_instant(item: dict[str, Any], limit_name: str) -> int:
    """The raw ``b_{name}_fi`` on ``item``."""
    return int(item[schema.bucket_attr(limit_name, schema.BUCKET_FIELD_FI)]["N"])


def full_fill_instant(
    t_ms: int, capacity_milli: int, ra_milli: int, rp_ms: int, shards: int = 1
) -> int:
    """The fill instant of a shard holding its whole share at ``t_ms``."""
    return fill_instant.created_full(
        t_ms, fill_instant.ShardParams(capacity_milli, ra_milli, rp_ms, shards)
    )


def empty_fill_instant(
    item: dict[str, Any], limit_name: str, now_ms: int, shard_count: int | None = None
) -> int:
    """The fill instant that leaves ``limit_name`` on ``item`` empty at ``now_ms``.

    From the item's stored ``cp`` / ``ra`` / ``rp`` and ``shard_count`` (or the
    one given, for a write that also changes it); ``wcu`` is never divided.
    """

    def n(field: str) -> int:
        return int(item[schema.bucket_attr(limit_name, field)]["N"])

    shards = shard_count or int(item.get("shard_count", {}).get("N", "1"))
    if limit_name == schema.WCU_LIMIT_NAME:
        shards = 1
    params = fill_instant.ShardParams(
        n(schema.BUCKET_FIELD_CP), n(schema.BUCKET_FIELD_RA), n(schema.BUCKET_FIELD_RP), shards
    )
    return fill_instant.created_empty(now_ms, params)
