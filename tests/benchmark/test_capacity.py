"""Capacity consumption tests for documentation.

These tests validate the RCU/WCU claims in docs/performance.md by counting
the actual DynamoDB API calls made during each operation.

Run with:
    pytest tests/benchmark/test_capacity.py -v

Note: These tests use moto (mocked DynamoDB) to enable API call counting.
"""

from datetime import timedelta

import pytest

from tests.fixtures.sharding import pinned_shard
from zae_limiter import Limit, RateLimitExceeded, schema
from zae_limiter.schedule import ScheduleEntry

pytestmark = pytest.mark.benchmark

# NOTE: Issue #133 optimized acquire() to use BatchGetItem instead of multiple GetItem calls.
# The three-tier limit resolution queries remain, but bucket reads are now batched.
# Config caching (#130) will further reduce the Query operations.
#
# NOTE: ADR-125 added a disabled gate to the slow path. Every slow-path acquire()
# pays one extra BatchGetItem with 3 keys (entity config, entity _default_ config,
# resource config) per entity it touches. The walk is deliberately uncached, so it
# is not folded into the config batch, and it is skipped entirely on the
# speculative fast path, where the guard is an attribute_not_exists on the bucket.


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
        - 1 BatchGetItem with 3 keys = disabled walk (ADR-125)
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
        # Entity META + bucket reads folded into single BatchGetItem, preceded by
        # the slow-path disabled walk (ADR-125)
        assert len(capacity_counter.batch_get_item) == 2, (
            "Should have 2 BatchGetItem calls (disabled walk, META + bucket)"
        )
        assert capacity_counter.batch_get_item[0] == 3, (
            "First BatchGetItem should walk entity, entity _default_ and resource config"
        )
        assert capacity_counter.batch_get_item[1] == 2, (
            "Second BatchGetItem should fetch 1 bucket + 1 META"
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
        - 1 BatchGetItem with 3 keys = disabled walk (ADR-125)
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
        # Entity META + 1 composite bucket (all limits in single item), preceded by
        # the slow-path disabled walk (ADR-125). Neither call grows with num_limits.
        assert len(capacity_counter.batch_get_item) == 2, (
            "Should have 2 BatchGetItem calls (disabled walk, META + composite bucket)"
        )
        assert capacity_counter.batch_get_item[0] == 3, (
            "First BatchGetItem should walk entity, entity _default_ and resource config"
        )
        assert capacity_counter.batch_get_item[1] == 2, (
            "Second BatchGetItem should fetch 1 composite bucket + 1 META"
        )
        # Single-item optimization (issue #313): 1 item → PutItem instead of TransactWriteItems
        assert capacity_counter.put_item == 1, "Should use PutItem for single composite bucket"
        assert len(capacity_counter.transact_write_items) == 0, (
            "Should not use TransactWriteItems for single-item write"
        )

    def test_acquire_with_cascade_capacity(self, sync_limiter, capacity_counter):
        """Verify: acquire() with cascade uses 4 BatchGetItem calls.

        Expected calls (with META folded into BatchGetItem - issue #116):
        - GetItem for version check
        - Query for limit resolution
        - 1 BatchGetItem with 3 keys (child disabled walk, ADR-125)
        - 1 BatchGetItem with 2 keys (child META + child bucket)
        - 1 BatchGetItem with 3 keys (parent disabled walk, ADR-125)
        - 1 BatchGetItem with 1 key (parent bucket)
        - 1 TransactWriteItems with 2 items (child + parent buckets) = 2 WCUs

        The cascade path reads the child before the parent because the parent is
        only discovered after reading the child's META record. Each entity is
        gated on its own disabled state, so the walk runs once per entity.
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

        # Verify two-phase BatchGetItem (issue #116), each phase gated by a
        # disabled walk for that entity (ADR-125)
        assert len(capacity_counter.batch_get_item) == 4, (
            "Should have 4 BatchGetItem calls "
            "(child disabled walk, child META+bucket, parent disabled walk, parent bucket)"
        )
        assert capacity_counter.batch_get_item[0] == 3, (
            "First BatchGetItem should walk the child's disabled config"
        )
        assert capacity_counter.batch_get_item[1] == 2, (
            "Second BatchGetItem should fetch child META + child bucket"
        )
        assert capacity_counter.batch_get_item[2] == 3, (
            "Third BatchGetItem should walk the parent's disabled config"
        )
        assert capacity_counter.batch_get_item[3] == 1, (
            "Fourth BatchGetItem should fetch parent bucket"
        )
        assert len(capacity_counter.transact_write_items) == 1, "Should have 1 transaction"
        assert capacity_counter.transact_write_items[0] == 2, (
            "Transaction should write 2 items (child + parent)"
        )

    def test_acquire_with_stored_limits_capacity(self, sync_limiter, capacity_counter):
        """Verify: acquire(use_stored_limits=True) uses single-item API (issue #313).

        Expected calls (with META folded into BatchGetItem - issue #116, composite limits ADR-114):
        - GetItem for version lookup
        - 1 BatchGetItem with 3 keys = disabled walk (ADR-125)
        - 1 BatchGetItem with 2 keys = entity META + 1 bucket
        - 1 PutItem (single-item optimization, halves WCU cost)

        Note: When limits parameter is provided, config resolution is skipped
        (ADR-122: _resolve_limits short-circuits on explicit limits).
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

        # Verify BatchGetItem optimization (issue #133 + #116). The disabled walk
        # runs even when limits are passed explicitly: config resolution is
        # short-circuited, but the disabled gate is not (ADR-125).
        assert len(capacity_counter.batch_get_item) == 2, (
            "Should have 2 BatchGetItem calls (disabled walk, META + bucket)"
        )
        assert capacity_counter.batch_get_item[0] == 3, (
            "First BatchGetItem should walk entity, entity _default_ and resource config"
        )
        assert capacity_counter.batch_get_item[1] == 2, (
            "Second BatchGetItem should fetch 1 bucket + 1 META"
        )
        # Single-item optimization (issue #313): 1 item → PutItem instead of TransactWriteItems
        assert capacity_counter.put_item == 1, "Should use PutItem for single-item write"
        assert len(capacity_counter.transact_write_items) == 0, (
            "Should not use TransactWriteItems for single-item write"
        )
        # Only version check GetItem (config resolution skipped when limits provided)
        assert capacity_counter.get_item == 1, "Should have 1 GetItem (version check only)"

    def test_acquire_batched_config_resolution_capacity(self, sync_limiter, capacity_counter):
        """Verify: acquire() without limits override uses BatchGetItem for configs (#298).

        Expected calls:
        - 1 GetItem (version check)
        - 1 BatchGetItem for config resolution (entity, entity_default, resource, system)
        - 1 BatchGetItem with 2 keys = entity META + 1 bucket
        - 1 PutItem (single-item optimization)

        The config BatchGetItem replaces up to 4 sequential GetItem calls.

        The disable walk (ADR-125) does NOT add a fourth call here: its levels
        are a subset of the config levels, so when the config fetch actually
        read them — as on this cold path — the walk is answered from that same
        response. It reverts to its own BatchGetItem whenever any level came
        from the config cache instead, because a cached value must never
        answer the gate; see
        tests/unit/test_disable.py::TestDisabledWalkReusesTheConfigFetch.
        """
        # Setup entity with stored limits
        limits = [Limit.per_minute("rpm", 1_000_000)]
        sync_limiter.create_entity("cap-batch-config", name="Batch Config Entity")
        sync_limiter.set_limits("cap-batch-config", limits)

        # Reset counter after setup
        capacity_counter.reset()

        with capacity_counter.counting():
            with sync_limiter.acquire(
                entity_id="cap-batch-config",
                resource="api",
                consume={"rpm": 1},
            ):
                pass

        # Verify 2 BatchGetItem calls: configs (serving the disable walk too), buckets
        assert len(capacity_counter.batch_get_item) == 2, (
            "Should have 2 BatchGetItem calls (configs + buckets); the disable walk "
            "reuses the config fetch rather than repeating it (ADR-125)"
        )
        # Config batch fetches 3 keys: entity config, resource config, system config
        # (entity_default also fetched = 4 keys total)
        assert capacity_counter.batch_get_item[0] >= 3, (
            "First BatchGetItem should fetch config keys"
        )
        # Bucket batch fetches META + bucket
        assert capacity_counter.batch_get_item[1] == 2, (
            "Second BatchGetItem should fetch 1 bucket + 1 META"
        )
        # No sequential GetItem for config resolution
        assert capacity_counter.get_item == 1, "Should have only 1 GetItem (version check)"

    def test_available_check_capacity(self, sync_limiter, capacity_counter):
        """Verify: available() reads bucket state without writes.

        A sharded entity's balance is spread across its shards (GHSA-76rv), so
        available() discovers them through the KEYS_ONLY GSI3 index and fetches
        the items it finds — 1 Query + 1 BatchGetItem, never a per-shard
        GetItem — then sums. Still strictly read-only.
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
        assert capacity_counter.query == 1, "GSI3 shard discovery should be a single Query"
        assert len(capacity_counter.batch_get_item) == 1, (
            "Discovered shards should be fetched in one BatchGetItem"
        )
        assert capacity_counter.total_wcus == 0, "available() should have no writes"

    @pytest.mark.parametrize("num_limits", [1, 2, 3])
    def test_available_check_multiple_limits_capacity(
        self, sync_limiter, capacity_counter, num_limits
    ):
        """Verify: available() read cost is independent of the limit count.

        All limits for an (entity, resource, shard) share one composite item
        (ADR-114), so shard discovery plus one BatchGetItem covers every limit:
        1 Query + 1 BatchGetItem of 1 item, whatever N is.
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

        # Verify read-only operation, O(1) in the number of limits
        assert capacity_counter.query == 1, "GSI3 shard discovery should be a single Query"
        assert capacity_counter.batch_get_item == [1], (
            f"{num_limits} limits share one composite item: 1 BatchGetItem of 1 key"
        )
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
        - 2 Queries: 1 table query (entity items) + 1 GSI3 query (bucket items)
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
        assert capacity_counter.query == 2, (
            "Should have 2 Queries: entity items + GSI3 bucket discovery"
        )
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
        assert capacity_counter.query == 2, (
            "Should have 2 Queries: entity items + GSI3 bucket discovery"
        )
        # Should have at least 2 batch write calls (30 items / 25 per batch = 2 batches)
        assert len(capacity_counter.batch_write_item) >= 2, (
            "Should have at least 2 BatchWriteItem calls for >25 items"
        )
        # Verify each batch is at most 25 items
        for batch_size in capacity_counter.batch_write_item:
            assert batch_size <= 25, "Each batch should have at most 25 items"

    def test_single_entity_uses_single_item_api(self, sync_limiter, capacity_counter):
        """Verify: single-item optimization uses correct API per path (issue #313).

        With speculative writes (default-on, issue #315):
        - First acquire: speculative UpdateItem fails (missing bucket) → PutItem fallback
        - Second acquire: speculative UpdateItem succeeds (pre-committed, 0 RCU)
        - Cascade first acquire: speculative UpdateItem fails → TransactWriteItems fallback
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]

        # --- Non-cascade: first acquire ---
        # Speculative UpdateItem fails (bucket missing) → fallback creates via PutItem
        with capacity_counter.counting():
            with sync_limiter.acquire(
                entity_id="single-api-test",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        assert capacity_counter.put_item == 1, "Fallback should create bucket via PutItem"
        assert capacity_counter.update_item == 1, "Speculative UpdateItem attempted before fallback"
        assert len(capacity_counter.transact_write_items) == 0, (
            "Single-item should not use TransactWriteItems"
        )

        # --- Non-cascade: second acquire ---
        # Speculative UpdateItem succeeds (pre-committed, no read needed)
        capacity_counter.reset()

        with capacity_counter.counting():
            with sync_limiter.acquire(
                entity_id="single-api-test",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        assert capacity_counter.update_item == 1, "Speculative UpdateItem succeeds (pre-committed)"
        assert capacity_counter.put_item == 0, "Second acquire should not use PutItem"
        assert len(capacity_counter.transact_write_items) == 0, (
            "Single-item should not use TransactWriteItems"
        )

        # --- Cascade: first acquire ---
        # Speculative UpdateItem fails (child bucket missing) → TransactWriteItems fallback
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
            "Cascade fallback should use TransactWriteItems"
        )
        assert capacity_counter.transact_write_items[0] == 2, (
            "Cascade should write 2 items (child + parent)"
        )
        assert capacity_counter.update_item == 1, (
            "Speculative UpdateItem attempted for child before fallback"
        )

    def test_adjust_uses_write_each(self, sync_limiter, capacity_counter):
        """Verify: adjust() uses write_each (independent UpdateItem calls).

        With speculative writes (default-on, issue #315):
        - Single entity: speculative UpdateItem (1) + adjust write_each (1) = 2 UpdateItem
        - Cascade: speculative child+parent (2 UpdateItem) + adjust write_each (2) = 4 UpdateItem

        write_each avoids TransactWriteItems for unconditional ADD adjustments,
        saving WCU cost (1 WCU per item vs 2 WCU per item in transactions).
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]

        # --- Single entity: adjust dispatches 1 UpdateItem ---
        with sync_limiter.acquire(
            entity_id="adjust-single",
            resource="api",
            limits=limits,
            consume={"rpm": 1},
        ) as lease:
            pass  # pre-warm bucket

        capacity_counter.reset()

        with capacity_counter.counting():
            with sync_limiter.acquire(
                entity_id="adjust-single",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ) as lease:
                lease.adjust(rpm=5)

        # Speculative UpdateItem (pre-committed) + adjustment write_each → UpdateItem
        assert capacity_counter.update_item == 2, (
            "Should have 2 UpdateItem calls (speculative commit + adjustment)"
        )
        assert len(capacity_counter.transact_write_items) == 0, (
            "Adjustment should not use TransactWriteItems"
        )

        # --- Cascade: adjust dispatches 2 independent UpdateItem calls ---
        sync_limiter.create_entity("adjust-parent", name="Parent")
        sync_limiter.create_entity(
            "adjust-child", name="Child", parent_id="adjust-parent", cascade=True
        )

        # Pre-warm buckets
        with sync_limiter.acquire(
            entity_id="adjust-child",
            resource="api",
            limits=limits,
            consume={"rpm": 1},
        ):
            pass

        capacity_counter.reset()

        with capacity_counter.counting():
            with sync_limiter.acquire(
                entity_id="adjust-child",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ) as lease:
                lease.adjust(rpm=5)

        # Speculative: child UpdateItem (1) + parent UpdateItem (1) = 2
        # Adjustment: write_each → 2 independent UpdateItem calls
        assert capacity_counter.update_item == 4, (
            "Should have 4 UpdateItem calls (2 speculative + 2 adjustment)"
        )
        assert len(capacity_counter.transact_write_items) == 0, (
            "Speculative path avoids TransactWriteItems"
        )


class TestSpeculativeCapacity:
    """Verify DynamoDB capacity for speculative UpdateItem path (issue #315).

    Speculative acquire skips the BatchGetItem read round trip by attempting
    a conditional UpdateItem directly. On success, saves 1 RCU (0 reads).
    On failure with exhausted bucket, raises immediately (0 RCU, 0 WCU).
    """

    def test_speculative_success_non_cascade(self, sync_limiter, capacity_counter):
        """Verify: speculative acquire with pre-warmed bucket uses 0 RCU for bucket reads.

        Expected calls (speculative path):
        - GetItem calls (version check + limit resolution)
        - 0 BatchGetItem (no bucket read — this is the savings!)
        - 1 UpdateItem (speculative consume with condition)
        - 0 PutItem, 0 TransactWriteItems
        """
        limits = [Limit.per_minute("rpm", 1_000_000)]

        # Pre-warm: normal path creates the bucket
        with sync_limiter.acquire(
            entity_id="spec-success",
            resource="api",
            limits=limits,
            consume={"rpm": 1},
        ):
            pass

        # Enable speculative writes
        sync_limiter._speculative_writes = True
        capacity_counter.reset()

        with capacity_counter.counting():
            with sync_limiter.acquire(
                entity_id="spec-success",
                resource="api",
                limits=limits,
                consume={"rpm": 1},
            ):
                pass

        # Key assertion: no BatchGetItem (speculative skips bucket read)
        assert len(capacity_counter.batch_get_item) == 0, (
            "Speculative path should not read buckets via BatchGetItem"
        )
        # Single UpdateItem for speculative consume
        assert capacity_counter.update_item == 1, (
            "Should have exactly 1 UpdateItem (speculative consume)"
        )
        assert capacity_counter.put_item == 0, "Speculative should not use PutItem"
        assert len(capacity_counter.transact_write_items) == 0, (
            "Speculative should not use TransactWriteItems"
        )

    def test_speculative_fast_rejection_zero_rcu(self, sync_limiter, capacity_counter):
        """Verify: fast rejection on exhausted bucket uses 0 RCU.

        When speculative UpdateItem fails and refill won't help, we raise
        RateLimitExceeded immediately using ALL_OLD data — no read round trip.

        Expected calls:
        - GetItem calls (version check + limit resolution)
        - 1 UpdateItem (condition fails, but API call still counted)
        - 0 BatchGetItem (no fallback to slow path)
        """
        from zae_limiter.exceptions import RateLimitExceeded

        limits = [Limit.per_minute("rpm", 100)]

        # Pre-warm and exhaust the bucket
        with sync_limiter.acquire(
            entity_id="spec-reject",
            resource="api",
            limits=limits,
            consume={"rpm": 100},
        ):
            pass

        # Enable speculative writes
        sync_limiter._speculative_writes = True
        capacity_counter.reset()

        with capacity_counter.counting():
            with pytest.raises(RateLimitExceeded):
                with sync_limiter.acquire(
                    entity_id="spec-reject",
                    resource="api",
                    limits=limits,
                    consume={"rpm": 50},
                ):
                    pass

        # Key assertion: no BatchGetItem (fast rejection, no slow path)
        assert len(capacity_counter.batch_get_item) == 0, (
            "Fast rejection should not fall back to BatchGetItem"
        )
        # The failed UpdateItem call is still counted
        assert capacity_counter.update_item == 1, "Should have 1 UpdateItem call (condition failed)"
        assert capacity_counter.put_item == 0, "Should not use PutItem"
        assert len(capacity_counter.transact_write_items) == 0, "Should not use TransactWriteItems"


class TestScheduledFastPathCapacity:
    """The load-bearing claim of #222: a schedule costs the fast path nothing.

    `vu` exists so the speculative condition can gate on a schedule without
    evaluating one. If a scheduled bucket ever reached config — or the
    aggregator's parse, or a shard discovery — the entire design would be a
    more expensive way to do what `set_limits` already does.
    """

    SCHEDULE = (ScheduleEntry(cron="* * * * *", tz="UTC", scale=0.5),)

    def test_future_vu_costs_no_reads(self, sync_limiter, capacity_counter):
        limits = [Limit.per_minute("rpm", 1_000_000).with_schedule(self.SCHEDULE)]
        # Warm the bucket, the entity cache and the config cache.
        with sync_limiter.acquire("vu-cap", "api", limits=limits, consume={"rpm": 1}):
            pass

        capacity_counter.reset()
        with capacity_counter.counting():
            with sync_limiter.acquire("vu-cap", "api", limits=limits, consume={"rpm": 1}):
                pass

        assert capacity_counter.get_item == 0, "fast path must not read config"
        assert capacity_counter.batch_get_item == [], "fast path must not batch-read"
        assert capacity_counter.query == 0, "fast path must not query"
        assert capacity_counter.update_item == 1, "one conditional UpdateItem, as unscheduled"

    def test_an_unscheduled_bucket_costs_exactly_the_same(self, sync_limiter, capacity_counter):
        """Discriminates the test above: the numbers are only meaningful if the
        unscheduled baseline they are being compared to is identical."""
        limits = [Limit.per_minute("rpm", 1_000_000)]
        with sync_limiter.acquire("vu-cap-base", "api", limits=limits, consume={"rpm": 1}):
            pass

        capacity_counter.reset()
        with capacity_counter.counting():
            with sync_limiter.acquire("vu-cap-base", "api", limits=limits, consume={"rpm": 1}):
                pass

        assert capacity_counter.get_item == 0
        assert capacity_counter.batch_get_item == []
        assert capacity_counter.query == 0
        assert capacity_counter.update_item == 1

    def test_the_fan_outs_forced_pass_costs_one_slow_acquire_not_every_one(
        self, sync_limiter, capacity_counter
    ):
        """`vu = 0` buys the clamp for exactly one demoted acquire. Before
        `clear_vu` the stamp was unremovable on an unscheduled bucket and every
        subsequent acquire paid the slow path's 1 RCU + 2 WCU, forever."""
        sync_limiter.create_entity("vu-cap-fanout")
        sync_limiter.set_limits("vu-cap-fanout", [Limit.per_minute("rpm", 1_000_000)])
        with sync_limiter.acquire("vu-cap-fanout", "api", consume={"rpm": 1}):
            pass
        sync_limiter.set_limits("vu-cap-fanout", [Limit.per_minute("rpm", 500_000)])

        capacity_counter.reset()
        with capacity_counter.counting():
            with sync_limiter.acquire("vu-cap-fanout", "api", consume={"rpm": 1}):
                pass
        assert capacity_counter.batch_get_item, "the forced pass IS a slow path"

        capacity_counter.reset()
        with capacity_counter.counting():
            with sync_limiter.acquire("vu-cap-fanout", "api", consume={"rpm": 1}):
                pass
        assert capacity_counter.batch_get_item == [], "and the next one is not"
        assert capacity_counter.update_item == 1


class TestDurationWindowCapacity:
    """The claim ADR-139 rests on: ``reset_after`` is a per-rollover cost, never
    a per-acquire one.

    Inside a live window the fast path is byte-identical to any other bucket's
    — 0 RCU + 1 WCU, no config read, no bucket read. A rollover costs exactly
    one ordinary slow pass plus the fan-out, ``(S - 1) × L`` conditional
    ``UpdateItem``s, and nothing extra at ``S = 1``.
    """

    RESOURCE = "gpt-4"
    WINDOW = timedelta(hours=5)
    WINDOW_MS = 5 * 3_600_000
    T0 = 1_757_000_000_000

    @staticmethod
    def _freeze(limiter, now_ms: int) -> None:
        limiter._repository._now_ms = lambda: now_ms

    @staticmethod
    def _counts(counter) -> dict:
        """Every counter, as a comparable snapshot."""
        return {
            "get_item": counter.get_item,
            "batch_get_item": list(counter.batch_get_item),
            "query": counter.query,
            "put_item": counter.put_item,
            "update_item": counter.update_item,
            "delete_item": counter.delete_item,
            "transact_write_items": list(counter.transact_write_items),
            "batch_write_item": list(counter.batch_write_item),
        }

    def _limits(self, window_limits: int) -> list[Limit]:
        return [
            Limit.quota(f"session{i}", 10_000, reset_after=self.WINDOW)
            for i in range(window_limits)
        ]

    def _seed(self, limiter, entity_id: str, shards: int, window_limits: int) -> None:
        """Anchor one window at ``T0`` and bring ``shards`` shards into it the
        way the real path does: shard 0 first, then doublings, then a draw of
        every new shard, which joins the window in progress."""
        repo = limiter._repository
        consume = {f"session{i}": 1 for i in range(window_limits)}
        limiter.set_limits(entity_id, self._limits(window_limits), resource=self.RESOURCE)
        self._freeze(limiter, self.T0)
        with limiter.acquire(entity_id, self.RESOURCE, consume=consume):
            pass
        count = 1
        while count < shards:
            count = repo.bump_shard_count(entity_id, self.RESOURCE, count)
        # A zero-token draw creates the shard without spending: the #587
        # transfer grants at most one share per created shard, so a later shard
        # can legitimately start empty and would reject a one-token draw.
        nothing = dict.fromkeys(consume, 0)
        for shard in range(1, shards):
            with pinned_shard(shard), limiter.acquire(entity_id, self.RESOURCE, consume=nothing):
                pass

    @staticmethod
    def _force_slow_pass(limiter, entity_id: str, shard: int) -> None:
        """Close one shard's fast path without moving its window.

        ``vu = 0`` is exactly what the limit-change fan-out stamps (#468), so
        the next acquire on the shard is an ordinary materialising slow pass —
        the baseline a rollover is measured against.
        """
        repo = limiter._repository
        repo._get_client().update_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(repo._namespace_id, entity_id, "gpt-4", shard)},
                "SK": {"S": schema.sk_state()},
            },
            UpdateExpression="SET #vu = :zero",
            ExpressionAttributeNames={"#vu": schema.BUCKET_FIELD_VU},
            ExpressionAttributeValues={":zero": {"N": "0"}},
        )

    def test_inside_a_live_window_an_acquire_is_one_write_and_no_reads(
        self, sync_limiter, capacity_counter
    ):
        """0 RCU + 1 WCU per acquire, ten times over, with zero config reads."""
        sync_limiter.set_limits(
            "win-fast",
            [Limit.quota("session", 10_000, reset_after=self.WINDOW)],
            resource=self.RESOURCE,
        )
        # Warms the config cache and the entity cache, and anchors the window.
        with sync_limiter.acquire("win-fast", self.RESOURCE, consume={"session": 1}):
            pass

        capacity_counter.reset()
        with capacity_counter.counting():
            for _ in range(10):
                with sync_limiter.acquire("win-fast", self.RESOURCE, consume={"session": 1}):
                    pass

        assert capacity_counter.get_item == 0, "the fast path must not read config"
        assert capacity_counter.batch_get_item == [], "nor batch-read buckets"
        assert capacity_counter.query == 0
        assert capacity_counter.update_item == 10, "one conditional UpdateItem per acquire"
        assert capacity_counter.total_rcus == 0
        assert capacity_counter.total_wcus == 10

    def test_an_exhausted_window_rejects_for_free(self, sync_limiter, capacity_counter):
        """A rejection inside the window is the speculative fast rejection: the
        one failed conditional and nothing else. It writes nothing, which is
        also why it cannot move the window's anchor (ADR-139)."""
        sync_limiter.set_limits(
            "win-reject",
            [Limit.quota("session", 3, reset_after=self.WINDOW)],
            resource=self.RESOURCE,
        )
        with sync_limiter.acquire("win-reject", self.RESOURCE, consume={"session": 3}):
            pass

        capacity_counter.reset()
        with capacity_counter.counting():
            with pytest.raises(RateLimitExceeded):
                with sync_limiter.acquire("win-reject", self.RESOURCE, consume={"session": 1}):
                    pass

        assert self._counts(capacity_counter) == {
            "get_item": 0,
            "batch_get_item": [],
            "query": 0,
            "put_item": 0,
            "update_item": 1,
            "delete_item": 0,
            "transact_write_items": [],
            "batch_write_item": [],
        }

    @pytest.mark.parametrize(
        ("shards", "window_limits"),
        [(1, 1), (4, 1), (4, 2)],
        ids=["S=1,L=1", "S=4,L=1", "S=4,L=2"],
    )
    def test_a_rollover_costs_one_slow_pass_plus_the_fan_out(
        self, sync_limiter, capacity_counter, shards, window_limits
    ):
        """Measured, not asserted from the formula: the rollover's counters are
        the non-rolling slow pass's plus ``(S - 1) × L`` UpdateItems, and every
        other counter is identical."""
        entity_id = f"win-roll-{shards}-{window_limits}"
        consume = {f"session{i}": 1 for i in range(window_limits)}
        self._seed(sync_limiter, entity_id, shards, window_limits)
        drawn = min(1, shards - 1)  # a sibling holding a full transferred share

        # Baseline: an identical slow pass on the same shard, window still live.
        self._force_slow_pass(sync_limiter, entity_id, drawn)
        self._freeze(sync_limiter, self.T0 + 1_000)
        capacity_counter.reset()
        with capacity_counter.counting(), pinned_shard(drawn):
            with sync_limiter.acquire(entity_id, self.RESOURCE, consume=consume):
                pass
        baseline = self._counts(capacity_counter)

        # The rollover: the same shard, past the window's end. The baseline
        # pass re-stamped `vu` at that end, so this is a boundary demotion.
        self._freeze(sync_limiter, self.T0 + self.WINDOW_MS + 100)
        capacity_counter.reset()
        with capacity_counter.counting(), pinned_shard(drawn):
            with sync_limiter.acquire(entity_id, self.RESOURCE, consume=consume):
                pass
        rollover = self._counts(capacity_counter)

        fan_out = (shards - 1) * window_limits
        assert rollover == {**baseline, "update_item": baseline["update_item"] + fan_out}
        # The slow pass itself, pinned so a change to it is visible here too:
        # the failed speculative UpdateItem, the uncached disabled walk
        # (ADR-125), META + bucket, and the one rf-locked write.
        assert baseline == {
            "get_item": 0,
            "batch_get_item": [3, 2],
            "query": 0,
            "put_item": 0,
            "update_item": 2,
            "delete_item": 0,
            "transact_write_items": [],
            "batch_write_item": [],
        }

        # And the fan-out landed: every shard is on the one new window.
        repo = sync_limiter._repository
        client = repo._get_client()
        starts = set()
        for shard in range(shards):
            item = client.get_item(
                TableName=repo.table_name,
                Key={
                    "PK": {"S": schema.pk_bucket(repo._namespace_id, entity_id, "gpt-4", shard)},
                    "SK": {"S": schema.sk_state()},
                },
            )["Item"]
            for i in range(window_limits):
                starts.add(int(item[schema.bucket_attr(f"session{i}", "ws")]["N"]))
        assert starts == {self.T0 + self.WINDOW_MS + 100}
