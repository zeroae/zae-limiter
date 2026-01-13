# Hierarchical Limits

zae-limiter supports two-level hierarchies for rate limiting, enabling patterns like:

- **Project → API Keys**: Limit total project usage while also limiting individual keys
- **Organization → Users**: Organization-wide limits with per-user quotas
- **Tenant → Services**: Multi-tenant limits with service-level controls

## Creating a Hierarchy

```python
# Create parent entity (project)
await limiter.create_entity(
    entity_id="project-1",
    name="Production Project",
)

# Create child entities (API keys)
await limiter.create_entity(
    entity_id="key-abc",
    parent_id="project-1",
    name="Web Application Key",
)

await limiter.create_entity(
    entity_id="key-xyz",
    parent_id="project-1",
    name="Mobile App Key",
)
```

## Cascade Mode

Use `cascade=True` to apply rate limits to both the child and parent:

```python
async with limiter.acquire(
    entity_id="key-abc",
    resource="gpt-4",
    limits=[
        Limit.per_minute("tpm", 10_000),  # Per-key limit
    ],
    consume={"tpm": 500},
    cascade=True,  # Also applies to parent (project-1)
) as lease:
    await call_api()
```

!!! note "Performance Impact"
    Cascade mode adds overhead: +1 GetEntity + parent bucket operations. Only enable when hierarchical enforcement is needed. See [Batch Operation Patterns](../performance.md#cascade-optimization) for optimization strategies.

**What happens:**

1. Check if `key-abc` has capacity (10k tpm)
2. Check if `project-1` has capacity (uses same limits)
3. If both pass, consume from both atomically
4. If either fails, reject with details about which limit was exceeded

## Different Limits Per Level

Set different limits for parents and children:

```python
# Set project-level limits (higher)
await limiter.set_limits(
    entity_id="project-1",
    limits=[
        Limit.per_minute("tpm", 100_000),  # 100k for entire project
    ],
)

# Set key-level limits (lower)
await limiter.set_limits(
    entity_id="key-abc",
    limits=[
        Limit.per_minute("tpm", 10_000),   # 10k per key
    ],
)

# Use stored limits with cascade
async with limiter.acquire(
    entity_id="key-abc",
    resource="gpt-4",
    limits=[Limit.per_minute("tpm", 5_000)],  # Default
    consume={"tpm": 500},
    cascade=True,
    use_stored_limits=True,  # Uses stored limits for both levels
) as lease:
    await call_api()
```

## Understanding Cascade Behavior

### Without Cascade

```python
# Only checks/consumes from key-abc
async with limiter.acquire(
    entity_id="key-abc",
    resource="gpt-4",
    limits=[Limit.per_minute("tpm", 10_000)],
    consume={"tpm": 500},
    cascade=False,  # Default
) as lease:
    ...
```

### With Cascade

```python
# Checks/consumes from BOTH key-abc AND project-1
async with limiter.acquire(
    entity_id="key-abc",
    resource="gpt-4",
    limits=[Limit.per_minute("tpm", 10_000)],
    consume={"tpm": 500},
    cascade=True,
) as lease:
    ...
```

## Error Handling with Hierarchies

When using cascade mode, `RateLimitExceeded` includes statuses for all entities:

```python
try:
    async with limiter.acquire(
        entity_id="key-abc",
        cascade=True,
        ...
    ):
        ...
except RateLimitExceeded as e:
    for status in e.statuses:
        print(f"Entity: {status.entity_id}")
        print(f"  Limit: {status.limit_name}")
        print(f"  Available: {status.available}")
        print(f"  Exceeded: {status.exceeded}")
```

## Use Cases

### Multi-Tenant SaaS

```python
# Tenant has 1M tokens/day
await limiter.set_limits(
    entity_id="tenant-acme",
    limits=[Limit.per_day("tpd", 1_000_000)],
)

# Each user gets 100k tokens/day
await limiter.set_limits(
    entity_id="user-123",
    limits=[Limit.per_day("tpd", 100_000)],
)

# Rate limit user, cascade to tenant
async with limiter.acquire(
    entity_id="user-123",
    cascade=True,
    use_stored_limits=True,
    ...
):
    ...
```

### API Key Management

```python
# Project limit: 10k RPM
await limiter.set_limits(
    entity_id="project-prod",
    limits=[Limit.per_minute("rpm", 10_000)],
)

# Production key: 5k RPM (half of project)
await limiter.set_limits(
    entity_id="key-prod",
    limits=[Limit.per_minute("rpm", 5_000)],
)

# Staging key: 1k RPM
await limiter.set_limits(
    entity_id="key-staging",
    limits=[Limit.per_minute("rpm", 1_000)],
)
```

## Limitations

- **Two levels only**: Parent → Child (no grandparents)
- **Single parent**: Each entity can have at most one parent
- **Cascade is optional**: Must be explicitly enabled per call

## Next Steps

- [LLM Integration](llm-integration.md) - Token estimation patterns
- [Failure Modes](failure-modes.md) - Handling service outages
- [API Reference](../api/limiter.md) - Complete API documentation
