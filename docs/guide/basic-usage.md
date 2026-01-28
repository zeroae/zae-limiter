# Basic Usage

This guide covers common rate limiting patterns with zae-limiter.

## The Acquire Context Manager

The `acquire()` method is the primary API for rate limiting:

```python
async with limiter.acquire(
    entity_id="user-123",      # Who is being rate limited
    resource="gpt-4",          # What resource they're accessing
    consume={"rpm": 1},        # How much to consume
) as lease:
    # Your code here - limits resolved from stored config
    pass
```

**Behavior:**

- On entry: Checks limits and consumes tokens
- On success: Commits the consumption
- On exception: Rolls back the consumption

Limits are resolved automatically from stored config (Entity > Resource > System). See [Configuration Hierarchy](config-hierarchy.md) for details.

## Multiple Limits

Track multiple limits in a single call:

```python
async with limiter.acquire(
    entity_id="api-key-123",
    resource="gpt-4",
    consume={"rpm": 1, "tpm": 500},
) as lease:
    response = await call_llm()
```

All limits are checked atomically. If any limit is exceeded, the request is rejected.

When using stored config, configure multiple limits at setup time:

=== "CLI"

    ```bash
    zae-limiter resource set-defaults gpt-4 \
        -l rpm:100 \
        -l tpm:10000
    ```

=== "Python"

    ```python
    await limiter.set_resource_defaults(
        resource="gpt-4",
        limits=[
            Limit.per_minute("rpm", 100),       # 100 requests/minute
            Limit.per_minute("tpm", 10_000),    # 10,000 tokens/minute
        ],
    )
    ```

!!! tip "Performance Tip"
    Combining multiple limits into a single `acquire()` call is more efficient than separate calls. See [Batch Operation Patterns](../performance.md#3-batch-operation-patterns) for details.

## Burst Capacity

Allow temporary bursts above the sustained rate:

```python
# Sustain 10k tokens/minute, but allow bursts up to 15k
limits = [
    Limit.per_minute("tpm", 10_000, burst=15_000),
]
```

The bucket starts full at `burst` capacity and refills at `capacity` tokens per period. See [Token Bucket Algorithm](token-bucket.md#capacity-and-burst) for details on how burst and capacity interact.

## Adjusting Consumption

Use `lease.adjust()` to modify consumption after the fact:

```python
async with limiter.acquire(
    entity_id="key-123",
    resource="gpt-4",
    consume={"tpm": 500},  # Initial estimate
) as lease:
    response = await call_llm()

    # Adjust based on actual usage
    actual_tokens = response.usage.total_tokens
    await lease.adjust(tpm=actual_tokens - 500)
```

!!! note "Negative Adjustments"
    `adjust()` can go negative, allowing the bucket to go into debt.
    This is useful for post-hoc reconciliation when actual usage exceeds estimates.
    See [Token Bucket Algorithm - Negative Buckets](token-bucket.md#negative-buckets-debt) for how debt works.

## Check Capacity Without Consuming

### Check Available Tokens

```python
available = await limiter.available(
    entity_id="key-123",
    resource="gpt-4",
)
print(f"Available tokens: {available['tpm']}")
```

### Check Time Until Available

```python
wait_seconds = await limiter.time_until_available(
    entity_id="key-123",
    resource="gpt-4",
    needed={"tpm": 5_000},
)

if wait_seconds > 0:
    print(f"Need to wait {wait_seconds}s for capacity")
```

## Automatic Limit Resolution

zae-limiter automatically resolves limits from stored configurations using a three-level hierarchy. See [Configuration Hierarchy](config-hierarchy.md) for full details.

**Resolution order (highest to lowest precedence):**

1. **Entity level** - Specific limits for an entity+resource pair
2. **Resource level** - Default limits for a resource (all entities)
3. **System level** - Global defaults (all resources)
4. **Override parameter** - Fallback if no stored config exists

```python
# Set system-wide defaults (lowest precedence)
await limiter.set_system_defaults(
    limits=[Limit.per_minute("rpm", 100)],
)

# Set resource defaults (overrides system for this resource)
await limiter.set_resource_defaults(
    resource="gpt-4",
    limits=[Limit.per_minute("rpm", 50)],
)

# Set entity-specific limits (highest precedence)
await limiter.set_limits(
    entity_id="user-premium",
    resource="gpt-4",
    limits=[
        Limit.per_minute("rpm", 500),        # 5x normal
        Limit.per_minute("tpm", 50_000),     # 5x normal
    ],
)

# Limits are resolved automatically - no special flag needed
async with limiter.acquire(
    entity_id="user-premium",
    resource="gpt-4",
    consume={"rpm": 1},  # Auto-resolves to entity-level (500 rpm)
) as lease:
    ...

# Free user falls back to resource defaults (50 rpm)
async with limiter.acquire(
    entity_id="user-free",
    resource="gpt-4",
    consume={"rpm": 1},  # Auto-resolves to resource-level
) as lease:
    ...

# Override stored config for a specific call
async with limiter.acquire(
    entity_id="user-premium",
    resource="gpt-4",
    consume={"rpm": 1},
    limits=[Limit.per_minute("rpm", 10)],  # Explicit override
) as lease:
    ...
```

!!! note "v0.5.0 Breaking Change"
    Prior to v0.5.0, you needed `use_stored_limits=True` to enable limit lookup.
    This parameter is now deprecated - limits are always resolved automatically.

## Entity Management

### Create Entities

```python
# Create a standalone entity
await limiter.create_entity(
    entity_id="user-123",
    name="John Doe",
)

# Create a child entity (API key under a project)
await limiter.create_entity(
    entity_id="key-abc",
    parent_id="project-1",
    name="Production API Key",
)
```

### Get Entity Information

```python
entity = await limiter.get_entity("user-123")
print(f"Name: {entity.name}")
print(f"Parent: {entity.parent_id}")
```

## Error Handling

### RateLimitExceeded Details

```python
try:
    async with limiter.acquire(
        entity_id="user-123",
        resource="gpt-4",
        consume={"rpm": 2},  # Exceeds capacity to trigger error
        limits=[Limit.per_minute("rpm", 1)],
    ):
        pass
except RateLimitExceeded as e:
    # All limit statuses
    for status in e.statuses:
        print(f"{status.limit_name}: {status.available}/{status.limit.capacity}")

    # Only violations
    for v in e.violations:
        print(f"Exceeded: {v.limit_name}")

    # Primary bottleneck
    print(f"Bottleneck: {e.primary_violation.limit_name}")

    # For API responses
    print(e.as_dict())
```

### Service Unavailable

```python
from zae_limiter import RateLimiterUnavailable

try:
    async with limiter.acquire(
        entity_id="user-123",
        resource="gpt-4",
        consume={"rpm": 1},
    ):
        pass
except RateLimiterUnavailable as e:
    # DynamoDB is unavailable
    # Behavior depends on on_unavailable setting
    print(f"Service unavailable: {e}")
```

## Config Cache

zae-limiter caches config data (system defaults, resource defaults, entity limits) to reduce DynamoDB reads. The cache has a 60-second TTL by default.

### Configuring Cache TTL

```python
from zae_limiter import RateLimiter, Repository

# Default: 60-second cache TTL
limiter = RateLimiter(
    repository=Repository(name="my-app", region="us-east-1"),
    config_cache_ttl=60,
)

# Disable caching (for testing)
limiter = RateLimiter(
    repository=Repository(name="my-app", region="us-east-1"),
    config_cache_ttl=0,
)
```

### Manual Cache Invalidation

After modifying config, force immediate refresh:

```python
await limiter.set_system_defaults([Limit.per_minute("rpm", 1000)])
await limiter.invalidate_config_cache()  # Optional: force refresh
```

### Monitoring Cache Performance

```python
stats = limiter.get_cache_stats()
print(f"Hits: {stats.hits}, Misses: {stats.misses}")
print(f"Cache entries: {stats.size}")
```

See [Config Cache Tuning](../performance.md#7-config-cache-tuning) for advanced configuration.

## Next Steps

- [Hierarchical Limits](hierarchical.md) - Parent/child rate limiting
- [LLM Integration](llm-integration.md) - Token estimation patterns
- [Unavailability Handling](unavailability.md) - Handling service outages
