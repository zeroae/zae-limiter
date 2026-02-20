# API Reference

This section contains the complete API documentation for zae-limiter, auto-generated from source code docstrings.

## Overview

The main components of the API are:

| Component | Description |
|-----------|-------------|
| [`RateLimiter`](limiter.md#zae_limiter.limiter.RateLimiter) | Async rate limiter client |
| [`SyncRateLimiter`](limiter.md#zae_limiter.sync_limiter.SyncRateLimiter) | Synchronous rate limiter client |
| [`Repository`](repository.md) | DynamoDB data access and infrastructure management |
| [`RepositoryProtocol`](repository.md#repositoryprotocol) | Protocol for pluggable backends |
| [`Limit`](models.md#zae_limiter.models.Limit) | Rate limit configuration |
| [`StackOptions`](models.md#zae_limiter.models.StackOptions) | Infrastructure deployment configuration |
| [`CacheStats`](models.md#zae_limiter.config_cache.CacheStats) | Cache performance statistics |
| [`ConfigSource`](models.md#zae_limiter.config_cache.ConfigSource) | Config resolution source identifier |
| [`RateLimitExceeded`](exceptions.md#zae_limiter.exceptions.RateLimitExceeded) | Exception when limit is exceeded |

## Quick Reference

### Creating a Limiter

```python
from zae_limiter import RateLimiter, SyncRateLimiter, Repository, SyncRepository

# Async — connect to existing infrastructure (recommended)
repo = await Repository.connect("my-app", "us-east-1")
limiter = RateLimiter(repository=repo)

# Sync
repo = SyncRepository.connect("my-app", "us-east-1")
limiter = SyncRateLimiter(repository=repo)
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
Limit.custom("requests", capacity=50, refill_amount=50, refill_period_seconds=30)
```

### Acquiring Limits

```python
from zae_limiter import RateLimiter, Limit, RateLimitExceeded

repo = await Repository.connect("limiter", "us-east-1")
limiter = RateLimiter(repository=repo)

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
    async with limiter.acquire(
        entity_id="user-123",
        resource="gpt-4",
        limits=[Limit.per_minute("rpm", 100)],
        consume={"rpm": 1},
    ):
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
├── __init__.py            # Public API exports
├── limiter.py             # RateLimiter (async)
├── sync_limiter.py        # Generated: SyncRateLimiter
├── models.py              # Limit, Entity, LimitStatus, BucketState, StackOptions, ...
├── exceptions.py          # RateLimitExceeded, RateLimiterUnavailable, ...
├── repository.py          # Repository (async DynamoDB operations)
├── sync_repository.py     # Generated: SyncRepository
├── repository_protocol.py # RepositoryProtocol (backend abstraction)
├── sync_repository_protocol.py  # Generated: SyncRepositoryProtocol
├── lease.py               # Lease (async context manager)
├── sync_lease.py          # Generated: SyncLease
├── config_cache.py        # Client-side config caching with TTL (async)
├── sync_config_cache.py   # Generated: SyncConfigCache
├── bucket.py              # Token bucket algorithm
├── schema.py              # DynamoDB key builders
├── naming.py              # Resource name validation
├── locust.py              # Locust load testing integration (RateLimiterUser, RateLimiterSession)
├── local.py               # LocalStack management commands
├── cli.py                 # CLI commands
└── infra/
    ├── stack_manager.py   # StackManager (async CloudFormation operations)
    ├── sync_stack_manager.py    # Generated: SyncStackManager
    ├── discovery.py       # Multi-stack discovery and listing (async)
    ├── sync_discovery.py  # Generated: SyncInfrastructureDiscovery
    ├── lambda_builder.py  # Lambda deployment package builder
    └── cfn_template.yaml  # CloudFormation template
```

## Public Exports

The following are exported from `zae_limiter`:

```python
from zae_limiter import (
    # Version
    __version__,

    # Main classes
    RateLimiter,
    SyncRateLimiter,
    Repository,
    SyncRepository,
    RepositoryProtocol,
    SyncRepositoryProtocol,
    Lease,
    SyncLease,
    StackManager,
    SyncStackManager,
    SyncConfigCache,

    # Models
    Limit,
    LimiterInfo,
    LimitName,
    Entity,
    LimitStatus,
    BucketState,
    UsageSnapshot,
    UsageSummary,
    ResourceCapacity,
    EntityCapacity,
    StackOptions,
    BackendCapabilities,
    Status,
    CacheStats,
    ConfigSource,

    # Audit
    AuditEvent,
    AuditAction,

    # Enums
    OnUnavailable,

    # Exceptions - Base
    ZAELimiterError,

    # Exceptions - Categories
    RateLimitError,
    InfrastructureError,
    EntityError,
    VersionError,

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
    NamespaceNotFoundError,

    # Exceptions - Version
    VersionMismatchError,
    IncompatibleSchemaError,

    # Exceptions - Validation
    ValidationError,
    InvalidIdentifierError,
    InvalidNameError,
)
```

## Detailed Documentation

- [RateLimiter](limiter.md) - Main rate limiter classes
- [Repository](repository.md) - Data access and infrastructure management
- [Models](models.md) - Data models and configuration
- [Exceptions](exceptions.md) - Exception types and handling
