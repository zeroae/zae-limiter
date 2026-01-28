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

# Create child entities (API keys) with cascade enabled
await limiter.create_entity(
    entity_id="key-abc",
    parent_id="project-1",
    name="Web Application Key",
    cascade=True,  # Enforce parent limits on every acquire
)

await limiter.create_entity(
    entity_id="key-xyz",
    parent_id="project-1",
    name="Mobile App Key",
    cascade=True,
)
```

## Cascade Mode

Create entities with `cascade=True` to apply rate limits to both the child and parent on every `acquire()` call:

```python
# Cascade is set once at entity creation
await limiter.create_entity(
    entity_id="key-abc",
    parent_id="project-1",
    cascade=True,  # All acquire() calls will also check parent
)

# acquire() automatically cascades to parent — no flag needed
async with limiter.acquire(
    entity_id="key-abc",
    resource="gpt-4",
    limits=[
        Limit.per_minute("tpm", 10_000),  # Per-key limit
    ],
    consume={"tpm": 500},
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

# acquire() auto-cascades because key-abc was created with cascade=True
async with limiter.acquire(
    entity_id="key-abc",
    resource="gpt-4",
    limits=None,  # Auto-resolves from stored config
    consume={"tpm": 500},
) as lease:
    await call_api()
```

## Understanding Cascade Behavior

### Without Cascade

```python
# Entity created without cascade (default)
await limiter.create_entity(entity_id="key-abc", parent_id="project-1")

# Only checks/consumes from key-abc
async with limiter.acquire(
    entity_id="key-abc",
    resource="gpt-4",
    limits=[Limit.per_minute("tpm", 10_000)],
    consume={"tpm": 500},
) as lease:
    ...
```

### With Cascade

```python
# Entity created with cascade enabled
await limiter.create_entity(entity_id="key-abc", parent_id="project-1", cascade=True)

# Checks/consumes from BOTH key-abc AND project-1
async with limiter.acquire(
    entity_id="key-abc",
    resource="gpt-4",
    limits=[Limit.per_minute("tpm", 10_000)],
    consume={"tpm": 500},
) as lease:
    ...
```

## Error Handling with Hierarchies

When an entity has cascade enabled, `RateLimitExceeded` includes statuses for all entities:

```python
try:
    async with limiter.acquire(
        entity_id="key-abc",  # Has cascade=True from create_entity()
        resource="gpt-4",
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

# Create user under tenant with cascade enabled
await limiter.create_entity(entity_id="user-123", parent_id="tenant-acme", cascade=True)

# Each user gets 100k tokens/day
await limiter.set_limits(
    entity_id="user-123",
    limits=[Limit.per_day("tpd", 100_000)],
)

# Rate limit user — auto-cascades to tenant
# limits=None auto-resolves from stored config
async with limiter.acquire(
    entity_id="user-123",
    resource="gpt-4",
    limits=None,
    consume={"tpm": 500},
) as lease:
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
- **Cascade is per-entity**: Set `cascade=True` on `create_entity()` to enable; it applies to all `acquire()` calls for that entity

## Next Steps

- [LLM Integration](llm-integration.md) - Token estimation patterns
- [Unavailability Handling](unavailability.md) - Handling service outages
- [API Reference](../api/limiter.md) - Complete API documentation
