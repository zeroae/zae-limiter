"""A shard created mid-window joins the window in progress (ADR-139, #625).

``BucketState.from_limit`` stamps ``ws = now``, which is right for an entity's
first shard and wrong for every later one: shards that each opened their own
window leave the entity with no single ``resets_at_ms``. The create path reads
shard 0's ``ws`` and inherits it while the window is still live.

Not a sync-generation source: these drive the async limiter through the async
helpers in ``tests/fixtures/sharding.py``, and the sync twin of the create path
is generated from the same ``limiter.py``.
"""

from unittest.mock import patch

import pytest

from tests.fixtures.windows import SESSION_10, T0
from zae_limiter import RateLimiterUnavailable
from zae_limiter.schema import (
    BUCKET_FIELD_TK,
    BUCKET_FIELD_WS,
    bucket_attr,
    pk_bucket,
    sk_state,
)

RESOURCE = "gpt-4"


def _key(repo, entity_id, shard):
    return {
        "PK": {"S": pk_bucket(repo._namespace_id, entity_id, RESOURCE, shard)},
        "SK": {"S": sk_state()},
    }


async def _raw(repo, entity_id, shard):
    client = await repo._get_client()
    response = await client.get_item(TableName=repo.table_name, Key=_key(repo, entity_id, shard))
    return response.get("Item") or {}


async def _stored_ws_on(repo, entity_id, limit_name, shard):
    attr = (await _raw(repo, entity_id, shard)).get(bucket_attr(limit_name, BUCKET_FIELD_WS))
    return None if attr is None else int(attr["N"])


async def _stored_tk_on(repo, entity_id, limit_name, shard):
    item = await _raw(repo, entity_id, shard)
    return int(item[bucket_attr(limit_name, BUCKET_FIELD_TK)]["N"])


async def _first_use(limiter, entity_id, now_ms, consume=1):
    """Configure ``SESSION_10`` for ``entity_id`` and anchor its window on shard 0."""
    repo = limiter._repository
    await repo.set_limits(entity_id, [SESSION_10], resource=RESOURCE)
    repo._now_ms = lambda: now_ms
    async with limiter.acquire(entity_id, RESOURCE, consume={"session": consume}):
        pass
    assert await _stored_ws_on(repo, entity_id, "session", 0) == now_ms


class TestGetShardWindowStarts:
    """``Repository.get_shard_window_starts`` — the projected sibling read."""

    async def test_no_limit_names_reads_nothing(self, limiter):
        repo = limiter._repository
        with patch.object(repo, "_get_client") as get_client:
            assert await repo.get_shard_window_starts("user-1", RESOURCE, []) == {}
        get_client.assert_not_called()

    async def test_reads_the_stored_start_of_each_named_window(self, limiter):
        repo = limiter._repository
        await _first_use(limiter, "user-1", T0)
        assert await repo.get_shard_window_starts("user-1", RESOURCE, ["session"]) == {
            "session": T0
        }

    async def test_a_missing_shard_or_limit_is_absent_from_the_result(self, limiter):
        repo = limiter._repository
        await _first_use(limiter, "user-1", T0)
        assert await repo.get_shard_window_starts("nobody", RESOURCE, ["session"]) == {}
        assert await repo.get_shard_window_starts("user-1", RESOURCE, ["session", "other"]) == {
            "session": T0
        }
        assert await repo.get_shard_window_starts("user-1", RESOURCE, ["session"], 1) == {}

    async def test_a_corrupt_stored_start_is_reported_as_unavailable(self, limiter):
        repo = limiter._repository
        await _first_use(limiter, "user-1", T0)
        client = await repo._get_client()
        await client.update_item(
            TableName=repo.table_name,
            Key=_key(repo, "user-1", 0),
            UpdateExpression="SET #ws = :bad",
            ExpressionAttributeNames={"#ws": bucket_attr("session", BUCKET_FIELD_WS)},
            ExpressionAttributeValues={":bad": {"N": "1.5"}},
        )
        with pytest.raises(RateLimiterUnavailable, match="b_session_ws"):
            await repo.get_shard_window_starts("user-1", RESOURCE, ["session"])
