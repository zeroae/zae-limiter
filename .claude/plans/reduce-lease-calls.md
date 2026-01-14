# Plan: Reduce DynamoDB Calls During Lease Acquisition

## Current Call Analysis

Based on `tests/benchmark/test_capacity.py` and the code in `limiter.py` and `repository.py`:

### Current Call Counts

| Operation | GetItems | Queries | TransactWrites | Total Calls |
|-----------|----------|---------|----------------|-------------|
| `acquire()` - 1 limit | 1 | 0 | 1 | 2 |
| `acquire()` - 2 limits | 2 | 0 | 1 | 3 |
| `acquire()` - N limits | N | 0 | 1 | N+1 |
| `acquire(cascade=True)` - 1 limit | 3 | 0 | 1 | 4 |
| `acquire(use_stored_limits=True)` - 1 limit | 1 | 2 | 1 | 4 |

### Current Code Flow (`limiter.py:343-424`)

```python
async def _do_acquire(self, entity_id, resource, limits, consume, cascade, use_stored_limits):
    # Step 1: If cascade, get entity to find parent_id
    if cascade:
        entity = await self._repository.get_entity(entity_id)  # 1 GetItem
        if entity and entity.parent_id:
            entity_ids.append(entity.parent_id)

    # Step 2: If use_stored_limits, query limits for each entity
    for eid in entity_ids:
        if use_stored_limits:
            stored = await self._repository.get_limits(eid, resource)  # 1 Query
            if not stored:
                stored = await self._repository.get_limits(eid, DEFAULT_RESOURCE)  # 1 Query

    # Step 3: For each entity and each limit, get bucket INDIVIDUALLY
    for eid in entity_ids:
        for limit in entity_limits[eid]:
            state = await self._repository.get_bucket(eid, resource, limit.name)  # 1 GetItem per limit!

    # Step 4: Commit transaction
    await self.repository.transact_write(items)  # 1 TransactWrite
```

## Optimization Opportunities (No Schema Changes)

### Option 1: Batch Bucket Reads with Query (Recommended)

**Current**: N `get_bucket()` GetItem calls for N limits
**Optimized**: 1 `get_buckets(entity_id, resource)` Query call

The repository ALREADY has `get_buckets()` method (`repository.py:314-334`) that queries all buckets for an entity/resource using `begins_with`:

```python
async def get_buckets(self, entity_id: str, resource: str | None = None) -> list[BucketState]:
    key_condition = "PK = :pk AND begins_with(SK, :sk_prefix)"
    # Returns all buckets in ONE query
```

**Impact**:
| Operation | Before | After | Savings |
|-----------|--------|-------|---------|
| `acquire()` - 2 limits | 3 calls | 2 calls | -33% |
| `acquire()` - 5 limits | 6 calls | 2 calls | -67% |
| `acquire(cascade=True)` - 2 limits | 5 calls | 3 calls | -40% |

**Implementation**:
1. Modify `_do_acquire()` to call `get_buckets(eid, resource)` once per entity
2. Match returned buckets to limits by name
3. Create new bucket states for missing limits

### Option 2: BatchGetItem for Multiple Known Keys

For cascade mode where we need entity + buckets, use DynamoDB BatchGetItem:

```python
# Current: Sequential GetItem calls
entity = await get_entity(entity_id)  # GetItem #1
child_bucket = await get_bucket(entity_id, resource, "rpm")  # GetItem #2
parent_bucket = await get_bucket(parent_id, resource, "rpm")  # GetItem #3

# Optimized: Single BatchGetItem
keys = [
    {"PK": pk_entity(entity_id), "SK": sk_meta()},
    {"PK": pk_entity(entity_id), "SK": sk_bucket(resource, "rpm")},
    {"PK": pk_entity(parent_id), "SK": sk_bucket(resource, "rpm")},
]
results = await batch_get_item(keys)  # 1 BatchGetItem call
```

**Impact**: Reduces latency by eliminating sequential round-trips (same RCU cost but faster).

### Option 3: Combined Entity + Buckets Query

All data for an entity shares the same partition key (`PK = ENTITY#{id}`):

```python
# Query that gets entity metadata + all resource buckets in ONE call
response = await client.query(
    KeyConditionExpression="PK = :pk AND (SK = :meta OR begins_with(SK, :bucket_prefix))",
    ExpressionAttributeValues={
        ":pk": pk_entity(entity_id),
        ":meta": sk_meta(),  # #META
        ":bucket_prefix": sk_bucket_prefix(resource),  # #BUCKET#{resource}#
    }
)
# Parse results: entity from SK=#META, buckets from SK begins_with #BUCKET#
```

**Note**: DynamoDB doesn't support OR in KeyConditionExpression directly. Would need either:
- Two queries (defeats purpose)
- Query with just PK and filter (less efficient but 1 call)
- Change to Query all items for entity and filter client-side

### Option 4: Parallel Async Execution

If keeping individual calls, run them in parallel:

```python
# Current: Sequential
for eid in entity_ids:
    for limit in limits:
        bucket = await get_bucket(eid, resource, limit.name)  # Awaited sequentially

# Optimized: Parallel with asyncio.gather
tasks = []
for eid in entity_ids:
    for limit in limits:
        tasks.append(get_bucket(eid, resource, limit.name))
results = await asyncio.gather(*tasks)  # All run in parallel
```

**Impact**: Same number of calls, but latency reduced to max(individual latencies) instead of sum.

## Recommended Implementation Plan

### Phase 1: Use `get_buckets()` Query (Biggest Win)

**File**: `src/zae_limiter/limiter.py`

```python
async def _do_acquire(self, entity_id, resource, limits, consume, cascade, use_stored_limits):
    # ... entity lookup unchanged ...

    # BEFORE: N GetItem calls
    # for limit in entity_limits[eid]:
    #     state = await self._repository.get_bucket(eid, resource, limit.name)

    # AFTER: 1 Query call
    for eid in entity_ids:
        existing_buckets = await self._repository.get_buckets(eid, resource)
        bucket_map = {b.limit_name: b for b in existing_buckets}

        for limit in entity_limits[eid]:
            state = bucket_map.get(limit.name)
            if state is None:
                state = BucketState.from_limit(eid, resource, limit, now_ms)
            # ... rest unchanged
```

**Expected Results**:
| Operation | Before | After | Savings |
|-----------|--------|-------|---------|
| `acquire()` - 2 limits | 3 calls | 2 calls | 33% |
| `acquire()` - 5 limits | 6 calls | 2 calls | 67% |
| `acquire(cascade=True)` - 2 limits | 5 calls | 3 calls | 40% |
| `acquire(cascade=True)` - 5 limits | 11 calls | 3 calls | 73% |

### Phase 2: Parallel Entity + Bucket Fetch for Cascade

Use `asyncio.gather()` for the entity lookup and bucket query:

```python
if cascade:
    # Parallel fetch: entity and buckets for primary entity
    entity_task = self._repository.get_entity(entity_id)
    buckets_task = self._repository.get_buckets(entity_id, resource)
    entity, buckets = await asyncio.gather(entity_task, buckets_task)
```

**Impact**: Reduces latency for cascade mode.

### Phase 3 (Optional): BatchGetItem for Remaining Point Lookups

Add `batch_get_items()` method to repository for fetching multiple specific items.

## Validation

Update `tests/benchmark/test_capacity.py` to verify reduced call counts:

```python
def test_acquire_multiple_limits_optimized(self, sync_limiter, capacity_counter):
    """Verify: acquire() with N limits = 1 Query + 1 TransactWrite (was N GetItems)."""
    limits = [Limit.per_minute(f"limit_{i}", 1_000_000) for i in range(5)]

    with capacity_counter.counting():
        with sync_limiter.acquire(...):
            pass

    # After optimization: 1 Query instead of 5 GetItems
    assert capacity_counter.query == 1, "Should use 1 Query for all buckets"
    assert capacity_counter.get_item == 0, "Should not use individual GetItems"
```

## Risks and Mitigations

1. **Query returns more data than needed**: If entity has many resources, `get_buckets(eid, resource)` only returns buckets for that specific resource (already filtered by SK prefix).

2. **Empty bucket handling**: Must handle case where query returns no buckets (new entity) - create from limits.

3. **Limit name mismatch**: Query returns buckets by limit_name, must match to provided limits correctly.

## Summary

| Change | Complexity | Impact |
|--------|------------|--------|
| Use `get_buckets()` query | Low | 33-73% fewer calls |
| Parallel async execution | Low | Faster latency |
| BatchGetItem for cascade | Medium | ~33% fewer calls for cascade |

**Recommendation**: Start with Phase 1 (use `get_buckets()`) for immediate 33-73% reduction in DynamoDB calls with minimal code changes.
