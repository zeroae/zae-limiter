"""LocalStack coverage for Repository.reset_bucket (issue #470)."""

import time

import pytest

from zae_limiter.models import Limit

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_acquire_after_reset_sees_full_capacity(localstack_limiter, test_repo):
    """Usage from before a reset must not carry forward to the next acquire()."""
    limits = [Limit.per_minute("rpm", 100)]

    async with localstack_limiter.acquire("user-1", "gpt-4", {"rpm": 60}, limits=limits):
        pass

    available_before = await localstack_limiter.available("user-1", "gpt-4", limits=limits)
    assert available_before["rpm"] == 40

    deleted = await test_repo.reset_bucket("user-1", "gpt-4")
    assert deleted == 1

    available_after = await localstack_limiter.available("user-1", "gpt-4", limits=limits)
    assert available_after["rpm"] == 100

    # A fresh acquire() must be able to consume the full capacity again, not
    # just the 40 tokens that were left over before the reset.
    async with localstack_limiter.acquire("user-1", "gpt-4", {"rpm": 100}, limits=limits):
        pass


async def test_reset_clears_debt_from_adjust(localstack_limiter, test_repo):
    """A bucket driven negative via lease.adjust() must come back clean."""
    limits = [Limit.per_minute("rpm", 100)]

    async with localstack_limiter.acquire("user-2", "gpt-4", {"rpm": 10}, limits=limits) as lease:
        await lease.adjust(rpm=200)  # post-hoc reconciliation, drives the bucket into debt

    available_before = await localstack_limiter.available("user-2", "gpt-4", limits=limits)
    assert available_before["rpm"] < 0

    assert await test_repo.reset_bucket("user-2", "gpt-4") == 1

    available_after = await localstack_limiter.available("user-2", "gpt-4", limits=limits)
    assert available_after["rpm"] == 100


async def test_reset_is_noop_when_never_acquired(test_repo):
    assert await test_repo.reset_bucket("never-acquired", "gpt-4") == 0


async def test_reset_deletes_every_shard(localstack_limiter, test_repo):
    """Multi-shard buckets must be fully cleared, not just shard 0 (GHSA-76rv)."""
    await test_repo.set_resource_defaults("sharded", [Limit.per_minute("rpm", 10_000)])
    async with localstack_limiter.acquire("user-3", "sharded", {"rpm": 1}):
        pass

    # Force a second shard the same way the aggregator would (see
    # test_disable_fanout.py::test_fanout_covers_every_shard for why a plain
    # sequence of acquire() calls cannot land a bucket on shard 1 here).
    new_shard_count = await test_repo.bump_shard_count("user-3", "sharded", 1)
    assert new_shard_count == 2

    shard0_states = await test_repo.get_buckets("user-3", resource="sharded", shard_id=0)
    assert shard0_states, "shard 0 bucket must exist before seeding shard 1"
    shard1_put = test_repo.build_composite_create(
        entity_id="user-3",
        resource="sharded",
        states=shard0_states,
        now_ms=int(time.time() * 1000),
        shard_id=1,
        shard_count=new_shard_count,
    )
    await test_repo.transact_write([shard1_put])

    assert await test_repo.get_buckets("user-3", resource="sharded", shard_id=0)
    assert await test_repo.get_buckets("user-3", resource="sharded", shard_id=1)

    deleted = await test_repo.reset_bucket("user-3", "sharded")
    assert deleted == 2

    assert await test_repo.get_buckets("user-3", resource="sharded", shard_id=0) == []
    assert await test_repo.get_buckets("user-3", resource="sharded", shard_id=1) == []

    # A fresh acquire() after a full-shard reset must succeed cleanly, even
    # though the entity cache may still remember shard_count=2 from before
    # the reset (reset_bucket does not evict it) and speculatively target a
    # now-deleted shard 1 -- the missing-bucket fallback must still recreate
    # the bucket on shard 0.
    async with localstack_limiter.acquire("user-3", "sharded", {"rpm": 1}):
        pass


async def test_reset_scoped_to_one_resource(localstack_limiter, test_repo):
    """Resetting one resource must not touch a sibling resource's usage."""
    limits = [Limit.per_minute("rpm", 100)]

    async with localstack_limiter.acquire("user-4", "gpt-4", {"rpm": 60}, limits=limits):
        pass
    async with localstack_limiter.acquire("user-4", "claude-3", {"rpm": 60}, limits=limits):
        pass

    assert await test_repo.reset_bucket("user-4", "gpt-4") == 1

    assert (await localstack_limiter.available("user-4", "gpt-4", limits=limits))["rpm"] == 100
    assert (await localstack_limiter.available("user-4", "claude-3", limits=limits))["rpm"] == 40
