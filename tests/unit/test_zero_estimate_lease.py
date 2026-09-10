"""A limit estimated at 0 must still be adjustable through the lease.

`lease.adjust()`, `consume()` and `release()` all iterate `Lease.entries`.
The speculative fast path used to build those entries by walking the
returned buckets and skipping any limit whose estimate was zero, so a limit
passed as `{"tpm": 0}` got no entry at all and every later adjustment
against it was a silent no-op — no exception, no warning.

The slow path iterates the resolved *limits* and appends unconditionally,
which is what makes this hard to see: the first `acquire()` for an entity
finds no bucket, falls back to the slow path, and populates the entity
cache. Only the *second* acquire takes the speculative path and drops the
adjustment. Every test here therefore acquires twice.

This matters because "estimate nothing up front, reconcile afterwards" is
the workflow the library exists for.
"""

import pytest

from zae_limiter import RateLimiter, Repository, schema
from zae_limiter.models import Limit

# Refill of 1 token/hour keeps refill negligible over a test, so the stored
# token count is a clean assertion target.
SLOW = dict(refill_amount=1, refill_period_seconds=3600)


@pytest.fixture
async def repo(mock_dynamodb):
    r = Repository(name="test-zero-est", region="us-east-1", _skip_deprecation_warning=True)
    await r.create_table()
    yield r
    await r.close()


async def _tokens(repo, entity_id, resource):
    buckets = await repo.get_buckets(entity_id, resource=resource)
    return {b.limit_name: b.tokens_milli for b in buckets}


@pytest.mark.asyncio
@pytest.mark.parametrize("speculative", [True, False])
class TestZeroEstimateIsStillAdjustable:
    async def test_second_acquire_persists_the_adjustment(self, repo, speculative):
        """The reported bug: round 1 works, round 2 silently drops the adjust."""
        await repo.set_system_defaults([Limit.custom("rpm", 1000, **SLOW)])
        await repo.create_entity("e1", parent_id=None, name="e1")

        limiter = RateLimiter(repository=repo, speculative_writes=speculative)
        async with limiter:
            for _ in range(2):
                async with limiter.acquire("e1", "api", {"rpm": 0}) as lease:
                    await lease.adjust(rpm=10)

        assert (await _tokens(repo, "e1", "api"))["rpm"] == 1_000_000 - 2 * 10_000

    async def test_zero_estimate_limit_appears_in_lease_consumed(self, repo, speculative):
        await repo.set_system_defaults([Limit.custom("rpm", 1000, **SLOW)])
        await repo.create_entity("e1", parent_id=None, name="e1")

        limiter = RateLimiter(repository=repo, speculative_writes=speculative)
        async with limiter:
            async with limiter.acquire("e1", "api", {"rpm": 0}) as lease:
                await lease.adjust(rpm=10)
            async with limiter.acquire("e1", "api", {"rpm": 0}) as lease:
                await lease.adjust(rpm=10)
                second_consumed = dict(lease.consumed)

        assert second_consumed == {"rpm": 10}

    async def test_mixed_estimates_both_track(self, repo, speculative):
        """The insidious variant: one limit meters normally, the other freezes."""
        await repo.set_system_defaults(
            [Limit.custom("rpm", 1000, **SLOW), Limit.custom("tpm", 1000, **SLOW)]
        )
        await repo.create_entity("e1", parent_id=None, name="e1")

        limiter = RateLimiter(repository=repo, speculative_writes=speculative)
        async with limiter:
            for _ in range(2):
                async with limiter.acquire("e1", "api", {"rpm": 1, "tpm": 0}) as lease:
                    await lease.adjust(tpm=10)

        tokens = await _tokens(repo, "e1", "api")
        assert tokens["rpm"] == 1_000_000 - 2 * 1_000
        assert tokens["tpm"] == 1_000_000 - 2 * 10_000

    async def test_release_on_a_zero_estimate_limit_is_persisted(self, repo, speculative):
        """`release()` is `adjust()` negated, so it was dropped the same way."""
        await repo.set_system_defaults([Limit.custom("rpm", 1000, **SLOW)])
        await repo.create_entity("e1", parent_id=None, name="e1")

        limiter = RateLimiter(repository=repo, speculative_writes=speculative)
        async with limiter:
            async with limiter.acquire("e1", "api", {"rpm": 100}) as lease:
                pass
            async with limiter.acquire("e1", "api", {"rpm": 0}) as lease:
                await lease.consume(rpm=50)
                await lease.release(rpm=20)

        assert (await _tokens(repo, "e1", "api"))["rpm"] == 1_000_000 - 100_000 - 30_000


@pytest.mark.asyncio
@pytest.mark.parametrize("speculative", [True, False])
class TestWcuStaysInternal:
    """The `amount == 0` filter was also, incidentally, hiding `wcu`.

    `result.buckets` on the speculative path comes from
    `_deserialize_composite_bucket()`, which includes the reserved `wcu`
    infrastructure limit — the WCU filtering in `get_buckets` is applied
    separately. Widening the filter must not leak `wcu` to callers.
    """

    async def test_wcu_absent_from_lease_consumed(self, repo, speculative):
        await repo.set_system_defaults([Limit.custom("rpm", 1000, **SLOW)])
        await repo.create_entity("e1", parent_id=None, name="e1")

        limiter = RateLimiter(repository=repo, speculative_writes=speculative)
        async with limiter:
            async with limiter.acquire("e1", "api", {"rpm": 1}):
                pass
            async with limiter.acquire("e1", "api", {"rpm": 1}) as lease:
                await lease.adjust(rpm=1)
                assert schema.WCU_LIMIT_NAME not in lease.consumed
                assert all(e.limit.name != schema.WCU_LIMIT_NAME for e in lease.entries)

    async def test_adjusting_wcu_from_the_lease_is_a_no_op(self, repo, speculative):
        await repo.set_system_defaults([Limit.custom("rpm", 1000, **SLOW)])
        await repo.create_entity("e1", parent_id=None, name="e1")

        limiter = RateLimiter(repository=repo, speculative_writes=speculative)
        async with limiter:
            async with limiter.acquire("e1", "api", {"rpm": 1}):
                pass
            async with limiter.acquire("e1", "api", {"rpm": 1}) as lease:
                await lease.adjust(**{schema.WCU_LIMIT_NAME: 500})
                assert lease.consumed.get(schema.WCU_LIMIT_NAME) is None
