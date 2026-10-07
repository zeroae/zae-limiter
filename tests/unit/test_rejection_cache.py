"""Tests for the client-side rejection cache (ADR-147, #695).

A failed conditional write costs 1 WCU, so a repeat rejection the client can
already predict from the last bucket state it saw must not reach DynamoDB. The
cache may only reject, never admit.
"""

from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import patch

import pytest

from zae_limiter import RateLimiter, RateLimitExceeded, schema
from zae_limiter.exceptions import ResourceDisabled
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
    await repo.create_entity("u")  # a record with no parent: phase 3 may trust it
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


_CLIENT_CALLS = (
    "get_item",
    "put_item",
    "update_item",
    "delete_item",
    "query",
    "scan",
    "batch_get_item",
    "batch_write_item",
    "transact_get_items",
    "transact_write_items",
)


@asynccontextmanager
async def _count_client_calls(repo: Repository):
    """Record every DynamoDB data call the repository's client makes."""
    from contextlib import ExitStack

    client = await repo._get_client()
    calls: list[str] = []

    def counting(name):
        real = getattr(client, name)

        async def call(*args, **kwargs):
            calls.append(name)
            return await real(*args, **kwargs)

        return call

    with ExitStack() as stack:
        for name in _CLIENT_CALLS:
            stack.enter_context(patch.object(client, name, side_effect=counting(name)))
        yield calls


def _cached(repo: Repository) -> bool:
    """Whether the cache holds a usable state for entity u, resource r."""
    return bool(repo._rejection_cache.views(repo._namespace_id, "u", "r", now_ms=repo._now_ms()))


async def _teach(limiter: RateLimiter, repo: Repository) -> None:
    """Drain u/r, then let one real failed write fill the cache."""
    await _drain(limiter)
    with pytest.raises(RateLimitExceeded):
        await _drain(limiter)
    assert _cached(repo)


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

    def test_trusted_entry_only_for_a_fresh_enabled_shard(self):
        clock = _Clock()
        cache = RejectionCache(ttl_seconds=1.0, max_entries=10, clock=clock)
        assert cache.trusted_entry("ns", "u", "r", 0, now_ms=1) is None  # nothing cached
        cache.store(
            "ns", "u", "r", 0, [_state(0)], shard_count=1, vu_ms=None, ttl_epoch=None, disabled=True
        )
        assert cache.trusted_entry("ns", "u", "r", 0, now_ms=1) is None  # disabled
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
        assert cache.trusted_entry("ns", "u", "r", 0, now_ms=1) is not None
        clock.now += 1.5
        assert cache.trusted_entry("ns", "u", "r", 0, now_ms=1) is None  # past the age cap

    def test_slow_pass_records_are_capped(self):
        clock = _Clock()
        cache = RejectionCache(ttl_seconds=1.0, max_entries=2, clock=clock)
        for entity in ("a", "b", "c"):
            cache.note_slow_pass("ns", entity, "r")
        assert not cache.slow_pass_within("ns", "a", "r", 60)  # the oldest went
        assert cache.slow_pass_within("ns", "c", "r", 60)
        clock.now += 61
        assert not cache.slow_pass_within("ns", "c", "r", 60)  # past the window
        disabled = RejectionCache(ttl_seconds=0)
        disabled.note_slow_pass("ns", "a", "r")
        assert not disabled.slow_pass_within("ns", "a", "r", 60)

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
        await _teach(limiter, repo)
        later = repo._now_ms() + 60_000
        with patch.object(repo, "_now_ms", return_value=later):
            assert _cached(repo)  # the entry is still trusted, and it projects a fit
            async with _count_update_items(repo) as calls:
                async with limiter.acquire("u", "r", consume={"rpm": 1}) as lease:
                    pass
        assert calls  # it went to DynamoDB, and DynamoDB admitted it
        assert lease.consumed == {"rpm": 1}
        assert repo.get_cache_stats().local_rejections == 0

    async def test_an_entry_past_the_ttl_goes_to_dynamodb(self, limiter, repo):
        clock = _Clock()
        repo._rejection_cache._clock = clock
        await _teach(limiter, repo)
        async with _count_update_items(repo) as calls:
            with pytest.raises(RateLimitExceeded):
                await _drain(limiter)
        assert calls == []  # inside the TTL: rejected locally
        clock.now += 1.5
        async with _count_update_items(repo) as calls:
            with pytest.raises(RateLimitExceeded):
                await _drain(limiter)
        assert len(calls) == 1  # past it: a real write

    async def test_ttl_zero_writes_every_rejection(self, repo):
        repo._rejection_cache = RejectionCache(ttl_seconds=0, max_entries=10)
        await repo.set_limits("u", [RPM], resource="r")
        async with RateLimiter(repository=repo) as limiter:
            await _drain(limiter)
            async with _count_update_items(repo) as calls:
                for _ in range(2):
                    with pytest.raises(RateLimitExceeded):
                        await _drain(limiter)
        assert len(calls) == 2
        assert repo.get_cache_stats().local_rejections == 0
        assert not _cached(repo)

    async def test_a_limits_override_is_not_rejected_locally(self, limiter, repo):
        await _teach(limiter, repo)
        async with _count_update_items(repo) as calls:
            with pytest.raises(RateLimitExceeded):
                async with limiter.acquire(
                    "u", "r", consume={"rpm": 1}, limits=[Limit.per_minute("rpm", 2)]
                ):
                    pass
        assert calls
        assert repo.get_cache_stats().local_rejections == 0

    async def test_a_rollback_forgets_the_entry(self, limiter, repo):
        """Tokens we give back must not be hidden by our own cache."""
        async with limiter.acquire("u", "r", consume={"rpm": 1}):  # slow path creates
            pass
        with pytest.raises(RuntimeError):
            async with limiter.acquire("u", "r", consume={"rpm": 1}):  # fast path: cached
                assert _cached(repo)
                raise RuntimeError("work failed")  # rollback credits rpm back
        assert not _cached(repo)
        async with limiter.acquire("u", "r", consume={"rpm": 1}):
            pass

    async def test_a_release_forgets_the_entry(self, limiter, repo):
        async with limiter.acquire("u", "r", consume={"rpm": 1}):  # slow path creates
            pass
        async with limiter.acquire("u", "r", consume={"rpm": 1}) as lease:  # fast path: cached
            assert _cached(repo)
            await lease.release(rpm=1)
        assert not _cached(repo)
        async with limiter.acquire("u", "r", consume={"rpm": 1}):
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

        # 20 acquires, each starting from "shard 0 known short, shard 1 unknown".
        # Only the first shard each one writes to matters; with the steering
        # broken, every first draw landing on shard 1 has odds of 1 in 2**20.
        ns = repo._namespace_id
        firsts: list[int] = []
        with patch.object(repo, "_speculative_consume_single", side_effect=spy):
            for _ in range(20):
                repo._rejection_cache.store(
                    ns,
                    "u",
                    "r",
                    0,
                    [_state(0)],
                    shard_count=2,
                    vu_ms=None,
                    ttl_epoch=None,
                    disabled=False,
                )
                repo._rejection_cache.forget(ns, "u", "r", 1)
                before = len(targets)
                try:
                    async with limiter.acquire("u", "r", consume={"rpm": 1}):
                        pass
                except RateLimitExceeded:
                    pass  # shard 1 drained too: fine, only the first draw is checked
                firsts.append(targets[before])
        assert firsts == [1] * 20

    async def test_a_phase_1_repository_still_works(self, limiter, repo):
        """Phase-2 review #5: `avoid_parent_shards` is passed only when non-empty."""
        await _drain(limiter)
        repo._learn_shard_count("u", "r", 2, meta=(False, None))
        _store(repo, "u", 0, 0, shard_count=2)
        real = repo.speculative_consume

        async def phase_1(
            entity_id,
            resource,
            consume,
            ttl_seconds=None,
            shard_id=None,
            now_ms=None,
            avoid_shards=frozenset(),
        ):
            return await real(
                entity_id, resource, consume, ttl_seconds, shard_id, now_ms, avoid_shards
            )

        with patch.object(repo, "speculative_consume", side_effect=phase_1) as spy:
            async with limiter.acquire("u", "r", consume={"rpm": 1}):
                pass  # shard 1: created by the slow path
        assert spy.call_args.kwargs["avoid_shards"] == frozenset({0})

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


def _clock_ms(repo: Repository, start_ms: int) -> list[int]:
    """Pin the token-bucket clock to a mutable instant; returns the cell."""
    cell = [start_ms]
    repo._now_ms = lambda: cell[0]
    return cell


async def _stamp_current_lambdas(repo: Repository) -> None:
    """Satisfy the reader-version gates (ADR-141, ADR-146) for this build."""
    from zae_limiter import __version__
    from zae_limiter.version import get_schema_version

    await repo.set_version_record(schema_version=get_schema_version(), lambda_version=__version__)


class TestQuotasAndWindows:
    """The projection must honour a reset the server would apply."""

    async def test_a_calendar_quota_is_not_rejected_past_its_reset(self, repo):
        from datetime import UTC, datetime

        quota = Limit.quota("rpd", 2, cron="0 0 * * *")
        await repo.set_limits("u", [quota], resource="r")
        before_midnight = int(datetime(2026, 10, 6, 23, 59, 30, tzinfo=UTC).timestamp() * 1000)
        clock = _clock_ms(repo, before_midnight)
        async with RateLimiter(repository=repo) as limiter:
            async with limiter.acquire("u", "r", consume={"rpd": 1}):  # slow path creates
                pass
            async with limiter.acquire("u", "r", consume={"rpd": 1}):  # fast path: cached
                pass
            async with _count_update_items(repo) as calls:
                with pytest.raises(RateLimitExceeded):
                    async with limiter.acquire("u", "r", consume={"rpd": 1}):
                        pass
            assert calls == []  # before midnight: rejected locally

            clock[0] = before_midnight + 60_000  # 00:00:30 the next day
            async with limiter.acquire("u", "r", consume={"rpd": 1}) as lease:
                pass
        assert lease.consumed == {"rpd": 1}

    async def test_a_session_quota_is_not_rejected_once_its_window_ends(self, repo):
        from datetime import timedelta

        await _stamp_current_lambdas(repo)
        session = Limit.quota("session", 2, reset_after=timedelta(seconds=60))
        await repo.set_limits("u", [session], resource="r")
        clock = _clock_ms(repo, repo._now_ms())
        async with RateLimiter(repository=repo) as limiter:
            async with limiter.acquire("u", "r", consume={"session": 1}):  # opens the window
                pass
            async with limiter.acquire("u", "r", consume={"session": 1}):  # fast path: cached
                pass
            async with _count_update_items(repo) as calls:
                with pytest.raises(RateLimitExceeded):
                    async with limiter.acquire("u", "r", consume={"session": 1}):
                        pass
            assert calls == []  # inside the window: rejected locally

            clock[0] += 61_000
            async with limiter.acquire("u", "r", consume={"session": 1}) as lease:
                pass
        assert lease.consumed == {"session": 1}


class TestShardCountBound:
    async def test_a_shard_past_the_count_is_ignored(self, repo):
        ns = repo._namespace_id
        for shard in (0, 5):
            repo._rejection_cache.store(
                ns,
                "u",
                "r",
                shard,
                [_state(0)],
                shard_count=1,
                vu_ms=None,
                ttl_epoch=None,
                disabled=False,
            )
        async with RateLimiter(repository=repo) as limiter:
            with pytest.raises(RateLimitExceeded):  # shard 0 is the only real shard
                limiter._known_short_shards("u", "r", {"rpm": 1}, repo._now_ms())
        repo._rejection_cache.forget(ns, "u", "r", 0)
        async with RateLimiter(repository=repo) as limiter:
            short, count = limiter._known_short_shards("u", "r", {"rpm": 1}, repo._now_ms())
        assert (short, count) == (set(), 1)


class TestSyncTwin:
    def test_the_sync_limiter_rejects_locally(self, mock_dynamodb):
        from zae_limiter import SyncRateLimiter
        from zae_limiter.sync_repository import SyncRepository

        repo = SyncRepository(
            name="test-rejection-sync", region="us-east-1", _skip_deprecation_warning=True
        )
        repo.create_table()
        repo._register_namespace("default")
        repo.set_limits("u", [RPM], resource="r")
        limiter = SyncRateLimiter(repository=repo)
        with limiter.acquire("u", "r", consume={"rpm": 2}):
            pass
        with pytest.raises(RateLimitExceeded):
            with limiter.acquire("u", "r", consume={"rpm": 2}):  # a real failed write
                pass
        client = repo._get_client()
        with patch.object(client, "update_item", side_effect=AssertionError("wrote")):
            with pytest.raises(RateLimitExceeded):
                with limiter.acquire("u", "r", consume={"rpm": 1}):
                    pass
        assert repo.get_cache_stats().local_rejections == 1
        repo.close()

    def test_the_sync_limiter_checks_the_parent_first(self, mock_dynamodb):
        """Phase 2 on the generated sync path: a parent known short, no DynamoDB call."""
        from contextlib import ExitStack

        from zae_limiter import SyncRateLimiter
        from zae_limiter.sync_repository import SyncRepository

        repo = SyncRepository(
            name="test-rejection-sync-cascade", region="us-east-1", _skip_deprecation_warning=True
        )
        repo.create_table()
        repo._register_namespace("default")
        repo.create_entity("org")
        repo.create_entity("u", parent_id="org", cascade=True)
        repo.set_limits("org", [RPM], resource="r")
        repo.set_limits("u", [Limit.per_minute("rpm", 100)], resource="r")
        limiter = SyncRateLimiter(repository=repo)
        for _ in range(2):  # slow path, then the warm parallel write: org spent
            with limiter.acquire("u", "r", consume={"rpm": 1}):
                pass
        client = repo._get_client()
        with ExitStack() as stack:
            for name in _CLIENT_CALLS:
                stack.enter_context(
                    patch.object(client, name, side_effect=AssertionError(f"called {name}"))
                )
            with pytest.raises(RateLimitExceeded) as excinfo:
                with limiter.acquire("u", "r", consume={"rpm": 1}):
                    pass
        assert [s.entity_id for s in excinfo.value.statuses] == ["u", "org"]
        assert repo.get_cache_stats().local_rejections == 1
        repo.close()


_ADMIN_CALLS = {
    "set_limits": lambda repo: repo.set_limits("u", [RPM], resource="r"),
    "delete_limits": lambda repo: repo.delete_limits("u", resource="r"),
    "set_resource_defaults": lambda repo: repo.set_resource_defaults("r", [RPM]),
    "delete_resource_defaults": lambda repo: repo.delete_resource_defaults("r"),
    "set_system_defaults": lambda repo: repo.set_system_defaults([RPM]),
    "delete_system_defaults": lambda repo: repo.delete_system_defaults(),
    "disable_resource": lambda repo: repo.disable_resource("r"),
    "enable_resource": lambda repo: repo.enable_resource("r"),
    "set_resource_cascade": lambda repo: repo.set_resource_cascade("r", True),
    "delete_entity": lambda repo: repo.delete_entity("u"),
}


class TestAdminWritesClear:
    @pytest.mark.parametrize("call", sorted(_ADMIN_CALLS))
    async def test_every_admin_write_clears_the_cache(self, repo, call):
        await _stamp_current_lambdas(repo)
        repo._rejection_cache.store(
            repo._namespace_id,
            "u",
            "r",
            0,
            [_state(0)],
            shard_count=1,
            vu_ms=None,
            ttl_epoch=None,
            disabled=False,
        )
        await _ADMIN_CALLS[call](repo)
        assert len(repo._rejection_cache) == 0


class _Admin:
    """A stand-in owner whose admin call stores a state mid-flight."""

    def __init__(self) -> None:
        self._rejection_cache = RejectionCache(ttl_seconds=1.0, max_entries=10, clock=_Clock())

    def _store_mid_flight(self) -> None:
        self._rejection_cache.store(
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


class TestClearsRejectionCache:
    """Review #5: an acquire racing an admin write must not leave a stale state."""

    async def test_async_clears_a_state_stored_during_the_call(self):
        from zae_limiter.rejection_cache import clears_rejection_cache

        class Owner(_Admin):
            @clears_rejection_cache
            async def change(self) -> str:
                self._store_mid_flight()
                return "done"

        owner = Owner()
        owner._store_mid_flight()
        assert await owner.change() == "done"
        assert len(owner._rejection_cache) == 0

    def test_sync_clears_a_state_stored_during_the_call(self):
        from zae_limiter.rejection_cache import clears_rejection_cache

        class Owner(_Admin):
            @clears_rejection_cache
            def change(self) -> str:
                self._store_mid_flight()
                return "done"

        owner = Owner()
        assert owner.change() == "done"
        assert len(owner._rejection_cache) == 0

    async def test_clears_even_when_the_call_raises(self):
        from zae_limiter.rejection_cache import clears_rejection_cache

        class Owner(_Admin):
            @clears_rejection_cache
            async def change(self) -> None:
                self._store_mid_flight()
                raise RuntimeError("write failed")

        owner = Owner()
        with pytest.raises(RuntimeError):
            await owner.change()
        assert len(owner._rejection_cache) == 0


class TestForgetAfterTheWriteLands:
    """Review #6: a refund forgets again once its write landed."""

    def _store_short(self, repo: Repository) -> None:
        repo._rejection_cache.store(
            repo._namespace_id,
            "u",
            "r",
            0,
            [_state(0)],
            shard_count=1,
            vu_ms=None,
            ttl_epoch=None,
            disabled=False,
        )

    async def test_a_state_stored_between_build_and_write_is_forgotten(self, repo):
        refund = repo.build_composite_adjust("u", "r", {"rpm": -1000}, shard_id=0)
        self._store_short(repo)  # an acquire races in after the build
        assert len(repo._rejection_cache) == 1
        await repo.write_each([refund])
        assert len(repo._rejection_cache) == 0

    def test_a_non_bucket_write_forgets_nothing(self, repo):
        self._store_short(repo)
        repo._forget_written_bucket(
            {"Put": {"Item": {"PK": {"S": f"{repo._namespace_id}/ENTITY#u"}}}}
        )
        assert len(repo._rejection_cache) == 1

    def test_a_malformed_bucket_key_is_ignored(self, repo):
        self._store_short(repo)
        repo._forget_written_bucket(
            {"Update": {"Key": {"PK": {"S": f"{repo._namespace_id}/BUCKET#u#r#not-a-shard"}}}}
        )
        assert len(repo._rejection_cache) == 1


class TestCascadingChild:
    """Review #8: a cascading child is never rejected locally."""

    def test_the_cache_remembers_a_cascading_bucket(self):
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
        assert not cache.cascades("ns", "u", "r")
        cache.store(
            "ns",
            "u",
            "r",
            1,
            [_state(0)],
            shard_count=2,
            vu_ms=None,
            ttl_epoch=None,
            disabled=False,
            cascades=True,
        )
        assert cache.cascades("ns", "u", "r")
        assert not cache.cascades("ns", "other", "r")

    async def test_a_disabled_parent_answers_403_every_time(self, repo):
        """With the child cached short, a local 429 would hide the parent's 403."""
        await repo.create_entity("org")
        await repo.create_entity("u", parent_id="org", cascade=True)
        await repo.set_limits("u", [RPM], resource="r")
        await repo.set_limits("org", [Limit.per_minute("rpm", 100)], resource="r")
        async with RateLimiter(repository=repo) as limiter:
            await _drain(limiter)  # creates child and parent, warms the entity cache
            await repo.disable_entity("org", resource="r")
            for _ in range(3):
                with pytest.raises(ResourceDisabled) as excinfo:
                    async with limiter.acquire("u", "r", consume={"rpm": 1}):
                        pass
                assert excinfo.value.entity_id == "org"
            # The child's short state is cached, and it was still not used.
            assert _cached(repo)
            assert repo._rejection_cache.cascades(repo._namespace_id, "u", "r")
        assert repo.get_cache_stats().local_rejections == 0


class TestOptions:
    def test_a_negative_ttl_is_refused(self):
        with pytest.raises(ValueError, match="rejection_cache_ttl"):
            RejectionCache(ttl_seconds=-1)

    def test_a_size_below_one_is_refused(self):
        with pytest.raises(ValueError, match="rejection_cache_size"):
            RejectionCache(max_entries=0)

    def test_the_constructor_passes_both_options(self, mock_dynamodb):
        repo = Repository(
            name="test-rejection-opts",
            region="us-east-1",
            rejection_cache_ttl=2.5,
            rejection_cache_size=42,
            _skip_deprecation_warning=True,
        )
        assert repo._rejection_cache.ttl_seconds == 2.5
        assert repo._rejection_cache.max_entries == 42

    def test_the_builder_records_both_options(self):
        builder = Repository.builder().rejection_cache_ttl(0).rejection_cache_size(7)
        assert builder._rejection_cache_ttl == 0
        assert builder._rejection_cache_size == 7

    async def test_a_backend_without_a_namespace_never_rejects_locally(self, repo):
        """`getattr` keeps a third-party backend working (ADR-147 decision 6)."""
        repo._rejection_cache.store(
            repo._namespace_id,
            "u",
            "r",
            0,
            [_state(0)],
            shard_count=1,
            vu_ms=None,
            ttl_epoch=None,
            disabled=False,
        )
        async with RateLimiter(repository=repo) as limiter:
            saved = repo._namespace_id
            repo._namespace_id = None
            try:
                assert limiter._known_short_shards("u", "r", {"rpm": 1}, 1) == (set(), 1)
            finally:
                repo._namespace_id = saved


def _store(
    repo: Repository,
    entity: str,
    shard: int,
    tokens: int,
    *,
    shard_count: int = 1,
    cascades: bool = False,
    parent_id: str | None = None,
    disabled: bool = False,
) -> None:
    state = _state(tokens, entity=entity)
    state.shard_count = shard_count
    repo._rejection_cache.store(
        repo._namespace_id,
        entity,
        "r",
        shard,
        [state],
        shard_count=shard_count,
        vu_ms=None,
        ttl_epoch=None,
        disabled=disabled,
        cascades=cascades,
        parent_id=parent_id,
    )


@pytest.fixture
async def family(repo):
    """org -> u, u cascades on r; org allows 2 rpm, u allows 100."""
    await repo.create_entity("org")
    await repo.create_entity("u", parent_id="org", cascade=True)
    await repo.set_limits("org", [RPM], resource="r")
    await repo.set_limits("u", [Limit.per_minute("rpm", 100)], resource="r")
    async with RateLimiter(repository=repo) as limiter:
        yield limiter


class TestCascadeParentPrecheck:
    """ADR-147 phase 2: the parent's cached state, checked before any write."""

    async def test_a_parent_known_short_rejects_with_no_write(self, family, repo):
        async with family.acquire("u", "r", consume={"rpm": 1}):  # slow path, cold cache
            pass
        async with family.acquire("u", "r", consume={"rpm": 1}):  # warm: parallel, both cached
            pass
        async with _count_client_calls(repo) as calls:
            with pytest.raises(RateLimitExceeded) as excinfo:
                async with family.acquire("u", "r", consume={"rpm": 1}):
                    pass
        assert calls == []  # no DynamoDB call of any kind
        # The server reports the child's statuses, then the parent's.
        assert [s.entity_id for s in excinfo.value.statuses] == ["u", "org"]
        assert excinfo.value.statuses[1].exceeded
        assert excinfo.value.retry_after_seconds > 0
        assert repo.get_cache_stats().local_rejections == 1

    async def test_the_entity_wide_guess_alone_never_triggers_it(self, repo):
        """Decision 1: only a policy learned from the child's own bucket counts."""
        _store(repo, "org", 0, 0)
        repo._entity_cache[(repo._namespace_id, "u")] = (True, "org", {})
        async with RateLimiter(repository=repo) as limiter:
            assert limiter._known_short_shards("u", "r", {"rpm": 1}, repo._now_ms()) == (set(), 1)

    async def test_a_learned_cascade_cache_entry_alone_is_not_enough(self, repo):
        """Phase-2 review #2: `_cascade_cache` is never expired, so it cannot decide."""
        _store(repo, "org", 0, 0)
        repo._learn_shard_count("u", "r", 1, meta=(True, "org"))
        async with RateLimiter(repository=repo) as limiter:
            assert limiter._known_short_shards("u", "r", {"rpm": 1}, repo._now_ms()) == (set(), 1)

    async def test_an_aged_child_stamp_does_not_name_the_parent(self, repo):
        clock = _Clock()
        repo._rejection_cache._clock = clock
        _store(repo, "u", 0, 2, cascades=True, parent_id="org")
        clock.now += 1.5  # the child's stamp is now past the age cap
        _store(repo, "org", 0, 0)  # a sibling keeps the parent's state fresh
        async with RateLimiter(repository=repo) as limiter:
            assert limiter._known_short_shards("u", "r", {"rpm": 1}, repo._now_ms()) == (set(), 1)

    async def test_a_policy_turned_off_elsewhere_is_not_believed_past_the_ttl(self, repo):
        """Review #2, end to end: u stops cascading in another process; v keeps org short."""
        await repo.create_entity("org")
        await repo.create_entity("u", parent_id="org", cascade=True)
        await repo.create_entity("v", parent_id="org", cascade=True)
        await repo.set_limits("org", [RPM], resource="r")
        await repo.set_limits("u", [Limit.per_minute("rpm", 100)], resource="r")
        await repo.set_limits("v", [Limit.per_minute("rpm", 100)], resource="r")
        clock = _Clock()
        repo._rejection_cache._clock = clock
        async with RateLimiter(repository=repo) as limiter:
            async with limiter.acquire("u", "r", consume={"rpm": 1}):  # slow path
                pass
            async with limiter.acquire("u", "r", consume={"rpm": 1}):  # fast: org now spent
                pass
            # Another process turns u's cascade off; this process's caches are
            # not told (the rejection cache and `_cascade_cache` keep their view).
            cache = repo._rejection_cache
            saved = dict(repo._cascade_cache)
            saved_buckets = {key: dict(shards) for key, shards in cache._buckets.items()}
            saved_order = dict(cache._order)
            await _stamp_current_lambdas(repo)
            await repo.set_entity_cascade("u", False, resource="r")  # clears this process's cache
            repo._cascade_cache.update(saved)
            cache._buckets.update(saved_buckets)  # ...which another process could not have done
            cache._order.update(saved_order)
            assert cache.cascades(repo._namespace_id, "u", "r")  # the stale stamp is still here
            clock.now += 3600  # u's own stamp is long past the age cap
            for _ in range(2):  # v keeps org's cached state fresh and short
                with pytest.raises(RateLimitExceeded):
                    async with limiter.acquire("v", "r", consume={"rpm": 1}):
                        pass
            # The server admits u: it no longer cascades.
            async with limiter.acquire("u", "r", consume={"rpm": 1}) as lease:
                pass
        assert lease.consumed == {"rpm": 1}

    async def test_a_parent_with_room_on_one_shard_is_written_to(self, repo):
        _store(repo, "org", 0, 0, shard_count=2)
        _store(repo, "u", 0, 2, cascades=True, parent_id="org")
        async with RateLimiter(repository=repo) as limiter:
            assert limiter._known_short_shards("u", "r", {"rpm": 1}, repo._now_ms()) == (set(), 1)
            assert limiter._known_short_parent_shards("u", "r", {"rpm": 1}, repo._now_ms()) == {0}

    async def test_the_parents_own_cascade_flag_is_ignored(self, repo):
        """A child's lease never reaches the grandparent (#686)."""
        _store(repo, "org", 0, 0, cascades=True, parent_id="grandparent")
        _store(repo, "u", 0, 2, cascades=True, parent_id="org")
        async with RateLimiter(repository=repo) as limiter:
            with pytest.raises(RateLimitExceeded):
                limiter._known_short_shards("u", "r", {"rpm": 1}, repo._now_ms())

    async def test_a_disabled_parent_state_is_unknown(self, repo):
        """A disabled parent must still reach DynamoDB and its 403."""
        _store(repo, "org", 0, 0, disabled=True)
        _store(repo, "u", 0, 2, cascades=True, parent_id="org")
        async with RateLimiter(repository=repo) as limiter:
            assert limiter._known_short_shards("u", "r", {"rpm": 1}, repo._now_ms()) == (set(), 1)

    async def test_a_disabled_child_gets_403_every_time(self, family, repo):
        """Phase-2 review #1: a disabled child with a parent known short is 403, not 429."""
        async with family.acquire("u", "r", consume={"rpm": 2}):  # slow path; drains org
            pass
        async with family.acquire("u", "r", consume={"rpm": 0}):  # fast path: stamps cached
            pass
        await repo.disable_entity("u", resource="r")  # through this process: clears the cache
        for _ in range(4):
            with pytest.raises(ResourceDisabled) as excinfo:
                async with family.acquire("u", "r", consume={"rpm": 1}):
                    pass
            assert excinfo.value.entity_id == "u"
        assert repo.get_cache_stats().local_rejections == 0

    def test_parent_of_is_none_for_a_disabled_child(self):
        cache = RejectionCache(ttl_seconds=1.0, max_entries=10, clock=_Clock())
        cache.store(
            "ns",
            "u",
            "r",
            0,
            [_state(0)],
            shard_count=2,
            vu_ms=None,
            ttl_epoch=None,
            disabled=False,
            cascades=True,
            parent_id="org",
        )
        assert cache.parent_of("ns", "u", "r", now_ms=1) == "org"
        cache.store(
            "ns",
            "u",
            "r",
            1,
            [_state(0)],
            shard_count=2,
            vu_ms=None,
            ttl_epoch=None,
            disabled=True,
            cascades=True,
            parent_id="org",
        )
        assert cache.parent_of("ns", "u", "r", now_ms=1) is None


class TestCascadingChildKnownShort:
    """Decision 3: rejected locally only while the parent is known not disabled."""

    async def test_rejected_locally_with_a_trusted_enabled_parent(self, repo):
        _store(repo, "u", 0, 0, cascades=True, parent_id="org")
        _store(repo, "org", 0, 2)
        async with RateLimiter(repository=repo) as limiter:
            with pytest.raises(RateLimitExceeded) as excinfo:
                limiter._known_short_shards("u", "r", {"rpm": 1}, repo._now_ms())
        # The server answers from the child's image alone.
        assert [s.entity_id for s in excinfo.value.statuses] == ["u"]

    async def test_not_rejected_without_a_parent_state(self, repo):
        _store(repo, "u", 0, 0, cascades=True, parent_id="org")
        async with RateLimiter(repository=repo) as limiter:
            assert limiter._known_short_shards("u", "r", {"rpm": 1}, repo._now_ms()) == ({0}, 1)

    async def test_not_rejected_when_the_parent_state_is_disabled(self, repo):
        _store(repo, "u", 0, 0, cascades=True, parent_id="org")
        _store(repo, "org", 0, 2, disabled=True)
        async with RateLimiter(repository=repo) as limiter:
            assert limiter._known_short_shards("u", "r", {"rpm": 1}, repo._now_ms()) == ({0}, 1)

    async def test_not_rejected_when_any_parent_shard_is_disabled(self, repo):
        """Phase-2 review #3: a disable that stopped part-way still means 403."""
        _store(repo, "u", 0, 0, cascades=True, parent_id="org")
        _store(repo, "org", 0, 2, shard_count=2)  # trusted, enabled
        _store(repo, "org", 1, 2, shard_count=2, disabled=True)
        async with RateLimiter(repository=repo) as limiter:
            assert limiter._known_short_shards("u", "r", {"rpm": 1}, repo._now_ms()) == ({0}, 1)


class TestParentShardSteering:
    """Decision 4: the parallel write draws the parent's shard around short ones."""

    async def test_never_draws_a_known_short_parent_shard(self, family, repo):
        async with family.acquire("u", "r", consume={"rpm": 1}):  # slow path creates
            pass
        # The fast path now shows the child's own cascade stamp; until it has,
        # decision 1 rightly skips the parent check.
        async with family.acquire("u", "r", consume={"rpm": 1}):
            pass
        assert (
            repo._rejection_cache.parent_of(repo._namespace_id, "u", "r", repo._now_ms()) == "org"
        )
        repo._learn_shard_count("org", "r", 2, meta=(False, None))
        ns = repo._namespace_id
        parent_targets: list[int] = []
        real = repo._speculative_consume_single

        async def spy(entity_id, resource, consume, ttl_seconds=None, shard_id=0, now_ms=None):
            if entity_id == "org":
                parent_targets.append(shard_id)
            return await real(
                entity_id, resource, consume, ttl_seconds, shard_id=shard_id, now_ms=now_ms
            )

        firsts: list[int] = []
        with patch.object(repo, "_speculative_consume_single", side_effect=spy):
            for _ in range(20):
                # Each round starts from a trusted child stamp: a refund
                # forgets it, and decision 1 then rightly skips the parent.
                _store(repo, "u", 0, 100, cascades=True, parent_id="org")
                _store(repo, "org", 0, 0, shard_count=2)
                repo._rejection_cache.forget(ns, "org", "r", 1)
                before = len(parent_targets)
                try:
                    async with family.acquire("u", "r", consume={"rpm": 1}):
                        pass
                except RateLimitExceeded:
                    pass
                if len(parent_targets) > before:
                    firsts.append(parent_targets[before])
        assert firsts and all(shard == 1 for shard in firsts)


async def _bucket_item(repo: Repository, entity_id: str) -> dict:
    """Entity's r shard-0 bucket item, read straight from the table."""
    client = await repo._get_client()
    response = await client.get_item(
        TableName=repo.table_name,
        Key={
            "PK": {"S": schema.pk_bucket(repo._namespace_id, entity_id, "r", 0)},
            "SK": {"S": schema.sk_state()},
        },
    )
    return response["Item"]


async def _consumed(repo: Repository, entity_id: str) -> int:
    """`b_rpm_tc` (millitokens) on entity's r shard 0, read straight from the table."""
    client = await repo._get_client()
    item = (
        await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(repo._namespace_id, entity_id, "r", 0)},
                "SK": {"S": schema.sk_state()},
            },
        )
    ).get("Item")
    return 0 if item is None else int(item[schema.bucket_attr("rpm", "tc")]["N"])


async def _spent_and_cached(limiter: RateLimiter) -> None:
    """Spend u/r's 2 rpm so that a fast-path success image (tk = 0) is cached.

    Pins the cache's own clock unless a test already did: on a slow machine
    the real monotonic clock can pass the 1 s age cap mid-test (phase-3 review).
    """
    cache = limiter._repository._rejection_cache
    if not isinstance(cache._clock, _Clock):
        cache._clock = _Clock()
    async with limiter.acquire("u", "r", consume={"rpm": 1}):  # slow path creates
        pass
    async with limiter.acquire("u", "r", consume={"rpm": 1}):  # fast path: cached, tk = 0
        pass


class TestRefillFromCache:
    """ADR-147 phase 3: refill would help ⇒ one locked write, no read."""

    async def test_one_write_and_no_read_when_refill_covers_it(self, limiter, repo):
        await _spent_and_cached(limiter)
        later = repo._now_ms() + 60_000  # a full minute: both tokens back
        with patch.object(repo, "_now_ms", return_value=later):
            async with _count_client_calls(repo) as calls:
                async with limiter.acquire("u", "r", consume={"rpm": 1}) as lease:
                    pass
        assert calls == ["update_item"]  # today: a failed write, a read, a write
        assert lease.consumed == {"rpm": 1}
        bucket = (await repo.get_buckets("u", "r"))[0]
        assert bucket.tokens_milli == 1000  # refilled to 2, debited 1

    async def test_a_write_since_the_state_was_cached_falls_back(self, limiter, repo):
        """The rf lock: another writer moved rf, so the state is stale."""
        await _spent_and_cached(limiter)
        client = await repo._get_client()
        await client.update_item(  # another process's refill
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(repo._namespace_id, "u", "r", 0)},
                "SK": {"S": schema.sk_state()},
            },
            UpdateExpression="SET rf = rf + :one",
            ExpressionAttributeValues={":one": {"N": "1"}},
        )
        later = repo._now_ms() + 60_000
        with patch.object(repo, "_now_ms", return_value=later):
            async with _count_client_calls(repo) as calls:
                async with limiter.acquire("u", "r", consume={"rpm": 1}) as lease:
                    pass
        assert lease.consumed == {"rpm": 1}
        assert calls[0] == "update_item" and len(calls) > 1  # lost lock, today's path

    async def test_a_limit_change_elsewhere_is_not_refilled_at_the_old_limit(self, limiter, repo):
        """The vu pin: another process cut the limit and stamped vu = 0, rf unmoved."""
        await _spent_and_cached(limiter)
        client = await repo._get_client()
        await client.update_item(  # what _sync_bucket_params writes elsewhere
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(repo._namespace_id, "u", "r", 0)},
                "SK": {"S": schema.sk_state()},
            },
            UpdateExpression="SET #cp = :cp, #ra = :ra, vu = :zero",
            ExpressionAttributeNames={
                "#cp": schema.bucket_attr("rpm", schema.BUCKET_FIELD_CP),
                "#ra": schema.bucket_attr("rpm", schema.BUCKET_FIELD_RA),
            },
            ExpressionAttributeValues={
                ":cp": {"N": "1000"},
                ":ra": {"N": "1000"},
                ":zero": {"N": "0"},
            },
        )
        refills: list[object] = []
        real = repo.refill_from_cached_state

        async def spy(*args, **kwargs):
            result = await real(*args, **kwargs)
            refills.append(result)
            return result

        later = repo._now_ms() + 60_000
        with (
            patch.object(repo, "_now_ms", return_value=later),
            patch.object(repo, "refill_from_cached_state", side_effect=spy),
        ):
            async with limiter.acquire("u", "r", consume={"rpm": 1}):
                pass
        assert refills == [None]  # the pinned write refused; the slow path re-read
        bucket = (await repo.get_buckets("u", "r"))[0]
        assert bucket.capacity_milli == 1000
        assert bucket.tokens_milli <= 0  # refilled to the NEW capacity (1), then debited

    async def test_not_used_when_the_stored_balance_is_enough(self, limiter, repo):
        async with limiter.acquire("u", "r", consume={"rpm": 1}):  # creates: tk = 1 left
            pass
        async with limiter.acquire("u", "r", consume={"rpm": 0}):  # fast path: cached
            pass
        with patch.object(repo, "refill_from_cached_state") as refill:
            async with limiter.acquire("u", "r", consume={"rpm": 1}):
                pass
        refill.assert_not_called()

    async def test_not_used_for_a_cascading_bucket(self, family, repo):
        await repo.set_limits("org", [Limit.per_minute("rpm", 1000)], resource="r")
        async with family.acquire("u", "r", consume={"rpm": 50}):
            pass
        async with family.acquire("u", "r", consume={"rpm": 50}):  # cached: u at 0, cascades
            pass
        later = repo._now_ms() + 60_000
        with (
            patch.object(repo, "_now_ms", return_value=later),
            patch.object(repo, "refill_from_cached_state") as refill,
        ):
            async with family.acquire("u", "r", consume={"rpm": 1}):
                pass
        refill.assert_not_called()

    @pytest.mark.parametrize(
        "case",
        [
            "eligible",  # the control: reaches the write, so every other case is a real "no"
            "vu",
            "quota",
            "window",
            "wcu_drained",
            "missing_limit",
            "config_limit_missing",
            "untrusted",
            "no_recent_slow_pass",
        ],
    )
    async def test_not_used_for_items_only_the_slow_path_materialises(self, repo, case):
        ns = repo._namespace_id
        configured = (
            [RPM, Limit.per_minute("tpm", 100)] if case == "config_limit_missing" else [RPM]
        )
        await repo.create_entity("u")  # a record with no parent
        await repo.set_limits("u", configured, resource="r")
        await repo.resolve_limits("u", "r")  # warm config cache: peek_limits answers
        now = repo._now_ms()
        rpm = _state(0)
        rpm.last_refill_ms = now - 60_000  # a full minute of refill pending
        states = [rpm]
        vu_ms = None
        consume = {"rpm": 1}
        if case == "vu":
            vu_ms = now + 3_600_000
        elif case == "quota":
            rpm.refill_amount_milli = 0
        elif case == "window":
            rpm.window_start_ms = now - 1000
            rpm.reset_after_seconds = 3600
        elif case == "wcu_drained":
            wcu = _state(0, name="wcu")
            wcu.last_refill_ms = now
            states.append(wcu)
        elif case == "missing_limit":
            consume = {"rpm": 1, "tpm": 1}
        clock = _Clock()
        repo._rejection_cache._clock = clock
        repo._rejection_cache.store(
            ns, "u", "r", 0, states, shard_count=1, vu_ms=vu_ms, ttl_epoch=None, disabled=False
        )
        if case != "no_recent_slow_pass":  # a slow pass re-read u's config just now
            repo._rejection_cache.note_slow_pass(ns, "u", "r")
        if case == "untrusted":
            # Older than one slow-pass window (option A), though a slow pass is recent.
            clock.now += repo._config_cache_ttl + 1
            repo._rejection_cache.note_slow_pass(ns, "u", "r")
        with patch.object(repo, "refill_from_cached_state", return_value=None) as refill:
            async with RateLimiter(repository=repo) as limiter:
                assert await limiter._refill_from_cache("u", "r", consume, now) is None
        if case == "eligible":
            refill.assert_called_once()
        else:
            refill.assert_not_called()

    async def test_with_ttl_off_the_write_leaves_ttl_alone(self, repo):
        """Multiplier 0: the slow path stamps no ttl, so neither does phase 3."""
        repo._bucket_ttl_refill_multiplier = 0
        await repo.create_entity("u")  # a record with no parent
        await repo.set_resource_defaults("r", [RPM])
        async with RateLimiter(repository=repo) as limiter:
            await _spent_and_cached(limiter)
            later = repo._now_ms() + 60_000
            with patch.object(repo, "_now_ms", return_value=later):
                async with _count_client_calls(repo) as calls:
                    async with limiter.acquire("u", "r", consume={"rpm": 1}):
                        pass
        assert calls == ["update_item"]
        assert "ttl" not in await _bucket_item(repo, "u")

    async def test_peek_is_none_while_any_level_is_uncached(self, repo):
        await repo.set_limits("u", [RPM], resource="r")
        assert repo._config_cache.peek_limits("u", "r") is None  # nothing resolved yet
        await repo.resolve_limits("u", "r")
        assert repo._config_cache.peek_limits("u", "r") is not None

    async def test_peek_is_none_when_no_level_has_limits(self, repo):
        await repo.resolve_limits("nobody", "r")  # every level cached, none with limits
        assert repo._config_cache.peek_limits("nobody", "r") is None

    async def test_an_unexpected_error_is_raised_not_swallowed(self, limiter, repo):
        from botocore.exceptions import ClientError

        await _spent_and_cached(limiter)
        client = await repo._get_client()
        boom = ClientError({"Error": {"Code": "ValidationException"}}, "UpdateItem")
        later = repo._now_ms() + 60_000
        with (
            patch.object(repo, "_now_ms", return_value=later),
            patch.object(client, "update_item", side_effect=boom),
        ):
            with pytest.raises(ClientError):
                await repo.refill_from_cached_state(
                    "u",
                    "r",
                    0,
                    {"rpm": 1000},
                    {"rpm": 0},
                    0,
                    later,
                    cached_tokens={"rpm": 0},
                    cached_shard_count=1,
                )

    async def test_an_entity_created_later_with_a_parent_is_not_trusted(self, repo):
        """Verification finding A: no record is not "no parent"; one can arrive later."""
        await repo.set_limits("u", [Limit.per_minute("rpm", 10)], resource="r")
        await repo.set_limits("org", [RPM], resource="r")  # the parent allows 2
        await repo.create_entity("org")
        repo._rejection_cache._clock = _Clock()
        async with RateLimiter(repository=repo) as limiter:
            async with limiter.acquire("org", "r", consume={"rpm": 1}):  # org: 1 left
                pass
            async with limiter.acquire("u", "r", consume={"rpm": 1}):  # u has no record yet
                pass
            async with limiter.acquire("u", "r", consume={"rpm": 6}):  # fast path: tk 3 cached
                pass
            assert (repo._namespace_id, "u") not in repo._record_parents
            other = Repository(
                name="test-rejection", region="us-east-1", _skip_deprecation_warning=True
            )
            other._namespace_id = repo._namespace_id
            await other.create_entity("u", parent_id="org", cascade=True)  # another process
            await other.close()
            later = repo._now_ms() + 12_000
            with (
                patch.object(repo, "_now_ms", return_value=later),
                patch.object(repo, "refill_from_cached_state") as refill,
            ):
                with pytest.raises(RateLimitExceeded):  # org cannot cover 4
                    async with limiter.acquire("u", "r", consume={"rpm": 4}):
                        pass
        refill.assert_not_called()

    async def test_a_missed_disable_stamp_is_re_checked_within_a_window(self, limiter, repo):
        """Verification finding B: phase 3 chains on stamps at most one config window."""
        clock = _Clock()
        repo._rejection_cache._clock = clock
        async with limiter.acquire("u", "r", consume={"rpm": 1}):  # slow path: noted at t0
            pass
        clock.now += repo._config_cache_ttl + 1  # the slow pass is now a window old...
        async with limiter.acquire("u", "r", consume={"rpm": 1}):  # ...this state is fresh
            pass
        other = Repository(
            name="test-rejection", region="us-east-1", _skip_deprecation_warning=True
        )
        other._namespace_id = repo._namespace_id
        await other.disable_resource("r")  # another process; its fan-out...
        await other.close()
        client = await repo._get_client()
        await client.update_item(  # ...misses u's bucket (ADR-125's documented race)
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(repo._namespace_id, "u", "r", 0)},
                "SK": {"S": schema.sk_state()},
            },
            UpdateExpression="REMOVE #d",
            ExpressionAttributeNames={"#d": schema.BUCKET_FIELD_DISABLED},
        )
        later = repo._now_ms() + 60_000
        with patch.object(repo, "_now_ms", return_value=later):
            with pytest.raises(ResourceDisabled):  # the slow path re-read config
                async with limiter.acquire("u", "r", consume={"rpm": 1}):
                    pass

    async def _refills(self, repo, limiter, *, consume=1):
        """Acquire a minute later through a spy; return what phase 3's write returned."""
        results: list[object] = []
        real = repo.refill_from_cached_state

        async def spy(*args, **kwargs):
            result = await real(*args, **kwargs)
            results.append(result)
            return result

        later = repo._now_ms() + 60_000
        with (
            patch.object(repo, "_now_ms", return_value=later),
            patch.object(repo, "refill_from_cached_state", side_effect=spy),
        ):
            try:
                async with limiter.acquire("u", "r", consume={"rpm": consume}):
                    pass
            except (RateLimitExceeded, ResourceDisabled) as exc:
                results.append(type(exc).__name__)
        return results

    async def _raw_update(self, repo, expression, names=None, values=None):
        client = await repo._get_client()
        kwargs = {
            "TableName": repo.table_name,
            "Key": {
                "PK": {"S": schema.pk_bucket(repo._namespace_id, "u", "r", 0)},
                "SK": {"S": schema.sk_state()},
            },
            "UpdateExpression": expression,
        }
        if names:
            kwargs["ExpressionAttributeNames"] = names
        if values:
            kwargs["ExpressionAttributeValues"] = values
        await client.update_item(**kwargs)

    async def test_a_disable_stamped_elsewhere_refuses_the_write(self, limiter, repo):
        """Verification finding C: the `disabled` pin, from another process."""
        await _spent_and_cached(limiter)
        await self._raw_update(
            repo,
            "SET #d = :t",
            {"#d": schema.BUCKET_FIELD_DISABLED},
            {":t": {"BOOL": True}},
        )
        results = await self._refills(repo, limiter)
        assert results == [None, "ResourceDisabled"]  # pinned write refused; 403 after

    async def test_an_expired_ttl_refuses_the_write(self, limiter, repo):
        """Verification finding C: the TTL pin, an item expired but not yet swept."""
        await _spent_and_cached(limiter)
        await self._raw_update(repo, "SET #t = :past", {"#t": "ttl"}, {":past": {"N": "1"}})
        results = await self._refills(repo, limiter)
        assert results[0] is None  # the pinned write refused; today's path took it

    async def test_a_debit_elsewhere_smaller_than_the_refill_is_still_refused(self, repo):
        """The floor rides at any sign: refill above the debit must not hide a debt.

        Inside the 1 s age cap: the bucket is drained, another process spends
        1000 more, and 0.9 s later a request for 1 projects as fitting (9 of
        refill). Without the floor the write admitted it at -992.
        """
        now = [1_800_000_000_000]
        await repo.create_entity("u")
        await repo.set_limits("u", [Limit.per_minute("rpm", 600)], resource="r")  # 10/s
        repo._rejection_cache._clock = lambda: now[0] / 1000
        results = []
        real_refill = repo.refill_from_cached_state

        async def refill_spy(*args, **kwargs):
            result = await real_refill(*args, **kwargs)
            results.append(result)
            return result

        with (
            patch("zae_limiter.config_cache.time.time", side_effect=lambda: now[0] / 1000),
            patch.object(repo, "_now_ms", side_effect=lambda: now[0]),
            patch.object(repo, "refill_from_cached_state", side_effect=refill_spy),
        ):
            async with RateLimiter(repository=repo) as limiter:
                async with limiter.acquire("u", "r", consume={"rpm": 1}):  # slow pass: creates
                    pass
                async with limiter.acquire("u", "r", consume={"rpm": 599}):  # fast path: tk 0
                    pass
                now[0] += 900
                client = await repo._get_client()
                await client.update_item(  # another process spends 1000
                    TableName=repo.table_name,
                    Key={
                        "PK": {"S": schema.pk_bucket(repo._namespace_id, "u", "r", 0)},
                        "SK": {"S": schema.sk_state()},
                    },
                    UpdateExpression="ADD #t :d",
                    ExpressionAttributeNames={"#t": "b_rpm_tk"},
                    ExpressionAttributeValues={":d": {"N": "-1000000"}},
                )
                with pytest.raises(RateLimitExceeded):  # today's path reads the debt
                    async with limiter.acquire("u", "r", consume={"rpm": 1}):
                        pass
        assert results == [None]  # phase 3 tried it; the floor refused it

    async def test_uses_a_state_older_than_the_rejection_age_cap(self, repo):
        """Option A: phase 3 takes a state past `ttl_seconds`; local rejection does not."""
        ns = repo._namespace_id
        await repo.create_entity("u")
        await repo.set_limits("u", [RPM], resource="r")
        await repo.resolve_limits("u", "r")  # warm the config cache
        now = repo._now_ms()
        state = replace(_state(0), last_refill_ms=now - 60_000)  # a minute of refill due
        clock = _Clock()
        repo._rejection_cache._clock = clock
        repo._rejection_cache.store(
            ns, "u", "r", 0, [state], shard_count=1, vu_ms=None, ttl_epoch=None, disabled=False
        )
        clock.now += 30  # past the 1 s cap, inside the 60 s window
        repo._rejection_cache.note_slow_pass(ns, "u", "r")
        assert not repo._rejection_cache.views(ns, "u", "r", now)  # never rejects from it
        with patch.object(repo, "refill_from_cached_state", return_value=None) as refill:
            async with RateLimiter(repository=repo) as limiter:
                await limiter._refill_from_cache("u", "r", {"rpm": 1}, now)
        refill.assert_called_once()

    async def _drained_to_50(self, repo, limiter, t0):
        """1000/min, drained to a cached 50 by a fast-path acquire at `t0`."""
        with patch.object(repo, "_now_ms", return_value=t0):
            async with limiter.acquire("u", "r", consume={"rpm": 900}):  # slow path: creates
                pass
            async with limiter.acquire("u", "r", consume={"rpm": 50}):  # fast path: 50 cached
                pass

    async def test_a_limit_cut_elsewhere_is_not_refilled_at_the_old_rate(self, repo):
        """Review of #700: a slow pass behind the stored `rf` clears the sync's `vu = 0`."""
        await repo.create_entity("u")
        await repo.set_limits("u", [Limit.per_minute("rpm", 1000)], resource="r")
        other = Repository(
            name="test-rejection", region="us-east-1", _skip_deprecation_warning=True
        )
        other._namespace_id = repo._namespace_id
        async with RateLimiter(repository=repo) as limiter, RateLimiter(repository=other) as l2:
            t0 = repo._now_ms()
            await self._drained_to_50(repo, limiter, t0)
            await other.set_limits("u", [Limit.per_minute("rpm", 10)], resource="r")  # vu = 0
            with patch.object(other, "_now_ms", return_value=t0):  # clock at the stored rf
                async with l2.acquire("u", "r", consume={"rpm": 1}):  # clears vu, rf unmoved
                    pass
            with patch.object(repo, "_now_ms", return_value=t0 + 30_000):
                with pytest.raises(RateLimitExceeded):  # 10/min cannot cover 400
                    async with limiter.acquire("u", "r", consume={"rpm": 400}):
                        pass
        await other.close()

    async def test_a_limit_cut_during_the_slow_pass_is_not_recorded_as_the_old(self, repo):
        """Review of #700: the recorded state carries what the slow path read."""
        await repo.create_entity("u")
        await repo.set_limits("u", [Limit.per_minute("rpm", 1000)], resource="r")
        async with RateLimiter(repository=repo) as limiter:
            t0 = repo._now_ms()
            await self._drained_to_50(repo, limiter, t0)
            client = await repo._get_client()
            real_write = repo.transact_write
            fired = []

            async def racing_write(items):
                if not fired:  # another process's param sync lands between read and write
                    fired.append(True)
                    await client.update_item(
                        TableName=repo.table_name,
                        Key={
                            "PK": {"S": schema.pk_bucket(repo._namespace_id, "u", "r", 0)},
                            "SK": {"S": schema.sk_state()},
                        },
                        UpdateExpression="SET #cp = :cp, #ra = :ra, vu = :zero",
                        ExpressionAttributeNames={
                            "#cp": schema.bucket_attr("rpm", schema.BUCKET_FIELD_CP),
                            "#ra": schema.bucket_attr("rpm", schema.BUCKET_FIELD_RA),
                        },
                        ExpressionAttributeValues={
                            ":cp": {"N": "10000"},
                            ":ra": {"N": "10000"},
                            ":zero": {"N": "0"},
                        },
                    )
                return await real_write(items)

            with (
                patch.object(repo, "_now_ms", return_value=t0 + 6_000),
                patch.object(limiter, "_refill_from_cache", return_value=None),
                patch.object(repo, "transact_write", side_effect=racing_write),
            ):
                try:  # the slow pass; its write loses the `vu` pin to the cut (#701)
                    async with limiter.acquire("u", "r", consume={"rpm": 100}):
                        pass
                except RateLimitExceeded:
                    pass  # the consumption-only retry: 50 cannot cover 100
            assert fired
            with patch.object(repo, "_now_ms", return_value=t0 + 36_000):
                with pytest.raises(RateLimitExceeded):  # 10/min cannot cover 400
                    async with limiter.acquire("u", "r", consume={"rpm": 400}):
                        pass

    async def test_a_refund_during_the_slow_pass_is_not_hidden(self, repo):
        """Review of #700: a refund between read and write must not be recorded away."""
        await repo.create_entity("u")
        await repo.set_limits("u", [Limit.per_minute("rpm", 100)], resource="r")
        async with RateLimiter(repository=repo) as limiter:
            t0 = repo._now_ms()
            with patch.object(repo, "_now_ms", return_value=t0):
                async with limiter.acquire("u", "r", consume={"rpm": 90}):  # tk 10
                    pass
            real_write = repo.transact_write
            fired = []

            async def refund_lands_first(items):
                if not fired:  # another lease in this process releases 50
                    fired.append(True)
                    await repo.write_each([repo.build_composite_adjust("u", "r", {"rpm": -50_000})])
                return await real_write(items)

            with (
                patch.object(repo, "_now_ms", return_value=t0 + 6_000),
                patch.object(repo, "transact_write", side_effect=refund_lands_first),
                patch.object(limiter, "_refill_from_cache", return_value=None),
            ):
                async with limiter.acquire("u", "r", consume={"rpm": 15}):  # the slow pass
                    pass
            assert fired
            with patch.object(repo, "_now_ms", return_value=t0 + 6_001):
                async with limiter.acquire("u", "r", consume={"rpm": 40}):  # 55 are there
                    pass

    def test_forgotten_since_tracks_forgets_clears_and_evictions(self):
        cache = RejectionCache(ttl_seconds=1.0, max_entries=1)
        mark = cache.mark()
        assert not cache.forgotten_since("ns", "u", "r", mark)
        cache.forget("ns", "u", "r", 0)
        assert cache.forgotten_since("ns", "u", "r", mark)
        assert not cache.forgotten_since("ns", "v", "r", mark)
        cache.forget("ns", "v", "r", 0)  # evicts u's record: the answer stays yes
        assert cache.forgotten_since("ns", "u", "r", mark)
        mark = cache.mark()
        cache.clear()
        assert cache.forgotten_since("ns", "w", "r", mark)

    async def test_get_entity_on_no_record_forgets_the_parent(self, repo):
        repo._record_parents[(repo._namespace_id, "ghost")] = None  # a stale answer
        assert await repo.get_entity("ghost") is None
        assert (repo._namespace_id, "ghost") not in repo._record_parents

    async def test_create_entity_in_this_process_records_the_parent(self, repo):
        await repo.create_entity("org")
        await repo.create_entity("u", parent_id="org", cascade=True)
        assert repo._record_parents[(repo._namespace_id, "u")] == "org"
        assert repo._record_parents[(repo._namespace_id, "org")] is None

    async def test_cascade_turned_on_elsewhere_still_debits_the_parent(self, repo):
        """Phase-3 review #1: the write pins `cascade` off; a policy change refuses it."""
        await repo.create_entity("org")
        await repo.create_entity("u", parent_id="org", cascade=False)
        await repo.set_limits("org", [Limit.per_minute("rpm", 100)], resource="r")
        await repo.set_limits("u", [RPM], resource="r")
        repo._rejection_cache._clock = _Clock()
        async with RateLimiter(repository=repo) as limiter:
            await _spent_and_cached(limiter)
            async with limiter.acquire("org", "r", consume={"rpm": 1}):  # org's bucket exists
                pass
            before = await _consumed(repo, "org")
            # Another process turns u's cascade on: its fan-out stamps `cascade`
            # without moving rf or vu, and this process's cache does not hear of it.
            other = Repository(
                name="test-rejection", region="us-east-1", _skip_deprecation_warning=True
            )
            other._namespace_id = repo._namespace_id
            await _stamp_current_lambdas(other)
            await other.set_entity_cascade("u", True, resource="r")
            await other.close()
            later = repo._now_ms() + 60_000
            with patch.object(repo, "_now_ms", return_value=later):
                async with limiter.acquire("u", "r", consume={"rpm": 1}):
                    pass
        assert await _consumed(repo, "org") - before == 1000  # the parent was debited

    async def test_a_pre_684_stamp_on_a_cascading_child_is_not_trusted(self, repo):
        """Phase-3 review #1b: `cascade=False` with no `parent_id` on a child that has one."""
        await repo.create_entity("org")
        await repo.create_entity("u", parent_id="org", cascade=True)
        await repo.set_limits("org", [Limit.per_minute("rpm", 100)], resource="r")
        await repo.set_limits("u", [RPM], resource="r")
        repo._rejection_cache._clock = _Clock()
        async with RateLimiter(repository=repo) as limiter:
            async with limiter.acquire("u", "r", consume={"rpm": 1}):  # slow path
                pass
            client = await repo._get_client()
            await client.update_item(  # the stamp an older version left on u's item
                TableName=repo.table_name,
                Key={
                    "PK": {"S": schema.pk_bucket(repo._namespace_id, "u", "r", 0)},
                    "SK": {"S": schema.sk_state()},
                },
                UpdateExpression="SET #c = :f REMOVE parent_id",
                ExpressionAttributeNames={"#c": "cascade"},
                ExpressionAttributeValues={":f": {"BOOL": False}},
            )
            async with limiter.acquire("u", "r", consume={"rpm": 1}):  # fast path caches it
                pass
            before = await _consumed(repo, "org")
            later = repo._now_ms() + 60_000
            with (
                patch.object(repo, "_now_ms", return_value=later),
                patch.object(repo, "refill_from_cached_state") as refill,
            ):
                async with limiter.acquire("u", "r", consume={"rpm": 1}):
                    pass
        refill.assert_not_called()
        assert await _consumed(repo, "org") - before == 1000  # the slow path cascaded

    async def test_a_refund_elsewhere_never_lifts_the_bucket_past_capacity(self, limiter, repo):
        """Phase-3 review #2: a credit ADDs tokens without moving rf; tk is pinned."""
        repo._rejection_cache._clock = _Clock()
        await _spent_and_cached(limiter)  # cached tk = 0
        client = await repo._get_client()
        await client.update_item(  # another process's lease releases 1 (rf untouched)
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(repo._namespace_id, "u", "r", 0)},
                "SK": {"S": schema.sk_state()},
            },
            UpdateExpression="ADD #t :d",
            ExpressionAttributeNames={"#t": schema.bucket_attr("rpm", schema.BUCKET_FIELD_TK)},
            ExpressionAttributeValues={":d": {"N": "1000"}},
        )
        admitted = 0
        later = repo._now_ms() + 60_000
        with patch.object(repo, "_now_ms", return_value=later):
            for amount in (2, 1, 1):  # all at one instant: capacity 2 bounds the total
                try:
                    async with limiter.acquire("u", "r", consume={"rpm": amount}):
                        admitted += amount
                except RateLimitExceeded:
                    pass
        assert admitted == 2

    async def test_a_doubling_elsewhere_refuses_the_write(self, repo):
        """Phase-3 review #3: shard_count moves without rf; it is pinned."""
        await repo.create_entity("u")  # a record with no parent
        await repo.set_limits("u", [Limit.per_minute("rpm", 100)], resource="r")
        repo._rejection_cache._clock = _Clock()
        async with RateLimiter(repository=repo) as limiter:
            async with limiter.acquire("u", "r", consume={"rpm": 50}):  # slow path
                pass
            async with limiter.acquire("u", "r", consume={"rpm": 50}):  # cached tk 0, count 1
                pass
            client = await repo._get_client()
            key = {
                "PK": {"S": schema.pk_bucket(repo._namespace_id, "u", "r", 0)},
                "SK": {"S": schema.sk_state()},
            }
            await client.update_item(  # another process's bump_shard_count on shard 0
                TableName=repo.table_name,
                Key=key,
                UpdateExpression="SET shard_count = :n",
                ExpressionAttributeValues={":n": {"N": "2"}},
            )
            results: list[object] = []
            real = repo.refill_from_cached_state

            async def spy(*args, **kwargs):
                result = await real(*args, **kwargs)
                results.append(result)
                return result

            later = repo._now_ms() + 60_000
            with (
                patch.object(repo, "_now_ms", return_value=later),
                patch.object(repo, "refill_from_cached_state", side_effect=spy),
                patch("zae_limiter.repository.random.randrange", return_value=0),
            ):
                async with limiter.acquire("u", "r", consume={"rpm": 50}):
                    pass
            item = (await client.get_item(TableName=repo.table_name, Key=key))["Item"]
        assert results == [None]  # the pinned write refused
        # The slow path refilled toward the new per-shard ceiling (50) and debited 50.
        assert int(item[schema.bucket_attr("rpm", schema.BUCKET_FIELD_TK)]["N"]) == 0

    async def test_a_resource_schedule_is_honoured(self, repo):
        """Phase-3 review #4: a resource-level schedule never reaches the item (no vu)."""
        from zae_limiter import ScheduleEntry

        four = Limit.per_minute("rpm", 4)
        await repo.set_resource_defaults("r", [four])
        repo._rejection_cache._clock = _Clock()
        async with RateLimiter(repository=repo) as limiter:
            for _ in range(4):  # spend the 4; the last fast-path image caches tk = 0
                async with limiter.acquire("u", "r", consume={"rpm": 1}):
                    pass
            halved = four.with_schedule((ScheduleEntry(cron="* * * * *", scale=0.5),))
            await repo.set_resource_defaults("r", [halved])  # clears the rejection cache...
            # ...but not this level of the config cache, which lags by its own TTL
            # on both paths alike; let it expire.
            await repo.invalidate_config_cache()
            # A read warms the config cache with the schedule and writes no bucket,
            # so u's item still has no `vu`: only the gate can see the schedule.
            await limiter.check_availability("u", "r")
            with pytest.raises(RateLimitExceeded):  # a fast rejection re-fills the cache
                async with limiter.acquire("u", "r", consume={"rpm": 1}):
                    pass
            item = await _bucket_item(repo, "u")
            assert "vu" not in item and _cached(repo)
            admitted = 0
            later = repo._now_ms() + 60_000
            with patch.object(repo, "_now_ms", return_value=later):
                for _ in range(5):
                    try:
                        async with limiter.acquire("u", "r", consume={"rpm": 1}):
                            admitted += 1
                    except RateLimitExceeded:
                        break
        assert admitted == 2  # the scheduled capacity, not the base 4

    async def test_a_cold_config_cache_takes_todays_path(self, repo):
        repo._config_cache._enabled = False
        await repo.set_limits("u", [RPM], resource="r")
        repo._rejection_cache._clock = _Clock()
        async with RateLimiter(repository=repo) as limiter:
            await _spent_and_cached(limiter)
            later = repo._now_ms() + 60_000
            with (
                patch.object(repo, "_now_ms", return_value=later),
                patch.object(repo, "refill_from_cached_state") as refill,
            ):
                async with limiter.acquire("u", "r", consume={"rpm": 1}):
                    pass
        refill.assert_not_called()

    async def test_the_ttl_is_refreshed_as_the_slow_path_would(self, repo):
        """Phase-3 review #5: a resource-level bucket's ttl moves forward."""
        await repo.create_entity("u")  # a record with no parent
        await repo.set_resource_defaults("r", [RPM])
        repo._rejection_cache._clock = _Clock()
        async with RateLimiter(repository=repo) as limiter:
            await _spent_and_cached(limiter)
            client = await repo._get_client()
            key = {
                "PK": {"S": schema.pk_bucket(repo._namespace_id, "u", "r", 0)},
                "SK": {"S": schema.sk_state()},
            }
            before = int(
                (await client.get_item(TableName=repo.table_name, Key=key))["Item"]["ttl"]["N"]
            )
            later = repo._now_ms() + 60_000
            with patch.object(repo, "_now_ms", return_value=later):
                async with _count_client_calls(repo) as calls:
                    async with limiter.acquire("u", "r", consume={"rpm": 1}):
                        pass
            after = int(
                (await client.get_item(TableName=repo.table_name, Key=key))["Item"]["ttl"]["N"]
            )
        assert calls == ["update_item"]  # phase 3 took it
        assert after == before + 60  # stamped from the write's instant

    def test_the_sync_limiter_refills_from_the_cache(self, mock_dynamodb):
        from contextlib import ExitStack

        from zae_limiter import SyncRateLimiter
        from zae_limiter.sync_repository import SyncRepository

        repo = SyncRepository(
            name="test-rejection-sync-refill", region="us-east-1", _skip_deprecation_warning=True
        )
        repo.create_table()
        repo._register_namespace("default")
        repo.create_entity("u")  # a record with no parent: phase 3 may trust it
        repo.set_limits("u", [RPM], resource="r")
        repo._rejection_cache._clock = _Clock()
        limiter = SyncRateLimiter(repository=repo)
        for _ in range(2):
            with limiter.acquire("u", "r", consume={"rpm": 1}):
                pass
        client = repo._get_client()
        calls: list[str] = []

        def counting(name):
            real = getattr(client, name)

            def call(*args, **kwargs):
                calls.append(name)
                return real(*args, **kwargs)

            return call

        later = repo._now_ms() + 60_000
        with ExitStack() as stack:
            stack.enter_context(patch.object(repo, "_now_ms", return_value=later))
            for name in _CLIENT_CALLS:
                stack.enter_context(patch.object(client, name, side_effect=counting(name)))
            with limiter.acquire("u", "r", consume={"rpm": 1}) as lease:
                pass
        assert calls == ["update_item"]
        assert lease.consumed == {"rpm": 1}
        repo.close()


def test_recording_written_states_skips_a_backend_without_the_cache():
    """A third-party backend has no `_rejection_cache`: nothing to record."""
    from types import SimpleNamespace
    from typing import cast

    from zae_limiter.lease import Lease
    from zae_limiter.repository_protocol import RepositoryProtocol

    lease = Lease(repository=cast(RepositoryProtocol, SimpleNamespace()))
    lease._record_written_states({("u", "r", 0): []}, [], condition_failed=True)


class TestSteadyLoad:
    """A minute of steady over-demand, with the cache on and off (ADR-147, #695).

    One request every 0.2 s, each needing a whole second of refill. The cache
    must admit exactly what DynamoDB alone admits, write far less, and never
    have a refill-from-cache write refused for a state its own slow pass made
    stale (the slow path records what its rf-locked write left).
    """

    async def test_a_state_spent_elsewhere_in_the_longer_window_is_refused(self, repo):
        """Option A: a 30 s old state is used, and the server refuses it when stale."""
        now = [1_800_000_000_000]
        await repo.create_entity("u")
        await repo.set_limits("u", [Limit.per_minute("rpm", 600)], resource="r")  # 10/s
        repo._rejection_cache._clock = lambda: now[0] / 1000
        results = []
        real_refill = repo.refill_from_cached_state

        async def refill_spy(*args, **kwargs):
            result = await real_refill(*args, **kwargs)
            results.append(result)
            return result

        with (
            patch("zae_limiter.config_cache.time.time", side_effect=lambda: now[0] / 1000),
            patch.object(repo, "_now_ms", side_effect=lambda: now[0]),
            patch.object(repo, "refill_from_cached_state", side_effect=refill_spy),
        ):
            async with RateLimiter(repository=repo) as limiter:
                async with limiter.acquire("u", "r", consume={"rpm": 1}):  # slow pass: creates
                    pass
                async with limiter.acquire("u", "r", consume={"rpm": 599}):  # fast path: tk 0
                    pass
                now[0] += 30_000  # 300 tokens of refill; the 1 s age cap is long past
                client = await repo._get_client()
                await client.update_item(  # another process spends 1000
                    TableName=repo.table_name,
                    Key={
                        "PK": {"S": schema.pk_bucket(repo._namespace_id, "u", "r", 0)},
                        "SK": {"S": schema.sk_state()},
                    },
                    UpdateExpression="ADD #t :d",
                    ExpressionAttributeNames={"#t": "b_rpm_tk"},
                    ExpressionAttributeValues={":d": {"N": "-1000000"}},
                )
                with pytest.raises(RateLimitExceeded):  # today's path reads the debt
                    async with limiter.acquire("u", "r", consume={"rpm": 10}):
                        pass
        assert results == [None]  # phase 3 tried the 30 s old state; the floor refused it

    @pytest.mark.parametrize("ttl", [0.0, 1.0, 2.0], ids=["cache-off", "ttl-1s", "ttl-2s"])
    async def test_admits_the_same_and_writes_less(self, repo, ttl):
        now = [1_800_000_000_000]
        await repo.create_entity("u")
        await repo.set_limits("u", [Limit.per_minute("rpm", 600)], resource="r")  # 10/s
        repo._rejection_cache.ttl_seconds = ttl
        repo._rejection_cache._clock = lambda: now[0] / 1000
        refused = []
        real_refill = repo.refill_from_cached_state

        async def refill_spy(*args, **kwargs):
            result = await real_refill(*args, **kwargs)
            refused.append(result is None)
            return result

        admitted = 0
        with (
            patch("zae_limiter.config_cache.time.time", side_effect=lambda: now[0] / 1000),
            patch.object(repo, "_now_ms", side_effect=lambda: now[0]),
            patch.object(repo, "refill_from_cached_state", side_effect=refill_spy),
        ):
            async with RateLimiter(repository=repo) as limiter:
                async with _count_client_calls(repo) as calls:
                    for _ in range(300):
                        try:
                            async with limiter.acquire("u", "r", consume={"rpm": 10}):
                                admitted += 1
                        except RateLimitExceeded:
                            pass
                        now[0] += 200
        writes = calls.count("update_item") + calls.count("transact_write_items")
        assert admitted == 119  # 60 from the full bucket, then one a second
        assert not any(refused)
        if ttl == 0.0:
            assert writes >= 300  # every request reaches DynamoDB
        else:
            assert writes < 200
        if ttl > 0:  # phase 3 trusts a state up to one slow-pass window old (option A)
            assert len(refused) >= 40
            assert calls.count("batch_get_item") <= 10
