"""Unit tests for config cache."""

import asyncio
import threading
import time
from unittest.mock import AsyncMock, Mock

import pytest

from zae_limiter.config_cache import _NO_CONFIG, CacheStats, ConfigCache
from zae_limiter.models import Limit


class TestCacheStats:
    """Tests for CacheStats dataclass."""

    def test_cache_stats_defaults(self) -> None:
        """Test CacheStats default values."""
        stats = CacheStats()
        assert stats.hits == 0
        assert stats.misses == 0
        assert stats.size == 0
        assert stats.ttl_seconds == 0

    def test_cache_stats_as_dict(self) -> None:
        """Test CacheStats.as_dict() returns expected format."""
        stats = CacheStats(hits=100, misses=10, size=5, ttl_seconds=60)
        result = stats.as_dict()

        assert result == {"hits": 100, "misses": 10, "size": 5, "ttl": 60}


class TestConfigCacheBasics:
    """Tests for ConfigCache basic functionality."""

    def test_cache_enabled_by_default(self) -> None:
        """Test cache is enabled with default TTL."""
        cache = ConfigCache()
        assert cache.enabled is True
        assert cache.ttl_seconds == 60

    def test_cache_disabled_when_ttl_zero(self) -> None:
        """Test TTL=0 disables caching."""
        cache = ConfigCache(ttl_seconds=0)
        assert cache.enabled is False

    def test_get_stats_empty_cache(self) -> None:
        """Test get_stats on empty cache."""
        cache = ConfigCache(ttl_seconds=60)
        stats = cache.get_stats()

        assert stats.hits == 0
        assert stats.misses == 0
        assert stats.size == 0
        assert stats.ttl_seconds == 60


class TestConfigCacheSystemDefaults:
    """Tests for system defaults caching."""

    @pytest.mark.asyncio
    async def test_system_defaults_cache_miss(self) -> None:
        """Test cache miss for system defaults."""
        cache = ConfigCache(ttl_seconds=60)

        limits = [Limit.per_minute("tpm", 10000)]
        fetch_fn = AsyncMock(return_value=(limits, "allow"))

        result = await cache.get_system_defaults(fetch_fn)

        assert result == (limits, "allow")
        fetch_fn.assert_called_once()

        stats = cache.get_stats()
        assert stats.misses == 1
        assert stats.hits == 0

    @pytest.mark.asyncio
    async def test_system_defaults_cache_hit(self) -> None:
        """Test cache hit for system defaults."""
        cache = ConfigCache(ttl_seconds=60)

        limits = [Limit.per_minute("tpm", 10000)]
        fetch_fn = AsyncMock(return_value=(limits, "allow"))

        # First call - miss
        await cache.get_system_defaults(fetch_fn)
        # Second call - hit
        result = await cache.get_system_defaults(fetch_fn)

        assert result == (limits, "allow")
        fetch_fn.assert_called_once()  # Only called once

        stats = cache.get_stats()
        assert stats.misses == 1
        assert stats.hits == 1

    @pytest.mark.asyncio
    async def test_system_defaults_cache_expired(self) -> None:
        """Test cache expiration for system defaults."""
        cache = ConfigCache(ttl_seconds=1)  # 1 second TTL

        limits1 = [Limit.per_minute("tpm", 10000)]
        limits2 = [Limit.per_minute("tpm", 20000)]
        fetch_fn = AsyncMock(side_effect=[(limits1, "allow"), (limits2, "block")])

        # First call - miss
        result1 = await cache.get_system_defaults(fetch_fn)
        assert result1 == (limits1, "allow")

        # Wait for expiration
        await asyncio.sleep(1.1)

        # Second call - expired, new fetch
        result2 = await cache.get_system_defaults(fetch_fn)
        assert result2 == (limits2, "block")

        assert fetch_fn.call_count == 2

    @pytest.mark.asyncio
    async def test_system_defaults_disabled_cache(self) -> None:
        """Test system defaults with disabled cache (TTL=0)."""
        cache = ConfigCache(ttl_seconds=0)

        limits = [Limit.per_minute("tpm", 10000)]
        fetch_fn = AsyncMock(return_value=(limits, "allow"))

        # Each call should fetch
        await cache.get_system_defaults(fetch_fn)
        await cache.get_system_defaults(fetch_fn)

        assert fetch_fn.call_count == 2

    def test_system_defaults_sync_cache_hit(self) -> None:
        """Test sync cache hit for system defaults."""
        cache = ConfigCache(ttl_seconds=60)

        limits = [Limit.per_minute("tpm", 10000)]
        fetch_fn = Mock(return_value=(limits, "allow"))

        # First call - miss
        cache.get_system_defaults_sync(fetch_fn)
        # Second call - hit
        result = cache.get_system_defaults_sync(fetch_fn)

        assert result == (limits, "allow")
        fetch_fn.assert_called_once()


class TestConfigCacheResourceDefaults:
    """Tests for resource defaults caching."""

    @pytest.mark.asyncio
    async def test_resource_defaults_cache_miss(self) -> None:
        """Test cache miss for resource defaults."""
        cache = ConfigCache(ttl_seconds=60)

        limits = [Limit.per_minute("tpm", 10000)]
        fetch_fn = AsyncMock(return_value=limits)

        result = await cache.get_resource_defaults("gpt-4", fetch_fn)

        assert result == limits
        fetch_fn.assert_called_once_with("gpt-4")

    @pytest.mark.asyncio
    async def test_resource_defaults_cache_hit(self) -> None:
        """Test cache hit for resource defaults."""
        cache = ConfigCache(ttl_seconds=60)

        limits = [Limit.per_minute("tpm", 10000)]
        fetch_fn = AsyncMock(return_value=limits)

        # First call - miss
        await cache.get_resource_defaults("gpt-4", fetch_fn)
        # Second call - hit
        result = await cache.get_resource_defaults("gpt-4", fetch_fn)

        assert result == limits
        fetch_fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_resource_defaults_different_resources(self) -> None:
        """Test separate cache entries for different resources."""
        cache = ConfigCache(ttl_seconds=60)

        limits_gpt4 = [Limit.per_minute("tpm", 10000)]
        limits_gpt35 = [Limit.per_minute("tpm", 5000)]

        async def fetch_fn(resource: str) -> list[Limit]:
            if resource == "gpt-4":
                return limits_gpt4
            return limits_gpt35

        fetch_mock = AsyncMock(side_effect=fetch_fn)

        # Different resources should have separate cache entries
        result1 = await cache.get_resource_defaults("gpt-4", fetch_mock)
        result2 = await cache.get_resource_defaults("gpt-3.5", fetch_mock)
        result3 = await cache.get_resource_defaults("gpt-4", fetch_mock)  # Cache hit

        assert result1 == limits_gpt4
        assert result2 == limits_gpt35
        assert result3 == limits_gpt4

        # Only 2 fetch calls (gpt-4 and gpt-3.5)
        assert fetch_mock.call_count == 2

    def test_resource_defaults_sync_cache_hit(self) -> None:
        """Test sync cache hit for resource defaults."""
        cache = ConfigCache(ttl_seconds=60)

        limits = [Limit.per_minute("tpm", 10000)]
        fetch_fn = Mock(return_value=limits)

        # First call - miss
        cache.get_resource_defaults_sync("gpt-4", fetch_fn)
        # Second call - hit
        result = cache.get_resource_defaults_sync("gpt-4", fetch_fn)

        assert result == limits
        fetch_fn.assert_called_once()


class TestConfigCacheEntityLimits:
    """Tests for entity limits caching with negative caching."""

    @pytest.mark.asyncio
    async def test_entity_limits_cache_miss(self) -> None:
        """Test cache miss for entity limits."""
        cache = ConfigCache(ttl_seconds=60)

        limits = [Limit.per_minute("tpm", 10000)]
        fetch_fn = AsyncMock(return_value=limits)

        result = await cache.get_entity_limits("user-1", "gpt-4", fetch_fn)

        assert result == limits
        fetch_fn.assert_called_once_with("user-1", "gpt-4")

    @pytest.mark.asyncio
    async def test_entity_limits_cache_hit(self) -> None:
        """Test cache hit for entity limits."""
        cache = ConfigCache(ttl_seconds=60)

        limits = [Limit.per_minute("tpm", 10000)]
        fetch_fn = AsyncMock(return_value=limits)

        # First call - miss
        await cache.get_entity_limits("user-1", "gpt-4", fetch_fn)
        # Second call - hit
        result = await cache.get_entity_limits("user-1", "gpt-4", fetch_fn)

        assert result == limits
        fetch_fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_entity_limits_negative_caching(self) -> None:
        """Test negative caching for entities without config."""
        cache = ConfigCache(ttl_seconds=60)

        # Return empty list (no config)
        fetch_fn = AsyncMock(return_value=[])

        # First call - miss, stores negative cache
        result1 = await cache.get_entity_limits("user-1", "gpt-4", fetch_fn)
        assert result1 == []

        # Second call - hit from negative cache
        result2 = await cache.get_entity_limits("user-1", "gpt-4", fetch_fn)
        assert result2 == []

        # Only one fetch
        fetch_fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_entity_limits_different_entities(self) -> None:
        """Test separate cache entries for different entity×resource pairs."""
        cache = ConfigCache(ttl_seconds=60)

        limits = [Limit.per_minute("tpm", 10000)]
        fetch_fn = AsyncMock(return_value=limits)

        # Different entity×resource pairs
        await cache.get_entity_limits("user-1", "gpt-4", fetch_fn)
        await cache.get_entity_limits("user-1", "gpt-3.5", fetch_fn)
        await cache.get_entity_limits("user-2", "gpt-4", fetch_fn)

        # 3 separate cache entries
        assert fetch_fn.call_count == 3

    def test_entity_limits_sync_cache_hit(self) -> None:
        """Test sync cache hit for entity limits."""
        cache = ConfigCache(ttl_seconds=60)

        limits = [Limit.per_minute("tpm", 10000)]
        fetch_fn = Mock(return_value=limits)

        # First call - miss
        cache.get_entity_limits_sync("user-1", "gpt-4", fetch_fn)
        # Second call - hit
        result = cache.get_entity_limits_sync("user-1", "gpt-4", fetch_fn)

        assert result == limits
        fetch_fn.assert_called_once()

    def test_entity_limits_sync_negative_caching(self) -> None:
        """Test negative caching in sync mode."""
        cache = ConfigCache(ttl_seconds=60)

        fetch_fn = Mock(return_value=[])

        # First call - miss
        result1 = cache.get_entity_limits_sync("user-1", "gpt-4", fetch_fn)
        # Second call - negative cache hit
        result2 = cache.get_entity_limits_sync("user-1", "gpt-4", fetch_fn)

        assert result1 == []
        assert result2 == []
        fetch_fn.assert_called_once()


class TestConfigCacheInvalidation:
    """Tests for cache invalidation."""

    @pytest.mark.asyncio
    async def test_invalidate_clears_all_entries(self) -> None:
        """Test that invalidate() clears all cache entries."""
        cache = ConfigCache(ttl_seconds=60)

        # Populate cache
        system_fetch = AsyncMock(return_value=([Limit.per_minute("tpm", 10000)], "allow"))
        resource_fetch = AsyncMock(return_value=[Limit.per_minute("tpm", 5000)])
        entity_fetch = AsyncMock(return_value=[Limit.per_minute("tpm", 1000)])

        await cache.get_system_defaults(system_fetch)
        await cache.get_resource_defaults("gpt-4", resource_fetch)
        await cache.get_entity_limits("user-1", "gpt-4", entity_fetch)

        stats = cache.get_stats()
        assert stats.size == 3

        # Invalidate
        cache.invalidate()

        stats = cache.get_stats()
        assert stats.size == 0

        # Next calls should be misses
        await cache.get_system_defaults(system_fetch)
        await cache.get_resource_defaults("gpt-4", resource_fetch)
        await cache.get_entity_limits("user-1", "gpt-4", entity_fetch)

        # Each fetch should be called twice
        assert system_fetch.call_count == 2
        assert resource_fetch.call_count == 2
        assert entity_fetch.call_count == 2

    @pytest.mark.asyncio
    async def test_invalidate_async_clears_all_entries(self) -> None:
        """Test that invalidate_async() clears all cache entries."""
        cache = ConfigCache(ttl_seconds=60)

        # Populate cache
        system_fetch = AsyncMock(return_value=([Limit.per_minute("tpm", 10000)], "allow"))
        await cache.get_system_defaults(system_fetch)

        stats = cache.get_stats()
        assert stats.size == 1

        # Invalidate async
        await cache.invalidate_async()

        stats = cache.get_stats()
        assert stats.size == 0


class TestConfigCacheStats:
    """Tests for cache statistics."""

    @pytest.mark.asyncio
    async def test_stats_track_hits_and_misses(self) -> None:
        """Test that stats accurately track hits and misses."""
        cache = ConfigCache(ttl_seconds=60)

        fetch_fn = AsyncMock(return_value=([Limit.per_minute("tpm", 10000)], "allow"))

        # 3 misses
        await cache.get_system_defaults(fetch_fn)
        cache.invalidate()
        await cache.get_system_defaults(fetch_fn)
        cache.invalidate()
        await cache.get_system_defaults(fetch_fn)

        # 5 hits
        for _ in range(5):
            await cache.get_system_defaults(fetch_fn)

        stats = cache.get_stats()
        assert stats.misses == 3
        assert stats.hits == 5

    @pytest.mark.asyncio
    async def test_stats_track_size(self) -> None:
        """Test that stats accurately track cache size."""
        cache = ConfigCache(ttl_seconds=60)

        system_fetch = AsyncMock(return_value=([Limit.per_minute("tpm", 10000)], "allow"))
        resource_fetch = AsyncMock(return_value=[Limit.per_minute("tpm", 5000)])
        entity_fetch = AsyncMock(return_value=[Limit.per_minute("tpm", 1000)])

        await cache.get_system_defaults(system_fetch)
        assert cache.get_stats().size == 1

        await cache.get_resource_defaults("gpt-4", resource_fetch)
        assert cache.get_stats().size == 2

        await cache.get_entity_limits("user-1", "gpt-4", entity_fetch)
        assert cache.get_stats().size == 3


class TestConfigCacheThreadSafety:
    """Tests for thread safety."""

    def test_sync_operations_are_thread_safe(self) -> None:
        """Test that sync operations are thread-safe."""
        cache = ConfigCache(ttl_seconds=60)
        call_count = 0
        lock = threading.Lock()

        def fetch_fn() -> tuple[list[Limit], str | None]:
            nonlocal call_count
            with lock:
                call_count += 1
            time.sleep(0.01)  # Simulate slow fetch
            return [Limit.per_minute("tpm", 10000)], "allow"

        results: list[tuple[list[Limit], str | None]] = []

        def worker() -> None:
            for _ in range(10):
                result = cache.get_system_defaults_sync(fetch_fn)
                with lock:
                    results.append(result)

        # Start multiple threads
        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All results should be the same
        assert len(results) == 50
        expected = ([Limit.per_minute("tpm", 10000)], "allow")
        for result in results:
            assert result == expected

        # Fetch should be called once (other calls should be cache hits)
        # Due to race conditions at startup, might be called a few times
        assert call_count >= 1
        assert call_count < 10  # Much less than 50


class TestConfigCacheAsyncSafety:
    """Tests for async safety."""

    @pytest.mark.asyncio
    async def test_async_operations_are_concurrent_safe(self) -> None:
        """Test that async operations are safe under concurrency."""
        cache = ConfigCache(ttl_seconds=60)
        call_count = 0

        async def fetch_fn() -> tuple[list[Limit], str | None]:
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.01)  # Simulate slow fetch
            return [Limit.per_minute("tpm", 10000)], "allow"

        async def worker() -> list[tuple[list[Limit], str | None]]:
            results = []
            for _ in range(10):
                result = await cache.get_system_defaults(fetch_fn)
                results.append(result)
            return results

        # Run multiple concurrent workers
        worker_results = await asyncio.gather(*[worker() for _ in range(5)])

        # All results should be the same
        expected = ([Limit.per_minute("tpm", 10000)], "allow")
        for results in worker_results:
            assert len(results) == 10
            for result in results:
                assert result == expected

        # Fetch should be called once (due to async lock)
        # May be called a few times due to initial race
        assert call_count >= 1
        assert call_count < 10  # Much less than 50


class TestConfigCacheDisabledBranches:
    """Tests for cache disabled branches (TTL=0) to ensure 100% coverage."""

    def test_system_defaults_sync_disabled_cache(self) -> None:
        """Test sync system defaults with disabled cache (TTL=0)."""
        cache = ConfigCache(ttl_seconds=0)

        limits = [Limit.per_minute("tpm", 10000)]
        fetch_fn = Mock(return_value=(limits, "allow"))

        # Each call should fetch (no caching)
        cache.get_system_defaults_sync(fetch_fn)
        cache.get_system_defaults_sync(fetch_fn)

        assert fetch_fn.call_count == 2

    @pytest.mark.asyncio
    async def test_resource_defaults_disabled_cache(self) -> None:
        """Test async resource defaults with disabled cache (TTL=0)."""
        cache = ConfigCache(ttl_seconds=0)

        limits = [Limit.per_minute("rpm", 1000)]
        fetch_fn = AsyncMock(return_value=limits)

        # Each call should fetch (no caching)
        await cache.get_resource_defaults("gpt-4", fetch_fn)
        await cache.get_resource_defaults("gpt-4", fetch_fn)

        assert fetch_fn.call_count == 2

    def test_resource_defaults_sync_disabled_cache(self) -> None:
        """Test sync resource defaults with disabled cache (TTL=0)."""
        cache = ConfigCache(ttl_seconds=0)

        limits = [Limit.per_minute("rpm", 1000)]
        fetch_fn = Mock(return_value=limits)

        # Each call should fetch (no caching)
        cache.get_resource_defaults_sync("gpt-4", fetch_fn)
        cache.get_resource_defaults_sync("gpt-4", fetch_fn)

        assert fetch_fn.call_count == 2

    @pytest.mark.asyncio
    async def test_entity_limits_disabled_cache(self) -> None:
        """Test async entity limits with disabled cache (TTL=0)."""
        cache = ConfigCache(ttl_seconds=0)

        limits = [Limit.per_minute("rpm", 500)]
        fetch_fn = AsyncMock(return_value=limits)

        # Each call should fetch (no caching)
        await cache.get_entity_limits("user-1", "gpt-4", fetch_fn)
        await cache.get_entity_limits("user-1", "gpt-4", fetch_fn)

        assert fetch_fn.call_count == 2

    def test_entity_limits_sync_disabled_cache(self) -> None:
        """Test sync entity limits with disabled cache (TTL=0)."""
        cache = ConfigCache(ttl_seconds=0)

        limits = [Limit.per_minute("rpm", 500)]
        fetch_fn = Mock(return_value=limits)

        # Each call should fetch (no caching)
        cache.get_entity_limits_sync("user-1", "gpt-4", fetch_fn)
        cache.get_entity_limits_sync("user-1", "gpt-4", fetch_fn)

        assert fetch_fn.call_count == 2


class TestConfigCacheNegativeCachingSentinel:
    """Tests for negative caching sentinel value."""

    def test_no_config_sentinel_is_unique(self) -> None:
        """Test that _NO_CONFIG sentinel is a unique object."""
        assert _NO_CONFIG is not None
        assert _NO_CONFIG != []
        assert _NO_CONFIG != []


class TestConfigCacheResolveLimits:
    """Tests for batched config resolution (issue #298)."""

    @pytest.mark.asyncio
    async def test_all_cached_no_batch_call(self) -> None:
        """When all slots are cached, no batch call is made."""
        cache = ConfigCache(ttl_seconds=60)

        # Pre-populate all 4 cache slots
        entity_limits = [Limit.per_minute("rpm", 100)]
        cache._entity_limits[("user-1", "gpt-4")] = cache._make_entry(entity_limits)
        cache._entity_limits[("user-1", "_default_")] = cache._make_entry(_NO_CONFIG)
        cache._resource_defaults["gpt-4"] = cache._make_entry([])
        cache._system_defaults = cache._make_entry(([], None))

        batch_fn = AsyncMock(return_value={})

        limits, on_unavailable, source = await cache.resolve_limits("user-1", "gpt-4", batch_fn)

        assert limits == entity_limits
        assert source == "entity"
        batch_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_all_missed_single_batch_call(self) -> None:
        """When all slots miss, one batch call fetches everything."""
        from zae_limiter import schema

        cache = ConfigCache(ttl_seconds=60)

        system_limits = [Limit.per_minute("rpm", 1000)]

        async def batch_fn(keys):
            return {(schema.pk_system(), schema.sk_config()): (system_limits, "allow")}

        limits, on_unavailable, source = await cache.resolve_limits("user-1", "gpt-4", batch_fn)

        assert limits is not None
        assert len(limits) == 1
        assert limits[0].name == "rpm"
        assert limits[0].capacity == 1000
        assert on_unavailable == "allow"
        assert source == "system"

    @pytest.mark.asyncio
    async def test_entity_precedence_over_system(self) -> None:
        """Entity config takes precedence over system config."""
        from zae_limiter import schema

        cache = ConfigCache(ttl_seconds=60)

        entity_limits = [Limit.per_minute("rpm", 100)]
        system_limits = [Limit.per_minute("rpm", 1000)]

        async def batch_fn(keys):
            result = {}
            for pk, sk in keys:
                if pk == schema.pk_entity("user-1") and sk == schema.sk_config("gpt-4"):
                    result[(pk, sk)] = (entity_limits, None)
                elif pk == schema.pk_system() and sk == schema.sk_config():
                    result[(pk, sk)] = (system_limits, None)
            return result

        limits, _, source = await cache.resolve_limits("user-1", "gpt-4", batch_fn)

        assert limits is not None
        assert limits[0].capacity == 100
        assert source == "entity"

    @pytest.mark.asyncio
    async def test_partial_cache_only_fetches_misses(self) -> None:
        """When entity is cached (negative), only resource/system are fetched."""
        from zae_limiter import schema

        cache = ConfigCache(ttl_seconds=60)

        # Entity has no config (negative cache)
        cache._entity_limits[("user-1", "gpt-4")] = cache._make_entry(_NO_CONFIG)
        cache._entity_limits[("user-1", "_default_")] = cache._make_entry(_NO_CONFIG)

        resource_limits = [Limit.per_minute("rpm", 500)]

        called_keys: list[tuple[str, str]] = []

        async def batch_fn(keys):
            called_keys.extend(keys)
            result = {}
            for pk, sk in keys:
                if pk == schema.pk_resource("gpt-4"):
                    result[(pk, sk)] = (resource_limits, None)
            return result

        limits, _, source = await cache.resolve_limits("user-1", "gpt-4", batch_fn)

        assert limits is not None
        assert limits[0].capacity == 500
        assert source == "resource"
        # Verify entity keys were NOT fetched (they were in cache)
        entity_pk = schema.pk_entity("user-1")
        fetched_pks = [pk for pk, _ in called_keys]
        assert entity_pk not in fetched_pks

    @pytest.mark.asyncio
    async def test_negative_caching_stored_for_entity_miss(self) -> None:
        """When entity has no config in DynamoDB, negative cache is stored."""
        from zae_limiter import schema

        cache = ConfigCache(ttl_seconds=60)

        system_limits = [Limit.per_minute("rpm", 1000)]

        call_count = 0

        async def batch_fn(keys):
            nonlocal call_count
            call_count += 1
            return {(schema.pk_system(), schema.sk_config()): (system_limits, None)}

        # First call
        limits1, _, source1 = await cache.resolve_limits("user-1", "gpt-4", batch_fn)
        assert limits1 is not None
        assert source1 == "system"
        assert call_count == 1

        # Second call - should be all cache hits, no batch call
        limits2, _, source2 = await cache.resolve_limits("user-1", "gpt-4", batch_fn)
        assert limits2 is not None
        assert source2 == "system"
        assert call_count == 1  # No additional batch call

    @pytest.mark.asyncio
    async def test_nothing_found_returns_none(self) -> None:
        """When no config exists at any level, returns None."""
        cache = ConfigCache(ttl_seconds=60)

        async def batch_fn(keys):
            return {}  # Nothing found

        limits, on_unavailable, source = await cache.resolve_limits("user-1", "gpt-4", batch_fn)

        assert limits is None
        assert source is None

    @pytest.mark.asyncio
    async def test_disabled_cache_batches_every_time(self) -> None:
        """With TTL=0, every call makes a batch request."""
        from zae_limiter import schema

        cache = ConfigCache(ttl_seconds=0)

        system_limits = [Limit.per_minute("rpm", 1000)]
        call_count = 0

        async def batch_fn(keys):
            nonlocal call_count
            call_count += 1
            return {(schema.pk_system(), schema.sk_config()): (system_limits, None)}

        await cache.resolve_limits("user-1", "gpt-4", batch_fn)
        await cache.resolve_limits("user-1", "gpt-4", batch_fn)

        assert call_count == 2

    @pytest.mark.asyncio
    async def test_entity_default_fallback(self) -> None:
        """Entity _default_ config is used when resource-specific is missing."""
        from zae_limiter import schema

        cache = ConfigCache(ttl_seconds=60)

        entity_default_limits = [Limit.per_minute("rpm", 200)]

        async def batch_fn(keys):
            result = {}
            for pk, sk in keys:
                if pk == schema.pk_entity("user-1") and sk == schema.sk_config("_default_"):
                    result[(pk, sk)] = (entity_default_limits, None)
            return result

        limits, _, source = await cache.resolve_limits("user-1", "gpt-4", batch_fn)

        assert limits is not None
        assert limits[0].capacity == 200
        assert source == "entity_default"

    @pytest.mark.asyncio
    async def test_check_cache_slot_unknown_type(self) -> None:
        """_check_cache_slot returns (False, None) for unknown slot types."""
        cache = ConfigCache(ttl_seconds=60)
        is_cached, value = cache._check_cache_slot("unknown")
        assert is_cached is False
        assert value is None

    @pytest.mark.asyncio
    async def test_empty_limits_entity_negative_cached(self) -> None:
        """When entity config exists but has empty limits, negative cache is stored."""
        from zae_limiter import schema

        cache = ConfigCache(ttl_seconds=60)

        system_limits = [Limit.per_minute("rpm", 1000)]

        async def batch_fn(keys):
            result: dict = {}
            for pk, sk in keys:
                if pk == schema.pk_entity("user-1") and sk == schema.sk_config("gpt-4"):
                    result[(pk, sk)] = ([], None)  # Entity exists but empty limits
                elif pk == schema.pk_entity("user-1") and sk == schema.sk_config("_default_"):
                    result[(pk, sk)] = ([], None)  # Entity default exists but empty limits
                elif pk == schema.pk_system() and sk == schema.sk_config():
                    result[(pk, sk)] = (system_limits, None)
            return result

        limits, _, source = await cache.resolve_limits("user-1", "gpt-4", batch_fn)

        assert limits is not None
        assert source == "system"  # Falls through to system since entity limits are empty

    @pytest.mark.asyncio
    async def test_uncached_nothing_found(self) -> None:
        """_evaluate_uncached returns None when no items have limits."""
        cache = ConfigCache(ttl_seconds=0)  # Disabled cache -> uncached path

        async def batch_fn(keys):
            return {}

        limits, on_unavailable, source = await cache.resolve_limits("user-1", "gpt-4", batch_fn)

        assert limits is None
        assert source is None
