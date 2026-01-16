# API Reference

This section contains the complete API documentation for zae-limiter, auto-generated from source code docstrings.

## Overview

The main components of the API are:

| Component | Description |
|-----------|-------------|
| [`RateLimiter`](limiter.md#zae_limiter.limiter.RateLimiter) | Async rate limiter client |
| [`SyncRateLimiter`](limiter.md#zae_limiter.limiter.SyncRateLimiter) | Synchronous rate limiter client |
| [`Limit`](models.md#zae_limiter.models.Limit) | Rate limit configuration |
| [`RateLimitExceeded`](exceptions.md#zae_limiter.exceptions.RateLimitExceeded) | Exception when limit is exceeded |

## Quick Reference

### Creating a Limiter

```python
from zae_limiter import RateLimiter, SyncRateLimiter

# Async
limiter = RateLimiter(
    name="limiter",  # Creates ZAEL-limiter resources
    region="us-east-1",
)

# Sync
limiter = SyncRateLimiter(
    name="limiter",  # Creates ZAEL-limiter resources
    region="us-east-1",
)
```

### Defining Limits

```python
from zae_limiter import Limit

# Factory methods
Limit.per_second("rps", 10)
Limit.per_minute("rpm", 100)
Limit.per_hour("rph", 1000)
Limit.per_day("rpd", 10000)

# With burst capacity
Limit.per_minute("tpm", 10_000, burst=15_000)

# Custom period
Limit.custom("requests", capacity=50, refill_period_seconds=30)
```

### Acquiring Limits

```python
from zae_limiter import RateLimiter, Limit, RateLimitExceeded

limiter = RateLimiter(name="limiter")

try:
    async with limiter.acquire(
        entity_id="user-123",
        resource="api",
        limits=[Limit.per_minute("rpm", 100)],
        consume={"rpm": 1},
    ) as lease:
        # Do work
        await lease.adjust(rpm=5)  # Adjust if needed
except RateLimitExceeded as e:
    print(f"Retry after: {e.retry_after_seconds}s")
```

### Handling Exceptions

```python
from zae_limiter import RateLimitExceeded, RateLimiterUnavailable

try:
    async with limiter.acquire(...):
        pass
except RateLimitExceeded as e:
    # Rate limit exceeded
    print(e.retry_after_seconds)
    print(e.violations)
    print(e.as_dict())
except RateLimiterUnavailable as e:
    # DynamoDB unavailable
    print(f"Service unavailable: {e}")
```

## Module Structure

```
zae_limiter/
├── __init__.py        # Public API exports
├── limiter.py         # RateLimiter, SyncRateLimiter
├── models.py          # Limit, Entity, LimitStatus, BucketState
├── exceptions.py      # RateLimitExceeded, RateLimiterUnavailable
├── lease.py           # Lease context manager
├── bucket.py          # Token bucket algorithm
├── schema.py          # DynamoDB key builders
├── repository.py      # DynamoDB operations
└── cli.py             # CLI commands
```

## Public Exports

The following are exported from `zae_limiter`:

```python
from zae_limiter import (
    # Main classes
    RateLimiter,
    SyncRateLimiter,
    Lease,
    SyncLease,

    # Models
    Limit,
    Entity,
    LimitStatus,
    BucketState,

    # Audit
    AuditEvent,
    AuditAction,

    # Enums
    OnUnavailable,

    # Exceptions - Base
    ZAELimiterError,

    # Exceptions - Rate Limit
    RateLimitExceeded,

    # Exceptions - Entity
    EntityNotFoundError,
    EntityExistsError,

    # Exceptions - Infrastructure
    RateLimiterUnavailable,
    StackCreationError,
    StackAlreadyExistsError,
    InfrastructureNotFoundError,

    # Exceptions - Version
    VersionMismatchError,
    IncompatibleSchemaError,
)
```

## Detailed Documentation

- [RateLimiter](limiter.md) - Main rate limiter classes
- [Models](models.md) - Data models and configuration
- [Exceptions](exceptions.md) - Exception types and handling
