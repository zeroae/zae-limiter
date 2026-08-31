"""LocalStack coverage for eager disable fan-out (ADR-125)."""

import time

import pytest

from zae_limiter.exceptions import ResourceDisabled
from zae_limiter.models import Limit

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_fanout_covers_every_shard(localstack_limiter, test_repo):
    """A multi-shard bucket must be disabled on all of its shards.

    In production, a shard N>0 bucket item is only ever created by the
    aggregator Lambda's DynamoDB Streams shard-count propagation
    (``zae_limiter_aggregator.processor.propagate_shard_count``) reacting to
    a ``shard_count`` change on shard 0. The client's own slow path
    (``Repository.build_composite_create`` via ``Lease._commit_initial``)
    always targets shard 0, and the speculative fast path can only UPDATE an
    existing shard item, never create one (``attribute_exists(PK)``
    condition). So there is no way for a plain sequence of ``acquire()``
    calls to land a bucket on shard 1 -- with ``shard_count=2`` a
    speculatively-selected shard 1 always misses and falls back to the slow
    path, which writes shard 0 again. This fixture runs against
    ``shared_minimal_stack`` (no aggregator), so this test seeds shard 1 the
    same way the aggregator would: a real PutItem, built with the same
    ``build_composite_create`` the production write path uses and issued
    through ``transact_write`` against real LocalStack DynamoDB (exercising
    the same GSI2/GSI3/GSI4 propagation the fan-out itself depends on), not
    a fabricated fixture object.
    """
    await test_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 10_000)])
    async with localstack_limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
        pass

    # Force a second shard on the shard-0 item (source of truth).
    new_shard_count = await test_repo.bump_shard_count("user-1", "gpt-4", 1)
    assert new_shard_count == 2

    # Seed shard 1 deterministically, mirroring propagate_shard_count's
    # "Path 2: Pre-create new shards" -- clone shard 0's live bucket state
    # into a fresh item at shard_id=1 via the real bucket-creation builder.
    shard0_states = await test_repo.get_buckets("user-1", resource="gpt-4", shard_id=0)
    assert shard0_states, "shard 0 bucket must exist before seeding shard 1"
    shard1_put = test_repo.build_composite_create(
        entity_id="user-1",
        resource="gpt-4",
        states=shard0_states,
        now_ms=int(time.time() * 1000),
        shard_id=1,
        shard_count=new_shard_count,
    )
    await test_repo.transact_write([shard1_put])

    # Sanity: both shards genuinely hold a bucket before the fan-out runs.
    assert await test_repo.get_buckets("user-1", resource="gpt-4", shard_id=0)
    assert await test_repo.get_buckets("user-1", resource="gpt-4", shard_id=1)

    assert await test_repo.disable_entity("user-1", resource="gpt-4") >= 2

    with pytest.raises(ResourceDisabled):
        async with localstack_limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
            pass


async def test_first_acquire_on_disabled_resource_creates_no_bucket(localstack_limiter, test_repo):
    """The slow-path gate must fire before a bucket is created."""
    await test_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 100)], disabled=True)
    with pytest.raises(ResourceDisabled):
        async with localstack_limiter.acquire("brand-new", "gpt-4", {"rpm": 1}):
            pass

    assert await test_repo.get_buckets("brand-new") == []


async def test_carve_out_survives_resource_disable_across_shards(localstack_limiter, test_repo):
    """An entity-level enable must not be stamped by a resource-level disable."""
    await test_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 1000)])
    await test_repo.set_limits(
        "vip-1", [Limit.per_minute("rpm", 1000)], resource="gpt-4", disabled=False
    )
    for entity in ("user-1", "vip-1"):
        async with localstack_limiter.acquire(entity, "gpt-4", {"rpm": 1}):
            pass

    await test_repo.disable_resource("gpt-4")

    async with localstack_limiter.acquire("vip-1", "gpt-4", {"rpm": 1}):
        pass
    with pytest.raises(ResourceDisabled):
        async with localstack_limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
            pass
