# Basic Usage

This guide covers common rate limiting patterns with zae-limiter.

## The Acquire Context Manager

The `acquire()` method is the primary API for rate limiting:

```python
async with limiter.acquire(
    entity_id="user-123",      # Who is being rate limited
    resource="gpt-4",          # What resource they're accessing
    limits=[...],              # Rate limit definitions
    consume={"rpm": 1},        # How much to consume
) as lease:
    # Your code here
    pass
```

**Behavior:**

- On entry: Checks limits and consumes tokens
- On success: Commits the consumption
- On exception: Rolls back the consumption

## Multiple Limits

Track multiple limits in a single call:

```python
async with limiter.acquire(
    entity_id="api-key-123",
    resource="gpt-4",
    limits=[
        Limit.per_minute("rpm", 100),       # 100 requests/minute
        Limit.per_minute("tpm", 10_000),    # 10,000 tokens/minute
        Limit.per_hour("rph", 1_000),       # 1,000 requests/hour
    ],
    consume={"rpm": 1, "tpm": 500, "rph": 1},
) as lease:
    response = await call_llm()
```

All limits are checked atomically. If any limit is exceeded, the request is rejected.

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
    limits=[Limit.per_minute("tpm", 10_000)],
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
    limits=[Limit.per_minute("tpm", 10_000)],
)
print(f"Available tokens: {available['tpm']}")
```

### Check Time Until Available

```python
wait_seconds = await limiter.time_until_available(
    entity_id="key-123",
    resource="gpt-4",
    limits=[Limit.per_minute("tpm", 10_000)],
    needed={"tpm": 5_000},
)

if wait_seconds > 0:
    print(f"Need to wait {wait_seconds}s for capacity")
```

## Stored Limits

Configure per-entity limits stored in DynamoDB:

```python
# Set custom limits for a premium user
await limiter.set_limits(
    entity_id="user-premium",
    limits=[
        Limit.per_minute("rpm", 500),        # 5x normal
        Limit.per_minute("tpm", 50_000),     # 5x normal
    ],
)

# Use stored limits (falls back to defaults if not found)
async with limiter.acquire(
    entity_id="user-premium",
    resource="gpt-4",
    limits=[Limit.per_minute("rpm", 100)],  # Default
    consume={"rpm": 1},
    use_stored_limits=True,  # Use stored if available
) as lease:
    ...
```

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
    async with limiter.acquire(...):
        ...
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
    return e.as_dict()
```

### Service Unavailable

```python
from zae_limiter import RateLimiterUnavailable

try:
    async with limiter.acquire(...):
        ...
except RateLimiterUnavailable as e:
    # DynamoDB is unavailable
    # Behavior depends on on_unavailable setting
    print(f"Service unavailable: {e}")
```

## Next Steps

- [Hierarchical Limits](hierarchical.md) - Parent/child rate limiting
- [LLM Integration](llm-integration.md) - Token estimation patterns
- [Unavailability Handling](unavailability.md) - Handling service outages
