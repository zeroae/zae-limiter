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
