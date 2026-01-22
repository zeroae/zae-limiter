"""Performance benchmark tests against LocalStack.

These benchmarks measure realistic DynamoDB latency including network round-trips.

Run with:
    # Start LocalStack (from project root)
    docker compose up -d

    # Set environment variables and run benchmarks
    export AWS_ENDPOINT_URL=http://localhost:4566
    export AWS_ACCESS_KEY_ID=test
    export AWS_SECRET_ACCESS_KEY=test
    export AWS_DEFAULT_REGION=us-east-1
    pytest tests/benchmark/test_localstack.py -v --benchmark-json=benchmark.json

Skip benchmarks in regular test runs:
    pytest -m "not benchmark" -v
"""

import pytest

from zae_limiter import Limit

pytestmark = [pytest.mark.benchmark, pytest.mark.integration]


class TestLocalStackBenchmarks:
    """Benchmarks against LocalStack for realistic DynamoDB latency.

    These tests require LocalStack to be running.
    """

    def test_acquire_release_localstack(self, benchmark, sync_localstack_limiter):
        """Benchmark acquire/release against real DynamoDB (LocalStack).

        Measures realistic latency including network round-trip.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]

        def operation():
            with sync_localstack_limiter.acquire(
                entity_id="ls-bench-entity",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        benchmark(operation)

    @pytest.fixture
    def cascade_hierarchy(self, sync_localstack_limiter):
        """Setup parent-child hierarchy for cascade tests."""
        sync_localstack_limiter.create_entity("ls-cascade-parent", name="Parent")
        sync_localstack_limiter.create_entity(
            "ls-cascade-child", name="Child", parent_id="ls-cascade-parent"
        )
        return sync_localstack_limiter

    def test_cascade_localstack(self, benchmark, cascade_hierarchy):
        """Benchmark cascade against LocalStack.

        Measures realistic cascade overhead including network latency.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]

        def operation():
            with cascade_hierarchy.acquire(
                entity_id="ls-cascade-child",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
                cascade=True,
            ):
                pass

        benchmark(operation)


class TestLocalStackLatencyBenchmarks:
    """Realistic latency measurements with LocalStack.

    These benchmarks capture p50/p95/p99 latency including actual network
    round-trips to LocalStack's DynamoDB emulation. Results are more
    representative of production behavior than moto-based tests.
    """

    @pytest.mark.benchmark(group="localstack-acquire")
    def test_acquire_realistic_latency(self, benchmark, sync_localstack_limiter):
        """Measure realistic latency including network overhead.

        This test captures the full latency of an acquire operation
        including network round-trips to LocalStack's DynamoDB.

        Expected: Higher than moto benchmarks due to actual network I/O.
        Typical values: 10-50ms p50, 20-100ms p95.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]

        def operation():
            with sync_localstack_limiter.acquire(
                entity_id="ls-latency-entity",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        benchmark(operation)

    @pytest.mark.benchmark(group="localstack-acquire")
    def test_acquire_two_limits_realistic_latency(self, benchmark, sync_localstack_limiter):
        """Measure multi-limit acquire with realistic network latency.

        Tests the overhead of tracking multiple limits (rpm + tpm pattern)
        with actual network I/O.
        """
        limits = [
            Limit.per_minute("rpm", 1_000_000),
            Limit.per_minute("tpm", 100_000_000),
        ]

        def operation():
            with sync_localstack_limiter.acquire(
                entity_id="ls-latency-multi",
                resource="api",
                limits=limits,
                consume={"rpm": 1, "tpm": 100},
            ):
                pass

        benchmark(operation)

    @pytest.fixture
    def cascade_latency_hierarchy(self, sync_localstack_limiter):
        """Setup parent-child hierarchy for cascade latency tests."""
        sync_localstack_limiter.create_entity("ls-latency-parent", name="Parent")
        sync_localstack_limiter.create_entity(
            "ls-latency-child", name="Child", parent_id="ls-latency-parent"
        )
        return sync_localstack_limiter

    @pytest.mark.benchmark(group="localstack-acquire")
    def test_cascade_realistic_latency(self, benchmark, cascade_latency_hierarchy):
        """Measure cascade with realistic network latency.

        Cascade requires additional network round-trips:
        - Entity lookup (to find parent)
        - Parent bucket read
        - Transaction with child + parent updates

        Expected: Higher overhead than non-cascade due to extra round-trips.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]

        def operation():
            with cascade_latency_hierarchy.acquire(
                entity_id="ls-latency-child",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
                cascade=True,
            ):
                pass

        benchmark(operation)

    @pytest.mark.benchmark(group="localstack-check")
    def test_available_realistic_latency(self, benchmark, sync_localstack_limiter):
        """Measure read-only availability check with network latency.

        The available() method only reads bucket state.
        Compare with acquire to measure transaction overhead.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]

        # Setup: create bucket first
        with sync_localstack_limiter.acquire(
            entity_id="ls-available-entity",
            resource="api",
            limits=limits,
            consume={"rpm": 1},
        ):
            pass

        def operation():
            sync_localstack_limiter.available(
                entity_id="ls-available-entity",
                resource="api",
                limits=limits,
            )

        benchmark(operation)


class TestCascadeOptimizationBenchmarks:
    """Benchmarks for cascade optimization using BatchGetItem (issue #133).

    These benchmarks measure the impact of the BatchGetItem optimization
    which reduces multiple GetItem calls to a single BatchGetItem call
    when resolving cascade scenarios.
    """

    @pytest.fixture
    def deep_hierarchy(self, sync_localstack_limiter):
        """Setup deeper hierarchy for cascade optimization testing."""
        sync_localstack_limiter.create_entity("opt-root", name="Root")
        sync_localstack_limiter.create_entity(
            "opt-level1", name="Level 1", parent_id="opt-root"
        )
        sync_localstack_limiter.create_entity(
            "opt-level2", name="Level 2", parent_id="opt-level1"
        )
        return sync_localstack_limiter

    @pytest.mark.benchmark(group="cascade-optimization")
    def test_cascade_with_batchgetitem_optimization(
        self, benchmark, deep_hierarchy
    ):
        """Measure cascade using optimized BatchGetItem pattern.

        The acquire() method uses BatchGetItem to fetch entity and parent
        buckets in a single round-trip (issue #133), reducing latency.

        Expected: Single round-trip vs sequential GetItem calls.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]

        def operation():
            with deep_hierarchy.acquire(
                entity_id="opt-level2",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
                cascade=True,
            ):
                pass

        benchmark(operation)

    @pytest.mark.benchmark(group="cascade-optimization")
    def test_cascade_multiple_resources(self, benchmark, deep_hierarchy):
        """Measure cascade with multiple resources (realistic workload).

        Measures cascade overhead when tracking multiple resource limits
        (e.g., gpt-4 and gpt-3.5-turbo).
        """
        limits = [
            Limit.per_minute("rpm", 1_000_000),
            Limit.per_minute("tpm", 100_000_000),
        ]

        def operation():
            with deep_hierarchy.acquire(
                entity_id="opt-level2",
                resource="api",
                limits=limits,
                consume={"rpm": 1, "tpm": 100},
                cascade=True,
            ):
                pass

        benchmark(operation)

    @pytest.fixture
    def cascade_with_config(self, sync_localstack_limiter):
        """Setup hierarchy with stored config for optimization testing."""
        limits = [Limit.per_minute("rpm", 1_000_000)]

        # Create hierarchy
        sync_localstack_limiter.create_entity("opt-config-root", name="Root")
        sync_localstack_limiter.create_entity(
            "opt-config-child",
            name="Child",
            parent_id="opt-config-root",
        )

        # Set stored limits to exercise config cache + cascade
        sync_localstack_limiter.set_limits("opt-config-root", limits)
        sync_localstack_limiter.set_limits("opt-config-child", limits)

        return sync_localstack_limiter

    @pytest.mark.benchmark(group="cascade-optimization")
    def test_cascade_with_config_cache_optimization(
        self, benchmark, cascade_with_config
    ):
        """Measure cascade with both config cache and BatchGetItem optimization.

        This is the ideal case: config cache reduces config lookups, and
        BatchGetItem reduces bucket fetches to a single round-trip.

        Expected: Combines benefits of both optimizations.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]

        # Warm up cache
        with cascade_with_config.acquire(
            entity_id="opt-config-child",
            resource="api",
            limits=limits,
            consume={"rpm": 1},
            cascade=True,
        ):
            pass

        def operation():
            with cascade_with_config.acquire(
                entity_id="opt-config-child",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
                cascade=True,
            ):
                pass

        benchmark(operation)
