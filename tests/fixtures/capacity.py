"""DynamoDB capacity counting fixtures for benchmarks."""

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest


@dataclass
class CapacityCounter:
    """Tracks DynamoDB API calls for RCU/WCU validation.

    Attributes:
        get_item: Number of GetItem calls (1 RCU each)
        batch_get_item: List of item counts per BatchGetItem call (issue #133)
        query: Number of Query calls (RCUs depend on data returned)
        put_item: Number of PutItem calls (1 WCU each)
        update_item: Number of UpdateItem calls (1 WCU each, issue #313)
        delete_item: Number of DeleteItem calls (1 WCU each, issue #313)
        transact_write_items: List of item counts per TransactWriteItems call
        batch_write_item: List of item counts per BatchWriteItem call
    """

    get_item: int = 0
    batch_get_item: list[int] = field(default_factory=list)
    query: int = 0
    put_item: int = 0
    update_item: int = 0
    delete_item: int = 0
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
        return (
            self.put_item
            + self.update_item
            + self.delete_item
            + sum(self.transact_write_items)
            + sum(self.batch_write_item)
        )

    def reset(self) -> None:
        """Reset all counters to zero."""
        self.get_item = 0
        self.batch_get_item.clear()
        self.query = 0
        self.put_item = 0
        self.update_item = 0
        self.delete_item = 0
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
    original_update_item = client.update_item
    original_delete_item = client.delete_item
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

    def counting_update_item(*args: Any, **kwargs: Any) -> Any:
        counter.update_item += 1
        return original_update_item(*args, **kwargs)

    def counting_delete_item(*args: Any, **kwargs: Any) -> Any:
        counter.delete_item += 1
        return original_delete_item(*args, **kwargs)

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
    client.update_item = counting_update_item
    client.delete_item = counting_delete_item
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
        client.update_item = original_update_item
        client.delete_item = original_delete_item
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
