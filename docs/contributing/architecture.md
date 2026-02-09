# Architecture

This guide covers the internal architecture of zae-limiter, including the DynamoDB schema and token bucket implementation.

## DynamoDB Schema (Single Table)

All data is stored in a single DynamoDB table using a composite key pattern:

| Record Type | PK | SK |
|-------------|----|----|
| Entity metadata | `ENTITY#{id}` | `#META` |
| Bucket | `ENTITY#{id}` | `#BUCKET#{resource}#{limit_name}` |
| Entity config | `ENTITY#{id}` | `#CONFIG#{resource}` |
| Resource config | `RESOURCE#{resource}` | `#CONFIG` |
| System config | `SYSTEM#` | `#CONFIG` |
| Usage snapshot | `ENTITY#{id}` | `#USAGE#{resource}#{window_key}` |
| System version | `SYSTEM#` | `#VERSION` |
| Audit events | `AUDIT#{entity_id}` | `#AUDIT#{timestamp}` |

### Global Secondary Indexes

| Index | Purpose | Key Pattern |
|-------|---------|-------------|
| **GSI1** | Parent → Children lookup | `GSI1PK=PARENT#{id}` → `GSI1SK=CHILD#{id}` |
| **GSI2** | Resource aggregation | `GSI2PK=RESOURCE#{name}` → buckets/usage |
| **GSI3** | Entity config queries (sparse) | `GSI3PK=ENTITY_CONFIG#{resource}` → `GSI3SK=entity_id` |

### Access Patterns

| Pattern | Query |
|---------|-------|
| Get entity | `PK=ENTITY#{id}, SK=#META` |
| Get buckets | `PK=ENTITY#{id}, SK begins_with #BUCKET#` |
| Batch get buckets | `BatchGetItem` with multiple PK/SK pairs |
| Get children | GSI1: `GSI1PK=PARENT#{id}` |
| Resource capacity | GSI2: `GSI2PK=RESOURCE#{name}, SK begins_with BUCKET#` |
| Get version | `PK=SYSTEM#, SK=#VERSION` |
| Get audit events | `PK=AUDIT#{entity_id}, SK begins_with #AUDIT#` |
| Get usage snapshots | `PK=ENTITY#{id}, SK begins_with #USAGE#` |
| Get system config | `PK=SYSTEM#, SK=#CONFIG` |
| Get resource config | `PK=RESOURCE#{resource}, SK=#CONFIG` |
| Get entity config | `PK=ENTITY#{id}, SK=#CONFIG#{resource}` |
| List entities with custom limits | GSI3: `GSI3PK=ENTITY_CONFIG#{resource}` |

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
    "PK": "ENTITY#user-1",
    "SK": "#META",
    "entity_id": "user-1",
    "name": "User One",
    "parent_id": null,
    "metadata": {...}
}
```

**Bucket records** (composite, one item per entity+resource):

```python
# Bucket record (FLAT structure, ADR-114/115):
{
    "PK": "ENTITY#user-1",
    "SK": "#BUCKET#gpt-4",
    "entity_id": "user-1",
    "resource": "gpt-4",
    "b_tpm_tk": 9500000,            # tokens_milli for tpm limit
    "b_tpm_cp": 10000000,           # capacity_milli for tpm limit
    "b_tpm_tc": 500000,             # total_consumed_milli for tpm
    "b_rpm_tk": 95000,              # tokens_milli for rpm limit
    "b_rpm_cp": 100000,             # capacity_milli for rpm limit
    "b_rpm_tc": 5000,               # total_consumed_milli for rpm
    "rf": 1704067200000,            # last_refill_ms (shared across limits)
    "GSI2PK": "RESOURCE#gpt-4",
    "ttl": 1234567890
}
```

The `total_consumed_milli` counter tracks net consumption (increases on consume,
decreases on release) and is used by the aggregator Lambda to accurately calculate
consumption deltas. This counter is independent of token bucket refill, solving
the issue where `old_tokens - new_tokens` gives incorrect results when refill rate
exceeds consumption rate. See [Issue #179](https://github.com/zeroae/zae-limiter/issues/179).

**Usage snapshots use a FLAT structure** (no nested `data` map):

```python
# Usage snapshot (FLAT structure):
{
    "PK": "ENTITY#user-1",
    "SK": "#USAGE#gpt-4#2024-01-01T14:00:00Z",
    "entity_id": "user-1",
    "resource": "gpt-4",        # Top-level attribute
    "window": "hourly",         # Top-level attribute
    "window_start": "...",      # Top-level attribute
    "tpm": 5000,                # Counter at top-level
    "total_events": 10,         # Counter at top-level
    "GSI2PK": "RESOURCE#gpt-4",
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
    "PK": "RESOURCE#gpt-4",           # or SYSTEM# or ENTITY#{id}
    "SK": "#CONFIG",                   # or #CONFIG#{resource} for entity level
    "resource": "gpt-4",
    "l_tpm_cp": 100000,               # capacity for tpm limit
    "l_tpm_bx": 100000,               # burst for tpm limit
    "l_tpm_ra": 100000,               # refill_amount for tpm limit
    "l_tpm_rp": 60,                   # refill_period_seconds for tpm limit
    "config_version": 1               # Atomic counter for cache invalidation
}
```

Config records use four-level precedence: **Entity (resource-specific) > Entity (_default_) > Resource > System > Constructor defaults**.

**Key builders:**

- `pk_system()` - Returns `SYSTEM#`
- `pk_resource(resource)` - Returns `RESOURCE#{resource}`
- `pk_entity(entity_id)` - Returns `ENTITY#{entity_id}`
- `sk_config()` - Returns `#CONFIG` (system/resource level)
- `sk_config(resource)` - Returns `#CONFIG#{resource}` (entity level)

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

### Burst Capacity

Burst allows temporary exceeding of sustained rate:

```python
# Sustained: 10k tokens/minute
# Burst: 15k tokens (one-time)
Limit.per_minute("tpm", 10_000, burst=15_000)
```

When `burst > capacity`, users can consume up to `burst` tokens immediately, then sustain at `capacity` rate.

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
| Speculative compensation | `_compensate_child()` via `write_each()` | `UpdateItem` | 1 WCU | Single item |
| Parent-only slow path | `_try_parent_only_acquire()` via `_commit_initial()` | `UpdateItem` | 1 WCU | Single item |

The speculative path uses `ReturnValuesOnConditionCheckFailure=ALL_OLD` to inspect bucket state
on failure without a separate read. On success, `ReturnValues=ALL_NEW` provides the post-write
state including denormalized `cascade` and `parent_id` fields.

```
Speculative flow:
1. UpdateItem with condition: attribute_exists(PK) AND tk >= consumed
   +- SUCCESS -> Lease is pre-committed (_initial_committed=True)
   |  +- cascade=False -> DONE
   |  +- cascade=True -> Speculative UpdateItem on parent
   |     +- SUCCESS -> DONE (child + parent both speculative)
   |     +- FAIL -> Check parent ALL_OLD (child stays consumed)
   |        +- No item / missing limit -> Compensate child, fall back
   |        +- Refill won't help -> Compensate child, RateLimitExceeded
   |        +- Refill would help -> Parent-only slow path (read + write parent)
   |           +- SUCCESS -> DONE (child speculative + parent slow path)
   |           +- FAIL -> Compensate child, fall back to full slow path
   +- FAIL -> Check ALL_OLD
      +- No item (bucket missing) -> Fall back to normal path
      +- Refill would help -> Fall back to normal path
      +- Refill won't help -> RateLimitExceeded (fast rejection)
```

Cascade and `parent_id` are denormalized into composite bucket items (via `build_composite_create`)
so the speculative path avoids a separate entity metadata lookup.

**Deferred cascade compensation:** When the child speculative write succeeds but the parent
fails, child compensation is deferred. If refill would help the parent, a parent-only slow
path is attempted: read parent buckets (0.5 RCU), refill + try_consume, write via single-item
UpdateItem (1 WCU). This avoids the cost of compensating the child (1 WCU), re-reading it
(0.5 RCU), and using TransactWriteItems for the full cascade write (4 WCU). The child is
only compensated when the parent-only path also fails.

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
├── schema.py              # DynamoDB key builders
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
├── handler.py                # Lambda entry point
├── processor.py              # Stream processing logic for usage snapshots
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

## Next Steps

- [Development Setup](development.md) - Setting up your environment
- [Testing](testing.md) - Test organization and fixtures
