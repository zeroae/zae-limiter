"""`acquire()` honours `on_unavailable` for an unreadable stored schedule (#222 §6).

No new code in `limiter.py` makes this work, and that is the finding rather
than a gap: `acquire()`'s `except Exception` handler already yields a degraded
lease under ALLOW and re-wraps under BLOCK, and `RateLimiterUnavailable` is
deliberately **not** in its `(RateLimitExceeded, ValidationError,
ResourceDisabled, Warning)` re-raise tuple. Only the exception type at the
Repository boundary was missing. These tests pin that it stays that way — a
future addition of `RateLimiterUnavailable` to that tuple, or a decision to
treat an undecodable schedule as "no schedule", both break here.

Lives in its own module rather than in `tests/unit/test_limiter.py` because it
crosses the repository/limiter seam and neither owner should have to merge it.
"""

import pytest

from zae_limiter import Limit, OnUnavailable, RateLimiter, RateLimiterUnavailable
from zae_limiter.schedule import ScheduleEntry
from zae_limiter.schema import (
    BUCKET_FIELD_SCHED,
    limit_attr,
    pk_bucket,
    pk_entity,
    pk_system,
    sk_config,
    sk_state,
)

BUSINESS = (ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),)


async def _write_attr(repo, key, attr, value):
    client = await repo._get_client()
    await client.update_item(
        TableName=repo.table_name,
        Key=key,
        UpdateExpression="SET #a = :v",
        ExpressionAttributeNames={"#a": attr},
        ExpressionAttributeValues={":v": {"S": value}},
    )


async def _corrupt_entity_config(repo, entity_id, resource, limit_name, value="not-a-schedule"):
    """Leave an undecodable schedule on an entity config item.

    No public API can produce one — `set_limits` encodes from a validated
    `Limit` — so the attribute is written directly, exactly as a newer client
    or a corrupted write would leave it.
    """
    await _write_attr(
        repo,
        {
            "PK": {"S": pk_entity(repo._namespace_id, entity_id)},
            "SK": {"S": sk_config(resource)},
        },
        limit_attr(limit_name, "sched"),
        value,
    )
    await repo.invalidate_config_cache()


class TestUnreadableScheduleHonoursOnUnavailable:
    """The operator already chose what happens when the limiter cannot decide."""

    async def _corrupt(self, limiter, entity_id):
        repo = limiter._repository
        await repo.create_entity(entity_id)
        await repo.set_limits(
            entity_id,
            [Limit.per_minute("rpm", 1000).with_schedule(BUSINESS)],
            resource="gpt-4",
        )
        await _corrupt_entity_config(repo, entity_id, "gpt-4", "rpm")
        return repo

    async def test_block_raises_rate_limiter_unavailable(self, limiter):
        repo = await self._corrupt(limiter, "ou-1")
        slow = RateLimiter(repository=repo, speculative_writes=False)

        with pytest.raises(RateLimiterUnavailable):
            async with slow.acquire(
                "ou-1", "gpt-4", consume={"rpm": 1}, on_unavailable=OnUnavailable.BLOCK
            ):
                pass

    async def test_block_does_not_leak_a_value_error(self, limiter):
        """Discriminates against "it raises something": a bare `ValueError` out
        of `acquire()` is not a documented outcome and no caller catches it."""
        repo = await self._corrupt(limiter, "ou-1b")
        slow = RateLimiter(repository=repo, speculative_writes=False)

        with pytest.raises(RateLimiterUnavailable) as excinfo:
            async with slow.acquire(
                "ou-1b", "gpt-4", consume={"rpm": 1}, on_unavailable=OnUnavailable.BLOCK
            ):
                pass
        assert not isinstance(excinfo.value, ValueError)
        assert isinstance(excinfo.value.cause, RateLimiterUnavailable)

    async def test_allow_degrades(self, limiter):
        repo = await self._corrupt(limiter, "ou-2")
        slow = RateLimiter(repository=repo, speculative_writes=False)

        async with slow.acquire(
            "ou-2", "gpt-4", consume={"rpm": 1}, on_unavailable=OnUnavailable.ALLOW
        ) as lease:
            assert lease.degraded is True

    async def test_a_degraded_lease_is_not_inferred_from_empty_entries(self, limiter):
        """CLAUDE.md invariant 2: never infer degradation from `entries == []`.
        Pinned here because this is the second producer of such a lease."""
        repo = await self._corrupt(limiter, "ou-3")
        slow = RateLimiter(repository=repo, speculative_writes=False)

        async with slow.acquire(
            "ou-3", "gpt-4", consume={"rpm": 1}, on_unavailable=OnUnavailable.ALLOW
        ) as lease:
            await lease.adjust(rpm=5)  # must not raise the declared-scope error

    async def test_the_fast_path_honours_it_too(self, limiter):
        """A corrupt schedule on the *bucket* item is reached without reading
        any config, through the speculative failure image. It must land in the
        same handler — otherwise the default configuration (speculative writes
        on) is the one case §6 does not cover."""
        repo = limiter._repository
        await repo.create_entity("ou-5")
        await repo.set_limits(
            "ou-5", [Limit.per_minute("rpm", 1000).with_schedule(BUSINESS)], resource="gpt-4"
        )
        async with limiter.acquire("ou-5", "gpt-4", consume={"rpm": 1}):
            pass
        await _write_attr(
            repo,
            {
                "PK": {"S": pk_bucket(repo._namespace_id, "ou-5", "gpt-4", 0)},
                "SK": {"S": sk_state()},
            },
            BUCKET_FIELD_SCHED,
            "not-a-schedule",
        )

        with pytest.raises(RateLimiterUnavailable):
            async with limiter.acquire(
                "ou-5", "gpt-4", consume={"rpm": 5_000}, on_unavailable=OnUnavailable.BLOCK
            ):
                pass

    async def test_a_readable_schedule_is_unaffected(self, limiter):
        """Discriminates every test above against "always degrade when
        scheduled"."""
        repo = limiter._repository
        await repo.create_entity("ou-4")
        await repo.set_limits(
            "ou-4", [Limit.per_minute("rpm", 1000).with_schedule(BUSINESS)], resource="gpt-4"
        )
        slow = RateLimiter(repository=repo, speculative_writes=False)

        async with slow.acquire("ou-4", "gpt-4", consume={"rpm": 1}) as lease:
            assert lease.degraded is False


class TestSystemLevelCorruptionDowngradesTheMode:
    """Known limitation, documented in §6/§9 rather than fixed here.

    `acquire()` resolves the mode *before* its try block, and
    `Repository.resolve_on_unavailable()` swallows every exception and falls
    back to its cached value or `"block"`. So a corrupt schedule on the
    **system config item specifically** makes the mode itself unresolvable, and
    an operator who configured `allow` gets `block` unless the value was
    already cached from an earlier successful read.

    Fixing it means teaching `resolve_on_unavailable` to tell "cannot reach
    DynamoDB" from "read a config item I cannot parse", which is wider than §6
    needs. These tests pin the blast radius so a later change cannot widen it
    silently: entity- and resource-level corruption must stay unaffected.
    """

    async def test_system_level_corruption_forces_block(self, limiter):
        repo = limiter._repository
        await repo.set_system_defaults(
            [Limit.per_minute("rpm", 1000).with_schedule(BUSINESS)], on_unavailable="allow"
        )
        await repo.create_entity("sys-1")
        await _write_attr(
            repo,
            {"PK": {"S": pk_system(repo._namespace_id)}, "SK": {"S": sk_config()}},
            limit_attr("rpm", "sched"),
            "not-a-schedule",
        )
        await repo.invalidate_config_cache()
        repo._on_unavailable_cache = None  # as a fresh process would start

        slow = RateLimiter(repository=repo, speculative_writes=False)
        with pytest.raises(RateLimiterUnavailable):
            async with slow.acquire("sys-1", "gpt-4", consume={"rpm": 1}):
                pass

    async def test_entity_level_corruption_leaves_allow_intact(self, limiter):
        """The other half of the limitation, and the part that keeps it
        narrow: the mode still resolves, so the operator's `allow` applies."""
        repo = limiter._repository
        await repo.set_system_defaults([Limit.per_minute("rpm", 1000)], on_unavailable="allow")
        await repo.create_entity("sys-2")
        await repo.set_limits(
            "sys-2", [Limit.per_minute("rpm", 500).with_schedule(BUSINESS)], resource="gpt-4"
        )
        await _corrupt_entity_config(repo, "sys-2", "gpt-4", "rpm")
        repo._on_unavailable_cache = None

        slow = RateLimiter(repository=repo, speculative_writes=False)
        async with slow.acquire("sys-2", "gpt-4", consume={"rpm": 1}) as lease:
            assert lease.degraded is True

    async def test_resource_level_corruption_leaves_allow_intact(self, limiter):
        repo = limiter._repository
        await repo.set_system_defaults([Limit.per_minute("rpm", 1000)], on_unavailable="allow")
        await repo.create_entity("sys-3")
        await repo.set_resource_defaults(
            "gpt-4", [Limit.per_minute("rpm", 500).with_schedule(BUSINESS)]
        )
        await _write_attr(
            repo,
            {
                "PK": {"S": f"{repo._namespace_id}/RESOURCE#gpt-4"},
                "SK": {"S": sk_config()},
            },
            limit_attr("rpm", "sched"),
            "not-a-schedule",
        )
        await repo.invalidate_config_cache()
        repo._on_unavailable_cache = None

        slow = RateLimiter(repository=repo, speculative_writes=False)
        async with slow.acquire("sys-3", "gpt-4", consume={"rpm": 1}) as lease:
            assert lease.degraded is True
