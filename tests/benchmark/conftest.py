"""Benchmark test fixtures.

Reuses fixtures from unit and integration for consistency.
Adds specialized fixtures for capacity counting and pre-warmed entities.
"""

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest

from tests.integration.conftest import (
    aggregator_stack_options,
    localstack_endpoint,
    minimal_stack_options,
    sync_localstack_limiter,
    unique_name,
    unique_name_class,
)
from tests.unit.conftest import (
    aws_credentials,
    mock_dynamodb,
    sync_limiter,
)
from zae_limiter import Limit, SyncRateLimiter
from zae_limiter.sync_repository import SyncRepository

__all__ = [
    "aws_credentials",
    "mock_dynamodb",
    "sync_limiter",
    "sync_limiter_no_cache",
    "localstack_endpoint",
    "minimal_stack_options",
    "aggregator_stack_options",
    "sync_localstack_limiter",
    "sync_localstack_limiter_no_cache",
    "sync_localstack_limiter_with_aggregator",
    "unique_name",
    "unique_name_class",
    "capacity_counter",
    "benchmark_entities",
]


@dataclass
class CapacityCounter:
    """Tracks DynamoDB API calls for RCU/WCU validation.

    Attributes:
        get_item: Number of GetItem calls (1 RCU each)
        batch_get_item: List of item counts per BatchGetItem call (issue #133)
        query: Number of Query calls (RCUs depend on data returned)
        put_item: Number of PutItem calls (1 WCU each)
        transact_write_items: List of item counts per TransactWriteItems call
        batch_write_item: List of item counts per BatchWriteItem call
    """

    get_item: int = 0
    batch_get_item: list[int] = field(default_factory=list)
    query: int = 0
    put_item: int = 0
    transact_write_items: list[int] = field(default_factory=list)
    batch_write_item: list[int] = field(default_factory=list)

    @property
    def total_rcus(self) -> int:
        """Estimate total RCUs consumed (simplified: 1 RCU per GetItem/Query).

        Note: BatchGetItem items are counted individually as they each consume
        0.5 RCU for items up to 4KB (eventually consistent read).
        """
        return self.get_item + sum(self.batch_get_item) + self.query

    @property
    def total_wcus(self) -> int:
        """Estimate total WCUs consumed."""
        return self.put_item + sum(self.transact_write_items) + sum(self.batch_write_item)

    def reset(self) -> None:
        """Reset all counters to zero."""
        self.get_item = 0
        self.batch_get_item.clear()
        self.query = 0
        self.put_item = 0
        self.transact_write_items.clear()
        self.batch_write_item.clear()


@contextmanager
def _counting_client(counter: CapacityCounter, limiter: Any) -> Generator[None, None, None]:
    """Context manager that wraps DynamoDB boto3 client to count API calls.

    This wraps the sync DynamoDB client methods to count calls without
    interfering with the actual operations.

    Args:
        counter: The CapacityCounter to update
        limiter: The SyncRateLimiter whose repository's client to wrap
    """
    repo = limiter._repository

    client = repo._client
    if client is None:
        yield
        return

    # Store original methods
    original_get_item = client.get_item
    original_batch_get = client.batch_get_item
    original_query = client.query
    original_put_item = client.put_item
    original_transact = client.transact_write_items
    original_batch = client.batch_write_item

    # Sync counting wrappers for boto3 client
    def counting_get_item(*args: Any, **kwargs: Any) -> Any:
        counter.get_item += 1
        return original_get_item(*args, **kwargs)

    def counting_batch_get(*args: Any, **kwargs: Any) -> Any:
        request_items = kwargs.get("RequestItems", {})
        total_items = sum(len(items.get("Keys", [])) for items in request_items.values())
        counter.batch_get_item.append(total_items)
        return original_batch_get(*args, **kwargs)

    def counting_query(*args: Any, **kwargs: Any) -> Any:
        counter.query += 1
        return original_query(*args, **kwargs)

    def counting_put_item(*args: Any, **kwargs: Any) -> Any:
        counter.put_item += 1
        return original_put_item(*args, **kwargs)

    def counting_transact(*args: Any, **kwargs: Any) -> Any:
        items = kwargs.get("TransactItems", [])
        counter.transact_write_items.append(len(items))
        return original_transact(*args, **kwargs)

    def counting_batch(*args: Any, **kwargs: Any) -> Any:
        request_items = kwargs.get("RequestItems", {})
        total_items = sum(len(items) for items in request_items.values())
        counter.batch_write_item.append(total_items)
        return original_batch(*args, **kwargs)

    # Apply wrappers
    client.get_item = counting_get_item
    client.batch_get_item = counting_batch_get
    client.query = counting_query
    client.put_item = counting_put_item
    client.transact_write_items = counting_transact
    client.batch_write_item = counting_batch

    try:
        yield
    finally:
        # Restore original methods
        client.get_item = original_get_item
        client.batch_get_item = original_batch_get
        client.query = original_query
        client.put_item = original_put_item
        client.transact_write_items = original_transact
        client.batch_write_item = original_batch


@pytest.fixture
def capacity_counter(sync_limiter: Any) -> Generator[CapacityCounter, None, None]:
    """Fixture to count DynamoDB API calls for capacity validation.

    Usage:
        def test_example(self, sync_limiter, capacity_counter):
            with capacity_counter.counting():
                # do operations
                pass
            assert capacity_counter.get_item == 1

    Note: This fixture only works with moto-based tests, not LocalStack.
    The counter tracks calls at the aioboto3 client level.

    The sync_limiter fixture must be used before this fixture to ensure
    the DynamoDB client is created and cached.
    """
    counter = CapacityCounter()

    # Attach the counting context manager as a method
    # The limiter is captured in the closure
    counter.counting = lambda: _counting_client(counter, sync_limiter)  # type: ignore[attr-defined]

    yield counter


@pytest.fixture
def sync_localstack_limiter_with_aggregator(
    localstack_endpoint, aggregator_stack_options, unique_name
):
    """SyncRateLimiter with Lambda aggregator for benchmark tests.

    Creates a LocalStack-based limiter with aggregator Lambda enabled
    for benchmarking cold start and warm start latency.

    This fixture is primarily used for Lambda cold/warm start benchmarks
    that require the aggregator function to be deployed.
    """
    limiter = SyncRateLimiter(
        name=unique_name,
        endpoint_url=localstack_endpoint,
        region="us-east-1",
        stack_options=aggregator_stack_options,
    )

    with limiter:
        yield limiter

    try:
        limiter.delete_stack()
    except Exception as e:
        print(f"Warning: Stack cleanup failed: {e}")


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
        # Create entity
        sync_limiter.create_entity(entity_id, name=f"Benchmark Entity {entity_id}")
        # Pre-warm bucket by doing one acquire
        with sync_limiter.acquire(
            entity_id=entity_id,
            resource="benchmark",
            limits=limits,
            consume={"rpm": 1},
        ):
            pass

    return entity_ids


# ---------------------------------------------------------------------------
# Optimization comparison fixtures (issue #134)
# ---------------------------------------------------------------------------


@pytest.fixture
def sync_limiter_no_cache(mock_dynamodb):
    """SyncRateLimiter with config cache disabled for baseline comparison.

    Use this fixture alongside sync_limiter to compare performance
    with and without config caching optimization.
    """
    repo = SyncRepository(
        name="test-no-cache",
        region="us-east-1",
    )
    repo.create_table()

    limiter = SyncRateLimiter(
        repository=repo,
        config_cache_ttl=0,  # Disable config cache
    )

    with limiter:
        yield limiter


@pytest.fixture
def sync_localstack_limiter_no_cache(localstack_endpoint, minimal_stack_options):
    """SyncRateLimiter on LocalStack with config cache disabled.

    Use for realistic latency comparison with cache disabled.
    Uses a separate unique name to avoid collision with cached fixture.
    """
    import time
    import uuid

    # Generate shorter unique name (avoid exceeding 38 char limit)
    timestamp = int(time.time()) % 100000  # Last 5 digits
    unique_id = uuid.uuid4().hex[:4]
    name = f"bench-nc-{timestamp}-{unique_id}"

    limiter = SyncRateLimiter(
        name=name,
        endpoint_url=localstack_endpoint,
        region="us-east-1",
        stack_options=minimal_stack_options,
        config_cache_ttl=0,  # Disable config cache
    )

    with limiter:
        yield limiter

    try:
        limiter.delete_stack()
    except Exception as e:
        print(f"Warning: Stack cleanup failed: {e}")
