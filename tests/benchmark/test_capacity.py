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
        """Verify: acquire() with single limit uses single-item API (issue #313).

        Expected calls (with META folded into BatchGetItem - issue #116):
        - 1 GetItem (version check)
        - 4 Query (three-tier limit resolution: entity, resource, system + parent check)
        - 1 BatchGetItem with 2 keys = entity META + 1 bucket
        - 1 PutItem (single-item optimization, halves WCU cost vs TransactWriteItems)

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

        # Verify BatchGetItem optimization (issue #133 + #116)
        # Entity META + bucket reads folded into single BatchGetItem
        assert len(capacity_counter.batch_get_item) == 1, "Should have 1 BatchGetItem call"
        assert capacity_counter.batch_get_item[0] == 2, (
            "BatchGetItem should fetch 1 bucket + 1 META"
        )
        # Single-item optimization (issue #313): 1 item → PutItem instead of TransactWriteItems
        assert capacity_counter.put_item == 1, "Should use PutItem for single-item write"
        assert len(capacity_counter.transact_write_items) == 0, (
            "Should not use TransactWriteItems for single-item write"
        )

    @pytest.mark.parametrize("num_limits", [2, 3, 5])
    def test_acquire_multiple_limits_capacity(self, sync_limiter, capacity_counter, num_limits):
        """Verify: acquire() with N limits uses single BatchGetItem for composite bucket.

        Expected calls (with composite bucket items - ADR-114):
        - 1 GetItem (version check)
        - 4 Query (three-tier limit resolution + parent check)
        - 1 BatchGetItem with 2 keys = entity META + 1 composite bucket
        - 1 TransactWriteItems with 1 item (composite bucket with N limits)

        Note: With composite bucket items, all limits for the same entity/resource
        are stored in a single DynamoDB item, reducing both read and write costs.
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

        # Verify composite bucket optimization (ADR-114)
        # Entity META + 1 composite bucket (all limits in single item)
        assert len(capacity_counter.batch_get_item) == 1, "Should have 1 BatchGetItem call"
        assert capacity_counter.batch_get_item[0] == 2, (
            "BatchGetItem should fetch 1 composite bucket + 1 META"
        )
        # Single-item optimization (issue #313): 1 item → PutItem instead of TransactWriteItems
        assert capacity_counter.put_item == 1, "Should use PutItem for single composite bucket"
        assert len(capacity_counter.transact_write_items) == 0, (
            "Should not use TransactWriteItems for single-item write"
        )

    def test_acquire_with_cascade_capacity(self, sync_limiter, capacity_counter):
        """Verify: acquire() with cascade uses 2 BatchGetItem calls.

        Expected calls (with META folded into BatchGetItem - issue #116):
        - GetItem for version check
        - Query for limit resolution
        - 1 BatchGetItem with 2 keys (child META + child bucket)
        - 1 BatchGetItem with 1 key (parent bucket)
        - 1 TransactWriteItems with 2 items (child + parent buckets) = 2 WCUs

        The cascade path uses 2 BatchGetItem calls because the parent is
        only discovered after reading the child's META record.
        """
        # Setup hierarchy
        sync_limiter.create_entity("cap-cascade-parent", name="Parent")
        sync_limiter.create_entity(
            "cap-cascade-child", name="Child", parent_id="cap-cascade-parent", cascade=True
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
            ):
                pass

        # Verify two-phase BatchGetItem (issue #116)
        # Phase 1: child META + child bucket; Phase 2: parent bucket
        assert len(capacity_counter.batch_get_item) == 2, (
            "Should have 2 BatchGetItem calls (child META+bucket, parent bucket)"
        )
        assert capacity_counter.batch_get_item[0] == 2, (
            "First BatchGetItem should fetch child META + child bucket"
        )
        assert capacity_counter.batch_get_item[1] == 1, (
            "Second BatchGetItem should fetch parent bucket"
        )
        assert len(capacity_counter.transact_write_items) == 1, "Should have 1 transaction"
        assert capacity_counter.transact_write_items[0] == 2, (
            "Transaction should write 2 items (child + parent)"
        )

    def test_acquire_with_stored_limits_capacity(self, sync_limiter, capacity_counter):
        """Verify: acquire(use_stored_limits=True) uses single-item API (issue #313).

        Expected calls (with META folded into BatchGetItem - issue #116, composite limits ADR-114):
        - GetItem for version lookup
        - GetItem for limit resolution (entity #CONFIG, resource #CONFIG, system #CONFIG)
        - 1 BatchGetItem with 2 keys = entity META + 1 bucket
        - 1 PutItem (single-item optimization, halves WCU cost)

        Note: With composite limits (ADR-114), limit resolution uses GetItem on
        #CONFIG records instead of Query operations.
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

        # Verify BatchGetItem optimization (issue #133 + #116)
        assert len(capacity_counter.batch_get_item) == 1, "Should have 1 BatchGetItem call"
        assert capacity_counter.batch_get_item[0] == 2, (
            "BatchGetItem should fetch 1 bucket + 1 META"
        )
        # Single-item optimization (issue #313): 1 item → PutItem instead of TransactWriteItems
        assert capacity_counter.put_item == 1, "Should use PutItem for single-item write"
        assert len(capacity_counter.transact_write_items) == 0, (
            "Should not use TransactWriteItems for single-item write"
        )
        # GetItem calls for limit resolution (composite limits use GetItem, not Query)
        assert capacity_counter.get_item >= 3, "Should have GetItem calls for config resolution"

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
        assert capacity_counter.total_wcus == 0, "available() should have no writes"

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
        """Verify: set_limits() = 1 WCU (composite) + 1 WCU (audit).

        Expected calls with composite limits (ADR-114):
        - 1 PutItem for composite config item (all limits in one item)
        - 1 PutItem for audit event

        Note: With composite limits, all limits are stored in a single #CONFIG
        item, reducing write cost from N WCUs to 1 WCU regardless of limit count.
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

        # Verify capacity consumption with composite limits
        # TransactWriteItems: 1 composite config + 1 registry increment (issue #288)
        # PutItem: 1 audit event
        assert capacity_counter.transact_write_items == [2], (
            "Should transact 1 config + 1 registry increment"
        )
        assert capacity_counter.put_item == 1, "Should put 1 audit event"

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

    def test_single_entity_uses_single_item_api(self, sync_limiter, capacity_counter):
        """Verify: single-item optimization uses correct API per path (issue #313).

        - Non-cascade (1 item) → PutItem or UpdateItem (1 WCU)
        - Cascade (2 items) → TransactWriteItems (2x WCU for atomicity)
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]

        # --- Non-cascade: first acquire creates bucket via PutItem ---
        with capacity_counter.counting():
            with sync_limiter.acquire(
                entity_id="single-api-test",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        assert capacity_counter.put_item == 1, "First acquire should use PutItem"
        assert capacity_counter.update_item == 0, "First acquire should not use UpdateItem"
        assert len(capacity_counter.transact_write_items) == 0, (
            "Single-item should not use TransactWriteItems"
        )

        # --- Non-cascade: second acquire updates bucket via UpdateItem ---
        capacity_counter.reset()

        with capacity_counter.counting():
            with sync_limiter.acquire(
                entity_id="single-api-test",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        assert capacity_counter.update_item == 1, "Second acquire should use UpdateItem"
        assert capacity_counter.put_item == 0, "Second acquire should not use PutItem"
        assert len(capacity_counter.transact_write_items) == 0, (
            "Single-item should not use TransactWriteItems"
        )

        # --- Cascade: 2 items → must use TransactWriteItems ---
        sync_limiter.create_entity("single-api-parent", name="Parent")
        sync_limiter.create_entity(
            "single-api-child", name="Child", parent_id="single-api-parent", cascade=True
        )
        capacity_counter.reset()

        with capacity_counter.counting():
            with sync_limiter.acquire(
                entity_id="single-api-child",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        assert len(capacity_counter.transact_write_items) == 1, (
            "Cascade should use TransactWriteItems"
        )
        assert capacity_counter.transact_write_items[0] == 2, (
            "Cascade should write 2 items (child + parent)"
        )
        assert capacity_counter.put_item == 0, "Cascade should not use PutItem"
        assert capacity_counter.update_item == 0, "Cascade should not use UpdateItem"
