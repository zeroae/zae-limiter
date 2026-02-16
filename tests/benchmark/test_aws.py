"""Performance benchmark tests against real AWS DynamoDB.

These benchmarks measure production-representative latency and throughput
by running against actual AWS infrastructure.

IMPORTANT: These tests require:
1. Valid AWS credentials with permissions for:
   - CloudFormation (create/delete stacks)
   - DynamoDB (full access)
   - IAM (create roles) - unless using --no-aggregator with permission boundary
2. The --run-aws pytest flag

To run:
    pytest tests/benchmark/test_aws.py --run-aws -o "addopts=" -v --benchmark-json=aws.json

WARNING: These tests create real AWS resources and may incur charges.
Resources are cleaned up after tests, but verify via AWS Console.

NOTE: The gevent param requires GEVENT=1 env var for monkey-patching (skipped otherwise).
Example: GEVENT=1 pytest tests/benchmark/test_aws.py --run-aws -o "addopts=" -v
"""

import os
import time
import uuid
import warnings
from concurrent.futures import ThreadPoolExecutor

import pytest

from zae_limiter import Limit, StackOptions, SyncRateLimiter, SyncRepository

pytestmark = [pytest.mark.benchmark, pytest.mark.aws]


def _has_gevent() -> bool:
    try:
        import gevent  # noqa: F401

        return True
    except ImportError:
        return False


@pytest.fixture(scope="class")
def aws_unique_name():
    """Generate short unique name for AWS benchmark tests.

    Uses a short prefix to avoid IAM role name length issues when
    combined with role_name_format (see issue #252).
    Max length: 16 chars to leave room for role suffixes.
    """
    unique_id = uuid.uuid4().hex[:8]
    return f"awsbench-{unique_id}"


class TestAWSLatencyBenchmarks:
    """Latency benchmarks against real AWS DynamoDB.

    These tests provide production-representative latency measurements
    including actual network round-trips to DynamoDB.
    """

    @pytest.fixture(scope="class", params=[False, True], ids=["baseline", "speculative"])
    def aws_benchmark_limiter(self, request, aws_unique_name):
        """Create SyncRateLimiter on real AWS with minimal stack.

        Parametrized to run each test twice: once with speculative writes
        disabled (baseline) and once enabled (speculative). Each param gets
        its own stack/table to avoid collisions.
        """
        speculative = request.param
        table_name = f"{aws_unique_name}-{'spec' if speculative else 'base'}"

        stack_options = StackOptions(
            enable_aggregator=False,
            enable_alarms=False,
            usage_retention_days=1,
            permission_boundary="arn:aws:iam::aws:policy/PowerUserAccess",
            role_name_format="PowerUserPB-{}",
            policy_name_format="PowerUserPB-{}",
        )

        repo = SyncRepository.builder(table_name, "us-east-1").stack_options(stack_options).build()
        limiter = SyncRateLimiter(repository=repo, speculative_writes=speculative)

        with limiter:
            yield limiter

        # Cleanup
        try:
            repo.delete_stack()
        except Exception as e:
            warnings.warn(f"Stack cleanup failed: {e}", ResourceWarning, stacklevel=2)

    @pytest.mark.benchmark(group="aws-acquire")
    def test_acquire_single_limit_aws_latency(self, benchmark, aws_benchmark_limiter):
        """Measure p50/p95/p99 for single-limit acquire on real AWS.

        This is the baseline latency for the most common operation.
        Expected: 5-20ms p50, 10-50ms p95, 20-100ms p99.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]

        def operation():
            with aws_benchmark_limiter.acquire(
                entity_id="aws-latency-single",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        benchmark(operation)

    @pytest.mark.benchmark(group="aws-acquire")
    def test_acquire_two_limits_aws_latency(self, benchmark, aws_benchmark_limiter):
        """Measure multi-limit acquire overhead on real AWS.

        Tests the common rpm + tpm pattern for LLM APIs.
        """
        limits = [
            Limit.per_minute("rpm", 1_000_000),
            Limit.per_minute("tpm", 100_000_000),
        ]

        def operation():
            with aws_benchmark_limiter.acquire(
                entity_id="aws-latency-two",
                resource="api",
                limits=limits,
                consume={"rpm": 1, "tpm": 100},
            ):
                pass

        benchmark(operation)

    @pytest.fixture
    def aws_cascade_hierarchy(self, aws_benchmark_limiter):
        """Setup parent-child hierarchy for cascade tests."""
        aws_benchmark_limiter.create_entity("aws-cascade-parent", name="Parent")
        aws_benchmark_limiter.create_entity(
            "aws-cascade-child", name="Child", parent_id="aws-cascade-parent", cascade=True
        )
        return aws_benchmark_limiter

    @pytest.mark.benchmark(group="aws-acquire")
    def test_acquire_with_cascade_aws_latency(self, benchmark, aws_cascade_hierarchy):
        """Measure cascade overhead on real AWS.

        Cascade adds entity lookup and parent bucket operations,
        resulting in additional network round-trips.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]

        def operation():
            with aws_cascade_hierarchy.acquire(
                entity_id="aws-cascade-child",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        benchmark(operation)

    @pytest.mark.benchmark(group="aws-check")
    def test_available_check_aws_latency(self, benchmark, aws_benchmark_limiter):
        """Measure availability check latency on real AWS.

        The available() method is read-only (no transaction).
        Compare with acquire to measure transaction overhead.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]

        # Setup: create bucket first
        with aws_benchmark_limiter.acquire(
            entity_id="aws-available-entity",
            resource="api",
            limits=limits,
            consume={"rpm": 1},
        ):
            pass

        def operation():
            aws_benchmark_limiter.available(
                entity_id="aws-available-entity",
                resource="api",
                limits=limits,
            )

        benchmark(operation)


class TestAWSThroughputBenchmarks:
    """Throughput benchmarks against real AWS DynamoDB.

    These tests measure actual throughput including network latency
    and DynamoDB's optimistic locking behavior under contention.
    """

    @pytest.fixture(scope="class")
    def aws_throughput_limiter(self, aws_unique_name):
        """Create SyncRateLimiter for throughput tests."""
        # Use a different table from latency tests
        table_name = f"{aws_unique_name}-tp"

        stack_options = StackOptions(
            enable_aggregator=False,
            enable_alarms=False,
            usage_retention_days=1,
            permission_boundary="arn:aws:iam::aws:policy/PowerUserAccess",
            role_name_format="PowerUserPB-{}",
            policy_name_format="PowerUserPB-{}",
        )

        repo = SyncRepository.builder(table_name, "us-east-1").stack_options(stack_options).build()
        limiter = SyncRateLimiter(repository=repo)

        with limiter:
            yield limiter

        try:
            repo.delete_stack()
        except Exception as e:
            warnings.warn(f"Stack cleanup failed: {e}", ResourceWarning, stacklevel=2)

    def test_sequential_throughput_aws(self, aws_throughput_limiter):
        """Measure max sequential TPS on real AWS.

        Executes 100 sequential acquire operations and measures
        aggregate throughput.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]
        iterations = 100

        start = time.perf_counter()

        for _ in range(iterations):
            with aws_throughput_limiter.acquire(
                entity_id="aws-throughput-seq",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        elapsed = time.perf_counter() - start
        tps = iterations / elapsed

        print(f"\nAWS Sequential TPS: {tps:.2f} ops/sec")
        print(f"Average latency: {(elapsed / iterations) * 1000:.2f} ms")

        # AWS should complete reasonably fast
        assert elapsed < 120, "Sequential operations took too long"
        assert tps > 1, "TPS should be greater than 1 for real AWS"

    def test_sequential_throughput_multiple_entities_aws(self, aws_throughput_limiter):
        """Measure sequential TPS across multiple entities on AWS.

        Round-robin across 10 entities to eliminate single-bucket contention.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]
        num_entities = 10
        iterations_per_entity = 10
        total_iterations = num_entities * iterations_per_entity

        start = time.perf_counter()

        for i in range(total_iterations):
            entity_id = f"aws-throughput-multi-{i % num_entities}"
            with aws_throughput_limiter.acquire(
                entity_id=entity_id,
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        elapsed = time.perf_counter() - start
        tps = total_iterations / elapsed

        print(f"\nAWS Multi-entity Sequential TPS: {tps:.2f} ops/sec")
        print(f"Average latency: {(elapsed / total_iterations) * 1000:.2f} ms")

        assert elapsed < 120, "Multi-entity operations took too long"

    def test_concurrent_throughput_aws(self, aws_throughput_limiter):
        """Measure concurrent throughput on real AWS.

        Uses ThreadPoolExecutor to run concurrent sync operations on different
        entities, measuring parallel throughput without bucket contention.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]
        num_concurrent = 10
        iterations_per_task = 10

        def worker(task_id: int) -> int:
            """Execute iterations on dedicated entity."""
            entity_id = f"aws-concurrent-{task_id}"
            successes = 0

            for _ in range(iterations_per_task):
                try:
                    with aws_throughput_limiter.acquire(
                        entity_id=entity_id,
                        resource="api",
                        limits=limits,
                        consume={"rpm": 1},
                    ):
                        successes += 1
                except Exception as e:
                    print(f"Task {task_id} error: {e}")

            return successes

        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=num_concurrent) as executor:
            results = list(executor.map(worker, range(num_concurrent)))
        total_elapsed = time.perf_counter() - start

        total_successes = sum(results)
        total_iterations = num_concurrent * iterations_per_task

        tps = total_successes / total_elapsed

        print(f"\nAWS Concurrent TPS: {tps:.2f} ops/sec")
        print(f"Total successes: {total_successes}/{total_iterations}")
        print(f"Wall clock time: {total_elapsed:.2f} s")

        # All operations should succeed with proper thread concurrency
        assert total_successes == total_iterations, "All operations should succeed"

    def test_contention_behavior_aws(self, aws_throughput_limiter):
        """Measure contention behavior on real AWS.

        Multiple concurrent tasks compete for the same bucket to observe
        DynamoDB's optimistic locking retry behavior.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]
        num_concurrent = 10
        iterations_per_task = 5

        def worker(_task_id: int) -> tuple[int, int]:
            """Execute iterations on shared entity, counting outcomes."""
            successes = 0
            retries = 0

            for _ in range(iterations_per_task):
                try:
                    with aws_throughput_limiter.acquire(
                        entity_id="aws-contention-shared",
                        resource="api",
                        limits=limits,
                        consume={"rpm": 1},
                    ):
                        successes += 1
                except Exception as e:
                    error_msg = str(e)
                    if "ConditionalCheckFailed" in error_msg or "TransactionCanceled" in error_msg:
                        retries += 1
                    # Note: The limiter may retry internally, so we might not see failures

            return successes, retries

        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=num_concurrent) as executor:
            results = list(executor.map(worker, range(num_concurrent)))
        elapsed = time.perf_counter() - start

        total_successes = sum(r[0] for r in results)
        total_retries = sum(r[1] for r in results)
        total_operations = num_concurrent * iterations_per_task

        print("\nAWS Contention test results:")
        print(f"  Total operations: {total_operations}")
        print(f"  Successes: {total_successes}")
        print(f"  Visible retries: {total_retries}")
        print(f"  Elapsed time: {elapsed:.2f} s")

        # All operations should eventually succeed (internal retries handle contention)
        assert total_successes == total_operations, "All operations should succeed"


class TestAWSCascadeSpeculativeComparison:
    """Compare cascade speculative writes across parallel modes on real AWS.

    Tests three parallel_mode strategies for cache-warm cascade speculative writes:
    serial, gevent, and threadpool. Also includes non-cascade and cache-cold baselines.

    Real AWS DynamoDB provides the highest-fidelity measurement of the parallel
    speculative write optimization.
    """

    @pytest.fixture(scope="class")
    def aws_speculative_stack(self, aws_unique_name):
        """Deploy stack and create entities for speculative comparison tests."""
        table_name = f"{aws_unique_name}-spc"

        stack_options = StackOptions(
            enable_aggregator=False,
            enable_alarms=False,
            usage_retention_days=1,
            permission_boundary="arn:aws:iam::aws:policy/PowerUserAccess",
            role_name_format="PowerUserPB-{}",
            policy_name_format="PowerUserPB-{}",
        )

        repo = SyncRepository.builder(table_name, "us-east-1").stack_options(stack_options).build()
        limiter = SyncRateLimiter(repository=repo)

        with limiter:
            limits = [Limit.per_minute("rpm", 1_000_000)]

            # Setup hierarchy
            limiter.create_entity("aws-spec-parent", name="Parent")
            limiter.create_entity(
                "aws-spec-child",
                name="Child",
                parent_id="aws-spec-parent",
                cascade=True,
            )

            # Pre-warm buckets with two acquires (creates bucket items in DynamoDB)
            for _ in range(2):
                with limiter.acquire(
                    entity_id="aws-spec-child",
                    resource="api",
                    limits=limits,
                    consume={"rpm": 1},
                ):
                    pass

            yield table_name

        try:
            repo.delete_stack()
        except Exception as e:
            warnings.warn(f"Stack cleanup failed: {e}", ResourceWarning, stacklevel=2)

    @pytest.fixture(scope="class", params=[False, True], ids=["baseline", "speculative"])
    def aws_speculative_limiter(self, request, aws_speculative_stack):
        """Create SyncRateLimiter for non-cascade and cache-cold tests.

        Parametrized to run each test twice: baseline (speculative_writes=False)
        and speculative (speculative_writes=True) for side-by-side comparison.
        """
        speculative = request.param
        repo = SyncRepository.connect(aws_speculative_stack, "us-east-1")
        limiter = SyncRateLimiter(repository=repo, speculative_writes=speculative)

        # Warm entity cache
        limits = [Limit.per_minute("rpm", 1_000_000)]
        with limiter.acquire(
            entity_id="aws-spec-child",
            resource="api",
            limits=limits,
            consume={"rpm": 1},
        ):
            pass

        yield limiter
        repo.close()

    @pytest.fixture(
        params=[
            "serial",
            pytest.param(
                "gevent",
                marks=pytest.mark.skipif(
                    not _has_gevent() or not os.environ.get("GEVENT"),
                    reason="gevent requires GEVENT=1 env var for monkey-patching",
                ),
            ),
            "threadpool",
        ]
    )
    def parallel_mode_limiter(self, request, aws_speculative_stack):
        """Create SyncRateLimiter with specific parallel_mode, cache warmed."""
        mode = request.param
        repo = SyncRepository.connect(aws_speculative_stack, "us-east-1", parallel_mode=mode)
        limiter = SyncRateLimiter(repository=repo, speculative_writes=True)

        # Warm entity cache + speculative path
        limits = [Limit.per_minute("rpm", 1_000_000)]
        with limiter.acquire(
            entity_id="aws-spec-child",
            resource="api",
            limits=limits,
            consume={"rpm": 1},
        ):
            pass

        yield limiter
        repo.close()

    @pytest.mark.benchmark(group="aws-cascade-speculative")
    def test_non_cascade_speculative_aws(self, benchmark, aws_speculative_limiter):
        """Reference: non-cascade speculative write on real AWS.

        Single-entity speculative write (no parent).
        Provides a baseline to gauge the overhead of the parallel cascade path.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]

        def operation():
            with aws_speculative_limiter.acquire(
                entity_id="aws-spec-parent",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        benchmark(operation)

    @pytest.mark.benchmark(group="aws-cascade-speculative")
    def test_cascade_speculative_cache_cold_aws(self, benchmark, aws_speculative_limiter):
        """Baseline: cascade speculative writes with entity cache COLD on real AWS.

        Entity cache is cleared before each iteration, forcing the child-only
        speculative path. The parent goes through the normal slow path
        (BatchGetItem read + TransactWriteItems write) -- sequential round trips.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]

        def operation():
            aws_speculative_limiter._repository._entity_cache.clear()
            with aws_speculative_limiter.acquire(
                entity_id="aws-spec-child",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        benchmark(operation)

    @pytest.mark.benchmark(group="aws-cascade-speculative")
    def test_cascade_speculative_cache_warm_aws(self, benchmark, parallel_mode_limiter):
        """Cache-warm cascade speculative writes, parametrized by parallel_mode.

        Entity cache is pre-populated, enabling parallel speculative writes
        for both child + parent. Runs for each mode: serial, gevent, threadpool.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]
        mode = parallel_mode_limiter._repository._parallel_mode

        def operation():
            with parallel_mode_limiter.acquire(
                entity_id="aws-spec-child",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        benchmark.extra_info["parallel_mode"] = mode
        benchmark(operation)


class TestAWSCascadeBenchmarks:
    """Cascade-specific benchmarks on real AWS.

    Cascade operations have higher contention potential since
    multiple children share the same parent bucket. The concurrent
    test is parametrized by parallel_mode to measure how internal
    cascade parallelism interacts with external thread contention.
    """

    @pytest.fixture(scope="class")
    def aws_cascade_stack(self, aws_unique_name):
        """Deploy stack and create cascade hierarchy for benchmark tests."""
        table_name = f"{aws_unique_name}-cas"

        stack_options = StackOptions(
            enable_aggregator=False,
            enable_alarms=False,
            usage_retention_days=1,
            permission_boundary="arn:aws:iam::aws:policy/PowerUserAccess",
            role_name_format="PowerUserPB-{}",
            policy_name_format="PowerUserPB-{}",
        )

        repo = SyncRepository.builder(table_name, "us-east-1").stack_options(stack_options).build()
        limiter = SyncRateLimiter(repository=repo)

        with limiter:
            # Create parent and children
            limiter.create_entity("aws-cascade-root", name="Root")
            for i in range(10):
                limiter.create_entity(
                    f"aws-cascade-child-{i}",
                    name=f"Child {i}",
                    parent_id="aws-cascade-root",
                    cascade=True,
                )

            # Pre-warm buckets
            limits = [Limit.per_minute("rpm", 1_000_000)]
            for i in range(10):
                with limiter.acquire(
                    entity_id=f"aws-cascade-child-{i}",
                    resource="api",
                    limits=limits,
                    consume={"rpm": 1},
                ):
                    pass

            yield table_name

        try:
            repo.delete_stack()
        except Exception as e:
            warnings.warn(f"Stack cleanup failed: {e}", ResourceWarning, stacklevel=2)

    @pytest.fixture(scope="class")
    def aws_cascade_limiter(self, aws_cascade_stack):
        """Create default SyncRateLimiter for sequential cascade tests."""
        repo = SyncRepository.connect(aws_cascade_stack, "us-east-1")
        limiter = SyncRateLimiter(repository=repo, speculative_writes=True)

        # Warm entity cache
        limits = [Limit.per_minute("rpm", 1_000_000)]
        with limiter.acquire(
            entity_id="aws-cascade-child-0",
            resource="api",
            limits=limits,
            consume={"rpm": 1},
        ):
            pass

        yield limiter
        repo.close()

    @pytest.fixture(
        params=[
            "serial",
            pytest.param(
                "gevent",
                marks=pytest.mark.skipif(
                    not _has_gevent() or not os.environ.get("GEVENT"),
                    reason="gevent requires GEVENT=1 env var for monkey-patching",
                ),
            ),
            "threadpool",
        ]
    )
    def parallel_mode_cascade_limiter(self, request, aws_cascade_stack):
        """Create SyncRateLimiter with specific parallel_mode for concurrent tests."""
        mode = request.param
        repo = SyncRepository.connect(aws_cascade_stack, "us-east-1", parallel_mode=mode)
        limiter = SyncRateLimiter(repository=repo, speculative_writes=True)

        # Warm entity cache for all children
        limits = [Limit.per_minute("rpm", 1_000_000)]
        for i in range(10):
            with limiter.acquire(
                entity_id=f"aws-cascade-child-{i}",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        yield limiter
        repo.close()

    def test_cascade_sequential_throughput_aws(self, aws_cascade_limiter):
        """Measure sequential cascade TPS on AWS.

        All operations cascade to the shared parent bucket.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]
        iterations = 50

        start = time.perf_counter()

        for i in range(iterations):
            child_id = f"aws-cascade-child-{i % 10}"
            with aws_cascade_limiter.acquire(
                entity_id=child_id,
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        elapsed = time.perf_counter() - start
        tps = iterations / elapsed

        print(f"\nAWS Cascade Sequential TPS: {tps:.2f} ops/sec")
        print(f"Average latency: {(elapsed / iterations) * 1000:.2f} ms")

        assert elapsed < 120, "Cascade operations took too long"

    @pytest.mark.xfail(
        reason="TransactionConflict not retried in _commit_initial (#332)",
        strict=False,
    )
    def test_cascade_concurrent_throughput_aws(self, parallel_mode_cascade_limiter):
        """Measure concurrent cascade TPS on AWS, parametrized by parallel_mode.

        Multiple concurrent tasks update different children but share the parent,
        creating contention on the parent bucket. Tests how internal cascade
        parallelism (serial/gevent/threadpool) interacts with external thread
        contention.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]
        num_concurrent = 10
        iterations_per_task = 5
        mode = parallel_mode_cascade_limiter._repository._parallel_mode

        def worker(task_id: int) -> int:
            """Execute cascade operations on dedicated child."""
            child_id = f"aws-cascade-child-{task_id}"
            successes = 0

            for _ in range(iterations_per_task):
                try:
                    with parallel_mode_cascade_limiter.acquire(
                        entity_id=child_id,
                        resource="api",
                        limits=limits,
                        consume={"rpm": 1},
                    ):
                        successes += 1
                except Exception as e:
                    print(f"Task {task_id} cascade error ({mode}): {e}")

            return successes

        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=num_concurrent) as executor:
            results = list(executor.map(worker, range(num_concurrent)))
        total_elapsed = time.perf_counter() - start

        total_successes = sum(results)
        total_iterations = num_concurrent * iterations_per_task

        tps = total_successes / total_elapsed

        print(f"\nAWS Cascade Concurrent TPS ({mode}): {tps:.2f} ops/sec")
        print(f"Total successes: {total_successes}/{total_iterations}")

        # All operations should succeed (internal retries handle parent contention)
        assert total_successes == total_iterations, "All operations should succeed"
