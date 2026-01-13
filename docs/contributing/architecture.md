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

## Token Bucket Implementation

### Integer Arithmetic for Precision

All token values are stored as **millitokens** (×1000) to avoid floating-point precision issues in distributed systems:

```python
# User sees: 100 tokens/minute
# Stored as: 100,000 millitokens/minute
```

### Refill Rate Storage

Refill rates are stored as a fraction (amount/period) rather than a decimal:

```python
# 100 tokens per minute stored as:
refill_amount = 100_000  # millitokens
refill_period_seconds = 60
```

### Negative Buckets (Debt)

Buckets can go negative to support post-hoc reconciliation:

```python
# Estimate 500 tokens, actually used 2000
async with limiter.acquire(consume={"tpm": 500}) as lease:
    actual = await call_llm()  # Returns 2000 tokens
    await lease.adjust(tpm=2000 - 500)  # Bucket at -1500
```

The debt is repaid as tokens refill over time.

### Burst Capacity

Burst allows temporary exceeding of sustained rate:

```python
# Sustained: 10k tokens/minute
# Burst: 15k tokens (one-time)
Limit.per_minute("tpm", 10_000, burst=15_000)
```

When `burst > capacity`, users can consume up to `burst` tokens immediately, then sustain at `capacity` rate.

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
