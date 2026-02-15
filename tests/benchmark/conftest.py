"""Benchmark test fixtures.

Provides fixtures for capacity counting, pre-warmed entities,
and optimization comparison benchmarks.
"""

import uuid
from typing import Any

import pytest

from tests.fixtures.capacity import (  # noqa: F401
    CapacityCounter,
    _counting_client,
    capacity_counter,
)
from tests.fixtures.moto import (  # noqa: F401
    _patch_aiobotocore_response,
    aws_credentials,
    mock_dynamodb,
    sync_limiter,
)
from tests.fixtures.names import unique_name, unique_name_class, unique_namespace  # noqa: F401
from tests.fixtures.repositories import make_sync_test_repo
from tests.fixtures.stacks import (  # noqa: F401
    aggregator_stack_options,
    localstack_endpoint,
    minimal_stack_options,
    shared_aggregator_stack,
    shared_minimal_stack,
)
from zae_limiter import Limit, SyncRateLimiter
from zae_limiter.sync_repository import SyncRepository

# Namespace-scoped sync fixtures for LocalStack benchmarks


@pytest.fixture
def sync_localstack_limiter(shared_minimal_stack, unique_namespace):  # noqa: F811
    """SyncRateLimiter on the shared minimal stack with namespace isolation."""
    parent, scoped = make_sync_test_repo(shared_minimal_stack, unique_namespace)
    limiter = SyncRateLimiter(repository=scoped)
    with limiter:
        yield limiter
    parent.close()


@pytest.fixture
def sync_localstack_limiter_with_aggregator(shared_aggregator_stack, unique_namespace):  # noqa: F811
    """SyncRateLimiter with Lambda aggregator for benchmark tests."""
    parent, scoped = make_sync_test_repo(shared_aggregator_stack, unique_namespace)
    limiter = SyncRateLimiter(repository=scoped)
    with limiter:
        yield limiter
    parent.close()


@pytest.fixture
def benchmark_entities(sync_limiter: Any) -> list[str]:  # noqa: F811
    """Pre-create entities with pre-warmed buckets for throughput tests.

    Creates 100 entities, each with a pre-existing bucket to avoid
    cold-start overhead in benchmark measurements.

    Returns:
        List of entity IDs created.
    """
    entity_ids = [f"bench-entity-{i:03d}" for i in range(100)]
    limits = [Limit.per_minute("rpm", 1_000_000)]

    for entity_id in entity_ids:
        sync_limiter.create_entity(entity_id, name=f"Benchmark Entity {entity_id}")
        with sync_limiter.acquire(
            entity_id=entity_id,
            resource="benchmark",
            limits=limits,
            consume={"rpm": 1},
        ):
            pass

    return entity_ids


# Optimization comparison fixtures (issue #134)


@pytest.fixture
def sync_limiter_no_cache(mock_dynamodb):  # noqa: F811
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
def sync_localstack_limiter_no_cache(shared_minimal_stack):  # noqa: F811
    """SyncRateLimiter on LocalStack with config cache disabled."""
    ns = f"ns-nc-{uuid.uuid4().hex[:8]}"
    parent, scoped = make_sync_test_repo(shared_minimal_stack, ns)
    # Override config_cache_ttl on the scoped repo
    scoped._config_cache_ttl = 0
    limiter = SyncRateLimiter(repository=scoped)
    with limiter:
        yield limiter
    parent.close()
