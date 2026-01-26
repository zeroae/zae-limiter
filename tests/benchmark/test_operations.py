"""Performance benchmark tests for zae-limiter (moto-based).

These benchmarks measure:
- Acquire/release latency (p50/p95/p99)
- Concurrent access throughput (requests/sec)
- DynamoDB transaction overhead
- Cascade overhead (hierarchical limits)

Run with:
    pytest tests/benchmark/test_operations.py -v --benchmark-json=benchmark.json

Skip benchmarks in regular test runs:
    pytest -m "not benchmark" -v
"""

import pytest

from zae_limiter import Limit

pytestmark = pytest.mark.benchmark


class TestAcquireReleaseBenchmarks:
    """Benchmarks for acquire/release operations."""

    def test_acquire_release_single_limit(self, benchmark, sync_limiter):
        """Benchmark single limit acquire/release cycle.

        Measures p50/p95/p99 latency for the most common operation:
        acquiring and releasing a single rate limit.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]  # High limit to avoid rate limiting

        def operation():
            with sync_limiter.acquire(
                entity_id="bench-single",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        benchmark(operation)

    def test_acquire_release_multiple_limits(self, benchmark, sync_limiter):
        """Benchmark multiple limits acquire/release.

        Measures overhead of tracking multiple limits (rpm + tpm) in a single call.
        """
        limits = [
            Limit.per_minute("rpm", 1_000_000),
            Limit.per_minute("tpm", 100_000_000),
        ]

        def operation():
            with sync_limiter.acquire(
                entity_id="bench-multi",
                resource="api",
                limits=limits,
                consume={"rpm": 1, "tpm": 100},
            ):
                pass

        benchmark(operation)


class TestTransactionOverheadBenchmarks:
    """Benchmarks for DynamoDB transaction overhead."""

    def test_available_check(self, benchmark, sync_limiter):
        """Benchmark non-transactional read (baseline).

        The available() method does a simple GetItem, no transaction.
        This serves as a baseline for comparison with transactional operations.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]

        # Setup: create a bucket first
        with sync_limiter.acquire(
            entity_id="bench-available",
            resource="api",
            limits=limits,
            consume={"rpm": 1},
        ):
            pass

        def operation():
            sync_limiter.available(
                entity_id="bench-available",
                resource="api",
                limits=limits,
            )

        benchmark(operation)

    def test_transactional_acquire(self, benchmark, sync_limiter):
        """Benchmark transactional acquire (uses TransactWriteItems).

        Compare this with test_available_check to measure transaction overhead.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]

        def operation():
            with sync_limiter.acquire(
                entity_id="bench-tx",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        benchmark(operation)


class TestCascadeOverheadBenchmarks:
    """Benchmarks for hierarchical limit overhead."""

    @pytest.fixture
    def hierarchy_limiter(self, sync_limiter):
        """Setup parent-child hierarchy for cascade tests."""
        sync_limiter.create_entity("cascade-parent", name="Parent")
        sync_limiter.create_entity(
            "cascade-child",
            name="Child",
            parent_id="cascade-parent",
            cascade=True,
        )
        return sync_limiter

    def test_acquire_without_cascade(self, benchmark, hierarchy_limiter):
        """Baseline: acquire without cascade.

        Only consumes from child entity, no parent lookup.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]

        def operation():
            with hierarchy_limiter.acquire(
                entity_id="cascade-child",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        benchmark(operation)

    def test_acquire_with_cascade(self, benchmark, hierarchy_limiter):
        """Cascade enabled: measure overhead of parent lookup + dual consume.

        Compare with test_acquire_without_cascade to measure cascade overhead.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]

        def operation():
            with hierarchy_limiter.acquire(
                entity_id="cascade-child",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        benchmark(operation)

    def test_cascade_with_stored_limits(self, benchmark, hierarchy_limiter):
        """Cascade with stored limits lookup.

        Additional overhead from fetching stored limits for both parent and child.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]

        # Setup stored limits
        hierarchy_limiter.set_limits("cascade-parent", limits)
        hierarchy_limiter.set_limits("cascade-child", limits)

        def operation():
            with hierarchy_limiter.acquire(
                entity_id="cascade-child",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
                use_stored_limits=True,
            ):
                pass

        benchmark(operation)


class TestConfigLookupBenchmarks:
    """Benchmarks for centralized config lookup overhead."""

    @pytest.fixture
    def config_setup_limiter(self, sync_limiter):
        """Setup limiter with stored config for benchmarking."""
        limits = [Limit.per_minute("rpm", 1_000_000)]

        # Set system defaults
        sync_limiter.set_system_defaults(limits)

        # Set resource defaults
        sync_limiter.set_resource_defaults("benchmark-resource", limits)

        # Set entity limits
        sync_limiter.set_limits("config-entity", limits, resource="benchmark-resource")

        return sync_limiter

    def test_acquire_with_cached_config(self, benchmark, config_setup_limiter):
        """Measure acquire() when config is cached (warm cache).

        After the first acquire, config cache should be warm and subsequent
        calls should hit the cache rather than DynamoDB.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]

        # Warm up the cache with one acquire
        with config_setup_limiter.acquire(
            entity_id="config-entity",
            resource="benchmark-resource",
            limits=limits,
            consume={"rpm": 1},
        ):
            pass

        def operation():
            with config_setup_limiter.acquire(
                entity_id="config-entity",
                resource="benchmark-resource",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        benchmark(operation)

    def test_acquire_cold_config(self, benchmark, sync_limiter):
        """Measure acquire() with config cache miss (cold cache).

        First access to an entity should incur config cache miss and
        fetch stored limits from DynamoDB.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]

        # Set resource defaults so they can be fetched
        sync_limiter.set_resource_defaults("cold-resource", limits)

        # Create a counter to track cache effectiveness
        entity_id_counter = [0]

        def operation():
            # Use a unique entity ID each time to simulate cache miss
            entity_id_counter[0] += 1
            with sync_limiter.acquire(
                entity_id=f"cold-entity-{entity_id_counter[0]}",
                resource="cold-resource",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        benchmark(operation)

    def test_acquire_cascade_with_cached_config(self, benchmark, sync_limiter):
        """Measure cascade acquire with cached config (warm cache).

        After warming up the cache for both parent and child, cascade
        should benefit from cached config lookups.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]

        # Create hierarchy and set limits
        sync_limiter.create_entity("config-cascade-parent", name="Parent")
        sync_limiter.create_entity(
            "config-cascade-child", name="Child", parent_id="config-cascade-parent", cascade=True
        )
        sync_limiter.set_limits("config-cascade-parent", limits)
        sync_limiter.set_limits("config-cascade-child", limits)

        # Warm up cache
        with sync_limiter.acquire(
            entity_id="config-cascade-child",
            resource="api",
            limits=limits,
            consume={"rpm": 1},
        ):
            pass

        def operation():
            with sync_limiter.acquire(
                entity_id="config-cascade-child",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        benchmark(operation)


class TestConcurrentThroughputBenchmarks:
    """Benchmarks for throughput under load."""

    def test_sequential_acquisitions(self, benchmark, sync_limiter):
        """Benchmark sequential acquisitions for throughput baseline.

        Measures how many acquire/release cycles can be done sequentially.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]
        iterations = 10

        def operation():
            for i in range(iterations):
                with sync_limiter.acquire(
                    entity_id=f"bench-seq-{i}",
                    resource="api",
                    limits=limits,
                    consume={"rpm": 1},
                ):
                    pass

        benchmark(operation)

    def test_same_entity_sequential(self, benchmark, sync_limiter):
        """Benchmark sequential acquisitions on same entity.

        Measures contention overhead when repeatedly hitting the same bucket.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]
        iterations = 10

        def operation():
            for _ in range(iterations):
                with sync_limiter.acquire(
                    entity_id="bench-same",
                    resource="api",
                    limits=limits,
                    consume={"rpm": 1},
                ):
                    pass

        benchmark(operation)


class TestOptimizationComparison:
    """Direct comparison benchmarks for v0.5.0 optimizations (issue #134).

    These tests run the same operations with and without optimizations enabled
    to quantify the performance improvement. Tests are grouped for easy
    comparison in benchmark output.

    Optimizations compared:
    - Config cache (issue #135): Reduces config lookups from DynamoDB
    - BatchGetItem (issue #133): Reduces cascade bucket fetches to single call
    """

    @pytest.mark.benchmark(group="config-cache-comparison")
    def test_cascade_cache_disabled(self, benchmark, sync_limiter_no_cache):
        """Baseline: cascade with config cache DISABLED.

        This represents pre-optimization performance for config lookups.
        Compare with test_cascade_cache_enabled to measure cache impact.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]

        # Setup hierarchy
        sync_limiter_no_cache.create_entity("cmp-nocache-parent", name="Parent")
        sync_limiter_no_cache.create_entity(
            "cmp-nocache-child", name="Child", parent_id="cmp-nocache-parent", cascade=True
        )
        sync_limiter_no_cache.set_limits("cmp-nocache-parent", limits)
        sync_limiter_no_cache.set_limits("cmp-nocache-child", limits)

        def operation():
            with sync_limiter_no_cache.acquire(
                entity_id="cmp-nocache-child",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        benchmark(operation)

    @pytest.mark.benchmark(group="config-cache-comparison")
    def test_cascade_cache_enabled(self, benchmark, sync_limiter):
        """Optimized: cascade with config cache ENABLED (default).

        Compare with test_cascade_cache_disabled to measure cache impact.
        Expected: Faster due to cached config lookups.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]

        # Setup hierarchy
        sync_limiter.create_entity("cmp-cache-parent", name="Parent")
        sync_limiter.create_entity(
            "cmp-cache-child",
            name="Child",
            parent_id="cmp-cache-parent",
            cascade=True,
        )
        sync_limiter.set_limits("cmp-cache-parent", limits)
        sync_limiter.set_limits("cmp-cache-child", limits)

        # Warm up cache
        with sync_limiter.acquire(
            entity_id="cmp-cache-child",
            resource="api",
            limits=limits,
            consume={"rpm": 1},
        ):
            pass

        def operation():
            with sync_limiter.acquire(
                entity_id="cmp-cache-child",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        benchmark(operation)

    @pytest.mark.benchmark(group="stored-limits-comparison")
    def test_stored_limits_cache_disabled(self, benchmark, sync_limiter_no_cache):
        """Baseline: stored limits lookup with cache DISABLED.

        Each acquire() must fetch limits from DynamoDB.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]

        # Setup stored limits
        sync_limiter_no_cache.set_resource_defaults("cmp-resource", limits)

        counter = [0]

        def operation():
            counter[0] += 1
            with sync_limiter_no_cache.acquire(
                entity_id=f"cmp-nocache-entity-{counter[0]}",
                resource="cmp-resource",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        benchmark(operation)

    @pytest.mark.benchmark(group="stored-limits-comparison")
    def test_stored_limits_cache_enabled(self, benchmark, sync_limiter):
        """Optimized: stored limits lookup with cache ENABLED.

        After warmup, limits are served from cache.
        Expected: Faster due to avoided DynamoDB reads.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]

        # Setup stored limits
        sync_limiter.set_resource_defaults("cmp-resource", limits)

        # Warm up cache with first entity
        with sync_limiter.acquire(
            entity_id="cmp-cache-entity-warmup",
            resource="cmp-resource",
            limits=limits,
            consume={"rpm": 1},
        ):
            pass

        counter = [0]

        def operation():
            counter[0] += 1
            with sync_limiter.acquire(
                entity_id=f"cmp-cache-entity-{counter[0]}",
                resource="cmp-resource",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        benchmark(operation)
