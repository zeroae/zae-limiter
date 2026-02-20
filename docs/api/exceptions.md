# Exceptions

Exception types raised by zae-limiter.

## Exception Hierarchy

```
ZAELimiterError (base)
├── RateLimitError
│   ├── RateLimitExceeded
│   └── RateLimiterUnavailable
├── EntityError
│   ├── EntityNotFoundError
│   └── EntityExistsError
├── InfrastructureError
│   ├── StackCreationError
│   ├── StackAlreadyExistsError
│   ├── InfrastructureNotFoundError
│   └── NamespaceNotFoundError
└── VersionError
    ├── VersionMismatchError
    └── IncompatibleSchemaError
```

## Base Exception

::: zae_limiter.exceptions.ZAELimiterError
    options:
      show_root_heading: true
      show_source: false
      heading_level: 3

## Rate Limit Exceptions

::: zae_limiter.exceptions.RateLimitExceeded
    options:
      show_root_heading: true
      show_source: false
      members_order: source
      heading_level: 3

::: zae_limiter.exceptions.RateLimiterUnavailable
    options:
      show_root_heading: true
      show_source: false
      members_order: source
      heading_level: 3

## Entity Exceptions

::: zae_limiter.exceptions.EntityNotFoundError
    options:
      show_root_heading: true
      show_source: false
      heading_level: 3

::: zae_limiter.exceptions.EntityExistsError
    options:
      show_root_heading: true
      show_source: false
      heading_level: 3

## Infrastructure Exceptions

::: zae_limiter.exceptions.StackCreationError
    options:
      show_root_heading: true
      show_source: false
      members_order: source
      heading_level: 3

::: zae_limiter.exceptions.StackAlreadyExistsError
    options:
      show_root_heading: true
      show_source: false
      heading_level: 3

::: zae_limiter.exceptions.InfrastructureNotFoundError
    options:
      show_root_heading: true
      show_source: false
      heading_level: 3

::: zae_limiter.exceptions.NamespaceNotFoundError
    options:
      show_root_heading: true
      show_source: false
      heading_level: 3

## Version Exceptions

::: zae_limiter.exceptions.VersionMismatchError
    options:
      show_root_heading: true
      show_source: false
      heading_level: 3

::: zae_limiter.exceptions.IncompatibleSchemaError
    options:
      show_root_heading: true
      show_source: false
      heading_level: 3

## Exception Handling Examples

### Connection Errors

```python
from zae_limiter import Repository, NamespaceNotFoundError

try:
    repo = await Repository.open("tenant-alpha")
except NamespaceNotFoundError as e:
    # Namespace not registered — register it first or check for typos
    print(f"Namespace not found: {e.namespace_name}")
```

### Basic Handling

```python
from zae_limiter import RateLimitExceeded, RateLimiterUnavailable

try:
    async with limiter.acquire(
        entity_id="user-123",
        resource="api",
        limits=[Limit.per_minute("rpm", 100)],
        consume={"rpm": 1},
    ):
        await do_work()
except RateLimitExceeded as e:
    # Handle rate limit exceeded
    print(f"Rate limited. Retry after {e.retry_after_seconds}s")
except RateLimiterUnavailable as e:
    # Handle service unavailable
    print(f"Service unavailable: {e}")
```

### HTTP API Response

```{.python .requires-external}
from fastapi import HTTPException
from fastapi.responses import JSONResponse

@app.post("/api/endpoint")
async def endpoint():
    try:
        async with limiter.acquire(...):
            return await process_request()
    except RateLimitExceeded as e:
        return JSONResponse(
            status_code=429,
            content=e.as_dict(),
            headers={"Retry-After": e.retry_after_header},
        )
    except RateLimiterUnavailable:
        raise HTTPException(status_code=503, detail="Service temporarily unavailable")
```

### Detailed Error Information

```python
try:
    async with limiter.acquire(
        entity_id="user-123",
        resource="gpt-4",
        limits=[Limit.per_minute("rpm", 1)],
        consume={"rpm": 2},  # Exceeds capacity to trigger error
    ):
        pass
except RateLimitExceeded as e:
    # All limit statuses (both passed and failed)
    for status in e.statuses:
        print(f"Limit: {status.limit_name}")
        print(f"  Entity: {status.entity_id}")
        print(f"  Available: {status.available}")
        print(f"  Requested: {status.requested}")
        print(f"  Exceeded: {status.exceeded}")
        print(f"  Retry after: {status.retry_after_seconds}s")

    # Only the violations
    print(f"Violations: {len(e.violations)}")
    for v in e.violations:
        print(f"  - {v.limit_name}: {v.available} available")

    # Only the passed limits
    print(f"Passed: {len(e.passed)}")

    # Primary bottleneck (longest wait time)
    print(f"Bottleneck: {e.primary_violation.limit_name}")
    print(f"Total retry after: {e.retry_after_seconds}s")
```

### as_dict() Output

The `as_dict()` method returns a dictionary suitable for API responses:

```python
{
    "error": "rate_limit_exceeded",
    "message": "Rate limit exceeded for user-123/api: [rpm]. Retry after 45.2s",
    "retry_after_seconds": 45.2,
    "retry_after_ms": 45200,
    "limits": [
        {
            "entity_id": "user-123",
            "resource": "api",
            "limit_name": "rpm",
            "capacity": 100,
            "burst": 100,
            "available": -5,
            "requested": 10,
            "exceeded": True,
            "retry_after_seconds": 45.2,
        },
        {
            "entity_id": "user-123",
            "resource": "api",
            "limit_name": "tpm",
            "capacity": 10000,
            "burst": 10000,
            "available": 8500,
            "requested": 500,
            "exceeded": False,
            "retry_after_seconds": 0.0,
        },
    ],
}
```

!!! note "Single `limits` array"
    All limits (both exceeded and passed) are returned in a single `limits` array.
    Use the `exceeded` field to distinguish between violations and passed limits.
