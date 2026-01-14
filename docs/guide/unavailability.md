# Unavailability Handling

zae-limiter provides configurable behavior when DynamoDB is unavailable. This guide covers the failure modes and how to choose the right one for your application.

!!! note "Scope"
    This page covers **infrastructure unavailability** (DynamoDB errors, timeouts, throttling).

    For handling rate limit violations, see [Basic Usage](basic-usage.md#error-handling).

## Available Failure Modes

| Mode | Behavior | Use Case |
|------|----------|----------|
| `FAIL_CLOSED` | Reject requests | Security-critical, billing |
| `FAIL_OPEN` | Allow requests | User experience priority |

## What Triggers Failure Mode Logic

The failure mode **only applies to infrastructure errors**. These exceptions always propagate regardless of failure mode:

- `RateLimitExceeded` — Rate limit violated (business logic)
- `ValidationError` — Invalid configuration (user error)

Infrastructure errors that trigger failure mode:

- Connection timeouts
- DynamoDB throttling
- Network failures
- Service unavailable errors

## FAIL_CLOSED (Default)

When DynamoDB is unavailable, reject all rate-limited requests:

```python
from zae_limiter import RateLimiter, FailureMode, RateLimiterUnavailable

limiter = RateLimiter(
    name="limiter",  # Connects to ZAEL-limiter
    failure_mode=FailureMode.FAIL_CLOSED,  # Default
)

try:
    async with limiter.acquire(...):
        await do_work()
except RateLimiterUnavailable as e:
    # DynamoDB is unavailable
    return JSONResponse(
        status_code=503,
        content={"error": "Service temporarily unavailable"},
    )
```

**When to use:**

- Billing/metering systems where accuracy is critical
- Security-sensitive operations
- When over-consumption has significant costs
- Compliance requirements

## FAIL_OPEN

When DynamoDB is unavailable, allow requests to proceed:

```python
limiter = RateLimiter(
    name="limiter",  # Connects to ZAEL-limiter
    failure_mode=FailureMode.FAIL_OPEN,
)

# Requests proceed even if DynamoDB is down
async with limiter.acquire(...):
    await do_work()  # Runs without rate limiting
```

**When to use:**

- User experience is the priority
- Brief outages are acceptable
- Rate limiting is a soft limit
- Development/staging environments

### No-Op Lease Behavior

When `FAIL_OPEN` activates due to infrastructure failure:

- A **no-op lease** is returned with no bucket entries
- `lease.consume()`, `lease.adjust()`, and `lease.release()` silently do nothing
- Your code cannot detect degraded mode from the lease itself

To detect and log degraded operations, wrap with custom error handling:

```python
async def acquire_with_metrics(limiter, **kwargs):
    """Wrapper that tracks degraded operations."""
    try:
        async with limiter.acquire(**kwargs) as lease:
            yield lease
    except Exception as e:
        # FAIL_OPEN caught the error - we're in degraded mode
        # This only runs if you use FAIL_CLOSED and catch manually
        metrics.increment("rate_limiter.degraded")
        logger.warning(f"Rate limiter degraded: {e}")
        raise
```

## Per-Request Override

Override the default failure mode for specific requests:

```python
# Default to FAIL_CLOSED
limiter = RateLimiter(
    name="limiter",  # Connects to ZAEL-limiter
    failure_mode=FailureMode.FAIL_CLOSED,
)

# But allow this specific request to proceed
async with limiter.acquire(
    entity_id="user-123",
    resource="api",
    limits=[...],
    consume={"requests": 1},
    failure_mode=FailureMode.FAIL_OPEN,  # Override for this call
) as lease:
    await do_work()
```

## Handling Unavailable Errors

The `RateLimiterUnavailable` exception includes details about the failure:

```python
from zae_limiter import RateLimiterUnavailable

try:
    async with limiter.acquire(...):
        await do_work()
except RateLimiterUnavailable as e:
    # Log the underlying error
    logger.error(f"Rate limiter unavailable: {e}")

    # Decide how to handle
    if is_critical_operation:
        raise HTTPException(status_code=503)
    else:
        # Proceed without rate limiting
        await do_work()
```

## Best Practices

### 1. Choose Based on Risk

```python
# High-risk: billing, security
billing_limiter = RateLimiter(
    name="billing",  # Connects to ZAEL-billing
    failure_mode=FailureMode.FAIL_CLOSED,
)

# Lower-risk: general API
api_limiter = RateLimiter(
    name="api",  # Connects to ZAEL-api
    failure_mode=FailureMode.FAIL_OPEN,
)
```

### 2. Graceful Degradation

Implement fallback behavior:

```python
async def resilient_operation(entity_id: str):
    try:
        async with limiter.acquire(
            entity_id=entity_id,
            failure_mode=FailureMode.FAIL_CLOSED,
            ...
        ):
            return await premium_operation()
    except RateLimiterUnavailable:
        # Fall back to degraded mode
        logger.warning("Rate limiter unavailable, using fallback")
        return await basic_operation()
```

### 3. Health Checks

Include rate limiter health in your health checks:

```python
async def health_check():
    checks = {}

    # Check rate limiter connectivity
    try:
        await limiter.available(
            entity_id="health-check",
            resource="health",
            limits=[Limit.per_minute("requests", 1)],
        )
        checks["rate_limiter"] = "healthy"
    except Exception as e:
        checks["rate_limiter"] = f"unhealthy: {e}"

    return checks
```

## Observability

For monitoring rate limiter health and setting up alerts, see the [Monitoring Guide](../monitoring.md).

## Next Steps

- [Operations Guide](../operations/index.md) - Troubleshooting and operational procedures
- [Deployment](../infra/deployment.md) - Infrastructure setup
- [API Reference](../api/limiter.md) - Complete API documentation
