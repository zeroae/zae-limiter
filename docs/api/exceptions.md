# Exceptions

Exception types raised by zae-limiter.

## RateLimitExceeded

::: zae_limiter.exceptions.RateLimitExceeded
    options:
      show_root_heading: true
      show_source: false
      members_order: source
      heading_level: 3

## RateLimiterUnavailable

::: zae_limiter.exceptions.RateLimiterUnavailable
    options:
      show_root_heading: true
      show_source: false
      members_order: source
      heading_level: 3

## StackCreationError

::: zae_limiter.exceptions.StackCreationError
    options:
      show_root_heading: true
      show_source: false
      members_order: source
      heading_level: 3

## VersionError

::: zae_limiter.exceptions.VersionError
    options:
      show_root_heading: true
      show_source: false
      members_order: source
      heading_level: 3

## Exception Handling Examples

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

```python
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
    async with limiter.acquire(...):
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
    "message": "Rate limit exceeded for rpm",
    "retry_after_seconds": 45.2,
    "violations": [
        {
            "entity_id": "user-123",
            "limit_name": "rpm",
            "limit_capacity": 100,
            "available": -5,
            "requested": 1,
            "retry_after_seconds": 45.2,
        }
    ],
    "passed": [
        {
            "entity_id": "user-123",
            "limit_name": "tpm",
            "limit_capacity": 10000,
            "available": 8500,
            "requested": 500,
        }
    ],
}
```
