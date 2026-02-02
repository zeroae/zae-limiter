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

import time

import pytest

from zae_limiter import Limit

pytestmark = [pytest.mark.benchmark, pytest.mark.integration]


class TestLocalStackBenchmarks:
    """Benchmarks against LocalStack for realistic DynamoDB latency.

    These tests require LocalStack to be running.
    Uses module-scoped fixtures to share infrastructure across tests.
    """

    def test_acquire_release_localstack(
        self, benchmark, sync_localstack_limiter_module, unique_entity_prefix
    ):
        """Benchmark acquire/release against real DynamoDB (LocalStack).

        Measures realistic latency including network round-trip.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]
        entity_id = f"{unique_entity_prefix}-bench-entity"

        def operation():
            with sync_localstack_limiter_module.acquire(
                entity_id=entity_id,
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        benchmark(operation)

    @pytest.fixture
    def cascade_hierarchy(self, sync_localstack_limiter_module, unique_entity_prefix):
        """Setup parent-child hierarchy for cascade tests."""
        parent_id = f"{unique_entity_prefix}-cascade-parent"
        child_id = f"{unique_entity_prefix}-cascade-child"
        sync_localstack_limiter_module.create_entity(parent_id, name="Parent")
        sync_localstack_limiter_module.create_entity(
            child_id, name="Child", parent_id=parent_id, cascade=True
        )
        return sync_localstack_limiter_module, child_id

    def test_cascade_localstack(self, benchmark, cascade_hierarchy):
        """Benchmark cascade against LocalStack.

        Measures realistic cascade overhead including network latency.
        """
        limiter, child_id = cascade_hierarchy
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


class TestLocalStackLatencyBenchmarks:
    """Realistic latency measurements with LocalStack.

    These benchmarks capture p50/p95/p99 latency including actual network
    round-trips to LocalStack's DynamoDB emulation. Results are more
    representative of production behavior than moto-based tests.

    Uses module-scoped fixtures to share infrastructure across tests.
    """

    @pytest.mark.benchmark(group="localstack-acquire")
    def test_acquire_realistic_latency(
        self, benchmark, sync_localstack_limiter_module, unique_entity_prefix
    ):
        """Measure realistic latency including network overhead.

        This test captures the full latency of an acquire operation
        including network round-trips to LocalStack's DynamoDB.

        Expected: Higher than moto benchmarks due to actual network I/O.
        Typical values: 10-50ms p50, 20-100ms p95.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]
        entity_id = f"{unique_entity_prefix}-latency-entity"

        def operation():
            with sync_localstack_limiter_module.acquire(
                entity_id=entity_id,
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        benchmark(operation)

    @pytest.mark.benchmark(group="localstack-acquire")
    def test_acquire_two_limits_realistic_latency(
        self, benchmark, sync_localstack_limiter_module, unique_entity_prefix
    ):
        """Measure multi-limit acquire with realistic network latency.

        Tests the overhead of tracking multiple limits (rpm + tpm pattern)
        with actual network I/O.
        """
        limits = [
            Limit.per_minute("rpm", 1_000_000),
            Limit.per_minute("tpm", 100_000_000),
        ]
        entity_id = f"{unique_entity_prefix}-latency-multi"

        def operation():
            with sync_localstack_limiter_module.acquire(
                entity_id=entity_id,
                resource="api",
                limits=limits,
                consume={"rpm": 1, "tpm": 100},
            ):
                pass

        benchmark(operation)

    @pytest.fixture
    def cascade_latency_hierarchy(self, sync_localstack_limiter_module, unique_entity_prefix):
        """Setup parent-child hierarchy for cascade latency tests."""
        parent_id = f"{unique_entity_prefix}-latency-parent"
        child_id = f"{unique_entity_prefix}-latency-child"
        sync_localstack_limiter_module.create_entity(parent_id, name="Parent")
        sync_localstack_limiter_module.create_entity(
            child_id, name="Child", parent_id=parent_id, cascade=True
        )
        return sync_localstack_limiter_module, child_id

    @pytest.mark.benchmark(group="localstack-acquire")
    def test_cascade_realistic_latency(self, benchmark, cascade_latency_hierarchy):
        """Measure cascade with realistic network latency.

        Cascade requires additional network round-trips:
        - Entity lookup (to find parent)
        - Parent bucket read
        - Transaction with child + parent updates

        Expected: Higher overhead than non-cascade due to extra round-trips.
        """
        limiter, child_id = cascade_latency_hierarchy
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

    @pytest.mark.benchmark(group="localstack-check")
    def test_available_realistic_latency(
        self, benchmark, sync_localstack_limiter_module, unique_entity_prefix
    ):
        """Measure read-only availability check with network latency.

        The available() method only reads bucket state.
        Compare with acquire to measure transaction overhead.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]
        entity_id = f"{unique_entity_prefix}-available-entity"

        # Setup: create bucket first
        with sync_localstack_limiter_module.acquire(
            entity_id=entity_id,
            resource="api",
            limits=limits,
            consume={"rpm": 1},
        ):
            pass

        def operation():
            sync_localstack_limiter_module.available(
                entity_id=entity_id,
                resource="api",
                limits=limits,
            )

        benchmark(operation)


class TestCascadeOptimizationBenchmarks:
    """Benchmarks for cascade optimization using BatchGetItem (issue #133).

    These benchmarks measure the impact of the BatchGetItem optimization
    which reduces multiple GetItem calls to a single BatchGetItem call
    when resolving cascade scenarios.

    Uses module-scoped fixtures to share infrastructure across tests.
    """

    @pytest.fixture
    def deep_hierarchy(self, sync_localstack_limiter_module, unique_entity_prefix):
        """Setup deeper hierarchy for cascade optimization testing."""
        root_id = f"{unique_entity_prefix}-opt-root"
        level1_id = f"{unique_entity_prefix}-opt-level1"
        level2_id = f"{unique_entity_prefix}-opt-level2"
        sync_localstack_limiter_module.create_entity(root_id, name="Root")
        sync_localstack_limiter_module.create_entity(
            level1_id, name="Level 1", parent_id=root_id, cascade=True
        )
        sync_localstack_limiter_module.create_entity(
            level2_id, name="Level 2", parent_id=level1_id, cascade=True
        )
        return sync_localstack_limiter_module, level2_id

    @pytest.mark.benchmark(group="cascade-optimization")
    def test_cascade_with_batchgetitem_optimization(self, benchmark, deep_hierarchy):
        """Measure cascade using optimized BatchGetItem pattern.

        The acquire() method uses BatchGetItem to fetch entity and parent
        buckets in a single round-trip (issue #133), reducing latency.

        Expected: Single round-trip vs sequential GetItem calls.
        """
        limiter, level2_id = deep_hierarchy
        limits = [Limit.per_minute("rpm", 1_000_000)]

        def operation():
            with limiter.acquire(
                entity_id=level2_id,
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        benchmark(operation)

    @pytest.mark.benchmark(group="cascade-optimization")
    def test_cascade_multiple_resources(self, benchmark, deep_hierarchy):
        """Measure cascade with multiple resources (realistic workload).

        Measures cascade overhead when tracking multiple resource limits
        (e.g., gpt-4 and gpt-3.5-turbo).
        """
        limiter, level2_id = deep_hierarchy
        limits = [
            Limit.per_minute("rpm", 1_000_000),
            Limit.per_minute("tpm", 100_000_000),
        ]

        def operation():
            with limiter.acquire(
                entity_id=level2_id,
                resource="api",
                limits=limits,
                consume={"rpm": 1, "tpm": 100},
            ):
                pass

        benchmark(operation)

    @pytest.fixture
    def cascade_with_config(self, sync_localstack_limiter_module, unique_entity_prefix):
        """Setup hierarchy with stored config for optimization testing."""
        limits = [Limit.per_minute("rpm", 1_000_000)]
        root_id = f"{unique_entity_prefix}-opt-config-root"
        child_id = f"{unique_entity_prefix}-opt-config-child"

        # Create hierarchy
        sync_localstack_limiter_module.create_entity(root_id, name="Root")
        sync_localstack_limiter_module.create_entity(
            child_id,
            name="Child",
            parent_id=root_id,
            cascade=True,
        )

        # Set stored limits to exercise config cache + cascade
        sync_localstack_limiter_module.set_limits(root_id, limits)
        sync_localstack_limiter_module.set_limits(child_id, limits)

        return sync_localstack_limiter_module, child_id

    @pytest.mark.benchmark(group="cascade-optimization")
    def test_cascade_with_config_cache_optimization(self, benchmark, cascade_with_config):
        """Measure cascade with both config cache and BatchGetItem optimization.

        This is the ideal case: config cache reduces config lookups, and
        BatchGetItem reduces bucket fetches to a single round-trip.

        Expected: Combines benefits of both optimizations.
        """
        limiter, child_id = cascade_with_config
        limits = [Limit.per_minute("rpm", 1_000_000)]

        # Warm up cache
        with limiter.acquire(
            entity_id=child_id,
            resource="api",
            limits=limits,
            consume={"rpm": 1},
        ):
            pass

        def operation():
            with limiter.acquire(
                entity_id=child_id,
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        benchmark(operation)


class TestLocalStackOptimizationComparison:
    """Direct comparison of v0.5.0 optimizations with realistic network latency.

    These tests measure the actual impact of optimizations when network
    round-trips are involved, providing production-representative metrics.
    """

    @pytest.fixture
    def hierarchy_no_cache(self, sync_localstack_limiter_no_cache):
        """Setup hierarchy for cache-disabled comparison."""
        sync_localstack_limiter_no_cache.create_entity("ls-cmp-nocache-parent", name="Parent")
        sync_localstack_limiter_no_cache.create_entity(
            "ls-cmp-nocache-child", name="Child", parent_id="ls-cmp-nocache-parent", cascade=True
        )
        return sync_localstack_limiter_no_cache

    @pytest.fixture
    def hierarchy_with_cache(self, sync_localstack_limiter):
        """Setup hierarchy for cache-enabled comparison."""
        sync_localstack_limiter.create_entity("ls-cmp-cache-parent", name="Parent")
        sync_localstack_limiter.create_entity(
            "ls-cmp-cache-child", name="Child", parent_id="ls-cmp-cache-parent", cascade=True
        )
        return sync_localstack_limiter

    @pytest.mark.benchmark(group="localstack-cache-comparison")
    def test_cascade_cache_disabled_localstack(self, benchmark, hierarchy_no_cache):
        """Baseline: cascade with cache DISABLED on LocalStack.

        Measures realistic latency when every request fetches config from DynamoDB.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]

        # Set stored limits
        hierarchy_no_cache.set_limits("ls-cmp-nocache-parent", limits)
        hierarchy_no_cache.set_limits("ls-cmp-nocache-child", limits)

        def operation():
            with hierarchy_no_cache.acquire(
                entity_id="ls-cmp-nocache-child",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        benchmark(operation)

    @pytest.mark.benchmark(group="localstack-cache-comparison")
    def test_cascade_cache_enabled_localstack(self, benchmark, hierarchy_with_cache):
        """Optimized: cascade with cache ENABLED on LocalStack.

        After warmup, config is served from cache, reducing network round-trips.
        Compare with test_cascade_cache_disabled_localstack for improvement %.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]

        # Set stored limits
        hierarchy_with_cache.set_limits("ls-cmp-cache-parent", limits)
        hierarchy_with_cache.set_limits("ls-cmp-cache-child", limits)

        # Warm up cache
        with hierarchy_with_cache.acquire(
            entity_id="ls-cmp-cache-child",
            resource="api",
            limits=limits,
            consume={"rpm": 1},
        ):
            pass

        def operation():
            with hierarchy_with_cache.acquire(
                entity_id="ls-cmp-cache-child",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        benchmark(operation)


class TestLambdaColdStartBenchmarks:
    """Benchmarks for Lambda cold start latency with aggregator.

    These tests measure the latency of Lambda invocations through DynamoDB
    Streams when the aggregator function is freshly deployed (cold start).

    Cold start occurs when:
    - Lambda function is first invoked after deployment
    - Lambda function hasn't been invoked recently (timeout expired)
    - Lambda container was recycled

    In LocalStack, cold start is emulated but may not match real AWS latency.
    These benchmarks establish a baseline for cold start performance.

    Uses class-scoped fixture with unique_entity_prefix for data isolation.
    See issue #253 for details.
    """

    @pytest.fixture
    def lambda_cold_start_hierarchy(
        self, sync_localstack_limiter_with_aggregator, unique_entity_prefix
    ):
        """Setup entity for cold start benchmark.

        We create a fresh entity that hasn't been used yet to ensure
        the Lambda function gets invoked for the first time.

        Uses unique_entity_prefix for data isolation within the class-scoped stack.
        """
        entity_id = f"{unique_entity_prefix}-lambda-cold-entity"
        sync_localstack_limiter_with_aggregator.create_entity(
            entity_id, name="Lambda Cold Start Test"
        )
        return sync_localstack_limiter_with_aggregator, entity_id, unique_entity_prefix

    @pytest.mark.benchmark(group="lambda-cold-start")
    def test_lambda_cold_start_first_invocation(self, benchmark, lambda_cold_start_hierarchy):
        """Measure Lambda cold start time on first aggregator invocation.

        This benchmark captures the time from token consumption write
        through to Lambda processing the DynamoDB stream record.

        Cold start includes:
        - Container initialization
        - Runtime startup
        - Handler code loading
        - First stream record processing

        Expected: Higher latency than warm start (100-500ms typical).
        In LocalStack, latency may be lower due to local execution.
        """
        limiter, entity_id, _ = lambda_cold_start_hierarchy
        limits = [Limit.per_minute("rpm", 1_000_000)]

        def operation():
            # Consume tokens (triggers DynamoDB stream write)
            with limiter.acquire(
                entity_id=entity_id,
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass
            # Allow time for stream processing to complete
            time.sleep(0.5)

        benchmark(operation)

    @pytest.mark.benchmark(group="lambda-warm-start")
    def test_lambda_warm_start_subsequent_invocation(self, benchmark, lambda_cold_start_hierarchy):
        """Measure Lambda warm start time on subsequent invocations.

        After the initial cold start, Lambda container remains warm and
        reuses connection pools, avoiding initialization overhead.

        Warm start is faster than cold start because:
        - Container already initialized
        - Global variables already loaded
        - Connection pools pre-warmed

        Expected: 50-200ms latency (lower than cold start).
        """
        limiter, entity_id, _ = lambda_cold_start_hierarchy
        limits = [Limit.per_minute("rpm", 1_000_000)]

        # First, warm up the Lambda by doing an initial invocation
        # This simulates real usage where cold start is already past
        with limiter.acquire(
            entity_id=entity_id,
            resource="api",
            limits=limits,
            consume={"rpm": 1},
        ):
            pass
        time.sleep(0.5)

        def operation():
            # Subsequent invocation - Lambda container is warm
            with limiter.acquire(
                entity_id=entity_id,
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass
            # Allow time for stream processing
            time.sleep(0.5)

        benchmark(operation)

    @pytest.mark.benchmark(group="lambda-cold-start")
    def test_lambda_cold_start_multiple_concurrent_events(
        self, benchmark, lambda_cold_start_hierarchy
    ):
        """Measure Lambda cold start when handling concurrent stream events.

        When multiple token consumption operations trigger stream records
        simultaneously, Lambda may handle them:
        - Serially in same container (warm start after first)
        - In parallel via concurrent container instances (cold start for each)

        This test measures the aggregate time to process multiple concurrent
        consumption events during cold start phase.

        Expected: Scales with event count but benefits from batching.
        """
        limiter, _, prefix = lambda_cold_start_hierarchy
        limits = [Limit.per_minute("rpm", 1_000_000)]

        def operation():
            # Generate multiple concurrent consumptions
            # In a real system, these would create multiple stream records
            for i in range(3):
                with limiter.acquire(
                    entity_id=f"{prefix}-lambda-cold-entity-multi-{i}",
                    resource="api",
                    limits=limits,
                    consume={"rpm": 1},
                ):
                    pass
            # Allow time for all stream processing
            time.sleep(1.0)

        benchmark(operation)

    @pytest.mark.benchmark(group="lambda-warm-start")
    def test_lambda_warm_start_sustained_load(self, benchmark, lambda_cold_start_hierarchy):
        """Measure Lambda warm start under sustained token consumption load.

        After cold start, Lambda container handles multiple sequential
        invocations without re-initializing, maintaining connection pools
        and cached state.

        This test simulates sustained usage where Lambda stays warm
        throughout the benchmark measurement.

        Expected: Consistent latency (10-50ms per operation).
        """
        limiter, entity_id, _ = lambda_cold_start_hierarchy
        limits = [Limit.per_minute("rpm", 1_000_000)]

        # Pre-warm Lambda
        with limiter.acquire(
            entity_id=entity_id,
            resource="api",
            limits=limits,
            consume={"rpm": 1},
        ):
            pass
        time.sleep(0.5)

        def operation():
            # Repeated operations with warm container
            for _ in range(5):
                with limiter.acquire(
                    entity_id=entity_id,
                    resource="api",
                    limits=limits,
                    consume={"rpm": 1},
                ):
                    pass
                time.sleep(0.1)  # Small delay between operations
            time.sleep(0.5)  # Final wait for processing

        benchmark(operation)
