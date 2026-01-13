# Failure Modes

zae-limiter provides configurable behavior when DynamoDB is unavailable. This guide covers the failure modes and how to choose the right one for your application.

## Available Failure Modes

| Mode | Behavior | Use Case |
|------|----------|----------|
| `FAIL_CLOSED` | Reject requests | Security-critical, billing |
| `FAIL_OPEN` | Allow requests | User experience priority |

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

## Monitoring and Alerting

Monitor for rate limiter unavailability:

```python
import logging
from zae_limiter import RateLimiterUnavailable

logger = logging.getLogger(__name__)

async def monitored_acquire(limiter, **kwargs):
    try:
        async with limiter.acquire(**kwargs) as lease:
            yield lease
    except RateLimiterUnavailable as e:
        # Emit metric for monitoring
        metrics.increment("rate_limiter.unavailable")
        logger.warning(f"Rate limiter unavailable: {e}")
        raise
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

### 2. Use Circuit Breakers

Combine with circuit breakers to prevent cascading failures:

```python
from circuitbreaker import circuit

@circuit(failure_threshold=5, recovery_timeout=30)
async def rate_limited_operation(entity_id: str):
    async with limiter.acquire(
        entity_id=entity_id,
        failure_mode=FailureMode.FAIL_CLOSED,
        ...
    ):
        return await do_work()
```

### 3. Graceful Degradation

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

### 4. Health Checks

Include rate limiter health in your health checks:

```python
async def health_check():
    checks = {}

    # Check rate limiter
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

## DynamoDB Resilience

DynamoDB itself is highly available, but consider:

- **Region outages**: Use multi-region tables for critical systems
- **Throttling**: Configure appropriate capacity
- **Network issues**: Set appropriate timeouts

```python
limiter = RateLimiter(
    name="limiter",  # Connects to ZAEL-limiter
    region="us-east-1",
)
```

## Next Steps

- [Deployment](../infra/deployment.md) - Infrastructure setup
- [API Reference](../api/limiter.md) - Complete API documentation
