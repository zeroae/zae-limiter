"""Capacity consumption tests for documentation.

These tests validate the RCU/WCU claims in docs/performance.md by counting
the actual DynamoDB API calls made during each operation.

Run with:
    pytest tests/benchmark/test_capacity.py -v

Note: These tests use moto (mocked DynamoDB) to enable API call counting.
"""

import pytest

from zae_limiter import Limit

pytestmark = pytest.mark.benchmark

# NOTE: Issue #133 optimized acquire() to use BatchGetItem instead of multiple GetItem calls.
# The three-tier limit resolution queries remain, but bucket reads are now batched.
# Config caching (#130) will further reduce the Query operations.


class TestCapacityConsumption:
    """Verify DynamoDB capacity consumption per operation.

    Each test validates the documented RCU/WCU costs for operations.
    The capacity_counter fixture tracks actual API calls.
    """

    def test_acquire_single_limit_capacity(self, sync_limiter, capacity_counter):
        """Verify: acquire() with single limit uses BatchGetItem for bucket reads.

        Expected calls (with BatchGetItem optimization - issue #133):
        - 2 GetItem (entity lookup, version check)
        - 4 Query (three-tier limit resolution: entity, resource, system + parent check)
        - 1 BatchGetItem with 1 key = bucket read
        - 1 TransactWriteItems with 1 item

        Note: Query operations will be reduced by config caching (issue #130).
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]

        with capacity_counter.counting():
            with sync_limiter.acquire(
                entity_id="cap-single",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        # Verify BatchGetItem optimization (issue #133)
        # Bucket reads now use BatchGetItem instead of individual GetItem
        assert len(capacity_counter.batch_get_item) == 1, "Should have 1 BatchGetItem call"
        assert capacity_counter.batch_get_item[0] == 1, "BatchGetItem should fetch 1 bucket"
        # Additional GetItem calls for entity/version lookup remain
        assert capacity_counter.get_item >= 1, "Should have GetItem for entity/version lookup"
        assert len(capacity_counter.transact_write_items) == 1, "Should have 1 transaction"
        assert capacity_counter.transact_write_items[0] == 1, "Transaction should write 1 item"

    @pytest.mark.parametrize("num_limits", [2, 3, 5])
    def test_acquire_multiple_limits_capacity(self, sync_limiter, capacity_counter, num_limits):
        """Verify: acquire() with N limits uses single BatchGetItem for all buckets.

        Expected calls (with BatchGetItem optimization - issue #133):
        - 2 GetItem (entity lookup, version check)
        - 4 Query (three-tier limit resolution + parent check)
        - 1 BatchGetItem with N keys = bucket reads batched together
        - 1 TransactWriteItems with N items = N WCUs

        Note: Query operations will be reduced by config caching (issue #130).
        """
        limits = [Limit.per_minute(f"limit_{i}", 1_000_000) for i in range(num_limits)]
        consume = {f"limit_{i}": 1 for i in range(num_limits)}

        with capacity_counter.counting():
            with sync_limiter.acquire(
                entity_id=f"cap-multi-{num_limits}",
                resource="api",
                limits=limits,
                consume=consume,
            ):
                pass

        # Verify BatchGetItem optimization (issue #133)
        # All bucket reads batched into single call regardless of limit count
        assert len(capacity_counter.batch_get_item) == 1, "Should have 1 BatchGetItem call"
        assert capacity_counter.batch_get_item[0] == num_limits, (
            f"BatchGetItem should fetch {num_limits} buckets"
        )
        # Additional GetItem calls for entity/version lookup remain
        assert capacity_counter.get_item >= 1, "Should have GetItem for entity/version lookup"
        assert len(capacity_counter.transact_write_items) == 1, "Should have 1 transaction"
        assert capacity_counter.transact_write_items[0] == num_limits, (
            f"Transaction should write {num_limits} items"
        )

    def test_acquire_with_cascade_capacity(self, sync_limiter, capacity_counter):
        """Verify: acquire(cascade=True) batches child + parent bucket reads.

        Expected calls (with BatchGetItem optimization - issue #133):
        - GetItem for entity lookup, version check
        - Query for limit resolution + parent lookup
        - 1 BatchGetItem with 2 keys (child + parent buckets)
        - 1 TransactWriteItems with 2 items (child + parent buckets) = 2 WCUs

        Note: Query operations will be reduced by config caching (issue #130).
        """
        # Setup hierarchy
        sync_limiter.create_entity("cap-cascade-parent", name="Parent")
        sync_limiter.create_entity(
            "cap-cascade-child", name="Child", parent_id="cap-cascade-parent"
        )

        limits = [Limit.per_minute("rpm", 1_000_000)]

        # Reset counter after setup
        capacity_counter.reset()

        with capacity_counter.counting():
            with sync_limiter.acquire(
                entity_id="cap-cascade-child",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
                cascade=True,
            ):
                pass

        # Verify BatchGetItem optimization (issue #133)
        # Child and parent bucket reads batched together
        assert len(capacity_counter.batch_get_item) == 1, "Should have 1 BatchGetItem call"
        assert capacity_counter.batch_get_item[0] == 2, (
            "BatchGetItem should fetch 2 buckets (child + parent)"
        )
        # GetItem calls for entity/version lookup remain
        assert capacity_counter.get_item >= 1, "Should have GetItem for entity/version lookup"
        assert len(capacity_counter.transact_write_items) == 1, "Should have 1 transaction"
        assert capacity_counter.transact_write_items[0] == 2, (
            "Transaction should write 2 items (child + parent)"
        )

    def test_acquire_with_stored_limits_capacity(self, sync_limiter, capacity_counter):
        """Verify: acquire(use_stored_limits=True) uses BatchGetItem for bucket reads.

        Expected calls (with BatchGetItem optimization - issue #133):
        - GetItem for entity/version lookup
        - Query for limit resolution (entity, resource, system)
        - 1 BatchGetItem with 1 key = bucket read
        - 1 TransactWriteItems with 1 item = 1 WCU

        Note: Query operations will be reduced by config caching (issue #130).
        """
        # Setup stored limits
        limits = [Limit.per_minute("rpm", 1_000_000)]
        sync_limiter.create_entity("cap-stored", name="Stored Limits Entity")
        sync_limiter.set_limits("cap-stored", limits)

        # Reset counter after setup
        capacity_counter.reset()

        with capacity_counter.counting():
            with sync_limiter.acquire(
                entity_id="cap-stored",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
                use_stored_limits=True,
            ):
                pass

        # Verify BatchGetItem optimization (issue #133)
        assert len(capacity_counter.batch_get_item) == 1, "Should have 1 BatchGetItem call"
        assert capacity_counter.batch_get_item[0] == 1, "BatchGetItem should fetch 1 bucket"
        # Query calls for limit resolution
        assert capacity_counter.query >= 2, "Should have Query calls for stored limits"
        # GetItem calls for entity/version lookup remain
        assert capacity_counter.get_item >= 1, "Should have GetItem for entity/version lookup"

    def test_available_check_capacity(self, sync_limiter, capacity_counter):
        """Verify: available() reads bucket state without writes.

        Expected calls:
        - GetItem for entity/version lookup
        - Query for limit resolution
        - GetItem for bucket state check
        - 0 write operations

        Note: available() does not use BatchGetItem optimization.
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]

        # Setup: create a bucket first
        with sync_limiter.acquire(
            entity_id="cap-available",
            resource="api",
            limits=limits,
            consume={"rpm": 1},
        ):
            pass

        # Reset counter after setup
        capacity_counter.reset()

        with capacity_counter.counting():
            sync_limiter.available(
                entity_id="cap-available",
                resource="api",
                limits=limits,
            )

        # Verify read-only operation
        assert capacity_counter.get_item >= 1, "Should have GetItem calls for entity/version/bucket"
        assert len(capacity_counter.transact_write_items) == 0, "Should have no transactions"
        assert capacity_counter.put_item == 0, "Should have no puts"
        assert sum(capacity_counter.batch_write_item) == 0, "Should have no batch writes"

    @pytest.mark.parametrize("num_limits", [1, 2, 3])
    def test_available_check_multiple_limits_capacity(
        self, sync_limiter, capacity_counter, num_limits
    ):
        """Verify: available() with N limits reads N buckets without writes.

        Each limit requires a separate GetItem to check bucket state.
        Note: available() does not currently use BatchGetItem optimization.
        """
        limits = [Limit.per_minute(f"limit_{i}", 1_000_000) for i in range(num_limits)]
        consume = {f"limit_{i}": 1 for i in range(num_limits)}

        # Setup: create buckets first
        with sync_limiter.acquire(
            entity_id=f"cap-avail-multi-{num_limits}",
            resource="api",
            limits=limits,
            consume=consume,
        ):
            pass

        # Reset counter after setup
        capacity_counter.reset()

        with capacity_counter.counting():
            sync_limiter.available(
                entity_id=f"cap-avail-multi-{num_limits}",
                resource="api",
                limits=limits,
            )

        # Verify read-only operation
        # GetItem includes entity/version lookup + N bucket reads
        assert capacity_counter.get_item >= num_limits, f"Should read at least {num_limits} items"
        assert capacity_counter.total_wcus == 0, "available() should have no writes"

    def test_set_limits_capacity(self, sync_limiter, capacity_counter):
        """Verify: set_limits() = 1 RCU (query) + N WCUs.

        Expected calls:
        - 1 Query to find existing limits = 1 RCU
        - N PutItem calls (one per limit) = N WCUs

        Note: Also includes audit logging which adds 1 PutItem.
        """
        limits = [
            Limit.per_minute("rpm", 1000),
            Limit.per_minute("tpm", 100000),
        ]

        sync_limiter.create_entity("cap-set-limits", name="Set Limits Entity")

        # Reset counter after setup
        capacity_counter.reset()

        with capacity_counter.counting():
            sync_limiter.set_limits("cap-set-limits", limits)

        # Verify capacity consumption
        # Query to delete existing limits + check
        assert capacity_counter.query >= 1, "Should have at least 1 Query for existing limits"
        # PutItem: 2 limits + 1 audit event = 3
        assert capacity_counter.put_item == 3, "Should put 2 limits + 1 audit event"

    def test_delete_entity_capacity(self, sync_limiter, capacity_counter):
        """Verify: delete_entity() batches in 25-item chunks.

        Expected calls:
        - 1 Query to find all entity items = 1 RCU
        - BatchWriteItem in chunks of 25 items

        For small entities (few items), only 1 BatchWriteItem call.
        """
        # Create entity with multiple items
        limits = [Limit.per_minute("rpm", 1_000_000)]
        sync_limiter.create_entity("cap-delete", name="Delete Test Entity")

        # Create multiple buckets to ensure multiple items
        for resource in ["api1", "api2", "api3"]:
            with sync_limiter.acquire(
                entity_id="cap-delete",
                resource=resource,
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        # Reset counter after setup
        capacity_counter.reset()

        with capacity_counter.counting():
            sync_limiter.delete_entity("cap-delete")

        # Verify capacity consumption
        assert capacity_counter.query == 1, "Should have 1 Query to find entity items"
        assert len(capacity_counter.batch_write_item) >= 1, "Should have at least 1 BatchWriteItem"
        # All items should be deleted in the batch
        assert sum(capacity_counter.batch_write_item) >= 1, "Should delete at least 1 item"

    def test_delete_entity_large_capacity(self, sync_limiter, capacity_counter):
        """Verify: delete_entity() with >25 items uses multiple batch calls.

        Creates 30+ items to verify 25-item chunking behavior.
        """
        # Create entity with many items (>25)
        limits = [Limit.per_minute("rpm", 1_000_000)]
        sync_limiter.create_entity("cap-delete-large", name="Large Delete Test Entity")

        # Create 30 buckets (more than 25-item batch limit)
        for i in range(30):
            with sync_limiter.acquire(
                entity_id="cap-delete-large",
                resource=f"api{i}",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        # Reset counter after setup
        capacity_counter.reset()

        with capacity_counter.counting():
            sync_limiter.delete_entity("cap-delete-large")

        # Verify capacity consumption
        assert capacity_counter.query == 1, "Should have 1 Query to find entity items"
        # Should have at least 2 batch write calls (30 items / 25 per batch = 2 batches)
        assert len(capacity_counter.batch_write_item) >= 2, (
            "Should have at least 2 BatchWriteItem calls for >25 items"
        )
        # Verify each batch is at most 25 items
        for batch_size in capacity_counter.batch_write_item:
            assert batch_size <= 25, "Each batch should have at most 25 items"
