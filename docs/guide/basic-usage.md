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

- On entry: Checks limits, consumes tokens, and writes consumption to DynamoDB immediately
- On success: Commits any adjustments made during the context (no-op if none)
- On exception: Writes compensating deltas to restore consumed tokens (independent writes, 1 WCU each)

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

Allow temporary bursts above the sustained rate by setting `burst` higher than the rate:

```python
# Sustain 10k tokens/minute, but allow bursts up to 15k
limits = [
    Limit.per_minute("tpm", 10_000, burst=15_000),
]
```

The bucket starts full at `burst` capacity and refills at `rate` tokens per period. See [Token Bucket Algorithm](token-bucket.md#capacity-and-burst) for details on how burst and rate interact.

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

!!! warning "`consume` is the declared scope of a lease"
    Only limits named in `acquire(consume=...)` can be adjusted, consumed, or
    released through the lease, and only those appear in `lease.consumed`. A
    limit you did not name was never checked at admission, so adjusting it
    afterwards would drive a bucket negative that never had the chance to reject.

    If the cost is unknown up front, declare the limit with an estimate of `0`:

    ```python
    async with limiter.acquire(
        entity_id="key-123",
        resource="gpt-4",
        consume={"rpm": 1, "tpm": 0},  # tpm is in play; cost reconciled below
    ) as lease:
        response = await call_llm()
        await lease.adjust(tpm=response.usage.total_tokens)
    ```

    Declaring a limit means it gates admission, even at an estimate of `0`: the
    bucket must not be in debt. If a previous `adjust(tpm=...)` overdrew `tpm`,
    `acquire(consume={"rpm": 1, "tpm": 0})` raises `RateLimitExceeded` with a
    `retry_after` until refill clears the debt, while `{"rpm": 1}` alone is
    admitted. That is the point — an overdrawn `tpm` should wait for refill.

    An empty `consume` declares no limits, so nothing on that lease is adjustable —
    use `{"name": 0}` to declare a limit whose cost is unknown. A key in `consume`
    that names no limit configured for the resource is ignored at admission and
    reported by `acquire()` with a `FutureWarning`.

    Passing a key that names no declared limit (including a typo such as
    `adjust(tpmm=...)`) is ignored and emits a `FutureWarning` that lists the
    offending keys and the lease's declared limits. It becomes a `ValidationError`
    in v1.0.0. The no-op lease yielded under
    [`on_unavailable=ALLOW`](unavailability.md#no-op-lease-behavior) is exempt.

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

zae-limiter automatically resolves limits from stored configurations using a four-level hierarchy. See [Configuration Hierarchy](config-hierarchy.md) for full details.

**Resolution order (highest to lowest precedence):**

1. **Entity level (resource-specific)** - Specific limits for an entity+resource pair
2. **Entity level (_default_)** - Default limits for an entity (all resources)
3. **Resource level** - Default limits for a resource (all entities)
4. **System level** - Global defaults (all resources)
5. **Override parameter** - Fallback if no stored config exists

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

!!! info "Namespace Scoping"
    Entity management operations are scoped to the namespace configured on the repository. Entities in different namespaces are fully isolated — they cannot share parent/child relationships across namespace boundaries.

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

## Turning a Resource Off

Beyond throttling, you can disable a resource or entity outright — useful for
incident response, deprecating a model, or blocking a single caller without
touching its stored limits. See [ADR-125](../adr/125-resource-disable.md) for
the full design.

These methods live on `Repository` (not `RateLimiter`) — the same object
returned by `Repository.open()` that you passed to `RateLimiter(repository=repo)`:

```python
await repo.disable_resource("gpt-4")                  # off for everyone
await repo.enable_entity("vip-1", resource="gpt-4")    # carve out one entity
```

Disabling is **eager**: the call stamps every existing bucket for the
resource (or entity) before it returns, so the change takes effect on the
very next `acquire()` — no cache TTL or refill delay to wait out.

`acquire()` signals this with a distinct exception, `ResourceDisabled`, not
`RateLimitExceeded`. It is not a throttling signal — retrying will not help —
so map it to HTTP 403, not 429:

```{.python .lint-only}
from zae_limiter import RateLimitExceeded, ResourceDisabled

try:
    async with limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
        ...
except ResourceDisabled:
    # Not retryable -- the resource is intentionally off for this caller.
    return http_403()
except RateLimitExceeded as e:
    return http_429(retry_after=e.retry_after_seconds)
```

An entity-level `disabled: false` override wins even when the resource is
disabled for everyone else — the resolution walk is entity(resource) →
entity(`_default_`) → resource, and the first level with an explicit value
wins regardless of which level supplies the limits. System-level disable is
not supported; disabling always targets a specific resource, or an entity's
access to one.

## Config Cache

zae-limiter caches config data (system defaults, resource defaults, entity limits) to reduce DynamoDB reads. The cache has a 60-second TTL by default.

### Configuring Cache TTL

```python
from zae_limiter import Repository, RateLimiter

# Default: 60-second cache TTL
repo = await Repository.open(config_cache_ttl=60)
limiter = RateLimiter(repository=repo)

# Disable caching (for testing)
repo = await Repository.open(config_cache_ttl=0)
limiter = RateLimiter(repository=repo)
```

### Automatic Cache Eviction

Config-modifying methods (`set_limits()`, `delete_limits()`) automatically evict relevant cache entries. Manual invalidation is only needed after external changes (e.g., direct DynamoDB writes).

### Manual Cache Invalidation

After external config changes, force immediate refresh:

```python
await repo.invalidate_config_cache()
```

### Monitoring Cache Performance

```python
stats = repo.get_cache_stats()
print(f"Hits: {stats.hits}, Misses: {stats.misses}")
print(f"Cache entries: {stats.size}")
```

See [Config Cache Tuning](../performance.md#7-config-cache-tuning) for advanced configuration.

## Speculative Writes

Speculative writes are enabled by default, skipping the read round trip for pre-warmed buckets. To disable them:

```python
repo = await Repository.open()
limiter = RateLimiter(
    repository=repo,
    speculative_writes=False,  # Disable speculative writes
)
```

With speculative writes, `acquire()` attempts a conditional UpdateItem directly instead of reading bucket state first. On success, this saves one DynamoDB round trip (0 RCU, 1 WCU instead of 1 RCU + 1 WCU). When the bucket is exhausted and refill would not help, it rejects immediately without any writes (0 RCU, 0 WCU).

The speculative path falls back to the normal read-write path when:

- The bucket does not exist yet (first acquire for an entity)
- A new limit was added that is not in the bucket
- Token refill since last access would provide enough capacity

See [Performance Tuning - Speculative Writes](../performance.md#8-speculative-writes) for detailed cost analysis and guidance on when to disable this feature.

## Next Steps

- [Hierarchical Limits](hierarchical.md) - Parent/child rate limiting
- [LLM Integration](llm-integration.md) - Token estimation patterns
- [Unavailability Handling](unavailability.md) - Handling service outages
