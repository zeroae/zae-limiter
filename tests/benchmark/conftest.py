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
from dataclasses import dataclass

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
        flat: 15 standalone entity IDs (bench-entity-000..014)
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
        _skip_deprecation_warning=True,
    )
    repo.create_table()
    limiter = SyncRateLimiter(repository=repo)
    with limiter:
        yield limiter


@pytest.fixture(scope="module")
def benchmark_entities(benchmark_limiter):
    """15 pre-warmed flat entities + 1 parent with 10 cascade children.

    Created once per test file. All entities have completed one acquire
    cycle so buckets exist in DynamoDB (no cold-start overhead).

    Contents:
        flat: bench-entity-000..014 (standalone, pre-warmed on resource "benchmark")
        parents: [bench-parent-0]
        children: {bench-parent-0: [bench-child-0-00..09]} (cascade=True)
    """
    limits = [Limit.per_minute("rpm", 1_000_000)]

    # Create and warm 15 flat entities (max index used by tests: 12)
    flat_ids = [f"bench-entity-{i:03d}" for i in range(15)]
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
        _skip_deprecation_warning=True,
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
