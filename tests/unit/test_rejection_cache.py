"""Tests for the client-side rejection cache (ADR-147, #695).

A failed conditional write costs 1 WCU, so a repeat rejection the client can
already predict from the last bucket state it saw must not reach DynamoDB. The
cache may only reject, never admit.
"""

from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest

from zae_limiter import RateLimiter, RateLimitExceeded
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
        async with _count_update_items(repo) as calls:
            with pytest.raises(RateLimitExceeded) as excinfo:
                async with family.acquire("u", "r", consume={"rpm": 1}):
                    pass
        assert calls == []
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
