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

# Tests affected by three-tier limit resolution (Entity > Resource > System).
# Resolution adds up to 3 additional Query operations per acquire/available call.
# These will be addressed when config caching is implemented (issue #130).
_XFAIL_RESOLUTION_QUERIES = pytest.mark.xfail(
    reason="Three-tier resolution adds queries; will be addressed with caching (#130)",
    strict=True,
)


class TestCapacityConsumption:
    """Verify DynamoDB capacity consumption per operation.

    Each test validates the documented RCU/WCU costs for operations.
    The capacity_counter fixture tracks actual API calls.
    """

    @_XFAIL_RESOLUTION_QUERIES
    def test_acquire_single_limit_capacity(self, sync_limiter, capacity_counter):
        """Verify: acquire() with single limit = 1 RCU + 1 WCU.

        Expected calls:
        - 1 GetItem (read existing bucket or check) = 1 RCU
        - 1 TransactWriteItems with 1 item (create/update bucket) = 1 WCU
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

        # Verify capacity consumption
        assert capacity_counter.get_item == 1, "Should read 1 bucket"
        assert len(capacity_counter.transact_write_items) == 1, "Should have 1 transaction"
        assert capacity_counter.transact_write_items[0] == 1, "Transaction should write 1 item"
        assert capacity_counter.query == 0, "No queries for simple acquire"

    @_XFAIL_RESOLUTION_QUERIES
    @pytest.mark.parametrize("num_limits", [2, 3, 5])
    def test_acquire_multiple_limits_capacity(self, sync_limiter, capacity_counter, num_limits):
        """Verify: acquire() with N limits = N RCUs + N WCUs.

        Expected calls:
        - N GetItems (one per limit/bucket) = N RCUs
        - 1 TransactWriteItems with N items = N WCUs
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

        # Verify capacity consumption
        assert capacity_counter.get_item == num_limits, f"Should read {num_limits} buckets"
        assert len(capacity_counter.transact_write_items) == 1, "Should have 1 transaction"
        assert capacity_counter.transact_write_items[0] == num_limits, (
            f"Transaction should write {num_limits} items"
        )

    @_XFAIL_RESOLUTION_QUERIES
    def test_acquire_with_cascade_capacity(self, sync_limiter, capacity_counter):
        """Verify: acquire(cascade=True) = 3 RCUs + 2 WCUs.

        Expected calls:
        - 1 GetItem for entity (to find parent) = 1 RCU
        - 1 GetItem for child bucket = 1 RCU
        - 1 GetItem for parent bucket = 1 RCU
        - 1 TransactWriteItems with 2 items (child + parent buckets) = 2 WCUs
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

        # Verify capacity consumption
        # GetItem calls: 1 (entity lookup) + 1 (child bucket) + 1 (parent bucket) = 3
        assert capacity_counter.get_item == 3, "Should have 3 GetItem calls for cascade"
        assert len(capacity_counter.transact_write_items) == 1, "Should have 1 transaction"
        assert capacity_counter.transact_write_items[0] == 2, (
            "Transaction should write 2 items (child + parent)"
        )

    @_XFAIL_RESOLUTION_QUERIES
    def test_acquire_with_stored_limits_capacity(self, sync_limiter, capacity_counter):
        """Verify: acquire(use_stored_limits=True) adds 2 RCUs.

        Expected calls (without cascade):
        - 2 Query operations (child limits + default resource limits) = 2 RCUs
        - 1 GetItem for bucket = 1 RCU
        - 1 TransactWriteItems with 1 item = 1 WCU

        Total: 3 RCUs + 1 WCU
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

        # Verify capacity consumption
        # Query calls: 2 (entity limits + default resource)
        assert capacity_counter.query == 2, "Should have 2 Query calls for stored limits"
        assert capacity_counter.get_item == 1, "Should read 1 bucket"

    @_XFAIL_RESOLUTION_QUERIES
    def test_available_check_capacity(self, sync_limiter, capacity_counter):
        """Verify: available() = 1 RCU per limit, 0 WCUs.

        Expected calls:
        - N GetItems (one per limit to check bucket state) = N RCUs
        - 0 write operations
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

        # Verify capacity consumption
        assert capacity_counter.get_item == 1, "Should read 1 bucket"
        assert len(capacity_counter.transact_write_items) == 0, "Should have no transactions"
        assert capacity_counter.put_item == 0, "Should have no puts"
        assert sum(capacity_counter.batch_write_item) == 0, "Should have no batch writes"

    @_XFAIL_RESOLUTION_QUERIES
    @pytest.mark.parametrize("num_limits", [1, 2, 3])
    def test_available_check_multiple_limits_capacity(
        self, sync_limiter, capacity_counter, num_limits
    ):
        """Verify: available() with N limits = N RCUs.

        Each limit requires a separate GetItem to check bucket state.
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

        # Verify capacity consumption
        assert capacity_counter.get_item == num_limits, f"Should read {num_limits} buckets"
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
