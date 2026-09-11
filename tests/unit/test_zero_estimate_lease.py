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

import warnings

import pytest

from zae_limiter import OnUnavailable, RateLimiter, Repository, schema
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

    async def test_adjusting_wcu_from_the_lease_is_reported_and_ignored(self, repo, speculative):
        """`wcu` is never in `consume`, so adjusting it is an undeclared-key
        adjustment like any other (#455): warned about and not applied."""
        await repo.set_system_defaults([Limit.custom("rpm", 1000, **SLOW)])
        await repo.create_entity("e1", parent_id=None, name="e1")

        limiter = RateLimiter(repository=repo, speculative_writes=speculative)
        async with limiter:
            async with limiter.acquire("e1", "api", {"rpm": 1}):
                pass
            async with limiter.acquire("e1", "api", {"rpm": 1}) as lease:
                with pytest.warns(FutureWarning, match=schema.WCU_LIMIT_NAME):
                    await lease.adjust(**{schema.WCU_LIMIT_NAME: 500})
                assert lease.consumed.get(schema.WCU_LIMIT_NAME) is None

        buckets = await repo.get_buckets("e1", resource="api")
        assert all(b.limit_name != schema.WCU_LIMIT_NAME for b in buckets), (
            "get_buckets filters wcu; the adjust above must not have leaked it"
        )


@pytest.mark.parametrize("speculative", [True, False])
class TestUndeclaredLimitIsReported:
    """`consume` is the declared scope of a lease (#455).

    A limit the caller did not name in `acquire(consume=...)` was never
    checked at admission, so adjusting it afterwards would drive a bucket
    negative that never had the chance to reject. Both paths must therefore
    treat such a key the same way: report it and leave the bucket alone.

    Staged rollout: `FutureWarning` now, `ValidationError` at v1.0.0.
    """

    async def _rpm_and_tpm(self, repo):
        await repo.set_system_defaults(
            [Limit.custom("rpm", 1000, **SLOW), Limit.custom("tpm", 1000, **SLOW)]
        )
        await repo.create_entity("e1", parent_id=None, name="e1")

    async def test_adjust_on_undeclared_limit_warns_and_is_not_applied(self, repo, speculative):
        """The reported divergence: the first acquire (slow path) used to
        apply `adjust(tpm=...)` for a `tpm` never in `consume`; later acquires
        (fast path) dropped it silently. Now both warn and neither applies."""
        await self._rpm_and_tpm(repo)

        limiter = RateLimiter(repository=repo, speculative_writes=speculative)
        async with limiter:
            for _ in range(2):
                async with limiter.acquire("e1", "api", {"rpm": 1}) as lease:
                    with pytest.warns(FutureWarning, match=r"'tpm'"):
                        await lease.adjust(tpm=100)

        tokens = await _tokens(repo, "e1", "api")
        assert tokens["rpm"] == 1_000_000 - 2 * 1_000
        assert tokens["tpm"] == 1_000_000, "tpm was never declared; it must be untouched"

    async def test_typo_key_warns_and_names_the_declared_limits(self, repo, speculative):
        """`adjust(tpmm=...)` where `tpm` is the real limit: the warning must
        name the offending key, the declared limits, and the v1.0.0 error."""
        await self._rpm_and_tpm(repo)

        limiter = RateLimiter(repository=repo, speculative_writes=speculative)
        async with limiter:
            for _ in range(2):
                async with limiter.acquire("e1", "api", {"rpm": 1, "tpm": 500}) as lease:
                    with pytest.warns(FutureWarning) as record:
                        await lease.adjust(tpmm=100)

        assert len(record) == 1
        message = str(record[0].message)
        assert "'tpmm'" in message
        assert "'rpm'" in message and "'tpm'" in message
        assert "ValidationError" in message and "v1.0.0" in message
        # stacklevel must point at the caller, not at lease.py
        assert record[0].filename == __file__

        assert (await _tokens(repo, "e1", "api"))["tpm"] == 1_000_000 - 2 * 500_000

    async def test_consume_on_undeclared_limit_warns_and_is_not_applied(self, repo, speculative):
        await self._rpm_and_tpm(repo)

        limiter = RateLimiter(repository=repo, speculative_writes=speculative)
        async with limiter:
            for _ in range(2):
                async with limiter.acquire("e1", "api", {"rpm": 1}) as lease:
                    with pytest.warns(FutureWarning, match=r"consume\(\).*'tpm'"):
                        await lease.consume(tpm=100)
                    assert "tpm" not in lease.consumed

        assert (await _tokens(repo, "e1", "api"))["tpm"] == 1_000_000

    async def test_release_on_undeclared_limit_warns_and_is_not_applied(self, repo, speculative):
        await self._rpm_and_tpm(repo)

        limiter = RateLimiter(repository=repo, speculative_writes=speculative)
        async with limiter:
            for _ in range(2):
                async with limiter.acquire("e1", "api", {"rpm": 1}) as lease:
                    with pytest.warns(FutureWarning, match=r"release\(\).*'tpm'"):
                        await lease.release(tpm=100)

        assert (await _tokens(repo, "e1", "api"))["tpm"] == 1_000_000

    async def test_declared_zero_estimate_never_warns(self, repo, speculative):
        """`{"tpm": 0}` is a real declaration (#453); it must stay warning-free."""
        await self._rpm_and_tpm(repo)

        limiter = RateLimiter(repository=repo, speculative_writes=speculative)
        async with limiter:
            for _ in range(2):
                async with limiter.acquire("e1", "api", {"rpm": 1, "tpm": 0}) as lease:
                    with warnings.catch_warnings():
                        warnings.simplefilter("error", FutureWarning)
                        await lease.adjust(tpm=100)
                        await lease.consume(tpm=1)
                        await lease.release(tpm=1)

        assert (await _tokens(repo, "e1", "api"))["tpm"] == 1_000_000 - 2 * 100_000

    async def test_lease_consumed_reports_only_declared_limits(self, repo, speculative):
        """The slow path used to report `{"rpm": 1, "tpm": 0}`; the fast path
        `{"rpm": 1}`. The declared scope is the same on both."""
        await self._rpm_and_tpm(repo)

        limiter = RateLimiter(repository=repo, speculative_writes=speculative)
        async with limiter:
            for _ in range(2):
                async with limiter.acquire("e1", "api", {"rpm": 1}) as lease:
                    assert lease.consumed == {"rpm": 1}

    async def test_slow_path_still_persists_every_configured_limit(self, repo, speculative):
        """Guard for the design constraint behind #455: the slow path's write
        must still carry every resolved limit, not just the declared ones.
        `build_composite_create` writes only the states it is handed, and
        `build_composite_normal` advances the shared `rf` while crediting
        refill only to the limits it is handed — so narrowing the *write* to
        `consume` would create buckets without `tpm` and lose `tpm` refill on
        every `rpm`-only acquire. Only the lease's *adjustable scope* narrows."""
        await self._rpm_and_tpm(repo)

        limiter = RateLimiter(repository=repo, speculative_writes=speculative)
        async with limiter:
            async with limiter.acquire("e1", "api", {"rpm": 1}):
                pass

        tokens = await _tokens(repo, "e1", "api")
        assert tokens == {"rpm": 1_000_000 - 1_000, "tpm": 1_000_000}


@pytest.mark.parametrize("speculative", [True, False])
class TestDegradedLeaseIsExempt:
    """Under `on_unavailable=ALLOW`, an outage yields a degraded lease with no
    entries. Declared-scope validation must not turn that degradation into a
    warning storm (or, at v1.0.0, a failure): the lease is marked `degraded`
    explicitly rather than inferred from `entries == []`."""

    @staticmethod
    def _simulate_outage(repo, monkeypatch):
        from botocore.exceptions import ClientError

        async def down(*args, **kwargs):
            raise ClientError(
                {"Error": {"Code": "ServiceUnavailable", "Message": "DynamoDB down"}},
                "UpdateItem",
            )

        monkeypatch.setattr(repo, "speculative_consume", down)
        monkeypatch.setattr(repo, "batch_get_entity_and_buckets", down)

    async def test_adjust_consume_release_never_warn_on_degraded_lease(
        self, repo, speculative, monkeypatch
    ):
        await repo.set_system_defaults([Limit.custom("rpm", 1000, **SLOW)])
        self._simulate_outage(repo, monkeypatch)

        limiter = RateLimiter(repository=repo, speculative_writes=speculative)
        async with limiter:
            async with limiter.acquire(
                "e1", "api", {"rpm": 1}, on_unavailable=OnUnavailable.ALLOW
            ) as lease:
                assert lease.degraded is True
                assert lease.entries == []
                with warnings.catch_warnings():
                    warnings.simplefilter("error", FutureWarning)
                    await lease.adjust(rpm=100, tpm=100)
                    await lease.consume(rpm=1)
                    await lease.release(rpm=1)
                assert lease.consumed == {}

    async def test_real_lease_is_not_degraded(self, repo, speculative):
        await repo.set_system_defaults([Limit.custom("rpm", 1000, **SLOW)])
        await repo.create_entity("e1", parent_id=None, name="e1")

        limiter = RateLimiter(repository=repo, speculative_writes=speculative)
        async with limiter:
            for _ in range(2):
                async with limiter.acquire("e1", "api", {"rpm": 1}) as lease:
                    assert lease.degraded is False


class TestShardRetryLeaseIsUsable:
    """A lease from the shard-retry path must behave like any other lease.

    `_build_lease_from_speculative()` returns the lease used when the
    selected shard is exhausted and another shard serves the request. Unlike
    its sibling — which sets `_initial_committed` and seeds each entry's
    `_initial_consumed` — it was born `_committed=True`, and both
    `Lease.adjust()` and `Lease._rollback()` short-circuit on that flag. So
    adjustments raised `LeaseExpiredError` instead of reconciling, and an
    exception in the caller's body skipped compensation entirely, leaving
    tokens the speculative UpdateItem had already consumed unreturned.

    Setting `_initial_committed` alone is not enough: `_initial_consumed`
    must be seeded too, or `_commit_adjustments()` re-writes the initial
    consumption as a delta and double-counts it.
    """

    @staticmethod
    async def _two_shards_first_exhausted(limiter, monkeypatch):
        """Two shards, rpm exhausted on shard 0, selection pinned to shard 0."""
        import random as _random
        import time

        from zae_limiter import repository as _repo_mod
        from zae_limiter.models import BucketState

        repo = limiter._repository
        ns = repo._namespace_id
        limit = Limit.custom("rpm", 10, **SLOW)

        await limiter.create_entity("user-1")
        await limiter.set_system_defaults([limit])

        now_ms = int(time.time() * 1000)
        for shard_id in (0, 1):
            states = [BucketState.from_limit("user-1", "gpt-4", limit, now_ms)]
            await repo.transact_write(
                [
                    repo.build_composite_create(
                        "user-1", "gpt-4", states, now_ms, shard_id=shard_id, shard_count=2
                    )
                ]
            )
        repo._entity_cache[(ns, "user-1")] = (False, None, {"gpt-4": 2})

        # Drain shard 0 so the first speculative write is APP_LIMIT_EXHAUSTED.
        await repo._speculative_consume_single("user-1", "gpt-4", {"rpm": 10}, shard_id=0)

        # Pin selection to the exhausted shard; the retry then has exactly
        # one untried shard to choose, so the whole path is deterministic.
        monkeypatch.setattr(_repo_mod.random, "randrange", lambda _n: 0)
        monkeypatch.setattr(_random, "choice", lambda seq: seq[0])
        limiter._speculative_writes = True
        return repo

    async def test_adjust_after_shard_retry_is_persisted(self, limiter, monkeypatch):
        repo = await self._two_shards_first_exhausted(limiter, monkeypatch)

        async with limiter.acquire("user-1", "gpt-4", {"rpm": 10}) as lease:
            await lease.adjust(rpm=5)

        buckets = await repo.get_buckets("user-1", resource="gpt-4", shard_id=1)
        rpm = next(b for b in buckets if b.limit_name == "rpm")
        assert rpm.tokens_milli == 10_000 - 10_000 - 5_000

    async def test_shard_retry_lease_rolls_back_on_exception(self, limiter, monkeypatch):
        repo = await self._two_shards_first_exhausted(limiter, monkeypatch)

        with pytest.raises(RuntimeError):
            async with limiter.acquire("user-1", "gpt-4", {"rpm": 10}):
                raise RuntimeError("caller blew up")

        buckets = await repo.get_buckets("user-1", resource="gpt-4", shard_id=1)
        rpm = next(b for b in buckets if b.limit_name == "rpm")
        assert rpm.tokens_milli == 10_000, (
            "the speculative UpdateItem consumed these tokens; rollback skipped "
            "compensation because the lease was born _committed=True"
        )
