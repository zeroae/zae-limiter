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

from zae_limiter import OnUnavailable, RateLimiter, RateLimitExceeded, Repository, schema
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
class TestDeclaredLimitsGateAdmission:
    """Only declared limits gate admission and appear in RateLimitExceeded.

    The fast path's conditional UpdateItem covers only the limits in
    `consume`, so an undeclared limit in debt never rejects there. The slow
    path used to run `try_consume(state, 0, now)` for every resolved limit,
    and a bucket in debt fails that check even for a request of 0 — so an
    rpm-only acquire was REJECTED on the slow path but ADMITTED on the fast
    path. Both paths must agree: undeclared limits are refilled (they are
    still written) but never checked and never reported.
    """

    async def _rpm_and_tpm(self, repo, rpm_capacity=1000):
        await repo.set_system_defaults(
            [Limit.custom("rpm", rpm_capacity, **SLOW), Limit.custom("tpm", 1000, **SLOW)]
        )
        await repo.create_entity("e1", parent_id=None, name="e1")

    async def test_undeclared_limit_in_debt_does_not_reject(self, repo, speculative):
        await self._rpm_and_tpm(repo)

        limiter = RateLimiter(repository=repo, speculative_writes=speculative)
        async with limiter:
            # Drive tpm into debt through a declared zero-estimate adjust.
            async with limiter.acquire("e1", "api", {"rpm": 1, "tpm": 0}) as lease:
                await lease.adjust(tpm=2000)
            # rpm-only acquires must be admitted on both paths.
            for _ in range(2):
                async with limiter.acquire("e1", "api", {"rpm": 1}):
                    pass

        tokens = await _tokens(repo, "e1", "api")
        assert tokens["tpm"] == -1_000_000, "tpm is in debt and must stay untouched"
        assert tokens["rpm"] == 1_000_000 - 3 * 1_000

    async def test_acquire_rejection_lists_only_declared_limits(self, repo, speculative):
        await self._rpm_and_tpm(repo, rpm_capacity=1)

        limiter = RateLimiter(repository=repo, speculative_writes=speculative)
        async with limiter:
            # Create the bucket without consuming so the second call can take
            # the fast path under speculative=True.
            async with limiter.acquire("e1", "api", {"rpm": 0}):
                pass
            with pytest.raises(RateLimitExceeded) as exc_info:
                async with limiter.acquire("e1", "api", {"rpm": 5}):
                    pass

        exc = exc_info.value
        assert {s.limit_name for s in exc.statuses} == {"rpm"}
        assert {s.limit_name for s in exc.violations} == {"rpm"}
        assert exc.passed == []

    async def test_rejection_includes_declared_zero_estimate_limits(self, repo, speculative):
        """`{"rpm": 5, "tpm": 0}` rejected on rpm: the fast-reject path used
        to skip tpm because its amount was 0, while the slow path reported it
        as passed with requested=0. Declared is declared on both paths."""
        await self._rpm_and_tpm(repo, rpm_capacity=1)

        limiter = RateLimiter(repository=repo, speculative_writes=speculative)
        async with limiter:
            async with limiter.acquire("e1", "api", {"rpm": 0, "tpm": 0}):
                pass  # creates the bucket so the next call can fast-reject
            with pytest.raises(RateLimitExceeded) as exc_info:
                async with limiter.acquire("e1", "api", {"rpm": 5, "tpm": 0}):
                    pass

        exc = exc_info.value
        assert {s.limit_name for s in exc.statuses} == {"rpm", "tpm"}
        assert {s.limit_name for s in exc.violations} == {"rpm"}
        (tpm,) = exc.passed
        assert tpm.limit_name == "tpm" and tpm.requested == 0

    async def test_cascade_rejection_lists_only_declared_limits(self, repo, speculative):
        """The cascade fast-reject sites build child statuses from every
        bucket in the speculative result, which carries the reserved `wcu`
        limit and any undeclared limit. Neither may leak."""
        await repo.set_system_defaults(
            [Limit.custom("rpm", 2, **SLOW), Limit.custom("tpm", 1000, **SLOW)]
        )
        await repo.create_entity("parent", parent_id=None, name="parent")
        await repo.create_entity("child", parent_id="parent", name="child", cascade=True)
        # The child gets its own, larger rpm so only the parent drains.
        await repo.set_limits(
            "child",
            [Limit.custom("rpm", 100, **SLOW), Limit.custom("tpm", 1000, **SLOW)],
            resource="api",
        )

        limiter = RateLimiter(repository=repo, speculative_writes=speculative)
        async with limiter:
            for _ in range(2):  # drains the parent's rpm (capacity 2)
                async with limiter.acquire("child", "api", {"rpm": 1}):
                    pass
            with pytest.raises(RateLimitExceeded) as exc_info:
                async with limiter.acquire("child", "api", {"rpm": 1}):
                    pass

        exc = exc_info.value
        assert {s.limit_name for s in exc.statuses} == {"rpm"}
        assert {(s.entity_id, s.limit_name) for s in exc.violations} == {("parent", "rpm")}

    async def test_empty_consume_declares_nothing(self, repo, speculative):
        """On main the slow path built declared entries for every resolved
        limit, so `consume={}` then `adjust(tpm=...)` worked on the first
        call only. An empty `consume` declares nothing on either path."""
        await self._rpm_and_tpm(repo)

        limiter = RateLimiter(repository=repo, speculative_writes=speculative)
        async with limiter:
            for _ in range(2):
                async with limiter.acquire("e1", "api", {}) as lease:
                    assert lease.consumed == {}
                    with pytest.warns(FutureWarning) as record:
                        await lease.adjust(tpm=1200)
                    assert "'tpm'" in str(record[0].message)
                    assert "declared limits on this lease: []" in str(record[0].message)

        tokens = await _tokens(repo, "e1", "api")
        assert tokens == {"rpm": 1_000_000, "tpm": 1_000_000}, "nothing was consumed"


@pytest.mark.parametrize("speculative", [True, False])
class TestUnknownLimitInConsume:
    """A key in `consume` that names no configured limit is silently dropped
    at admission (`consume.get(limit.name, 0)`), after which every
    `adjust()` on it warns "not declared in consume" — pointing at the wrong
    fix, since the caller did declare it. Close it where the declaration is
    made: `acquire()` warns that the key is not configured for the resource.

    The fast path cannot see this (an unknown key makes `speculative_consume`
    fail and fall back), so the slow-path check covers both paths.
    """

    async def test_unknown_key_warns_at_acquire_and_gets_no_entry(self, repo, speculative):
        await repo.set_system_defaults([Limit.custom("rpm", 1000, **SLOW)])
        await repo.create_entity("e1", parent_id=None, name="e1")

        limiter = RateLimiter(repository=repo, speculative_writes=speculative)
        async with limiter:
            for _ in range(2):
                with pytest.warns(FutureWarning) as record:
                    async with limiter.acquire("e1", "api", {"rpm": 1, "tpm": 500}) as lease:
                        assert {e.limit.name for e in lease.entries} == {"rpm"}
                        assert lease.consumed == {"rpm": 1}

                acquire_warnings = [w for w in record if "acquire()" in str(w.message)]
                assert len(acquire_warnings) == 1
                message = str(acquire_warnings[0].message)
                assert "'tpm'" in message and "not configured" in message
                assert "'rpm'" in message
                assert "ValidationError" in message and "v1.0.0" in message
                # Entity and resource are logged, never embedded in the text:
                # a per-entity message makes __warningregistry__ grow per
                # entity and defeats the default "once per location" filter.
                assert "e1" not in message and "api" not in message
                # stacklevel must point at the acquire() caller, not the library
                assert acquire_warnings[0].filename == __file__

        assert (await _tokens(repo, "e1", "api"))["rpm"] == 1_000_000 - 2 * 1_000

    @pytest.mark.parametrize("mode", [OnUnavailable.BLOCK, OnUnavailable.ALLOW])
    async def test_warning_as_error_propagates_instead_of_degrading(self, repo, speculative, mode):
        """The unknown-key check runs inside acquire()'s outage handler. Under
        warnings-as-errors the raised FutureWarning must propagate as itself —
        not be classified as a backend outage and turned into
        RateLimiterUnavailable (BLOCK) or a silent degraded lease (ALLOW)."""
        await repo.set_system_defaults([Limit.custom("rpm", 1000, **SLOW)])
        await repo.create_entity("e1", parent_id=None, name="e1")

        limiter = RateLimiter(repository=repo, speculative_writes=speculative)
        async with limiter:
            with warnings.catch_warnings():
                warnings.simplefilter("error", FutureWarning)
                with pytest.raises(FutureWarning):
                    async with limiter.acquire(
                        "e1", "api", {"rpm": 1, "tpmm": 5}, on_unavailable=mode
                    ):
                        pytest.fail("acquire() must not yield a lease")

    async def test_child_with_subset_of_parent_limits_does_not_warn(self, repo, speculative):
        """Entity config replaces rather than merges: a child pinned to
        `[rpm]` cascading to a parent on `[rpm, tpm]` still has `tpm` gated
        and consumed on the parent. "Org-level tpm budget on the parent,
        per-user rpm on the child" is a legitimate configuration, so a key
        known to either side of the cascade must not warn."""
        await repo.set_system_defaults(
            [Limit.custom("rpm", 1000, **SLOW), Limit.custom("tpm", 1000, **SLOW)]
        )
        await repo.create_entity("parent", parent_id=None, name="parent")
        await repo.create_entity("child", parent_id="parent", name="child", cascade=True)
        await repo.set_limits("child", [Limit.custom("rpm", 1000, **SLOW)], resource="api")

        limiter = RateLimiter(repository=repo, speculative_writes=speculative)
        async with limiter:
            with warnings.catch_warnings():
                warnings.simplefilter("error", FutureWarning)
                for _ in range(2):
                    async with limiter.acquire("child", "api", {"rpm": 1, "tpm": 100}) as lease:
                        assert lease.consumed == {"rpm": 2, "tpm": 100}

        assert "tpm" not in await _tokens(repo, "child", "api")
        assert (await _tokens(repo, "parent", "api"))["tpm"] == 1_000_000 - 2 * 100_000

    async def _parent_rpm_only_child_rpm_tpm(self, repo):
        """Parent tracks rpm only; the cascading child tracks rpm + tpm."""
        await repo.set_system_defaults(
            [Limit.custom("rpm", 1000, **SLOW), Limit.custom("tpm", 1000, **SLOW)]
        )
        await repo.create_entity("parent", parent_id=None, name="parent")
        await repo.create_entity("child", parent_id="parent", name="child", cascade=True)
        await repo.set_limits("parent", [Limit.custom("rpm", 1000, **SLOW)], resource="api")

    async def test_parent_with_subset_of_child_limits_does_not_warn(self, repo, speculative):
        """The declaration is about the entity being acquired on. A cascade
        parent tracking a subset of the child's limits is a legitimate
        configuration: keys with no parent limit are silently not applied to
        the parent, and nothing warns."""
        await self._parent_rpm_only_child_rpm_tpm(repo)

        limiter = RateLimiter(repository=repo, speculative_writes=speculative)
        async with limiter:
            with warnings.catch_warnings():
                warnings.simplefilter("error", FutureWarning)
                # Cold cache -> slow path; the retry -> fast path, which falls
                # back to the slow path because the parent bucket has no tpm.
                for _ in range(2):
                    async with limiter.acquire("child", "api", {"rpm": 1, "tpm": 100}) as lease:
                        assert lease.consumed == {"rpm": 2, "tpm": 100}

        assert (await _tokens(repo, "child", "api"))["tpm"] == 1_000_000 - 2 * 100_000
        parent_tokens = await _tokens(repo, "parent", "api")
        assert "tpm" not in parent_tokens
        assert parent_tokens["rpm"] == 1_000_000 - 2 * 1_000

    async def test_parent_only_path_does_not_warn_on_parent_subset(
        self, repo, speculative, monkeypatch
    ):
        """Same configuration, forced through the parent-only slow path: the
        child's speculative write succeeds and the parent's fails with buckets
        that cover every consumed name (a stale tpm bucket) and refill would
        help. The parent's resolved limits are rpm only; no warning."""
        import time

        from zae_limiter.models import BucketState
        from zae_limiter.repository_protocol import SpeculativeResult

        await self._parent_rpm_only_child_rpm_tpm(repo)

        limiter = RateLimiter(repository=repo, speculative_writes=speculative)
        async with limiter:
            # Prime both buckets on the slow path.
            async with limiter.acquire("child", "api", {"rpm": 1, "tpm": 100}):
                pass
            limiter._speculative_writes = True

            now_ms = int(time.time() * 1000)

            def bucket(entity_id, name, tokens_milli, last_refill_ms):
                return BucketState(
                    entity_id=entity_id,
                    resource="api",
                    limit_name=name,
                    tokens_milli=tokens_milli,
                    last_refill_ms=last_refill_ms,
                    capacity_milli=1_000_000,
                    refill_amount_milli=1_000_000,
                    refill_period_ms=60_000,
                )

            original = repo.speculative_consume
            calls = 0

            async def mock_speculative(entity_id, resource, consume, ttl_seconds=None, **kw):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return SpeculativeResult(
                        success=True,
                        buckets=[
                            bucket("child", "rpm", 900_000, now_ms),
                            bucket("child", "tpm", 800_000, now_ms),
                        ],
                        cascade=True,
                        parent_id="parent",
                    )
                if calls == 2:
                    # Parent exhausted on rpm, refill would help; carries a
                    # stale tpm bucket so every consumed name is covered.
                    return SpeculativeResult(
                        success=False,
                        old_buckets=[
                            bucket("parent", "rpm", 0, now_ms - 30_000),
                            bucket("parent", "tpm", 0, now_ms - 30_000),
                        ],
                    )
                return await original(entity_id, resource, consume, ttl_seconds, **kw)

            monkeypatch.setattr(repo, "speculative_consume", mock_speculative)

            with warnings.catch_warnings():
                warnings.simplefilter("error", FutureWarning)
                async with limiter.acquire("child", "api", {"rpm": 1, "tpm": 100}) as lease:
                    parent_entries = [e for e in lease.entries if e.entity_id == "parent"]
                    assert {e.limit.name for e in parent_entries} == {"rpm"}
            assert calls == 2, "the parent-only slow path must have been taken"

    async def test_typo_key_with_parent_subset_still_warns_once(self, repo, speculative):
        await self._parent_rpm_only_child_rpm_tpm(repo)

        limiter = RateLimiter(repository=repo, speculative_writes=speculative)
        async with limiter:
            for _ in range(2):
                with pytest.warns(FutureWarning) as record:
                    async with limiter.acquire("child", "api", {"rpm": 1, "tpmm": 5}):
                        pass
                acquire_warnings = [w for w in record if "acquire()" in str(w.message)]
                assert len(acquire_warnings) == 1
                assert "'tpmm'" in str(acquire_warnings[0].message)
                assert "child" not in str(acquire_warnings[0].message)
                assert acquire_warnings[0].filename == __file__

    async def test_acquire_time_unknown_key_is_not_reported_again_on_adjust(
        self, repo, speculative
    ):
        """acquire() already reported the key with the right advice. A later
        adjust() on it must not warn a second time with the wrong advice
        ("name the limit in consume" — the caller did)."""
        await repo.set_system_defaults([Limit.custom("rpm", 1000, **SLOW)])
        await repo.create_entity("e1", parent_id=None, name="e1")

        limiter = RateLimiter(repository=repo, speculative_writes=speculative)
        async with limiter:
            for _ in range(2):
                with pytest.warns(FutureWarning) as record:
                    async with limiter.acquire("e1", "api", {"rpm": 1, "tpmm": 5}) as lease:
                        await lease.adjust(tpmm=3)
                        await lease.consume(tpmm=1)
                        await lease.release(tpmm=1)
                future = [w for w in record if issubclass(w.category, FutureWarning)]
                assert len(future) == 1, [str(w.message) for w in future]
                assert "acquire()" in str(future[0].message)

    async def test_message_is_identical_across_entities(self, repo, speculative, caplog):
        """The text must not vary per entity, or every entity adds a
        __warningregistry__ entry and defeats the default once-per-location
        filter (a warning storm). Entity and resource go to the log."""
        import logging

        await repo.set_system_defaults([Limit.custom("rpm", 1000, **SLOW)])
        for eid in ("e1", "e2"):
            await repo.create_entity(eid, parent_id=None, name=eid)

        limiter = RateLimiter(repository=repo, speculative_writes=speculative)
        messages = []
        async with limiter:
            with caplog.at_level(logging.WARNING, logger="zae_limiter.limiter"):
                for eid in ("e1", "e2"):
                    with pytest.warns(FutureWarning) as record:
                        async with limiter.acquire(eid, "api", {"rpm": 1, "tpmm": 5}):
                            pass
                    messages.append(
                        next(str(w.message) for w in record if "acquire()" in str(w.message))
                    )

        assert messages[0] == messages[1]
        logged = [r.getMessage() for r in caplog.records if "tpmm" in r.getMessage()]
        assert any("e1" in m and "api" in m for m in logged)
        assert any("e2" in m and "api" in m for m in logged)

    async def test_override_wording_when_limits_passed(self, repo, speculative):
        """`limits=[...]` replaces stored config for this call, so "not
        configured for this resource" would be false when the key exists in
        stored config. Say what was actually checked: the override."""
        await repo.set_system_defaults(
            [Limit.custom("rpm", 1000, **SLOW), Limit.custom("tpm", 1000, **SLOW)]
        )
        await repo.create_entity("e1", parent_id=None, name="e1")

        limiter = RateLimiter(repository=repo, speculative_writes=speculative)
        async with limiter:
            for _ in range(2):
                with pytest.warns(FutureWarning) as record:
                    async with limiter.acquire(
                        "e1",
                        "api",
                        {"rpm": 1, "tpm": 5},
                        limits=[Limit.custom("rpm", 1000, **SLOW)],
                    ):
                        pass
                message = next(str(w.message) for w in record if "acquire()" in str(w.message))
                assert "'tpm'" in message
                assert "`limits` override passed to acquire()" in message
                assert "not configured" not in message
                assert "override limits: ['rpm']" in message


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
