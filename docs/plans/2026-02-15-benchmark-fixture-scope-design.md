# Benchmark Fixture Scope Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Module-scope benchmark fixtures to eliminate per-test setup overhead and enable steady-state warm-path measurements.

**Architecture:** Add `mock_dynamodb_module`, `benchmark_limiter`, and `benchmark_entities` as module-scoped fixtures in `tests/benchmark/conftest.py`. Tests that measure warm-path performance switch to `benchmark_entities`; tests that compare optimizations (cache disabled, speculative cold) keep function-scoped `sync_limiter`.

**Tech Stack:** pytest fixtures, moto (mock_aws), SyncRateLimiter, SyncRepository

**Issue:** #171 | **Baseline:** 25 passed in 9.50s (44% is setup overhead)

---

### Task 1: Add module-scoped fixtures to conftest.py

**Files:**
- Modify: `tests/benchmark/conftest.py:1-63`

**Step 1: Write the new fixtures**

Replace the existing `benchmark_entities` fixture (lines 39-62) and add new module-scoped fixtures. Keep all other fixtures unchanged.

Replace the full file content of `tests/benchmark/conftest.py` with:

```python
"""Benchmark test fixtures.

Provides module-scoped pre-warmed entities for steady-state benchmarks,
plus function-scoped fixtures for optimization comparison tests.

Fixture scoping strategy:
- module: benchmark_entities, benchmark_limiter — shared DynamoDB state,
  one moto mock + table creation per test file. Tests use pre-warmed
  entities to measure warm-path performance without cold-start noise.
- function: sync_limiter, sync_limiter_no_cache — clean state per test.
  Used by capacity tests (exact RCU/WCU assertions) and optimization
  comparison tests (cache disabled, entity cache cleared per iteration).
"""

import os
import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest
from moto import mock_aws

from tests.fixtures.repositories import make_sync_test_repo
from zae_limiter import Limit, SyncRateLimiter
from zae_limiter.sync_repository import SyncRepository


# --- Module-scoped moto fixtures for warm-path benchmarks ---


@dataclass
class BenchmarkEntities:
    """Pre-warmed entities for steady-state benchmark measurements.

    All entities have pre-existing buckets (one acquire cycle completed)
    so benchmark iterations measure warm-path performance only.

    Attributes:
        flat: 100 standalone entity IDs (bench-entity-000..099)
        parents: Parent entity IDs (bench-parent-0)
        children: Mapping of parent_id to child entity IDs (cascade=True)
        limiter: The module-scoped SyncRateLimiter instance
    """

    flat: list[str]
    parents: list[str]
    children: dict[str, list[str]]
    limiter: SyncRateLimiter


@pytest.fixture(scope="module")
def mock_dynamodb_module():
    """Module-scoped moto mock — shared DynamoDB state across tests in a file.

    Uses os.environ directly because monkeypatch is function-scoped.
    The mock_aws() context manager intercepts all AWS calls for the
    duration of the module's test execution.
    """
    env_vars = {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
    }
    old_env = {k: os.environ.get(k) for k in env_vars}
    old_endpoint = os.environ.get("AWS_ENDPOINT_URL")

    os.environ.update(env_vars)
    os.environ.pop("AWS_ENDPOINT_URL", None)

    with mock_aws():
        yield

    # Restore original environment
    for k, v in old_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    if old_endpoint is not None:
        os.environ["AWS_ENDPOINT_URL"] = old_endpoint


@pytest.fixture(scope="module")
def benchmark_limiter(mock_dynamodb_module):
    """Module-scoped SyncRateLimiter — one table per test file.

    Tests that need custom config (set_limits, create_entity) on a shared
    table use this directly. Tests that need pre-warmed entities use
    benchmark_entities instead.
    """
    repo = SyncRepository(
        name="benchmark",
        region="us-east-1",
    )
    repo.create_table()
    limiter = SyncRateLimiter(repository=repo)
    with limiter:
        yield limiter


@pytest.fixture(scope="module")
def benchmark_entities(benchmark_limiter):
    """100 pre-warmed flat entities + 1 parent with 10 cascade children.

    Created once per test file. All entities have completed one acquire
    cycle so buckets exist in DynamoDB (no cold-start overhead).

    Contents:
        flat: bench-entity-000..099 (standalone, pre-warmed on resource "benchmark")
        parents: [bench-parent-0]
        children: {bench-parent-0: [bench-child-0-00..09]} (cascade=True)
    """
    limits = [Limit.per_minute("rpm", 1_000_000)]

    # Create and warm 100 flat entities
    flat_ids = [f"bench-entity-{i:03d}" for i in range(100)]
    for entity_id in flat_ids:
        benchmark_limiter.create_entity(entity_id, name=f"Benchmark Entity {entity_id}")
        with benchmark_limiter.acquire(
            entity_id=entity_id,
            resource="benchmark",
            limits=limits,
            consume={"rpm": 1},
        ):
            pass

    # Create and warm hierarchy: 1 parent + 10 cascade children
    parent_id = "bench-parent-0"
    benchmark_limiter.create_entity(parent_id, name="Benchmark Parent 0")
    child_ids = [f"bench-child-0-{i:02d}" for i in range(10)]
    for child_id in child_ids:
        benchmark_limiter.create_entity(
            child_id,
            name=f"Benchmark Child {child_id}",
            parent_id=parent_id,
            cascade=True,
        )
        with benchmark_limiter.acquire(
            entity_id=child_id,
            resource="benchmark",
            limits=limits,
            consume={"rpm": 1},
        ):
            pass

    return BenchmarkEntities(
        flat=flat_ids,
        parents=[parent_id],
        children={parent_id: child_ids},
        limiter=benchmark_limiter,
    )


# --- Namespace-scoped sync fixtures for LocalStack benchmarks ---


@pytest.fixture
def sync_localstack_limiter(shared_minimal_stack, unique_namespace):
    """SyncRateLimiter on the shared minimal stack with namespace isolation."""
    parent, scoped = make_sync_test_repo(shared_minimal_stack, unique_namespace)
    limiter = SyncRateLimiter(repository=scoped)
    with limiter:
        yield limiter
    parent.close()


@pytest.fixture
def sync_localstack_limiter_with_aggregator(shared_aggregator_stack, unique_namespace):
    """SyncRateLimiter with Lambda aggregator for benchmark tests."""
    parent, scoped = make_sync_test_repo(shared_aggregator_stack, unique_namespace)
    limiter = SyncRateLimiter(repository=scoped)
    with limiter:
        yield limiter
    parent.close()


# --- Function-scoped fixtures for optimization comparison benchmarks ---


@pytest.fixture
def sync_limiter_no_cache(mock_dynamodb):
    """SyncRateLimiter with config cache disabled for baseline comparison."""
    repo = SyncRepository(
        name="test-no-cache",
        region="us-east-1",
        config_cache_ttl=0,
    )
    repo.create_table()
    limiter = SyncRateLimiter(repository=repo)
    with limiter:
        yield limiter


@pytest.fixture
def sync_localstack_limiter_no_cache(shared_minimal_stack):
    """SyncRateLimiter on LocalStack with config cache disabled."""
    ns = f"ns-nc-{uuid.uuid4().hex[:8]}"
    parent, scoped = make_sync_test_repo(shared_minimal_stack, ns)
    # Override config_cache_ttl on the scoped repo
    scoped._config_cache_ttl = 0
    limiter = SyncRateLimiter(repository=scoped)
    with limiter:
        yield limiter
    parent.close()
```

**Step 2: Run existing tests to verify fixtures load correctly**

Run: `uv run pytest tests/benchmark/test_capacity.py -o "addopts=" -v --benchmark-skip`
Expected: All capacity tests PASS (they still use function-scoped `sync_limiter` from `tests/fixtures/moto.py`)

**Step 3: Commit**

```
⚡ perf(benchmark): add module-scoped moto fixtures and BenchmarkEntities (#171)
```

---

### Task 2: Update test_throughput.py to use benchmark_entities

**Files:**
- Modify: `tests/benchmark/test_throughput.py`

**Step 1: Rewrite test_throughput.py**

Replace full file content with:

```python
"""Throughput benchmark tests for documentation.

These tests measure maximum throughput and contention behavior
using pre-warmed entities (module-scoped) for steady-state measurements.

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

import pytest

from tests.benchmark.conftest import BenchmarkEntities
from zae_limiter import Limit

pytestmark = pytest.mark.benchmark


class TestThroughputBenchmarks:
    """Measure maximum throughput under various conditions.

    These tests calculate operations per second (TPS) for different scenarios.
    All use pre-warmed entities to measure steady-state performance.
    """

    def test_sequential_throughput_single_entity(self, benchmark_entities: BenchmarkEntities):
        """Measure max sequential TPS for single entity.

        All operations target the same pre-warmed entity, measuring the maximum
        throughput when there's no parallelism.

        Note: With moto, no contention occurs. Real DynamoDB would show
        optimistic locking retries under high load.
        """
        limiter = benchmark_entities.limiter
        entity_id = benchmark_entities.flat[0]
        limits = [Limit.per_minute("rpm", 1_000_000)]
        iterations = 100

        start = time.perf_counter()

        for _ in range(iterations):
            with limiter.acquire(
                entity_id=entity_id,
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

    def test_sequential_throughput_multiple_entities(self, benchmark_entities: BenchmarkEntities):
        """Measure max sequential TPS across multiple entities.

        Operations are distributed across 10 pre-warmed entities in round-robin
        fashion. This should show similar or better throughput than single entity
        since there's no bucket contention.
        """
        limiter = benchmark_entities.limiter
        entity_ids = benchmark_entities.flat[:10]
        limits = [Limit.per_minute("rpm", 1_000_000)]
        num_entities = len(entity_ids)
        iterations_per_entity = 10
        total_iterations = num_entities * iterations_per_entity

        start = time.perf_counter()

        for i in range(total_iterations):
            entity_id = entity_ids[i % num_entities]
            with limiter.acquire(
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

    def test_concurrent_throughput_single_entity(self, benchmark_entities: BenchmarkEntities):
        """Measure contention impact on single entity.

        Multiple concurrent threads acquire the same pre-warmed entity's bucket.
        With real DynamoDB, this would cause optimistic locking retries.
        With moto, operations serialize but don't fail.
        """
        limiter = benchmark_entities.limiter
        entity_id = benchmark_entities.flat[0]
        limits = [Limit.per_minute("rpm", 1_000_000)]
        num_concurrent = 10
        iterations_per_task = 10

        def worker(_task_id: int) -> int:
            """Execute iterations and return success count."""
            successes = 0
            for _ in range(iterations_per_task):
                try:
                    with limiter.acquire(
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
        print(f"\nConcurrent single-entity TPS: {tps:.2f} ops/sec")
        print(f"Total successes: {total_successes}/{total_iterations}")
        print(f"Wall clock time: {total_elapsed:.2f} s")

        # Moto is not thread-safe; sporadic failures are threading artifacts.
        # No bucket contention here, so nearly all should succeed.
        assert total_successes >= total_iterations * 9 // 10, (
            f">=90% should succeed ({total_successes}/{total_iterations})"
        )

    def test_concurrent_throughput_multiple_entities(self, benchmark_entities: BenchmarkEntities):
        """Measure parallel throughput with no contention.

        Each thread operates on a different pre-warmed entity, so there should be
        no bucket contention. This represents the ideal scaling scenario.
        """
        limiter = benchmark_entities.limiter
        limits = [Limit.per_minute("rpm", 1_000_000)]
        num_concurrent = 10
        iterations_per_task = 10

        def worker(task_id: int) -> int:
            """Execute iterations on dedicated entity."""
            entity_id = benchmark_entities.flat[task_id]
            successes = 0
            for _ in range(iterations_per_task):
                try:
                    with limiter.acquire(
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

        # Moto is not thread-safe; sporadic failures are threading artifacts.
        # No bucket contention here, so nearly all should succeed.
        assert total_successes >= total_iterations * 9 // 10, (
            f">=90% should succeed ({total_successes}/{total_iterations})"
        )

    def test_contention_retry_rate(self, benchmark_entities: BenchmarkEntities):
        """Measure transaction retry rate under contention.

        Note: Moto doesn't simulate true optimistic locking contention,
        so this test will show 0% retry rate. With real DynamoDB or
        LocalStack, retries would occur when concurrent operations
        try to update the same bucket.
        """
        limiter = benchmark_entities.limiter
        entity_id = benchmark_entities.flat[0]
        limits = [Limit.per_minute("rpm", 1_000_000)]
        num_concurrent = 20
        iterations_per_task = 5

        def worker(_task_id: int) -> tuple[int, int]:
            """Execute iterations, counting retries."""
            local_retries = 0
            local_successes = 0
            for _ in range(iterations_per_task):
                try:
                    with limiter.acquire(
                        entity_id=entity_id,
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
    """Throughput tests with hierarchical (cascade) limits.

    Uses pre-warmed parent + children from benchmark_entities.
    """

    def test_cascade_sequential_throughput(self, benchmark_entities: BenchmarkEntities):
        """Measure sequential TPS with cascade enabled.

        Cascade adds parent lookup and dual bucket updates,
        reducing throughput compared to non-cascade operations.
        """
        limiter = benchmark_entities.limiter
        parent_id = benchmark_entities.parents[0]
        child_ids = benchmark_entities.children[parent_id]
        limits = [Limit.per_minute("rpm", 1_000_000)]
        iterations = 50

        start = time.perf_counter()

        for i in range(iterations):
            child_id = child_ids[i % len(child_ids)]
            with limiter.acquire(
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

    def test_cascade_concurrent_throughput(self, benchmark_entities: BenchmarkEntities):
        """Measure concurrent TPS with cascade.

        All children share the same parent, creating contention on the
        parent bucket. With real DynamoDB, this would cause retries.
        """
        limiter = benchmark_entities.limiter
        parent_id = benchmark_entities.parents[0]
        child_ids = benchmark_entities.children[parent_id]
        limits = [Limit.per_minute("rpm", 1_000_000)]
        num_concurrent = 10
        iterations_per_task = 5

        def worker(task_id: int) -> int:
            """Execute cascade operations on dedicated child."""
            child_id = child_ids[task_id % len(child_ids)]
            successes = 0
            for _ in range(iterations_per_task):
                try:
                    with limiter.acquire(
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
```

**Step 2: Run tests to verify**

Run: `uv run pytest tests/benchmark/test_throughput.py -o "addopts=" -v --benchmark-skip`
Expected: All 7 tests PASS

**Step 3: Commit**

```
⚡ perf(benchmark): switch test_throughput.py to pre-warmed entities (#171)
```

---

### Task 3: Update test_latency.py to use benchmark_entities

**Files:**
- Modify: `tests/benchmark/test_latency.py`

**Step 1: Rewrite test_latency.py**

Replace full file content with:

```python
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

from tests.benchmark.conftest import BenchmarkEntities
from zae_limiter import Limit

pytestmark = pytest.mark.benchmark


class TestLatencyBenchmarks:
    """Capture latency percentiles for documentation.

    Each test measures a specific operation pattern using pytest-benchmark.
    The benchmark decorator captures p50/p95/p99/min/max statistics.
    All use pre-warmed entities for steady-state measurement.
    """

    @pytest.mark.benchmark(group="acquire")
    def test_acquire_single_limit_latency(self, benchmark, benchmark_entities: BenchmarkEntities):
        """Measure p50/p95/p99 for single-limit acquire.

        This is the most common operation: acquiring a single rate limit
        (e.g., requests per minute).

        Expected: ~5ms p50, ~10ms p95, ~20ms p99 (moto baseline)
        """
        limiter = benchmark_entities.limiter
        entity_id = benchmark_entities.flat[0]
        limits = [Limit.per_minute("rpm", 1_000_000)]

        def operation():
            with limiter.acquire(
                entity_id=entity_id,
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        benchmark(operation)

    @pytest.mark.benchmark(group="acquire")
    def test_acquire_two_limits_latency(self, benchmark, benchmark_entities: BenchmarkEntities):
        """Measure overhead of multi-limit acquire (rpm + tpm pattern).

        Common pattern for LLM APIs: tracking both requests per minute
        and tokens per minute in a single call.

        Expected: ~7ms p50, ~15ms p95, ~25ms p99 (adds ~2ms per limit)
        """
        limiter = benchmark_entities.limiter
        entity_id = benchmark_entities.flat[1]
        limits = [
            Limit.per_minute("rpm", 1_000_000),
            Limit.per_minute("tpm", 100_000_000),
        ]

        def operation():
            with limiter.acquire(
                entity_id=entity_id,
                resource="api",
                limits=limits,
                consume={"rpm": 1, "tpm": 100},
            ):
                pass

        benchmark(operation)

    @pytest.mark.benchmark(group="acquire")
    def test_acquire_with_cascade_latency(self, benchmark, benchmark_entities: BenchmarkEntities):
        """Measure cascade overhead.

        Cascade enables hierarchical limits where both child and parent
        entities are checked and updated.

        Expected: ~10ms p50, ~20ms p95, ~35ms p99 (adds entity lookup + parent ops)
        """
        limiter = benchmark_entities.limiter
        parent_id = benchmark_entities.parents[0]
        child_id = benchmark_entities.children[parent_id][0]
        limits = [Limit.per_minute("rpm", 1_000_000)]

        def operation():
            with limiter.acquire(
                entity_id=child_id,
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        benchmark(operation)

    @pytest.mark.benchmark(group="check")
    def test_available_check_latency(self, benchmark, benchmark_entities: BenchmarkEntities):
        """Measure read-only availability check.

        The available() method only reads bucket state without acquiring.
        This is a baseline for read operations.

        Expected: ~3ms p50, ~8ms p95, ~15ms p99 (no transaction overhead)
        """
        limiter = benchmark_entities.limiter
        entity_id = benchmark_entities.flat[0]
        limits = [Limit.per_minute("rpm", 1_000_000)]

        def operation():
            limiter.available(
                entity_id=entity_id,
                resource="api",
                limits=limits,
            )

        benchmark(operation)

    @pytest.mark.benchmark(group="acquire")
    def test_acquire_with_stored_limits_latency(
        self, benchmark, benchmark_entities: BenchmarkEntities
    ):
        """Measure stored limits query overhead.

        When use_stored_limits=True, the limiter queries DynamoDB for
        the entity's configured limits instead of using caller-provided limits.

        Expected: Adds ~2-5ms for the additional query operations.
        """
        limiter = benchmark_entities.limiter
        entity_id = benchmark_entities.flat[2]
        limits = [Limit.per_minute("rpm", 1_000_000)]

        # Setup stored limits on the shared limiter
        limiter.create_entity(entity_id, name="Stored Limits Entity")
        limiter.set_limits(entity_id, limits)

        def operation():
            with limiter.acquire(
                entity_id=entity_id,
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
    def test_baseline_no_cascade(self, benchmark, benchmark_entities: BenchmarkEntities):
        """Baseline: acquire without cascade.

        Uses a parent entity (no cascade) to measure non-cascade acquire.
        Compare with test_with_cascade to measure cascade overhead.
        """
        limiter = benchmark_entities.limiter
        parent_id = benchmark_entities.parents[0]
        limits = [Limit.per_minute("rpm", 1_000_000)]

        def operation():
            with limiter.acquire(
                entity_id=parent_id,
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        benchmark(operation)

    @pytest.mark.benchmark(group="cascade-comparison")
    def test_with_cascade(self, benchmark, benchmark_entities: BenchmarkEntities):
        """With cascade: acquire with cascade entity.

        Compare with test_baseline_no_cascade to measure cascade overhead.
        """
        limiter = benchmark_entities.limiter
        parent_id = benchmark_entities.parents[0]
        child_id = benchmark_entities.children[parent_id][0]
        limits = [Limit.per_minute("rpm", 1_000_000)]

        def operation():
            with limiter.acquire(
                entity_id=child_id,
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        benchmark(operation)

    @pytest.mark.benchmark(group="limits-comparison")
    def test_one_limit(self, benchmark, benchmark_entities: BenchmarkEntities):
        """Baseline: single limit acquire."""
        limiter = benchmark_entities.limiter
        entity_id = benchmark_entities.flat[10]
        limits = [Limit.per_minute("rpm", 1_000_000)]

        def operation():
            with limiter.acquire(
                entity_id=entity_id,
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        benchmark(operation)

    @pytest.mark.benchmark(group="limits-comparison")
    def test_two_limits(self, benchmark, benchmark_entities: BenchmarkEntities):
        """Two limits: measure per-limit overhead."""
        limiter = benchmark_entities.limiter
        entity_id = benchmark_entities.flat[11]
        limits = [
            Limit.per_minute("rpm", 1_000_000),
            Limit.per_minute("tpm", 100_000_000),
        ]

        def operation():
            with limiter.acquire(
                entity_id=entity_id,
                resource="api",
                limits=limits,
                consume={"rpm": 1, "tpm": 100},
            ):
                pass

        benchmark(operation)

    @pytest.mark.benchmark(group="limits-comparison")
    def test_five_limits(self, benchmark, benchmark_entities: BenchmarkEntities):
        """Five limits: measure scaling with limit count."""
        limiter = benchmark_entities.limiter
        entity_id = benchmark_entities.flat[12]
        limits = [Limit.per_minute(f"limit_{i}", 1_000_000) for i in range(5)]
        consume = {f"limit_{i}": 1 for i in range(5)}

        def operation():
            with limiter.acquire(
                entity_id=entity_id,
                resource="api",
                limits=limits,
                consume=consume,
            ):
                pass

        benchmark(operation)
```

**Step 2: Run tests to verify**

Run: `uv run pytest tests/benchmark/test_latency.py -o "addopts=" -v --benchmark-skip`
Expected: All 8 tests PASS

**Step 3: Commit**

```
⚡ perf(benchmark): switch test_latency.py to pre-warmed entities (#171)
```

---

### Task 4: Update test_operations.py — switchable tests only

**Files:**
- Modify: `tests/benchmark/test_operations.py`

Only update tests in categories 1 and 2 (warm-path and custom config on shared table). Keep `TestConfigLookupBenchmarks` and `TestOptimizationComparison` on function-scoped fixtures unchanged.

**Step 1: Rewrite test_operations.py**

Replace full file content with:

```python
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

Fixture scoping:
- TestAcquireReleaseBenchmarks, TestTransactionOverheadBenchmarks,
  TestCascadeOverheadBenchmarks, TestConcurrentThroughputBenchmarks
  use module-scoped benchmark_entities for steady-state measurement.
- TestConfigLookupBenchmarks, TestOptimizationComparison use
  function-scoped sync_limiter for clean-state optimization comparisons.
"""

from dataclasses import replace

import pytest

from tests.benchmark.conftest import BenchmarkEntities
from zae_limiter import Limit

pytestmark = pytest.mark.benchmark


class TestAcquireReleaseBenchmarks:
    """Benchmarks for acquire/release operations using pre-warmed entities."""

    def test_acquire_release_single_limit(self, benchmark, benchmark_entities: BenchmarkEntities):
        """Benchmark single limit acquire/release cycle.

        Measures p50/p95/p99 latency for the most common operation:
        acquiring and releasing a single rate limit.
        """
        limiter = benchmark_entities.limiter
        entity_id = benchmark_entities.flat[0]
        limits = [Limit.per_minute("rpm", 1_000_000)]

        def operation():
            with limiter.acquire(
                entity_id=entity_id,
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        benchmark(operation)

    def test_acquire_release_multiple_limits(
        self, benchmark, benchmark_entities: BenchmarkEntities
    ):
        """Benchmark multiple limits acquire/release.

        Measures overhead of tracking multiple limits (rpm + tpm) in a single call.
        """
        limiter = benchmark_entities.limiter
        entity_id = benchmark_entities.flat[1]
        limits = [
            Limit.per_minute("rpm", 1_000_000),
            Limit.per_minute("tpm", 100_000_000),
        ]

        def operation():
            with limiter.acquire(
                entity_id=entity_id,
                resource="api",
                limits=limits,
                consume={"rpm": 1, "tpm": 100},
            ):
                pass

        benchmark(operation)


class TestTransactionOverheadBenchmarks:
    """Benchmarks for DynamoDB transaction overhead using pre-warmed entities."""

    def test_available_check(self, benchmark, benchmark_entities: BenchmarkEntities):
        """Benchmark non-transactional read (baseline).

        The available() method does a simple GetItem, no transaction.
        This serves as a baseline for comparison with transactional operations.
        """
        limiter = benchmark_entities.limiter
        entity_id = benchmark_entities.flat[0]
        limits = [Limit.per_minute("rpm", 1_000_000)]

        def operation():
            limiter.available(
                entity_id=entity_id,
                resource="api",
                limits=limits,
            )

        benchmark(operation)

    def test_transactional_acquire(self, benchmark, benchmark_entities: BenchmarkEntities):
        """Benchmark transactional acquire (uses TransactWriteItems).

        Compare this with test_available_check to measure transaction overhead.
        """
        limiter = benchmark_entities.limiter
        entity_id = benchmark_entities.flat[2]
        limits = [Limit.per_minute("rpm", 1_000_000)]

        def operation():
            with limiter.acquire(
                entity_id=entity_id,
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        benchmark(operation)


class TestCascadeOverheadBenchmarks:
    """Benchmarks for hierarchical limit overhead using pre-warmed entities."""

    def test_acquire_without_cascade(self, benchmark, benchmark_entities: BenchmarkEntities):
        """Baseline: acquire without cascade.

        Uses the parent entity (no cascade), so cascade never triggers.
        Measures single-entity acquire overhead.
        """
        limiter = benchmark_entities.limiter
        parent_id = benchmark_entities.parents[0]
        limits = [Limit.per_minute("rpm", 1_000_000)]

        def operation():
            with limiter.acquire(
                entity_id=parent_id,
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        benchmark(operation)

    def test_acquire_with_cascade(self, benchmark, benchmark_entities: BenchmarkEntities):
        """Cascade enabled: measure overhead of parent lookup + dual consume.

        Uses a pre-warmed child entity with cascade=True and a parent.
        Compare with test_acquire_without_cascade to measure cascade overhead.
        """
        limiter = benchmark_entities.limiter
        parent_id = benchmark_entities.parents[0]
        child_id = benchmark_entities.children[parent_id][0]
        limits = [Limit.per_minute("rpm", 1_000_000)]

        def operation():
            with limiter.acquire(
                entity_id=child_id,
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        benchmark(operation)

    def test_cascade_with_stored_limits(self, benchmark, benchmark_entities: BenchmarkEntities):
        """Cascade with stored limits lookup.

        Additional overhead from fetching stored limits for both parent and child.
        """
        limiter = benchmark_entities.limiter
        parent_id = benchmark_entities.parents[0]
        child_id = benchmark_entities.children[parent_id][0]
        limits = [Limit.per_minute("rpm", 1_000_000)]

        # Setup stored limits on the shared limiter
        limiter.set_limits(parent_id, limits)
        limiter.set_limits(child_id, limits)

        def operation():
            with limiter.acquire(
                entity_id=child_id,
                resource="api",
                limits=limits,
                consume={"rpm": 1},
                use_stored_limits=True,
            ):
                pass

        benchmark(operation)


class TestConfigLookupBenchmarks:
    """Benchmarks for centralized config lookup overhead.

    Uses function-scoped sync_limiter for clean cache state per test.
    """

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
    """Benchmarks for throughput under load using pre-warmed entities."""

    def test_sequential_acquisitions(self, benchmark, benchmark_entities: BenchmarkEntities):
        """Benchmark sequential acquisitions for throughput baseline.

        Measures how many acquire/release cycles can be done sequentially.
        """
        limiter = benchmark_entities.limiter
        limits = [Limit.per_minute("rpm", 1_000_000)]
        iterations = 10

        def operation():
            for i in range(iterations):
                entity_id = benchmark_entities.flat[i]
                with limiter.acquire(
                    entity_id=entity_id,
                    resource="api",
                    limits=limits,
                    consume={"rpm": 1},
                ):
                    pass

        benchmark(operation)

    def test_same_entity_sequential(self, benchmark, benchmark_entities: BenchmarkEntities):
        """Benchmark sequential acquisitions on same entity.

        Measures contention overhead when repeatedly hitting the same bucket.
        """
        limiter = benchmark_entities.limiter
        entity_id = benchmark_entities.flat[0]
        limits = [Limit.per_minute("rpm", 1_000_000)]
        iterations = 10

        def operation():
            for _ in range(iterations):
                with limiter.acquire(
                    entity_id=entity_id,
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

    Uses function-scoped sync_limiter / sync_limiter_no_cache for clean
    state per test (cache disabled, entity cache cleared per iteration, etc.).

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

    @pytest.mark.benchmark(group="batch-config-resolution")
    def test_config_resolution_sequential(self, benchmark, sync_limiter_no_cache):
        """Baseline: 4 sequential GetItem calls for config resolution (#298).

        With batch operations disabled, _resolve_limits falls back to
        4 sequential GetItem calls (entity, entity_default, resource, system).
        Each iteration uses a unique entity ID to force cache misses.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]

        # Setup stored limits at resource level (resolved on cache miss)
        sync_limiter_no_cache.set_resource_defaults("batch-cmp-resource", limits)

        # Disable batch operations to force sequential path
        sync_limiter_no_cache._repository._capabilities = replace(
            sync_limiter_no_cache._repository._capabilities,
            supports_batch_operations=False,
        )

        counter = [0]

        def operation():
            counter[0] += 1
            with sync_limiter_no_cache.acquire(
                entity_id=f"seq-entity-{counter[0]}",
                resource="batch-cmp-resource",
                consume={"rpm": 1},
            ):
                pass

        benchmark(operation)

    @pytest.mark.benchmark(group="batch-config-resolution")
    def test_config_resolution_batched(self, benchmark, sync_limiter_no_cache):
        """Optimized: 1 BatchGetItem call for config resolution (#298).

        With batch operations enabled (default), _resolve_limits uses
        a single BatchGetItem to fetch all 4 config levels at once.
        Each iteration uses a unique entity ID to force cache misses.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]

        # Setup stored limits at resource level (resolved on cache miss)
        sync_limiter_no_cache.set_resource_defaults("batch-cmp-resource", limits)

        counter = [0]

        def operation():
            counter[0] += 1
            with sync_limiter_no_cache.acquire(
                entity_id=f"batch-entity-{counter[0]}",
                resource="batch-cmp-resource",
                consume={"rpm": 1},
            ):
                pass

        benchmark(operation)

    @pytest.mark.benchmark(group="cascade-speculative-comparison")
    def test_cascade_speculative_cache_cold(self, benchmark, sync_limiter):
        """Baseline: cascade speculative writes with entity cache COLD.

        Entity cache is cleared before each iteration, forcing the child-only
        speculative path. The parent goes through the normal slow path
        (BatchGetItem read + TransactWriteItems write).
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]

        # Setup hierarchy
        sync_limiter.create_entity("spec-cmp-parent", name="Parent")
        sync_limiter.create_entity(
            "spec-cmp-child",
            name="Child",
            parent_id="spec-cmp-parent",
            cascade=True,
        )

        # Pre-warm buckets + entity cache with first acquire
        with sync_limiter.acquire(
            entity_id="spec-cmp-child",
            resource="api",
            limits=limits,
            consume={"rpm": 1},
        ):
            pass

        # Enable speculative writes (default, but explicit for clarity)
        sync_limiter._speculative_writes = True

        # Second acquire warms speculative path (buckets now exist in DynamoDB)
        with sync_limiter.acquire(
            entity_id="spec-cmp-child",
            resource="api",
            limits=limits,
            consume={"rpm": 1},
        ):
            pass

        def operation():
            # Clear entity cache to force cold path each iteration
            sync_limiter._repository._entity_cache.clear()
            with sync_limiter.acquire(
                entity_id="spec-cmp-child",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        benchmark(operation)

    @pytest.mark.benchmark(group="cascade-speculative-comparison")
    def test_cascade_speculative_cache_warm(self, benchmark, sync_limiter):
        """Optimized: cascade speculative writes with entity cache WARM.

        Entity cache is pre-populated, enabling parallel speculative writes
        for both child + parent via ThreadPoolExecutor.
        Expected: Lower latency due to parallel DynamoDB writes.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]

        # Setup hierarchy
        sync_limiter.create_entity("spec-warm-parent", name="Parent")
        sync_limiter.create_entity(
            "spec-warm-child",
            name="Child",
            parent_id="spec-warm-parent",
            cascade=True,
        )

        # Pre-warm buckets + entity cache with first acquire
        with sync_limiter.acquire(
            entity_id="spec-warm-child",
            resource="api",
            limits=limits,
            consume={"rpm": 1},
        ):
            pass

        # Enable speculative writes (default, but explicit for clarity)
        sync_limiter._speculative_writes = True

        # Second acquire warms speculative path (buckets now exist in DynamoDB)
        with sync_limiter.acquire(
            entity_id="spec-warm-child",
            resource="api",
            limits=limits,
            consume={"rpm": 1},
        ):
            pass

        # Entity cache is now warm from the acquires above

        def operation():
            with sync_limiter.acquire(
                entity_id="spec-warm-child",
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
```

**Step 2: Run tests to verify**

Run: `uv run pytest tests/benchmark/test_operations.py -o "addopts=" -v --benchmark-skip`
Expected: All tests PASS (25 passed, some skipped)

**Step 3: Commit**

```
⚡ perf(benchmark): switch test_operations.py to pre-warmed entities (#171)
```

---

### Task 5: Run full benchmark suite and measure improvement

**Step 1: Run all moto-based benchmarks with --benchmark-skip**

Run: `uv run pytest tests/benchmark/test_throughput.py tests/benchmark/test_operations.py tests/benchmark/test_latency.py tests/benchmark/test_capacity.py -o "addopts=" -v --benchmark-skip --durations=0`

Expected: All tests pass. Setup times should show:
- Module-scoped tests: ~0.4s setup for first test in module, ~0s for rest
- Function-scoped tests (capacity, optimization comparison): ~0.12-0.42s each as before
- Total time should be noticeably less than baseline 9.50s

**Step 2: Run actual benchmarks to verify measurements are valid**

Run: `uv run pytest tests/benchmark/test_throughput.py -o "addopts=" -v --benchmark-only`

Expected: Benchmark results show consistent warm-path latencies (no cold-start outliers)

**Step 3: No commit needed — this is a verification step**

---

### Task 6: Update documentation

**Files:**
- Modify: `.claude/rules/testing.md`

**Step 1: Add fixture scope guidance**

After the "### Key patterns" section (around line 50), add a new section:

```markdown
### Fixture scope selection

| Scope | Use When | Example |
|-------|----------|---------|
| `function` | Test mutates state, needs isolation | `sync_limiter` (each test gets clean state) |
| `class` | Expensive setup shared by class | `e2e_limiter` (CloudFormation stack) |
| `module` | Expensive setup shared by file | `benchmark_entities` (100 pre-warmed entities) |
| `session` | Immutable configuration | `localstack_endpoint` (env var read) |

**Rule**: If fixture setup takes >100ms and is used by multiple tests in the same file, consider `scope="module"`.

**Module-scoped moto fixtures**: Can't use `monkeypatch` (function-scoped). Use `os.environ` directly with manual cleanup in teardown. The `mock_aws()` context manager scopes the mock to the module.
```

**Step 2: Commit**

```
📝 docs(test): add fixture scope selection guidance (#171)
```

---

### Task 7: Final verification and cleanup

**Step 1: Run the full test suite (not just benchmarks)**

Run: `uv run pytest tests/unit/ tests/benchmark/ -o "addopts=" -v --benchmark-skip`

Expected: All tests pass. No regressions from the fixture changes.

**Step 2: Run linting**

Run: `uv run ruff check tests/benchmark/ && uv run ruff format --check tests/benchmark/`

Expected: No lint errors

**Step 3: Commit any fixes needed, then final commit message for the branch**

No new commit unless fixes are needed.
