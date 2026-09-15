# Exceptions

Exception types raised by zae-limiter.

## Exception Hierarchy

```
ZAELimiterError (base)
├── RateLimitError
│   ├── RateLimitExceeded
│   └── LeaseExpiredError
├── EntityError
│   ├── EntityNotFoundError
│   └── EntityExistsError
├── InfrastructureError
│   ├── RateLimiterUnavailable
│   ├── StackOperationError
│   ├── StackAlreadyExistsError
│   ├── InfrastructureNotFoundError
│   ├── NamespaceNotFoundError
│   └── NamespaceStateError
├── VersionError
│   ├── VersionMismatchError
│   └── IncompatibleSchemaError
├── ValidationError
│   ├── InvalidIdentifierError
│   └── InvalidNameError
├── ResourceDisabled
└── FanoutIncomplete
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

::: zae_limiter.exceptions.LeaseExpiredError
    options:
      show_root_heading: true
      show_source: false
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

::: zae_limiter.exceptions.RateLimiterUnavailable
    options:
      show_root_heading: true
      show_source: false
      members_order: source
      heading_level: 3

::: zae_limiter.exceptions.StackOperationError
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

::: zae_limiter.exceptions.NamespaceStateError
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

## Validation Exceptions

::: zae_limiter.exceptions.ValidationError
    options:
      show_root_heading: true
      show_source: false
      heading_level: 3

::: zae_limiter.exceptions.InvalidIdentifierError
    options:
      show_root_heading: true
      show_source: false
      heading_level: 3

::: zae_limiter.exceptions.InvalidNameError
    options:
      show_root_heading: true
      show_source: false
      heading_level: 3

## Configuration State Exceptions

::: zae_limiter.exceptions.ResourceDisabled
    options:
      show_root_heading: true
      show_source: false
      heading_level: 3

::: zae_limiter.exceptions.FanoutIncomplete
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
            "kind": "rate",
            "capacity": 100,
            "refill_amount": 100,
            "refill_period_seconds": 60,
            "available": -5,
            "requested": 10,
            "exceeded": True,
            "retry_after_seconds": 45.2,
        },
        {
            "entity_id": "user-123",
            "resource": "api",
            "limit_name": "rpd",
            "kind": "quota",
            "capacity": 10000,
            "resets_at_ms": 1789531200000,
            "available": 0,
            "requested": 500,
            "exceeded": True,
            "retry_after_seconds": 29000.0,
        },
    ],
}
```

!!! note "Single `limits` array"
    Every limit declared in `acquire(consume=...)` — both exceeded and passed — is
    returned in a single `limits` array. Limits you did not name in `consume` are
    never reported, since they never gate admission. Use the `exceeded` field to
    distinguish between violations and passed limits.

#### `kind`: how the limit recovers

Each entry carries a `kind`, and the fields describing recovery differ by kind.
Read `kind` directly — do **not** infer a quota from `refill_amount == 0`.

| `kind` | Recovery fields | Meaning |
|--------|-----------------|---------|
| `"rate"` | `refill_amount`, `refill_period_seconds` | Drips back continuously at `refill_amount` per `refill_period_seconds`. |
| `"quota"` | `resets_at_ms` | Does not drip at all ([ADR-137](https://github.com/zeroae/zae-limiter/blob/main/docs/adr/137-reset-replaces-drip.md)). The whole allowance returns at a calendar instant. |

`capacity`, `available`, `requested`, `exceeded` and `retry_after_seconds` are
present on both kinds.

`resets_at_ms` is an **absolute** epoch-millisecond instant, so a client can
schedule a retry without parsing cron and without a reference clock of its own.
The key is always present on a quota entry, and carries a real instant for every
practical quota period — session, daily, weekly, monthly, quarterly, annual.

It is `null` only when the next reset is further out than the reset pattern's own
cycle — in practice only a pattern that skips whole years, such as `0 0 29 2 *`
firing on a leap day. Treat `null` as "no scheduled reset in reach", not as
"never resets", and fall back to `retry_after_seconds`.

!!! warning "A quota never reports `refill_amount`"
    A quota's stored `refill_amount` is fixed at 0 and its `refill_period_seconds`
    is an inert placeholder. Both are omitted rather than serialized, because a
    client dividing one by the other would compute "0 tokens per second, never
    recovers" for a limit that in fact returns whole at `resets_at_ms`.
