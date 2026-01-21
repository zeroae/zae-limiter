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

        limits = [Limit.per_minute("tpm",10000)]
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

        limits = [Limit.per_minute("tpm",10000)]
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

        limits1 = [Limit.per_minute("tpm",10000)]
        limits2 = [Limit.per_minute("tpm",20000)]
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

        limits = [Limit.per_minute("tpm",10000)]
        fetch_fn = AsyncMock(return_value=(limits, "allow"))

        # Each call should fetch
        await cache.get_system_defaults(fetch_fn)
        await cache.get_system_defaults(fetch_fn)

        assert fetch_fn.call_count == 2

    def test_system_defaults_sync_cache_hit(self) -> None:
        """Test sync cache hit for system defaults."""
        cache = ConfigCache(ttl_seconds=60)

        limits = [Limit.per_minute("tpm",10000)]
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

        limits = [Limit.per_minute("tpm",10000)]
        fetch_fn = AsyncMock(return_value=limits)

        result = await cache.get_resource_defaults("gpt-4", fetch_fn)

        assert result == limits
        fetch_fn.assert_called_once_with("gpt-4")

    @pytest.mark.asyncio
    async def test_resource_defaults_cache_hit(self) -> None:
        """Test cache hit for resource defaults."""
        cache = ConfigCache(ttl_seconds=60)

        limits = [Limit.per_minute("tpm",10000)]
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

        limits_gpt4 = [Limit.per_minute("tpm",10000)]
        limits_gpt35 = [Limit.per_minute("tpm",5000)]

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

        limits = [Limit.per_minute("tpm",10000)]
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

        limits = [Limit.per_minute("tpm",10000)]
        fetch_fn = AsyncMock(return_value=limits)

        result = await cache.get_entity_limits("user-1", "gpt-4", fetch_fn)

        assert result == limits
        fetch_fn.assert_called_once_with("user-1", "gpt-4")

    @pytest.mark.asyncio
    async def test_entity_limits_cache_hit(self) -> None:
        """Test cache hit for entity limits."""
        cache = ConfigCache(ttl_seconds=60)

        limits = [Limit.per_minute("tpm",10000)]
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

        limits = [Limit.per_minute("tpm",10000)]
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

        limits = [Limit.per_minute("tpm",10000)]
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
        system_fetch = AsyncMock(return_value=([Limit.per_minute("tpm",10000)], "allow"))
        resource_fetch = AsyncMock(return_value=[Limit.per_minute("tpm",5000)])
        entity_fetch = AsyncMock(return_value=[Limit.per_minute("tpm",1000)])

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
        system_fetch = AsyncMock(return_value=([Limit.per_minute("tpm",10000)], "allow"))
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

        fetch_fn = AsyncMock(return_value=([Limit.per_minute("tpm",10000)], "allow"))

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

        system_fetch = AsyncMock(return_value=([Limit.per_minute("tpm",10000)], "allow"))
        resource_fetch = AsyncMock(return_value=[Limit.per_minute("tpm",5000)])
        entity_fetch = AsyncMock(return_value=[Limit.per_minute("tpm",1000)])

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
            return [Limit.per_minute("tpm",10000)], "allow"

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
        expected = ([Limit.per_minute("tpm",10000)], "allow")
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
            return [Limit.per_minute("tpm",10000)], "allow"

        async def worker() -> list[tuple[list[Limit], str | None]]:
            results = []
            for _ in range(10):
                result = await cache.get_system_defaults(fetch_fn)
                results.append(result)
            return results

        # Run multiple concurrent workers
        worker_results = await asyncio.gather(*[worker() for _ in range(5)])

        # All results should be the same
        expected = ([Limit.per_minute("tpm",10000)], "allow")
        for results in worker_results:
            assert len(results) == 10
            for result in results:
                assert result == expected

        # Fetch should be called once (due to async lock)
        # May be called a few times due to initial race
        assert call_count >= 1
        assert call_count < 10  # Much less than 50


class TestConfigCacheNegativeCachingSentinel:
    """Tests for negative caching sentinel value."""

    def test_no_config_sentinel_is_unique(self) -> None:
        """Test that _NO_CONFIG sentinel is a unique object."""
        assert _NO_CONFIG is not None
        assert _NO_CONFIG != []
        assert _NO_CONFIG != []
