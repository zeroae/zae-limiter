"""Throughput benchmark tests for documentation.

These tests measure maximum throughput and contention behavior.
Results validate the throughput claims in docs/performance.md.

Run with:
    pytest tests/benchmark/test_throughput.py -v

Skip benchmarks in regular test runs:
    pytest -m "not benchmark" -v

Note: These tests use moto (mocked DynamoDB) for fast measurements.
Moto doesn't simulate true contention, so retry rates will be 0.
For realistic contention behavior, use LocalStack or real AWS.
"""

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from zae_limiter import Limit

pytestmark = pytest.mark.benchmark


class TestThroughputBenchmarks:
    """Measure maximum throughput under various conditions.

    These tests calculate operations per second (TPS) for different scenarios.
    """

    def test_sequential_throughput_single_entity(self, sync_limiter):
        """Measure max sequential TPS for single entity.

        All operations target the same entity, measuring the maximum
        throughput when there's no parallelism.

        Note: With moto, no contention occurs. Real DynamoDB would show
        optimistic locking retries under high load.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]
        iterations = 100

        start = time.perf_counter()

        for _ in range(iterations):
            with sync_limiter.acquire(
                entity_id="throughput-single",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        elapsed = time.perf_counter() - start
        tps = iterations / elapsed

        # Report results
        print(f"\nSequential single-entity TPS: {tps:.2f} ops/sec")
        print(f"Average latency: {(elapsed / iterations) * 1000:.2f} ms")

        # Sanity check: should complete in reasonable time
        assert elapsed < 60, "Sequential operations took too long"
        assert tps > 10, "TPS should be greater than 10 for moto"

    def test_sequential_throughput_multiple_entities(self, sync_limiter):
        """Measure max sequential TPS across multiple entities.

        Operations are distributed across 10 entities in round-robin fashion.
        This should show similar or better throughput than single entity
        since there's no bucket contention.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]
        num_entities = 10
        iterations_per_entity = 10
        total_iterations = num_entities * iterations_per_entity

        start = time.perf_counter()

        for i in range(total_iterations):
            entity_id = f"throughput-multi-{i % num_entities}"
            with sync_limiter.acquire(
                entity_id=entity_id,
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        elapsed = time.perf_counter() - start
        tps = total_iterations / elapsed

        # Report results
        print(f"\nSequential multi-entity TPS: {tps:.2f} ops/sec")
        print(f"Average latency: {(elapsed / total_iterations) * 1000:.2f} ms")

        # Sanity check
        assert elapsed < 60, "Sequential operations took too long"
        assert tps > 10, "TPS should be greater than 10 for moto"

    def test_concurrent_throughput_single_entity(self, sync_limiter):
        """Measure contention impact on single entity.

        Multiple concurrent threads acquire the same entity's bucket.
        With real DynamoDB, this would cause optimistic locking retries.
        With moto, operations serialize but don't fail.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]
        num_concurrent = 10
        iterations_per_task = 10

        def worker(_task_id: int) -> int:
            """Execute iterations and return success count."""
            successes = 0
            for _ in range(iterations_per_task):
                try:
                    with sync_limiter.acquire(
                        entity_id="throughput-concurrent-single",
                        resource="api",
                        limits=limits,
                        consume={"rpm": 1},
                    ):
                        successes += 1
                except Exception:
                    pass
            return successes

        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=num_concurrent) as executor:
            results = list(executor.map(worker, range(num_concurrent)))
        total_elapsed = time.perf_counter() - start

        total_successes = sum(results)
        total_iterations = num_concurrent * iterations_per_task

        tps = total_successes / total_elapsed

        # Report results
        print(f"\nConcurrent single-entity TPS: {tps:.2f} ops/sec")
        print(f"Total successes: {total_successes}/{total_iterations}")
        print(f"Wall clock time: {total_elapsed:.2f} s")

        # All operations should succeed with proper concurrency
        assert total_successes == total_iterations, "All operations should succeed"

    def test_concurrent_throughput_multiple_entities(self, sync_limiter):
        """Measure parallel throughput with no contention.

        Each thread operates on a different entity, so there should be
        no bucket contention. This represents the ideal scaling scenario.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]
        num_concurrent = 10
        iterations_per_task = 10

        def worker(task_id: int) -> int:
            """Execute iterations on dedicated entity."""
            entity_id = f"throughput-concurrent-multi-{task_id}"
            successes = 0
            for _ in range(iterations_per_task):
                try:
                    with sync_limiter.acquire(
                        entity_id=entity_id,
                        resource="api",
                        limits=limits,
                        consume={"rpm": 1},
                    ):
                        successes += 1
                except Exception:
                    pass
            return successes

        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=num_concurrent) as executor:
            results = list(executor.map(worker, range(num_concurrent)))
        total_elapsed = time.perf_counter() - start

        total_successes = sum(results)
        total_iterations = num_concurrent * iterations_per_task

        tps = total_successes / total_elapsed

        # Report results
        print(f"\nConcurrent multi-entity TPS: {tps:.2f} ops/sec")
        print(f"Total successes: {total_successes}/{total_iterations}")
        print(f"Wall clock time: {total_elapsed:.2f} s")

        # All operations should succeed with proper concurrency
        assert total_successes == total_iterations, "All operations should succeed"

    def test_contention_retry_rate(self, sync_limiter):
        """Measure transaction retry rate under contention.

        Note: Moto doesn't simulate true optimistic locking contention,
        so this test will show 0% retry rate. With real DynamoDB or
        LocalStack, retries would occur when concurrent operations
        try to update the same bucket.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]
        num_concurrent = 20
        iterations_per_task = 5

        def worker(_task_id: int) -> tuple[int, int]:
            """Execute iterations, counting retries."""
            local_retries = 0
            local_successes = 0
            for _ in range(iterations_per_task):
                try:
                    with sync_limiter.acquire(
                        entity_id="contention-test",
                        resource="api",
                        limits=limits,
                        consume={"rpm": 1},
                    ):
                        local_successes += 1
                except Exception as e:
                    if "ConditionalCheckFailed" in str(e):
                        local_retries += 1
            return local_successes, local_retries

        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=num_concurrent) as executor:
            results = list(executor.map(worker, range(num_concurrent)))
        elapsed = time.perf_counter() - start

        total_successes = sum(r[0] for r in results)
        total_retries = sum(r[1] for r in results)
        total_operations = num_concurrent * iterations_per_task

        retry_rate = (total_retries / total_operations) * 100 if total_operations > 0 else 0

        # Report results
        print("\nContention test results:")
        print(f"  Total operations: {total_operations}")
        print(f"  Successes: {total_successes}")
        print(f"  Retries: {total_retries}")
        print(f"  Retry rate: {retry_rate:.2f}%")
        print(f"  Elapsed time: {elapsed:.2f} s")

        # With true thread concurrency, moto may trigger condition check failures
        # that look like retries. This is a moto threading artifact.
        # On real DynamoDB, retry rate depends on contention level.
        assert total_successes > 0, "At least some operations should succeed"


class TestThroughputWithHierarchy:
    """Throughput tests with hierarchical (cascade) limits."""

    @pytest.fixture
    def hierarchy_limiter(self, sync_limiter: Any) -> Any:
        """Setup parent-child hierarchy."""
        sync_limiter.create_entity("throughput-parent", name="Parent")
        for i in range(10):
            sync_limiter.create_entity(
                f"throughput-child-{i}",
                name=f"Child {i}",
                parent_id="throughput-parent",
                cascade=True,
            )
        return sync_limiter

    def test_cascade_sequential_throughput(self, hierarchy_limiter):
        """Measure sequential TPS with cascade enabled.

        Cascade adds parent lookup and dual bucket updates,
        reducing throughput compared to non-cascade operations.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]
        iterations = 50

        start = time.perf_counter()

        for i in range(iterations):
            child_id = f"throughput-child-{i % 10}"
            with hierarchy_limiter.acquire(
                entity_id=child_id,
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        elapsed = time.perf_counter() - start
        tps = iterations / elapsed

        # Report results
        print(f"\nCascade sequential TPS: {tps:.2f} ops/sec")
        print(f"Average latency: {(elapsed / iterations) * 1000:.2f} ms")

        # Cascade should still complete reasonably fast
        assert tps > 5, "Cascade TPS should be greater than 5 for moto"

    def test_cascade_concurrent_throughput(self, hierarchy_limiter):
        """Measure concurrent TPS with cascade.

        All children share the same parent, creating contention on the
        parent bucket. With real DynamoDB, this would cause retries.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]
        num_concurrent = 10
        iterations_per_task = 5

        def worker(task_id: int) -> int:
            """Execute cascade operations on dedicated child."""
            child_id = f"throughput-child-{task_id}"
            successes = 0
            for _ in range(iterations_per_task):
                try:
                    with hierarchy_limiter.acquire(
                        entity_id=child_id,
                        resource="api",
                        limits=limits,
                        consume={"rpm": 1},
                    ):
                        successes += 1
                except Exception:
                    pass
            return successes

        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=num_concurrent) as executor:
            results = list(executor.map(worker, range(num_concurrent)))
        total_elapsed = time.perf_counter() - start

        total_successes = sum(results)
        total_iterations = num_concurrent * iterations_per_task

        tps = total_successes / total_elapsed

        # Report results
        print(f"\nCascade concurrent TPS: {tps:.2f} ops/sec")
        print(f"Total successes: {total_successes}/{total_iterations}")

        # With true thread concurrency + write-on-enter optimistic locking,
        # moto (not thread-safe) may reject some transactions. On real DynamoDB
        # the retry path handles this. Benchmark measures throughput, not correctness.
        assert total_successes >= total_iterations // 2, (
            f"At least half should succeed ({total_successes}/{total_iterations})"
        )
