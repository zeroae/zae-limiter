# Architecture

This guide covers the internal architecture of zae-limiter, including the DynamoDB schema and token bucket implementation.

## DynamoDB Schema (Single Table)

All data is stored in a single DynamoDB table using a composite key pattern:

| Record Type | PK | SK |
|-------------|----|----|
| Entity metadata | `{ns}/ENTITY#{id}` | `#META` |
| Bucket (v0.9.0+) | `{ns}/BUCKET#{id}#{resource}#{shard}` | `#STATE` |
| Entity config | `{ns}/ENTITY#{id}` | `#CONFIG#{resource}` |
| Resource config | `{ns}/RESOURCE#{resource}` | `#CONFIG` |
| System config | `{ns}/SYSTEM#` | `#CONFIG` |
| Usage snapshot | `{ns}/ENTITY#{id}` | `#USAGE#{resource}#{window_key}` |
| System version | `{ns}/SYSTEM#` | `#VERSION` |
| Audit events | `{ns}/AUDIT#{entity_id}` | `#AUDIT#{timestamp}` |
| Namespace forward | `_/SYSTEM#` | `#NAMESPACE#{name}` |
| Namespace reverse | `_/SYSTEM#` | `#NSID#{id}` |

### Global Secondary Indexes

| Index | Purpose | Key Pattern |
|-------|---------|-------------|
| **GSI1** | Parent → Children lookup | `GSI1PK={ns}/PARENT#{id}` → `GSI1SK=CHILD#{id}` |
| **GSI2** | Resource aggregation | `GSI2PK={ns}/RESOURCE#{name}` → buckets/usage |
| **GSI3** | Entity config queries (sparse) + Bucket discovery by entity (KEYS_ONLY) | `GSI3PK={ns}/ENTITY_CONFIG#{resource}` → `GSI3SK=entity_id` or `GSI3PK={ns}/ENTITY#{id}` → `GSI3SK=BUCKET#{resource}#{shard}` |
| **GSI4** | Namespace item discovery (KEYS_ONLY) | `GSI4PK={ns}` → `GSI4SK=PK` |

### Access Patterns

| Pattern | Query |
|---------|-------|
| Get entity | `PK={ns}/ENTITY#{id}, SK=#META` |
| Get bucket (specific shard) | `PK={ns}/BUCKET#{id}#{resource}#{shard}, SK=#STATE` |
| Get buckets (all resources) | GSI3: `GSI3PK={ns}/ENTITY#{id}` then BatchGetItem on discovered PKs |
| Batch get buckets | `BatchGetItem` with multiple `PK={ns}/BUCKET#{id}#{resource}#0, SK=#STATE` pairs |
| Get children | GSI1: `GSI1PK={ns}/PARENT#{id}` |
| Resource capacity | GSI2: `GSI2PK={ns}/RESOURCE#{name}, SK begins_with BUCKET#` |
| Get version | `PK={ns}/SYSTEM#, SK=#VERSION` |
| Get audit events | `PK={ns}/AUDIT#{entity_id}, SK begins_with #AUDIT#` |
| Get usage snapshots | `PK={ns}/ENTITY#{id}, SK begins_with #USAGE#` |
| Get system config | `PK={ns}/SYSTEM#, SK=#CONFIG` |
| Get resource config | `PK={ns}/RESOURCE#{resource}, SK=#CONFIG` |
| Get entity config | `PK={ns}/ENTITY#{id}, SK=#CONFIG#{resource}` |
| List entities with custom limits | GSI3: `GSI3PK={ns}/ENTITY_CONFIG#{resource}` |
| Namespace forward lookup | `PK=_/SYSTEM#, SK=#NAMESPACE#{name}` |
| Namespace reverse lookup | `PK=_/SYSTEM#, SK=#NSID#{id}` |
| List all items in namespace | GSI4: `GSI4PK={ns}` |

### Namespace Isolation

All partition key values are prefixed with an opaque namespace ID (`{ns}/`), providing logical isolation between tenants within a single DynamoDB table. The reserved namespace `_` is used for the namespace registry itself (forward and reverse lookup records).

- **Namespace ID format**: 11-character opaque string generated via `secrets.token_urlsafe(8)`
- **Default namespace**: Automatically registered on first deploy (CLI) or `RepositoryBuilder.build()`
- **GSI4**: A KEYS_ONLY index on `GSI4PK=namespace_id` enables `purge_namespace()` to discover and delete all items belonging to a namespace

### Optimized Read Patterns

The `acquire()` operation uses `BatchGetItem` to fetch all required buckets in a
single DynamoDB round trip (see [Issue #133](https://github.com/zeroae/zae-limiter/issues/133)):

```{.python .lint-only}
# Before: N sequential GetItem calls
for entity_id, resource, limit_name in bucket_keys:
    bucket = await get_bucket(entity_id, resource, limit_name)

# After: 1 BatchGetItem call
buckets = await batch_get_buckets(bucket_keys)
```

This optimization is particularly beneficial for cascade scenarios where both
entity and parent buckets are fetched together, reducing latency from
2×N round trips to 1.

### Config Resolution (ADR-122)

Config resolution uses the 4-level hierarchy: **Entity (resource-specific) > Entity (`_default_`) > Resource > System**. This logic lives on `Repository.resolve_limits()`, not on `RateLimiter`. Each backend can use native resolution strategies (e.g., SQL `UNION ALL`, Redis Lua scripts). The DynamoDB implementation uses `BatchGetItem` for all 4 config keys in a single round trip, with `ConfigCache` as an internal caching layer (60s TTL by default).

```{.python .lint-only}
# Repository resolves limits internally (ADR-122)
limits, on_unavailable, config_source = await repo.resolve_limits(entity_id, resource)
# config_source: "entity", "entity_default", "resource", "system", or None
```

Cache management methods (`invalidate_config_cache()`, `get_cache_stats()`) are on `Repository`, not `RateLimiter`. The `config_cache_ttl` parameter is on the `Repository` constructor.

### Item Structure

All records use **flat schema** (v0.6.0+, top-level attributes, no nested `data.M`).
See [ADR-111](../adr/111-flatten-all-records.md).

```{.python .lint-only}
# Entity record (FLAT structure):
{
    "PK": "{ns}/ENTITY#user-1",
    "SK": "#META",
    "entity_id": "user-1",
    "name": "User One",
    "parent_id": null,
    "metadata": {...}
}
```

**Bucket records** (composite, one item per entity+resource+shard, v0.9.0+):

```python
# Bucket record (FLAT structure, ADR-114/115, GHSA-76rv):
{
    "PK": "{ns}/BUCKET#user-1#gpt-4#0",   # per-(entity, resource, shard) PK
    "SK": "#STATE",
    "entity_id": "user-1",
    "resource": "gpt-4",
    "shard_count": 1,                       # total shards for this entity+resource
    "b_tpm_tk": 9500000,                    # tokens_milli for tpm limit
    "b_tpm_cp": 10000000,                   # capacity_milli for tpm limit
    "b_tpm_tc": 500000,                     # total_consumed_milli for tpm
    "b_rpm_tk": 95000,                      # tokens_milli for rpm limit
    "b_rpm_cp": 100000,                     # capacity_milli for rpm limit
    "b_rpm_tc": 5000,                       # total_consumed_milli for rpm
    "b_wcu_tk": 999000,                     # wcu infrastructure limit tokens
    "b_wcu_cp": 1000000,                    # wcu capacity (1000 WCU/sec)
    "b_wcu_tc": 1000,                       # wcu total consumed
    "rf": 1704067200000,                    # last_refill_ms (shared across limits)
    "cascade": False,
    "GSI2PK": "{ns}/RESOURCE#gpt-4",
    "GSI2SK": "BUCKET#user-1#0",
    "GSI3PK": "{ns}/ENTITY#user-1",         # bucket discovery by entity
    "GSI3SK": "BUCKET#gpt-4#0",
    "ttl": 1234567890
}
```

The `wcu` (write capacity unit) limit is a reserved infrastructure limit auto-injected on every bucket. It tracks per-partition write pressure and is hidden from user-facing output (get_buckets, RateLimitExceeded, usage snapshots). When exhausted, the client doubles `shard_count` to spread writes across more DynamoDB partitions.

The `total_consumed_milli` counter tracks net consumption (increases on consume,
decreases on release) and is used by the aggregator Lambda to accurately calculate
consumption deltas. This counter is independent of token bucket refill, solving
the issue where `old_tokens - new_tokens` gives incorrect results when refill rate
exceeds consumption rate. See [Issue #179](https://github.com/zeroae/zae-limiter/issues/179).

**Usage snapshots use a FLAT structure** (no nested `data` map):

```python
# Usage snapshot (FLAT structure):
{
    "PK": "{ns}/ENTITY#user-1",
    "SK": "#USAGE#gpt-4#2024-01-01T14:00:00Z",
    "entity_id": "user-1",
    "resource": "gpt-4",        # Top-level attribute
    "window": "hourly",         # Top-level attribute
    "window_start": "...",      # Top-level attribute
    "tpm": 5000,                # Counter at top-level
    "total_events": 10,         # Counter at top-level
    "GSI2PK": "{ns}/RESOURCE#gpt-4",
    "ttl": 1234567890
}
```

**Why snapshots are flat:** DynamoDB has a limitation where you cannot SET a map path
(`#data = if_not_exists(#data, :map)`) AND ADD to paths within it (`#data.counter`)
in the same UpdateExpression - it fails with "overlapping document paths" error.
Snapshots require atomic upsert (create-or-update) with ADD counters for usage
aggregation, so they use a flat structure to enable single-call atomic updates.

See: [Issue #168](https://github.com/zeroae/zae-limiter/issues/168)

**Config records use composite items** (v0.8.0+, ADR-114). All limits for a config level are stored in a single item:

```python
# Resource config (composite, FLAT structure):
{
    "PK": "{ns}/RESOURCE#gpt-4",       # or {ns}/SYSTEM# or {ns}/ENTITY#{id}
    "SK": "#CONFIG",                   # or #CONFIG#{resource} for entity level
    "resource": "gpt-4",
    "l_tpm_cp": 100000,               # capacity for tpm limit
    "l_tpm_ra": 100000,               # refill_amount for tpm limit
    "l_tpm_rp": 60,                   # refill_period_seconds for tpm limit
    "config_version": 1               # Atomic counter for cache invalidation
}
```

Config records use four-level precedence: **Entity (resource-specific) > Entity (_default_) > Resource > System > Constructor defaults**.

**Key builders:**

- `pk_system(namespace_id)` - Returns `{ns}/SYSTEM#`
- `pk_resource(namespace_id, resource)` - Returns `{ns}/RESOURCE#{resource}`
- `pk_entity(namespace_id, entity_id)` - Returns `{ns}/ENTITY#{entity_id}`
- `pk_bucket(namespace_id, entity_id, resource, shard_id)` - Returns `{ns}/BUCKET#{id}#{resource}#{shard}` (v0.9.0+)
- `sk_state()` - Returns `#STATE` (bucket state sort key)
- `sk_config()` - Returns `#CONFIG` (system/resource level)
- `sk_config(resource)` - Returns `#CONFIG#{resource}` (entity level)
- `sk_namespace(name)` - Returns `#NAMESPACE#{name}` (forward lookup)
- `sk_nsid(id)` - Returns `#NSID#{id}` (reverse lookup)
- `gsi3_pk_entity(namespace_id, entity_id)` - Returns `{ns}/ENTITY#{id}` (bucket discovery)
- `gsi3_sk_bucket(resource, shard_id)` - Returns `BUCKET#{resource}#{shard}` (bucket discovery)
- `parse_bucket_pk(pk)` - Parses `{ns}/BUCKET#{id}#{res}#{shard}` into components

**Audit entity IDs for config levels** (see [ADR-106](../adr/106-audit-entity-ids-for-config.md)):

- System config: Uses `$SYSTEM` as entity_id
- Resource config: Uses `$RESOURCE:{resource_name}` (e.g., `$RESOURCE:gpt-4`)

## Token Bucket Implementation

For a conceptual overview of the token bucket algorithm, see the [User Guide](../guide/token-bucket.md). This section covers implementation details for contributors.

### Core Functions

The algorithm is implemented in [`bucket.py`](https://github.com/zeroae/zae-limiter/blob/main/src/zae_limiter/bucket.py):

| Function | Purpose |
|----------|---------|
| `refill_bucket()` | Calculate refilled tokens with drift compensation |
| `try_consume()` | Atomic check-and-consume operation |
| `force_consume()` | Force consume (can go negative) |
| `calculate_retry_after()` | Calculate wait time for deficit |
| `calculate_available()` | Calculate currently available tokens |
| `build_limit_status()` | Build a LimitStatus for a bucket check |
| `would_refill_satisfy()` | Check if refilling would allow a request to succeed (speculative writes) |

### Mathematical Formulas

**Refill calculation** (lazy, on-demand):

```
tokens_to_add = (elapsed_ms × refill_amount_milli) // refill_period_ms
```

**Drift compensation** (prevents accumulated rounding errors):

```
time_used_ms = (tokens_to_add × refill_period_ms) // refill_amount_milli
new_last_refill = last_refill_ms + time_used_ms
```

The inverse calculation ensures we only "consume" the time that corresponds to whole tokens, preventing drift over many refill cycles.

**Retry-after calculation**:

```
time_ms = (deficit_milli × refill_period_ms) // refill_amount_milli
retry_seconds = (time_ms + 1) / 1000.0  # +1ms rounds up
```

### Integer Arithmetic for Precision

All token values are stored as **millitokens** (×1000) to avoid floating-point precision issues in distributed systems:

```python
# User sees: 100 tokens/minute
# Stored as: 100,000 millitokens/minute
capacity_milli = 100_000
```

**Why integers matter in distributed systems:**

- Floating-point operations can produce different results on different hardware
- DynamoDB stores numbers as strings, so precision loss can occur during serialization
- Rate limiting across multiple nodes requires identical calculations

### Refill Rate Storage

Refill rates are stored as a fraction (amount/period) rather than a decimal:

```python
# 100 tokens per minute stored as:
refill_amount_milli = 100_000  # millitokens (numerator)
refill_period_ms = 60_000      # milliseconds (denominator)
```

This avoids representing `1.6667 tokens/second` as a float. Instead:

```python
# 100 tokens/minute = 100,000 millitokens / 60,000 ms
# Integer division handles the math precisely
```

### Lazy Refill with Drift Compensation

Tokens are calculated on-demand rather than via a background timer. The `refill_bucket()` function:

1. Calculates elapsed time since last refill
2. Computes tokens to add using integer division
3. Tracks "time consumed" to prevent drift

```{.python .lint-only}
# From bucket.py:refill_bucket()
tokens_to_add = (elapsed_ms * refill_amount_milli) // refill_period_ms

# Drift compensation: only advance time for tokens actually added
time_used_ms = (tokens_to_add * refill_period_ms) // refill_amount_milli
new_last_refill = last_refill_ms + time_used_ms
```

Without drift compensation, repeated calls with small time intervals would accumulate rounding errors.

### Negative Buckets (Debt)

Buckets can go negative to support post-hoc reconciliation:

```{.python .lint-only}
# Estimate 500 tokens, actually used 2000
async with limiter.acquire(consume={"tpm": 500}) as lease:
    actual = await call_llm()  # Returns 2000 tokens
    await lease.adjust(tpm=2000 - 500)  # Bucket at -1500
```

The `force_consume()` function handles this:

```{.python .lint-only}
# From bucket.py:force_consume()
# Consume can go negative - no bounds checking
new_tokens_milli = refill.new_tokens_milli - (amount * 1000)
```

The debt is repaid as tokens refill over time. A bucket at -1500 millitokens needs 1.5 minutes to reach 0 (at 1000 tokens/minute).

### Design Decisions

| Decision | Rationale |
|----------|-----------|
| Integer over float | Identical results across distributed nodes; no precision drift |
| Lazy over continuous | No background timers; accurate retry_after; efficient |
| Negative allowed | Estimate-then-reconcile pattern; operations with unknown cost |
| Fraction over decimal | Exact representation of rates like 100/minute |

## Atomicity and Write Paths

### Write Path Overview

The `acquire()` context manager uses three distinct write paths, each with different
atomicity and cost characteristics:

| Write Path | Method | API Used | WCU Cost | Atomicity |
|------------|--------|----------|----------|-----------|
| Initial consumption | `_commit_initial()` | `transact_write()` | 2 WCU (transaction) or 1 WCU (single item) | Cross-item atomic |
| Post-enter adjustments | `_commit_adjustments()` | `write_each()` | 1 WCU per item | Independent per item |
| Rollback (on exception) | `_rollback()` | `write_each()` | 1 WCU per item | Independent per item |
| Aggregator refill | `try_refill_bucket()` | `UpdateItem` | 1 WCU | Single item |

### TransactWriteItems (Initial Consumption)

The initial consumption write (`_commit_initial()`) uses `transact_write()`, which
selects the DynamoDB API based on item count:

- **Single item**: Uses PutItem/UpdateItem (1 WCU) -- non-cascade case
- **Multiple items**: Uses TransactWriteItems (2 WCU per item) -- cascade case

```python
# Cascade: atomic multi-entity write
# 1. Consume from child entity bucket
# 2. Consume from parent entity bucket
# Both succeed or both fail
```

Transaction limits: max 100 items per transaction.

### Independent Writes (Adjustments and Rollbacks)

Adjustments (`_commit_adjustments()`) and rollbacks (`_rollback()`) use `write_each()`,
which dispatches each item independently as a single PutItem, UpdateItem, or DeleteItem
call (1 WCU each). This is safe because:

- These operations produce **unconditional ADD** expressions (no condition checks)
- Partial success is acceptable -- each item's delta is self-contained
- No cross-item invariant needs to hold between adjustment writes

```python
# write_each: each item written independently
# Item 1: ADD child bucket delta    (1 WCU)
# Item 2: ADD parent bucket delta   (1 WCU)
# No transaction overhead
```

This halves the WCU cost compared to using TransactWriteItems for these paths.

### Speculative Write Path (Issue #315)

When `speculative_writes=True`, `acquire()` adds a fast path before the normal read-write flow:

| Write Path | Method | API Used | WCU Cost | Atomicity |
|------------|--------|----------|----------|-----------|
| Speculative consumption | `speculative_consume()` | Conditional `UpdateItem` | 1 WCU (success) or 0 WCU (reject) | Single item |
| Speculative compensation | `_compensate_speculative()` via `write_each()` | `UpdateItem` | 1 WCU | Single item |
| Parallel speculative (issue #318) | `speculative_consume()` via `asyncio.gather` | 2x `UpdateItem` | 2 WCU | Independent items |
| Parent-only slow path | `_try_parent_only_acquire()` via `_commit_initial()` | `UpdateItem` | 1 WCU | Single item |

The speculative path uses `ReturnValuesOnConditionCheckFailure=ALL_OLD` to inspect bucket state
on failure without a separate read. On success, `ReturnValues=ALL_NEW` provides the post-write
state including denormalized `cascade` and `parent_id` fields.

```
Speculative flow (first acquire — sequential, populates entity cache):
1. Pick shard: random.randrange(shard_count) from entity cache (shard 0 if no cache)
2. UpdateItem on PK={ns}/BUCKET#{id}#{resource}#{shard} with condition:
   attribute_exists(PK) AND all app limits tk >= consumed AND wcu tk >= 1000
   +- SUCCESS -> Populate entity cache (cascade, parent_id, shard_counts), Lease pre-committed
   |  +- cascade=False -> DONE
   |  +- cascade=True -> Speculative UpdateItem on parent (sequential)
   |     +- SUCCESS -> DONE (child + parent both speculative)
   |     +- FAIL -> [parent failure handling]
   +- FAIL -> Check ALL_OLD
      +- No item (bucket missing) -> Fall back to normal path
      +- wcu exhausted -> Double shard_count (conditional write on shard 0), fall back
      +- App limits: Refill would help -> Fall back to normal path
      +- App limits: Refill won't help, multi-shard -> Retry on another shard (up to 2 retries)
      +- App limits: Refill won't help, single shard -> RateLimitExceeded (fast rejection)

Shard retry flow (when app limits exhausted on multi-shard entity):
1. Pick untried shard: random choice from remaining shards
2. Speculative UpdateItem on new shard
   +- SUCCESS -> Lease pre-committed on new shard, DONE
   +- FAIL -> Try next untried shard (up to _MAX_SHARD_RETRIES=2 total retries)
   +- All retries exhausted -> RateLimitExceeded

Speculative flow (subsequent acquire — parallel, issue #318):
1. Entity cache hit: cascade=True, parent_id known, shard_counts known
2. asyncio.gather(child_speculative, parent_speculative)
   +- BOTH SUCCEED -> DONE (1 round trip, 0 RCU, 2 WCU)
   +- CHILD FAILS, PARENT SUCCEEDS -> Compensate parent, check child
   +- CHILD SUCCEEDS, PARENT FAILS -> [parent failure handling]
   +- BOTH FAIL -> Check child ALL_OLD, fall back or fast-reject

Parent failure handling (shared by sequential and parallel paths):
   +- No ALL_OLD / missing limit -> Compensate child, fall back
   +- Refill won't help -> Compensate child, RateLimitExceeded
   +- Refill would help -> Parent-only slow path (read + write parent)
      +- SUCCESS -> DONE (child speculative + parent slow path)
      +- FAIL -> Compensate child, fall back to full slow path
```

Cascade and `parent_id` are denormalized into composite bucket items (via `build_composite_create`)
so the speculative path avoids a separate entity metadata lookup.

**Deferred cascade compensation:** When the child speculative write succeeds but the parent
fails, child compensation is deferred. If refill would help the parent, a parent-only slow
path is attempted: read parent buckets (0.5 RCU), refill + try_consume, write via single-item
UpdateItem (1 WCU). This avoids the cost of compensating the child (1 WCU), re-reading it
(0.5 RCU), and using TransactWriteItems for the full cascade write (4 WCU). The child is
only compensated when the parent-only path also fails.

**Entity metadata cache (issue #318, GHSA-76rv):** `Repository._entity_cache` stores
`{(namespace_id, entity_id): (cascade, parent_id, shard_counts)}` where `shard_counts` is
`dict[str, int]` mapping resource to shard count. `cascade` and `parent_id` are immutable
(no TTL); `shard_counts` is updated when shard doubling occurs. After the first acquire
populates the cache, `speculative_consume()` issues child and parent speculative writes
concurrently via `asyncio.gather`. This reduces cascade latency
from 2 sequential round trips to 1 parallel round trip. In the sync codepath,
`asyncio.gather(a, b)` is transformed to `self._run_in_executor(lambda: a, lambda: b)` using
a lazy `ThreadPoolExecutor(max_workers=2)` for true parallel execution.
The `_compensate_speculative()` method handles compensation for either child or parent when
one side of the parallel write fails.

The shard_count from the cache determines which shard to target. With multiple shards,
`speculative_consume()` picks a random shard via `random.randrange(shard_count)`. When the
speculative write succeeds, the returned `shard_count` from ALL_NEW updates the cache.

### Aggregator Refill Path (Issue #317)

The Lambda aggregator proactively refills token buckets for active entities, keeping
speculative writes on the fast path (1 RT) instead of falling back to the slow path (3 RT).

| Write Path | Method | API Used | WCU Cost | Atomicity |
|------------|--------|----------|----------|-----------|
| Aggregator refill | `try_refill_bucket()` | Conditional `UpdateItem` | 1 WCU (success) or 0 WCU (lock lost) | Single item |

The aggregator processes DynamoDB Stream records in each batch to:

1. **Aggregate bucket states** -- `aggregate_bucket_states()` accumulates `tc` deltas and keeps
   the last NewImage per (entity_id, resource, shard_id) across all stream records in the batch
2. **Compute refill** -- For each bucket, `try_refill_bucket()` calls `refill_bucket()` with
   effective capacity (`cp // shard_count`) and effective refill amount (`ra // shard_count`),
   then checks if projected tokens are insufficient to cover the observed consumption rate
3. **Write refill** -- Issues a single `UpdateItem` with `ADD b_{limit}_tk +refill_delta`
   and `SET rf = :now`, conditioned on `rf = :expected_rf` (optimistic lock)
4. **Proactive sharding** -- `try_proactive_shard()` checks if wcu consumption >= 80% of capacity
   on shard 0, and conditionally doubles `shard_count`
5. **Shard propagation** -- `propagate_shard_count()` detects shard_count changes in stream records
   from shard 0 and propagates to all other shards via conditional writes (`shard_count < :new`)

```
Aggregator refill flow (per composite bucket shard):
1. Aggregate tc deltas + last NewImage across stream batch per (entity, resource, shard)
2. For each limit: refill_bucket(tk, rf, now, cp/shard_count, ra/shard_count, rp)
   +- refill_delta = new_tk - current_tk
   +- projected = new_tk after refill
   +- consumption_estimate = max(0, accumulated tc_delta)
   +- projected >= consumption_estimate -> SKIP (sufficient tokens)
3. Any limit needs refill?
   +- NO -> SKIP
   +- YES -> UpdateItem (ADD tk +delta, SET rf = :now, condition rf = :expected_rf)
      +- SUCCESS -> refill written (1 WCU)
      +- ConditionalCheckFailedException -> silently skip (another writer updated rf)

Proactive sharding flow (per bucket):
1. Check wcu limit info in aggregated state
2. consumption_ratio = wcu_tc_delta / wcu_capacity_milli
   +- ratio < 0.8 -> SKIP
3. Is this shard 0?
   +- NO -> SKIP (only shard 0 is source of truth)
   +- YES -> UpdateItem (SET shard_count = :new, condition shard_count = :old)
      +- SUCCESS -> shard_count doubled
      +- ConditionalCheckFailedException -> concurrent bump, skip

Shard propagation flow (per stream record):
1. Detect shard_count increase (new > old) in stream record
2. Is this shard 0?
   +- NO -> SKIP
   +- YES -> For each target shard 1..new_count:
      UpdateItem (SET shard_count = :new, condition shard_count < :new OR not exists)
```

**Key design properties:**

- **ADD is commutative** with concurrent speculative writes -- the aggregator uses `ADD`
  for token deltas, so a concurrent `speculative_consume()` (also `ADD`) does not conflict
- **Optimistic lock on `rf`** prevents double-refill with the client slow path or another
  aggregator invocation
- **No read required** -- all state is derived from stream record NewImage fields
- **Shard-aware capacity** -- effective capacity and refill amount are divided by `shard_count`
  so each shard gets its proportional share of tokens
- **Proactive sharding** -- the aggregator doubles shard_count when wcu consumption >= 80%
  of capacity, preventing hot partitions before clients experience throttling
- **Shard propagation** -- shard_count changes on shard 0 are propagated to other shards
  via conditional writes that only update if the target has a lower value

### Optimistic Locking

Entity metadata uses version numbers for optimistic locking:

```python
# Read entity with version 5
# Update fails if version changed
condition_expression="version = :expected_version"
```

## Project Structure

```
src/zae_limiter/
├── __init__.py            # Public API exports
├── models.py              # Limit, Entity, LimitStatus, BucketState, StackOptions, ...
├── exceptions.py          # RateLimitExceeded, RateLimiterUnavailable, etc.
├── naming.py              # Resource name validation
├── bucket.py              # Token bucket math (integer arithmetic)
├── schema.py              # DynamoDB key builders (namespace-prefixed)
├── repository_protocol.py # RepositoryProtocol for backend abstraction
├── repository.py          # DynamoDB operations
├── config_cache.py        # Client-side config caching with TTL
├── lease.py               # Lease context manager
├── limiter.py             # RateLimiter, SyncRateLimiter
├── local.py               # LocalStack management commands
├── cli.py                 # CLI commands (deploy, delete, status, list, local, ...)
├── version.py             # Version tracking and compatibility
├── migrations/            # Schema migration framework
├── visualization/         # Usage snapshot formatting and display
└── infra/
    ├── stack_manager.py    # CloudFormation stack operations
    ├── lambda_builder.py   # Lambda deployment package builder
    ├── discovery.py        # Multi-stack discovery and listing
    └── cfn_template.yaml   # CloudFormation template

src/zae_limiter_aggregator/   # Lambda aggregator (top-level package)
├── __init__.py               # Re-exports handler, processor types
├── handler.py                # Lambda entry point (returns refills_written count)
├── processor.py              # Stream processing: usage snapshots + bucket refill
└── archiver.py               # S3 audit archival (gzip JSONL)
```

## Key Design Decisions

1. **Write-on-enter**: `acquire()` writes initial consumption to DynamoDB before yielding the lease, making tokens immediately visible to concurrent callers. On exception, a compensating write restores the consumed tokens
2. **Bucket can go negative**: `lease.adjust()` never throws, allows debt
3. **Cascade is per-entity config**: Set `cascade=True` on `create_entity()` to auto-cascade to parent on every `acquire()`
4. **Stored limits are the default (v0.5.0+)**: Limits resolved from System/Resource/Entity config automatically. Pass `limits` parameter to override
5. **Initial writes are atomic**: Multi-entity initial consumption uses `transact_write()` for cross-item atomicity
6. **Adjustments and rollbacks use independent writes**: `write_each()` dispatches each item as a single-item API call (1 WCU each), avoiding transaction overhead for unconditional ADD operations
7. **Speculative writes skip reads**: With `speculative_writes=True`, `acquire()` tries a conditional UpdateItem first, saving 1 round trip and 1 RCU when the bucket has sufficient tokens
8. **Entity metadata cache enables parallel cascade**: `Repository._entity_cache` stores `(cascade, parent_id, shard_counts)` per entity. `cascade` and `parent_id` are immutable; `shard_counts` (`dict[str, int]`) is updated on shard doubling. After first acquire, cascade speculative writes run concurrently via `asyncio.gather`
9. **Aggregator-assisted refill**: The Lambda aggregator proactively refills buckets for active entities, keeping speculative writes on the fast path by ensuring buckets have sufficient tokens between client requests. Effective capacity and refill amount are divided by `shard_count` so each shard receives its proportional share
10. **Pre-shard buckets (GHSA-76rv)**: Each bucket item lives on its own DynamoDB partition (`PK={ns}/BUCKET#{id}#{resource}#{shard}`). An auto-injected `wcu:1000` infrastructure limit tracks per-partition write pressure. When wcu is exhausted, the client doubles shard_count (conditional write on shard 0). The aggregator proactively doubles shards at >=80% wcu capacity and propagates shard_count changes from shard 0 to all other shards

## Next Steps

- [Development Setup](development.md) - Setting up your environment
- [Testing](testing.md) - Test organization and fixtures
