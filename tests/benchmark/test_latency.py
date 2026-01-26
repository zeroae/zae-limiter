"""Latency benchmark tests for documentation.

These benchmarks capture p50/p95/p99 latency percentiles for key operations.
Results are used to populate the latency tables in docs/performance.md.

Run with:
    pytest tests/benchmark/test_latency.py -v --benchmark-json=latency.json

Skip benchmarks in regular test runs:
    pytest -m "not benchmark" -v

Note: These tests use moto (mocked DynamoDB) for fast, repeatable measurements.
For realistic latency with network overhead, see test_localstack.py.
"""

import pytest

from zae_limiter import Limit

pytestmark = pytest.mark.benchmark


class TestLatencyBenchmarks:
    """Capture latency percentiles for documentation.

    Each test measures a specific operation pattern using pytest-benchmark.
    The benchmark decorator captures p50/p95/p99/min/max statistics.
    """

    @pytest.mark.benchmark(group="acquire")
    def test_acquire_single_limit_latency(self, benchmark, sync_limiter):
        """Measure p50/p95/p99 for single-limit acquire.

        This is the most common operation: acquiring a single rate limit
        (e.g., requests per minute).

        Expected: ~5ms p50, ~10ms p95, ~20ms p99 (moto baseline)
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]

        def operation():
            with sync_limiter.acquire(
                entity_id="latency-single",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        benchmark(operation)

    @pytest.mark.benchmark(group="acquire")
    def test_acquire_two_limits_latency(self, benchmark, sync_limiter):
        """Measure overhead of multi-limit acquire (rpm + tpm pattern).

        Common pattern for LLM APIs: tracking both requests per minute
        and tokens per minute in a single call.

        Expected: ~7ms p50, ~15ms p95, ~25ms p99 (adds ~2ms per limit)
        """
        limits = [
            Limit.per_minute("rpm", 1_000_000),
            Limit.per_minute("tpm", 100_000_000),
        ]

        def operation():
            with sync_limiter.acquire(
                entity_id="latency-two",
                resource="api",
                limits=limits,
                consume={"rpm": 1, "tpm": 100},
            ):
                pass

        benchmark(operation)

    @pytest.mark.benchmark(group="acquire")
    def test_acquire_with_cascade_latency(self, benchmark, sync_limiter):
        """Measure cascade overhead.

        Cascade enables hierarchical limits where both child and parent
        entities are checked and updated.

        Expected: ~10ms p50, ~20ms p95, ~35ms p99 (adds entity lookup + parent ops)
        """
        # Setup hierarchy once
        sync_limiter.create_entity("latency-cascade-parent", name="Parent")
        sync_limiter.create_entity(
            "latency-cascade-child", name="Child", parent_id="latency-cascade-parent", cascade=True
        )

        limits = [Limit.per_minute("rpm", 1_000_000)]

        def operation():
            with sync_limiter.acquire(
                entity_id="latency-cascade-child",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        benchmark(operation)

    @pytest.mark.benchmark(group="check")
    def test_available_check_latency(self, benchmark, sync_limiter):
        """Measure read-only availability check.

        The available() method only reads bucket state without acquiring.
        This is a baseline for read operations.

        Expected: ~3ms p50, ~8ms p95, ~15ms p99 (no transaction overhead)
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]

        # Setup: create bucket first
        with sync_limiter.acquire(
            entity_id="latency-available",
            resource="api",
            limits=limits,
            consume={"rpm": 1},
        ):
            pass

        def operation():
            sync_limiter.available(
                entity_id="latency-available",
                resource="api",
                limits=limits,
            )

        benchmark(operation)

    @pytest.mark.benchmark(group="acquire")
    def test_acquire_with_stored_limits_latency(self, benchmark, sync_limiter):
        """Measure stored limits query overhead.

        When use_stored_limits=True, the limiter queries DynamoDB for
        the entity's configured limits instead of using caller-provided limits.

        Expected: Adds ~2-5ms for the additional query operations.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]

        # Setup stored limits
        sync_limiter.create_entity("latency-stored", name="Stored Limits Entity")
        sync_limiter.set_limits("latency-stored", limits)

        def operation():
            with sync_limiter.acquire(
                entity_id="latency-stored",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
                use_stored_limits=True,
            ):
                pass

        benchmark(operation)


class TestLatencyComparison:
    """Comparison benchmarks to measure specific overheads.

    These tests run the same operation with and without specific features
    to measure the incremental cost of each feature.
    """

    @pytest.mark.benchmark(group="cascade-comparison")
    def test_baseline_no_cascade(self, benchmark, sync_limiter):
        """Baseline: acquire without cascade.

        Compare with test_with_cascade to measure cascade overhead.
        """
        sync_limiter.create_entity("compare-cascade-parent", name="Parent")
        sync_limiter.create_entity(
            "compare-cascade-child", name="Child", parent_id="compare-cascade-parent"
        )

        limits = [Limit.per_minute("rpm", 1_000_000)]

        def operation():
            with sync_limiter.acquire(
                entity_id="compare-cascade-child",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        benchmark(operation)

    @pytest.mark.benchmark(group="cascade-comparison")
    def test_with_cascade(self, benchmark, sync_limiter):
        """With cascade: acquire with cascade entity.

        Compare with test_baseline_no_cascade to measure cascade overhead.
        """
        sync_limiter.create_entity("compare-cascade2-parent", name="Parent")
        sync_limiter.create_entity(
            "compare-cascade2-child",
            name="Child",
            parent_id="compare-cascade2-parent",
            cascade=True,
        )

        limits = [Limit.per_minute("rpm", 1_000_000)]

        def operation():
            with sync_limiter.acquire(
                entity_id="compare-cascade2-child",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        benchmark(operation)

    @pytest.mark.benchmark(group="limits-comparison")
    def test_one_limit(self, benchmark, sync_limiter):
        """Baseline: single limit acquire."""
        limits = [Limit.per_minute("rpm", 1_000_000)]

        def operation():
            with sync_limiter.acquire(
                entity_id="compare-limits-1",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        benchmark(operation)

    @pytest.mark.benchmark(group="limits-comparison")
    def test_two_limits(self, benchmark, sync_limiter):
        """Two limits: measure per-limit overhead."""
        limits = [
            Limit.per_minute("rpm", 1_000_000),
            Limit.per_minute("tpm", 100_000_000),
        ]

        def operation():
            with sync_limiter.acquire(
                entity_id="compare-limits-2",
                resource="api",
                limits=limits,
                consume={"rpm": 1, "tpm": 100},
            ):
                pass

        benchmark(operation)

    @pytest.mark.benchmark(group="limits-comparison")
    def test_five_limits(self, benchmark, sync_limiter):
        """Five limits: measure scaling with limit count."""
        limits = [Limit.per_minute(f"limit_{i}", 1_000_000) for i in range(5)]
        consume = {f"limit_{i}": 1 for i in range(5)}

        def operation():
            with sync_limiter.acquire(
                entity_id="compare-limits-5",
                resource="api",
                limits=limits,
                consume=consume,
            ):
                pass

        benchmark(operation)
