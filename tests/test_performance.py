"""Performance benchmark tests for zae-limiter.

These benchmarks measure:
- Acquire/release latency (p50/p95/p99)
- Concurrent access throughput (requests/sec)
- DynamoDB transaction overhead
- Cascade overhead (hierarchical limits)

Run with:
    pytest tests/test_performance.py -v --benchmark-json=benchmark_results.json

For LocalStack integration benchmarks:
    AWS_ENDPOINT_URL=http://localhost:4566 \\
        pytest tests/test_performance.py -v -m "benchmark and integration"

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
        sync_limiter.create_entity("cascade-child", name="Child", parent_id="cascade-parent")
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
                cascade=False,
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
                cascade=True,
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
                cascade=True,
                use_stored_limits=True,
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


@pytest.mark.integration
class TestLocalStackBenchmarks:
    """Benchmarks against LocalStack for realistic DynamoDB latency.

    These tests require LocalStack to be running.
    Run with:
        AWS_ENDPOINT_URL=http://localhost:4566 pytest -m "benchmark and integration" -v
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
