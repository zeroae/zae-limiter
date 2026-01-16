# Architecture

This guide covers the internal architecture of zae-limiter, including the DynamoDB schema and token bucket implementation.

## DynamoDB Schema (Single Table)

All data is stored in a single DynamoDB table using a composite key pattern:

| Record Type | PK | SK |
|-------------|----|----|
| Entity metadata | `ENTITY#{id}` | `#META` |
| Bucket | `ENTITY#{id}` | `#BUCKET#{resource}#{limit_name}` |
| Limit config | `ENTITY#{id}` | `#LIMIT#{resource}#{limit_name}` |
| Usage snapshot | `ENTITY#{id}` | `#USAGE#{resource}#{window_key}` |
| System version | `SYSTEM#` | `#VERSION` |
| Audit events | `AUDIT#{entity_id}` | `#AUDIT#{timestamp}` |

### Global Secondary Indexes

| Index | Purpose | Key Pattern |
|-------|---------|-------------|
| **GSI1** | Parent → Children lookup | `GSI1PK=PARENT#{id}` → `GSI1SK=CHILD#{id}` |
| **GSI2** | Resource aggregation | `GSI2PK=RESOURCE#{name}` → buckets/usage |

### Access Patterns

| Pattern | Query |
|---------|-------|
| Get entity | `PK=ENTITY#{id}, SK=#META` |
| Get buckets | `PK=ENTITY#{id}, SK begins_with #BUCKET#` |
| Get children | GSI1: `GSI1PK=PARENT#{id}` |
| Resource capacity | GSI2: `GSI2PK=RESOURCE#{name}, SK begins_with BUCKET#` |
| Get version | `PK=SYSTEM#, SK=#VERSION` |
| Get audit events | `PK=AUDIT#{entity_id}, SK begins_with #AUDIT#` |
| Get usage snapshots | `PK=ENTITY#{id}, SK begins_with #USAGE#` |

### Item Structure

Most record types use a nested `data` map for business attributes:

```python
# Entity, Bucket, Audit records use nested data.M:
{
    "PK": "ENTITY#user-1",
    "SK": "#META",
    "entity_id": "user-1",
    "data": {                    # Nested map
        "name": "User One",
        "parent_id": null,
        "metadata": {...}
    }
}
```

**Exception: Usage snapshots use a FLAT structure** (no nested `data` map):

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

## Token Bucket Implementation

For a conceptual overview of the token bucket algorithm, see the [User Guide](../guide/token-bucket.md). This section covers implementation details for contributors.

### Core Functions

The algorithm is implemented in [`bucket.py`](https://github.com/zeroae/zae-limiter/blob/main/src/zae_limiter/bucket.py):

| Function | Purpose | Lines |
|----------|---------|-------|
| `refill_bucket()` | Calculate refilled tokens with drift compensation | 27-75 |
| `try_consume()` | Atomic check-and-consume operation | 78-134 |
| `force_consume()` | Force consume (can go negative) | 224-255 |
| `calculate_retry_after()` | Calculate wait time for deficit | 137-159 |

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

```python
# From bucket.py:refill_bucket()
tokens_to_add = (elapsed_ms * refill_amount_milli) // refill_period_ms

# Drift compensation: only advance time for tokens actually added
time_used_ms = (tokens_to_add * refill_period_ms) // refill_amount_milli
new_last_refill = last_refill_ms + time_used_ms
```

Without drift compensation, repeated calls with small time intervals would accumulate rounding errors.

### Negative Buckets (Debt)

Buckets can go negative to support post-hoc reconciliation:

```python
# Estimate 500 tokens, actually used 2000
async with limiter.acquire(consume={"tpm": 500}) as lease:
    actual = await call_llm()  # Returns 2000 tokens
    await lease.adjust(tpm=2000 - 500)  # Bucket at -1500
```

The `force_consume()` function handles this:

```python
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

## Atomicity

### TransactWriteItems

Multi-entity updates (like cascade mode) use DynamoDB transactions:

```python
# Single atomic operation:
# 1. Consume from child entity
# 2. Consume from parent entity
# Both succeed or both fail
```

Transaction limits: max 100 items per transaction.

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
├── __init__.py        # Public API exports
├── models.py          # Limit, Entity, LimitStatus, BucketState, StackOptions
├── exceptions.py      # RateLimitExceeded, RateLimiterUnavailable, etc.
├── naming.py          # Resource name validation and ZAEL- prefix logic
├── bucket.py          # Token bucket math (integer arithmetic)
├── schema.py          # DynamoDB key builders
├── repository.py      # DynamoDB operations
├── lease.py           # Lease context manager
├── limiter.py         # RateLimiter, SyncRateLimiter
├── cli.py             # CLI commands
├── version.py         # Version tracking and compatibility
├── migrations/        # Schema migration framework
└── infra/
    ├── stack_manager.py    # CloudFormation stack operations
    ├── lambda_builder.py   # Lambda deployment package builder
    └── cfn_template.yaml   # CloudFormation template
```

## Key Design Decisions

1. **Lease commits only on success**: If any exception occurs in the context, changes are rolled back
2. **Bucket can go negative**: `lease.adjust()` never throws, allows debt
3. **Cascade is optional**: Parent is only checked if `cascade=True`
4. **Stored limits override defaults**: When `use_stored_limits=True`
5. **Transactions are atomic**: Multi-entity updates succeed or fail together

## Next Steps

- [Development Setup](development.md) - Setting up your environment
- [Testing](testing.md) - Test organization and fixtures
