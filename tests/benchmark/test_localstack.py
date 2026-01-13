"""Performance benchmark tests against LocalStack.

These benchmarks measure realistic DynamoDB latency including network round-trips.

Run with:
    AWS_ENDPOINT_URL=http://localhost:4566 \\
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
