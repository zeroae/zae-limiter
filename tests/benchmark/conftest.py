"""Benchmark test fixtures.

Provides fixtures for capacity counting, pre-warmed entities,
and optimization comparison benchmarks.
"""

import uuid
from typing import Any

import pytest
import pytest_asyncio

from tests.fixtures.capacity import (  # noqa: F401
    CapacityCounter,
    _counting_client,
    capacity_counter,
)
from tests.fixtures.moto import (  # noqa: F401
    _patch_aiobotocore_response,
    aws_credentials,
    mock_dynamodb,
)
from tests.fixtures.names import unique_name, unique_name_class  # noqa: F401
from tests.fixtures.repositories import make_sync_test_repo
from tests.fixtures.stacks import (  # noqa: F401
    get_or_create_shared_stack,
    localstack_endpoint,
)
from zae_limiter import Limit, StackOptions, SyncRateLimiter
from zae_limiter.sync_repository import SyncRepository

# Re-export unit test fixtures for moto-based benchmarks


@pytest.fixture
def sync_limiter(mock_dynamodb):  # noqa: F811
    """Create a SyncRateLimiter with mocked DynamoDB and native sync."""
    repo = SyncRepository(
        name="test-rate-limits",
        region="us-east-1",
    )
    repo.create_table()
    limiter = SyncRateLimiter(repository=repo)
    with limiter:
        yield limiter


# Session-scoped shared stacks for LocalStack benchmarks


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def shared_minimal_stack(localstack_endpoint, tmp_path_factory):  # noqa: F811
    """Session-scoped shared stack without aggregator or alarms."""
    return await get_or_create_shared_stack(
        tmp_path_factory,
        "shared-minimal",
        localstack_endpoint,
    )


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def shared_aggregator_stack(localstack_endpoint, tmp_path_factory):  # noqa: F811
    """Session-scoped shared stack with aggregator Lambda."""
    return await get_or_create_shared_stack(
        tmp_path_factory,
        "shared-aggregator",
        localstack_endpoint,
        enable_aggregator=True,
    )


# Namespace-scoped sync fixtures for LocalStack benchmarks


@pytest.fixture
def unique_namespace():
    """Generate unique namespace name for per-test data isolation."""
    return f"ns-{uuid.uuid4().hex[:8]}"


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


# StackOptions fixtures — used by benchmarks that need specific stack config


@pytest.fixture(scope="session")
def minimal_stack_options():
    """Minimal stack - no aggregator, no alarms. Fastest deployment."""
    return StackOptions(enable_aggregator=False, enable_alarms=False)


@pytest.fixture(scope="session")
def aggregator_stack_options():
    """Stack with aggregator Lambda but no CloudWatch alarms."""
    return StackOptions(enable_aggregator=True, enable_alarms=False)


@pytest.fixture
def benchmark_entities(sync_limiter: Any) -> list[str]:
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
