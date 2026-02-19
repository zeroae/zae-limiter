# Unavailability Handling

zae-limiter provides configurable behavior when DynamoDB is unavailable. This guide covers the `on_unavailable` modes and how to choose the right one for your application.

!!! note "Scope"
    This page covers **infrastructure unavailability** (DynamoDB errors, timeouts, throttling).

    For handling rate limit violations, see [Basic Usage](basic-usage.md#error-handling).

## Available Modes

| Mode | Behavior | Use Case |
|------|----------|----------|
| `BLOCK` | Reject requests | Security-critical, billing |
| `ALLOW` | Allow requests | User experience priority |

## What Triggers on_unavailable Logic

The `on_unavailable` mode **only applies to infrastructure errors**. These exceptions always propagate regardless of mode:

- `RateLimitExceeded` — Rate limit violated (business logic)
- `ValidationError` — Invalid configuration (user error)

Infrastructure errors that trigger on_unavailable:

- Connection timeouts
- DynamoDB throttling
- Network failures
- Service unavailable errors

## BLOCK (Default)

When DynamoDB is unavailable, reject all rate-limited requests by raising `RateLimiterUnavailable`.

!!! warning "Exception Handling Required"
    When using `BLOCK` mode (the default), your application **must** catch `RateLimiterUnavailable` to handle infrastructure failures gracefully. This exception inherits from `InfrastructureError`, not `RateLimitExceeded`.

```python
from zae_limiter import Repository, RateLimiter, Limit, OnUnavailable, RateLimiterUnavailable

# Connect to existing infrastructure (must be deployed first)
repo = await Repository.connect("limiter", "us-east-1")
limiter = RateLimiter(repository=repo)

# Set BLOCK mode via system defaults (persisted in DynamoDB)
await limiter.set_system_defaults(
    limits=[Limit.per_minute("rpm", 1000)],
    on_unavailable=OnUnavailable.BLOCK,
)

try:
    async with limiter.acquire(
        entity_id="user-123",
        resource="gpt-4",
        limits=[Limit.per_minute("rpm", 100)],
        consume={"rpm": 1},
    ):
        await do_work()
except RateLimiterUnavailable as e:
    # DynamoDB is unavailable - handle degraded mode
    print(JSONResponse(
        status_code=503,
        content={"error": "Service temporarily unavailable"},
    ).status_code)
```

**When to use:**

- Billing/metering systems where accuracy is critical
- Security-sensitive operations
- When over-consumption has significant costs
- Compliance requirements

## ALLOW

When DynamoDB is unavailable, allow requests to proceed:

```python
repo = await Repository.connect("limiter", "us-east-1")
limiter = RateLimiter(repository=repo)

# Set ALLOW mode via system defaults (persisted in DynamoDB)
await limiter.set_system_defaults(
    limits=[Limit.per_minute("rpm", 1000)],
    on_unavailable=OnUnavailable.ALLOW,
)

# Requests proceed even if DynamoDB is down
async with limiter.acquire(
    entity_id="user-123",
    resource="gpt-4",
    limits=[Limit.per_minute("rpm", 100)],
    consume={"rpm": 1},
):
    await do_work()  # Runs without rate limiting
```

**When to use:**

- User experience is the priority
- Brief outages are acceptable
- Rate limiting is a soft limit
- Development/staging environments

### No-Op Lease Behavior

When `ALLOW` activates due to infrastructure failure:

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
        # BLOCK caught the error - we're in degraded mode
        # This only runs if you use BLOCK and catch manually
        metrics.increment("rate_limiter.degraded")
        logger.warning(f"Rate limiter degraded: {e}")
        raise
```

## Per-Request Override

Override the default mode for specific requests:

```python
# Default to BLOCK via builder
repo = await (
    Repository.builder("limiter", "us-east-1")
    .on_unavailable("block")
    .build()
)
limiter = RateLimiter(repository=repo)

# But allow this specific request to proceed
async with limiter.acquire(
    entity_id="user-123",
    resource="api",
    limits=[...],
    consume={"requests": 1},
    on_unavailable=OnUnavailable.ALLOW,  # Override for this call
) as lease:
    await do_work()
```

## Handling Unavailable Errors

The `RateLimiterUnavailable` exception includes details about the failure:

```python
from zae_limiter import RateLimiterUnavailable

try:
    async with limiter.acquire(
        entity_id="user-123",
        resource="gpt-4",
        limits=[Limit.per_minute("rpm", 100)],
        consume={"rpm": 1},
    ):
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
# High-risk: billing, security (BLOCK set via system defaults)
billing_repo = await Repository.connect("billing", "us-east-1")
billing_limiter = RateLimiter(repository=billing_repo)
await billing_limiter.set_system_defaults(
    limits=[Limit.per_minute("rpm", 1000)],
    on_unavailable=OnUnavailable.BLOCK,
)

# Lower-risk: general API (ALLOW set via system defaults)
api_repo = await Repository.connect("api", "us-east-1")
api_limiter = RateLimiter(repository=api_repo)
await api_limiter.set_system_defaults(
    limits=[Limit.per_minute("rpm", 1000)],
    on_unavailable=OnUnavailable.ALLOW,
)
```

### 2. Graceful Degradation

Implement fallback behavior:

```python
async def resilient_operation(entity_id: str):
    try:
        async with limiter.acquire(
            entity_id=entity_id,
            resource="api",
            on_unavailable=OnUnavailable.BLOCK,
        ):
            return await premium_operation()
    except RateLimiterUnavailable:
        # Fall back to degraded mode
        logger.warning("Rate limiter unavailable, using fallback")
        return await basic_operation()
```

### 3. Health Checks

Use `is_available()` to check rate limiter connectivity:

```python
async def health_check():
    checks = {}

    # Check rate limiter connectivity
    if await limiter.is_available():
        checks["rate_limiter"] = "healthy"
    else:
        checks["rate_limiter"] = "unhealthy"

    return checks
```

The `is_available()` method:

- Returns `True` if DynamoDB is reachable, `False` otherwise
- Never raises exceptions
- Uses a configurable timeout (default 1 second)
- Works without requiring initialization

```{.python .requires-external}
# FastAPI health endpoint example
@app.get("/health")
async def health():
    return {
        "status": "healthy" if await limiter.is_available() else "degraded",
    }

# Pre-flight check before operations
if not await limiter.is_available():
    logger.warning("Rate limiter unavailable, using fallback")
```

## Observability

For monitoring rate limiter health and setting up alerts, see the [Monitoring Guide](../monitoring.md).

## Next Steps

- [Operations Guide](../operations/index.md) - Troubleshooting and operational procedures
- [Deployment](../infra/deployment.md) - Infrastructure setup
- [API Reference](../api/limiter.md) - Complete API documentation
