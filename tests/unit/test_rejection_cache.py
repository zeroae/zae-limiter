"""Tests for the client-side rejection cache (ADR-147, #695).

A failed conditional write costs 1 WCU, so a repeat rejection the client can
already predict from the last bucket state it saw must not reach DynamoDB. The
cache may only reject, never admit.
"""

from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest

from zae_limiter import RateLimiter, RateLimitExceeded
from zae_limiter.models import BucketState, Limit
from zae_limiter.rejection_cache import RejectionCache
from zae_limiter.repository import Repository

RPM = Limit.per_minute("rpm", 2)


class _Clock:
    """A controllable monotonic clock for the cache's age cap."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _state(
    tokens: int,
    *,
    limit: Limit = RPM,
    name: str | None = None,
    entity: str = "u",
    resource: str = "r",
) -> BucketState:
    """A bucket image holding `tokens`, refilled just now (no drip pending)."""
    import time

    return BucketState(
        entity_id=entity,
        resource=resource,
        limit_name=name or limit.name,
        tokens_milli=tokens * 1000,
        last_refill_ms=int(time.time() * 1000),
        capacity_milli=limit.capacity * 1000,
        refill_amount_milli=limit.refill_amount * 1000,
        refill_period_ms=limit.refill_period_seconds * 1000,
    )


@pytest.fixture
async def repo(mock_dynamodb):
    repo = Repository(name="test-rejection", region="us-east-1", _skip_deprecation_warning=True)
    await repo.create_table()
    await repo._register_namespace("default")
    yield repo
    await repo.close()


@pytest.fixture
async def limiter(repo):
    await repo.set_limits("u", [RPM], resource="r")
    async with RateLimiter(repository=repo) as limiter:
        yield limiter


@asynccontextmanager
async def _count_update_items(repo: Repository):
    """Count UpdateItem calls the repository's client makes."""
    client = await repo._get_client()
    real = client.update_item
    calls: list[dict] = []

    async def counting(**kwargs):
        calls.append(kwargs)
        return await real(**kwargs)

    with patch.object(client, "update_item", side_effect=counting):
        yield calls


async def _drain(limiter: RateLimiter) -> None:
    """Spend the whole rpm allowance on entity u, resource r."""
    async with limiter.acquire("u", "r", consume={"rpm": 2}):
        pass


class TestRejectionCacheStore:
    """The cache itself: ages, flags and bounds."""

    def test_returns_a_fresh_view(self):
        cache = RejectionCache(ttl_seconds=1.0, max_entries=10, clock=_Clock())
        cache.store(
            "ns",
            "u",
            "r",
            0,
            [_state(0)],
            shard_count=1,
            vu_ms=None,
            ttl_epoch=None,
            disabled=False,
        )
        views = cache.views("ns", "u", "r", now_ms=1_000_000)
        assert list(views) == [0]
        assert views[0][0].tokens_milli == 0

    def test_entry_older_than_the_ttl_is_not_used(self):
        clock = _Clock()
        cache = RejectionCache(ttl_seconds=1.0, max_entries=10, clock=clock)
        cache.store(
            "ns",
            "u",
            "r",
            0,
            [_state(0)],
            shard_count=1,
            vu_ms=None,
            ttl_epoch=None,
            disabled=False,
        )
        clock.now += 1.01
        assert cache.views("ns", "u", "r", now_ms=1_000_000) == {}

    def test_ttl_zero_disables_the_cache(self):
        cache = RejectionCache(ttl_seconds=0, max_entries=10, clock=_Clock())
        cache.store(
            "ns",
            "u",
            "r",
            0,
            [_state(0)],
            shard_count=1,
            vu_ms=None,
            ttl_epoch=None,
            disabled=False,
        )
        assert not cache.enabled
        assert cache.views("ns", "u", "r", now_ms=1_000_000) == {}

    def test_a_passed_vu_is_not_used(self):
        """Past `vu` the parameters may have changed; only the slow path knows."""
        cache = RejectionCache(ttl_seconds=1.0, max_entries=10, clock=_Clock())
        cache.store(
            "ns",
            "u",
            "r",
            0,
            [_state(0)],
            shard_count=1,
            vu_ms=1_000_000,
            ttl_epoch=None,
            disabled=False,
        )
        assert cache.views("ns", "u", "r", now_ms=1_000_000) == {}
        assert list(cache.views("ns", "u", "r", now_ms=999_999)) == [0]

    def test_an_expired_ttl_is_not_used(self):
        """A swept-but-not-deleted bucket is recreated full by the slow path."""
        cache = RejectionCache(ttl_seconds=1.0, max_entries=10, clock=_Clock())
        cache.store(
            "ns",
            "u",
            "r",
            0,
            [_state(0)],
            shard_count=1,
            vu_ms=None,
            ttl_epoch=1000,
            disabled=False,
        )
        assert cache.views("ns", "u", "r", now_ms=1_000_000) == {}
        assert list(cache.views("ns", "u", "r", now_ms=999_999)) == [0]

    def test_a_disabled_stamp_is_not_used(self):
        """ResourceDisabled stays the server's answer (ADR-125)."""
        cache = RejectionCache(ttl_seconds=1.0, max_entries=10, clock=_Clock())
        cache.store(
            "ns", "u", "r", 0, [_state(0)], shard_count=1, vu_ms=None, ttl_epoch=None, disabled=True
        )
        assert cache.views("ns", "u", "r", now_ms=1_000_000) == {}

    def test_entries_are_keyed_by_namespace_and_shard(self):
        cache = RejectionCache(ttl_seconds=1.0, max_entries=10, clock=_Clock())
        cache.store(
            "a", "u", "r", 1, [_state(0)], shard_count=2, vu_ms=None, ttl_epoch=None, disabled=False
        )
        assert cache.views("b", "u", "r", now_ms=1) == {}
        assert list(cache.views("a", "u", "r", now_ms=1)) == [1]

    def test_forget_drops_one_shard(self):
        cache = RejectionCache(ttl_seconds=1.0, max_entries=10, clock=_Clock())
        for shard in (0, 1):
            cache.store(
                "ns",
                "u",
                "r",
                shard,
                [_state(0)],
                shard_count=2,
                vu_ms=None,
                ttl_epoch=None,
                disabled=False,
            )
        cache.forget("ns", "u", "r", 0)
        cache.forget("ns", "u", "r", 7)  # unknown: no error
        assert list(cache.views("ns", "u", "r", now_ms=1)) == [1]

    def test_clear_drops_everything(self):
        cache = RejectionCache(ttl_seconds=1.0, max_entries=10, clock=_Clock())
        cache.store(
            "ns",
            "u",
            "r",
            0,
            [_state(0)],
            shard_count=1,
            vu_ms=None,
            ttl_epoch=None,
            disabled=False,
        )
        cache.clear()
        assert cache.views("ns", "u", "r", now_ms=1) == {}

    def test_oldest_entry_is_evicted_at_the_cap(self):
        cache = RejectionCache(ttl_seconds=1.0, max_entries=2, clock=_Clock())
        for entity in ("a", "b", "c"):
            cache.store(
                "ns",
                entity,
                "r",
                0,
                [_state(0)],
                shard_count=1,
                vu_ms=None,
                ttl_epoch=None,
                disabled=False,
            )
        assert cache.views("ns", "a", "r", now_ms=1) == {}
        assert list(cache.views("ns", "c", "r", now_ms=1)) == [0]

    def test_a_lookup_reads_only_its_own_bucket(self):
        """`views` runs on every acquire, so it must not scan the cache (review #1).

        A scan over 10,000 entries measured 2.9 ms per call; 1,000 indexed
        lookups fit comfortably in half a second.
        """
        import timeit

        cache = RejectionCache(ttl_seconds=1e9, max_entries=10_000, clock=_Clock())
        for i in range(10_000):
            cache.store(
                "ns",
                f"e{i}",
                "r",
                0,
                [_state(0)],
                shard_count=1,
                vu_ms=None,
                ttl_epoch=None,
                disabled=False,
            )
        assert list(cache.views("ns", "e5", "r", now_ms=1)) == [0]
        assert timeit.timeit(lambda: cache.views("ns", "e5", "r", now_ms=1), number=1000) < 0.5

    def test_eviction_keeps_the_index_in_step(self):
        cache = RejectionCache(ttl_seconds=1.0, max_entries=3, clock=_Clock())
        for shard in range(3):
            cache.store(
                "ns",
                "a",
                "r",
                shard,
                [_state(0)],
                shard_count=3,
                vu_ms=None,
                ttl_epoch=None,
                disabled=False,
            )
        cache.store(
            "ns",
            "b",
            "r",
            0,
            [_state(0)],
            shard_count=1,
            vu_ms=None,
            ttl_epoch=None,
            disabled=False,
        )
        assert len(cache) == 3
        assert list(cache.views("ns", "a", "r", now_ms=1)) == [1, 2]
        cache.forget("ns", "b", "r", 0)
        cache.forget("ns", "a", "r", 1)
        cache.forget("ns", "a", "r", 2)
        assert len(cache) == 0
        assert cache._buckets == {}

    def test_restoring_a_key_moves_it_to_the_back(self):
        cache = RejectionCache(ttl_seconds=1.0, max_entries=2, clock=_Clock())
        cache.store(
            "ns",
            "a",
            "r",
            0,
            [_state(0)],
            shard_count=1,
            vu_ms=None,
            ttl_epoch=None,
            disabled=False,
        )
        cache.store(
            "ns",
            "b",
            "r",
            0,
            [_state(0)],
            shard_count=1,
            vu_ms=None,
            ttl_epoch=None,
            disabled=False,
        )
        cache.store(
            "ns",
            "a",
            "r",
            0,
            [_state(0)],
            shard_count=1,
            vu_ms=None,
            ttl_epoch=None,
            disabled=False,
        )
        cache.store(
            "ns",
            "c",
            "r",
            0,
            [_state(0)],
            shard_count=1,
            vu_ms=None,
            ttl_epoch=None,
            disabled=False,
        )
        assert cache.views("ns", "b", "r", now_ms=1) == {}
        assert list(cache.views("ns", "a", "r", now_ms=1)) == [0]

    def test_counts_local_rejections(self):
        cache = RejectionCache(ttl_seconds=1.0, max_entries=10, clock=_Clock())
        cache.record_local_rejection()
        cache.record_local_rejection()
        assert cache.local_rejections == 2


class TestLocalRejection:
    """The limiter rejects from the cache instead of writing."""

    async def test_a_repeat_rejection_makes_no_dynamodb_write(self, limiter, repo):
        await _drain(limiter)
        with pytest.raises(RateLimitExceeded):
            await _drain(limiter)  # a real failed write teaches the cache

        async with _count_update_items(repo) as calls:
            with pytest.raises(RateLimitExceeded) as excinfo:
                async with limiter.acquire("u", "r", consume={"rpm": 1}):
                    pass
        assert calls == []
        status = excinfo.value.statuses[0]
        assert status.limit_name == "rpm" and status.exceeded
        assert excinfo.value.retry_after_seconds > 0
        assert repo.get_cache_stats().local_rejections == 1

    async def test_our_own_success_predicts_the_next_rejection(self, limiter, repo):
        """The success image already says the next request cannot fit."""
        async with limiter.acquire("u", "r", consume={"rpm": 1}):  # slow path creates
            pass
        async with limiter.acquire("u", "r", consume={"rpm": 1}):  # fast path, cached
            pass
        async with _count_update_items(repo) as calls:
            with pytest.raises(RateLimitExceeded):
                await _drain(limiter)
        assert calls == []

    async def test_a_request_the_projection_fits_still_writes(self, limiter, repo):
        """The cache never admits: a fitting request goes to DynamoDB."""
        await _drain(limiter)
        later = repo._now_ms() + 60_000
        with patch.object(repo, "_now_ms", return_value=later):
            async with _count_update_items(repo) as calls:
                async with limiter.acquire("u", "r", consume={"rpm": 1}) as lease:
                    pass
        assert calls  # it went to DynamoDB, and DynamoDB admitted it
        assert lease.consumed == {"rpm": 1}

    async def test_an_entry_past_the_ttl_goes_to_dynamodb(self, limiter, repo):
        clock = _Clock()
        repo._rejection_cache._clock = clock
        await _drain(limiter)
        clock.now += 1.5
        async with _count_update_items(repo) as calls:
            with pytest.raises(RateLimitExceeded):
                await _drain(limiter)
        assert len(calls) == 1

    async def test_ttl_zero_writes_every_rejection(self, repo):
        repo._rejection_cache = RejectionCache(ttl_seconds=0, max_entries=10)
        await repo.set_limits("u", [RPM], resource="r")
        async with RateLimiter(repository=repo) as limiter:
            await _drain(limiter)
            async with _count_update_items(repo) as calls:
                with pytest.raises(RateLimitExceeded):
                    await _drain(limiter)
        assert len(calls) == 1

    async def test_a_limits_override_is_not_rejected_locally(self, limiter, repo):
        await _drain(limiter)
        async with _count_update_items(repo) as calls:
            with pytest.raises(RateLimitExceeded):
                async with limiter.acquire(
                    "u", "r", consume={"rpm": 1}, limits=[Limit.per_minute("rpm", 2)]
                ):
                    pass
        assert len(calls) >= 1

    async def test_a_rollback_forgets_the_entry(self, limiter, repo):
        """Tokens we give back must not be hidden by our own cache."""
        with pytest.raises(RuntimeError):
            async with limiter.acquire("u", "r", consume={"rpm": 2}):
                with pytest.raises(RateLimitExceeded):
                    await _drain(limiter)
                raise RuntimeError("work failed")  # rollback credits rpm back

        async with limiter.acquire("u", "r", consume={"rpm": 2}):
            pass

    async def test_a_release_forgets_the_entry(self, limiter, repo):
        async with limiter.acquire("u", "r", consume={"rpm": 2}) as lease:
            await lease.release(rpm=2)
        async with limiter.acquire("u", "r", consume={"rpm": 2}):
            pass

    async def test_an_admin_change_clears_the_cache(self, limiter, repo):
        await _drain(limiter)
        with pytest.raises(RateLimitExceeded):
            await _drain(limiter)  # a real failed write fills the cache
        assert repo._rejection_cache.views(repo._namespace_id, "u", "r", now_ms=repo._now_ms())
        await repo.set_limits("u", [Limit.per_minute("rpm", 10)], resource="r")
        assert (
            repo._rejection_cache.views(repo._namespace_id, "u", "r", now_ms=repo._now_ms()) == {}
        )

    async def test_invalidate_config_cache_clears_it(self, limiter, repo):
        await _drain(limiter)
        with pytest.raises(RateLimitExceeded):
            await _drain(limiter)
        assert repo._rejection_cache.views(repo._namespace_id, "u", "r", now_ms=repo._now_ms())
        await repo.invalidate_config_cache()
        assert (
            repo._rejection_cache.views(repo._namespace_id, "u", "r", now_ms=repo._now_ms()) == {}
        )


class TestShards:
    """A short shard speaks for itself only (ADR-134, ADR-147 decision 4)."""

    async def test_draws_around_a_known_short_shard(self, limiter, repo):
        await _drain(limiter)  # shard 0 exists and is spent
        repo._learn_shard_count("u", "r", 2, meta=(False, None))
        repo._rejection_cache.store(
            repo._namespace_id,
            "u",
            "r",
            0,
            [_state(0)],
            shard_count=2,
            vu_ms=None,
            ttl_epoch=None,
            disabled=False,
        )
        targets: list[int] = []
        real = repo._speculative_consume_single

        async def spy(entity_id, resource, consume, ttl_seconds=None, shard_id=0, now_ms=None):
            targets.append(shard_id)
            return await real(
                entity_id, resource, consume, ttl_seconds, shard_id=shard_id, now_ms=now_ms
            )

        with patch.object(repo, "_speculative_consume_single", side_effect=spy):
            async with limiter.acquire("u", "r", consume={"rpm": 1}):
                pass  # shard 1 does not exist yet: the slow path creates it
        assert targets[0] == 1

    async def test_rejects_locally_only_when_every_shard_is_short(self, repo):
        ns = repo._namespace_id
        now = repo._now_ms()
        repo._learn_shard_count("u", "r", 2, meta=(False, None))
        cache = repo._rejection_cache
        cache.store(
            ns, "u", "r", 0, [_state(0)], shard_count=2, vu_ms=None, ttl_epoch=None, disabled=False
        )
        async with RateLimiter(repository=repo) as limiter:
            assert limiter._known_short_shards("u", "r", {"rpm": 1}, now)[0] == {0}
            cache.store(
                ns,
                "u",
                "r",
                1,
                [_state(0)],
                shard_count=2,
                vu_ms=None,
                ttl_epoch=None,
                disabled=False,
            )
            with pytest.raises(RateLimitExceeded):
                limiter._known_short_shards("u", "r", {"rpm": 1}, now)

    async def test_a_cold_entity_cache_cannot_hide_a_shard(self, repo):
        """The cached state's own shard_count counts: one short shard of two is
        not "every shard" just because the entity cache does not know there are
        two (found by test_cold_cache_retry_sizes_the_new_shard_from_the_failure_image)."""
        state = _state(0)
        state.shard_count = 2
        repo._rejection_cache.store(
            repo._namespace_id,
            "u",
            "r",
            0,
            [state],
            shard_count=2,
            vu_ms=None,
            ttl_epoch=None,
            disabled=False,
        )
        async with RateLimiter(repository=repo) as limiter:
            short, count = limiter._known_short_shards("u", "r", {"rpm": 1}, repo._now_ms())
        assert (short, count) == ({0}, 2)

    async def test_a_limit_missing_from_the_image_is_unknown(self, repo):
        """A newly configured limit is seeded by the slow path (#633)."""
        ns = repo._namespace_id
        repo._rejection_cache.store(
            ns, "u", "r", 0, [_state(0)], shard_count=1, vu_ms=None, ttl_epoch=None, disabled=False
        )
        async with RateLimiter(repository=repo) as limiter:
            short, _ = limiter._known_short_shards("u", "r", {"tpm": 1}, repo._now_ms())
        assert short == set()

    async def test_only_wcu_short_is_not_a_rejection(self, repo):
        ns = repo._namespace_id
        wcu = _state(0, name="wcu")
        rpm = _state(2)
        repo._rejection_cache.store(
            ns, "u", "r", 0, [rpm, wcu], shard_count=1, vu_ms=None, ttl_epoch=None, disabled=False
        )
        async with RateLimiter(repository=repo) as limiter:
            short, _ = limiter._known_short_shards("u", "r", {"rpm": 1}, repo._now_ms())
        assert short == set()


class TestScopesShareTheCache:
    async def test_a_namespace_scope_shares_the_cache(self, repo):
        await repo._register_namespace("other")
        scoped = await repo.namespace("other")
        assert scoped._rejection_cache is repo._rejection_cache
