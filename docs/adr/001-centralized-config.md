# ADR-001: Centralized Configuration Access Patterns

**Status:** Proposed
**Date:** 2026-01-18
**Issue:** [#129](https://github.com/zeroae/zae-limiter/issues/129)
**Milestone:** v0.5.0

## Context

zae-limiter is a distributed rate limiting library where multiple clients must behave consistently. Currently:

1. **Limits passed explicitly** - Each `acquire()` call requires limits, or `use_stored_limits=True` opts into per-entity lookup
2. **No global defaults** - Cannot set system-wide or resource-level default limits
3. **No caching** - `use_stored_limits=True` queries DynamoDB on every call
4. **Scattered config** - Behavior settings (`on_unavailable`, `auto_update`) are constructor-only, risking inconsistent fail-open/fail-closed behavior across clients

### Current Schema Patterns

The codebase has three DynamoDB schema patterns:

| Pattern | Records | Use Case |
|---------|---------|----------|
| Nested `data.M` | Entities, Limits, Audit, Version | No atomic counters needed |
| Hybrid | Buckets (`total_consumed_milli` flat) | Mostly nested + one atomic counter |
| Flat | Snapshots | Atomic upsert with ADD counters |

### DynamoDB Limitation

DynamoDB rejects UpdateExpressions that combine:
```
SET #data = if_not_exists(#data, :map)
ADD #data.counter :delta
```

Error: "overlapping document paths" (see issues [#168](https://github.com/zeroae/zae-limiter/issues/168), [#179](https://github.com/zeroae/zae-limiter/issues/179))

This limitation drove snapshots to flat schema and influences our config design.

## Decision

Implement a **three-level configuration hierarchy** with **flat schema** and **in-memory TTL caching**.

### 1. Schema Design: Flat Records

Extend existing `#LIMIT#` key patterns with **flat schema** (no nested `data.M`) for consistency with the snapshot pattern:

| Level | PK | SK | Purpose |
|-------|----|----|---------|
| System | `SYSTEM#` | `#LIMIT#{resource}#{limit_name}` | Global defaults + infrastructure config |
| Resource | `RESOURCE#{resource}` | `#LIMIT#{resource}#{limit_name}` | Resource-specific limits and failure policy |
| Entity | `ENTITY#{id}` | `#LIMIT#{resource}#{limit_name}` | Entity-specific overrides (premium users) |

See **Section 3: Record Structures by Level** for detailed examples of each level.

### 2. Config Fields and Scope

Config fields have different scopes based on their nature:

| Field | System | Resource | Entity | Description |
|-------|--------|----------|--------|-------------|
| `capacity`, `burst`, `refill_*` | ✅ | ✅ | ✅ | Limit definition |
| `on_unavailable` | ✅ | ✅ | ✅ | "allow" or "block" on DynamoDB errors |
| `auto_update` | ✅ | ❌ | ❌ | Auto-update Lambda on version mismatch |
| `strict_version` | ✅ | ❌ | ❌ | Fail on client/schema version mismatch |

**Why `on_unavailable` supports all three levels:**
- **Resource-level:** Different failure policies per resource (expensive model → block, cheap model → allow)
- **Entity-level:** Premium users get fail-open (better UX), free tier gets fail-closed (protect revenue)
- **Zero additional cost:** Config is already fetched at all levels; adding fields costs nothing

**Why `auto_update` and `strict_version` are system-only:**
- **`auto_update`:** Lambda is infrastructure - one function per table, not per resource/entity
- **`strict_version`:** Version compatibility must be consistent across all operations

**Not centralized (StackOptions - infrastructure):**
- Lambda memory, timeout, alarms

### 3. Record Structures by Level

**System-level config** (global defaults + infrastructure settings):
```python
{
    "PK": "SYSTEM#",
    "SK": "#LIMIT#gpt-4#tpm",
    "resource": "gpt-4",
    "limit_name": "tpm",
    # Limit fields
    "capacity": 10000,
    "burst": 10000,
    "refill_amount": 10000,
    "refill_period_seconds": 60,
    # Behavioral config (all fields available at system level)
    "on_unavailable": "block",
    "auto_update": True,       # System-only
    "strict_version": True,    # System-only
}
```

**Resource-level config** (per-resource limits and failure policy):
```python
{
    "PK": "RESOURCE#gpt-4",
    "SK": "#LIMIT#gpt-4#tpm",
    "resource": "gpt-4",
    "limit_name": "tpm",
    # Limit fields
    "capacity": 40000,              # Higher than system default
    "burst": 40000,
    "refill_amount": 40000,
    "refill_period_seconds": 60,
    # Behavioral config (only on_unavailable)
    "on_unavailable": "block",      # Expensive model: fail closed
}

# Cheaper model with different failure policy
{
    "PK": "RESOURCE#gpt-3.5-turbo",
    "SK": "#LIMIT#gpt-3.5-turbo#tpm",
    "resource": "gpt-3.5-turbo",
    "limit_name": "tpm",
    "capacity": 100000,
    "burst": 100000,
    "refill_amount": 100000,
    "refill_period_seconds": 60,
    "on_unavailable": "allow",      # Cheap model: fail open for availability
}
```

**Entity-level config** (premium user overrides):
```python
{
    "PK": "ENTITY#premium-user-1",
    "SK": "#LIMIT#gpt-4#tpm",
    "resource": "gpt-4",
    "limit_name": "tpm",
    # Higher limits for premium user
    "capacity": 100000,
    "burst": 100000,
    "refill_amount": 100000,
    "refill_period_seconds": 60,
    # Better UX for premium users
    "on_unavailable": "allow",      # Premium: fail open for availability
}
```

**Precedence:** Entity > Resource > System > Constructor defaults

Each level only needs to specify fields it wants to override. Missing fields inherit from the next level down.

### 4. API Change: Stored Limits as Default

**Current behavior (opt-in):**
```python
async with limiter.acquire(
    entity_id="user-1",
    resource="gpt-4",
    limits=[Limit.per_minute("tpm", 10000)],  # Required
    use_stored_limits=False,  # Default
):
    ...
```

**New behavior (always stored):**
```python
async with limiter.acquire(
    entity_id="user-1",
    resource="gpt-4",
    # Limits resolved from System/Resource/Entity config automatically
):
    ...
```

**Resolution order:**
1. Entity config → if exists, use it
2. Resource config → if exists, use it
3. System config → fallback
4. Error if no config found

**Backward compatibility:**
- `limits` parameter accepted as override
- `use_stored_limits=False` deprecated with warning, removed in v1.0

### 5. Caching Strategy

| Level | TTL | Rationale |
|-------|-----|-----------|
| System | 60s | Rarely changes, high hit rate |
| Resource | 60s | Few keys (~10-50), high hit rate |
| Entity | 60s | Many keys, use negative caching |

**Negative caching:** Cache "no entity config" to avoid repeated misses for users without custom limits.

```python
entity_config_cache = {
    "user-123": None,           # No custom config (negative cache)
    "premium-user-1": {...},    # Has custom config
}
```

**Force refresh:** `limiter.invalidate_config_cache()` method

### 6. Distributed Cache Consistency

The config cache is **local to each RateLimiter instance**. There is no cross-process cache invalidation.

**Behavior:**
- Config changes made by one process are visible to other processes after TTL expires (max 60s staleness)
- For immediate propagation across processes, restart application or call `invalidate_config_cache()` on each instance
- This is acceptable because config changes are infrequent (typically during deployment or admin operations)

**Why no distributed invalidation:**
- Adds infrastructure complexity (SNS/EventBridge)
- 60s staleness is acceptable for config changes
- Keeps the library simple and self-contained

### 7. Read Consistency Strategy

Config reads use **eventually consistent** reads to reduce RCU cost by 50%.

| Consistency | Cost | Latency | Use Case |
|-------------|------|---------|----------|
| Strongly consistent | 1 RCU / 4KB | Higher | Real-time accuracy required |
| Eventually consistent | 0.5 RCU / 4KB | Lower | Acceptable staleness (<1s typical) |

**Rationale:** Since we already accept 60s cache staleness, sub-second DynamoDB eventual consistency is negligible. This reduces config fetch cost from 3 RCU to 1.5 RCU per cache miss.

### 8. v0.6.0 Recommendation: Flatten All Records

This ADR recommends **flattening all existing records** (entities, limits, audit, version) in v0.6.0 to:
- Enable atomic operations everywhere
- Establish consistent patterns
- Simplify the codebase

See issue [#180](https://github.com/zeroae/zae-limiter/issues/180) for the full analysis.

## Consequences

### RCU Cost Analysis

**Config read cost (cache miss):** 1.5 RCU (3 items × 0.5 RCU each with eventually consistent reads)

Each GetItem or BatchGetItem item = 0.5 RCU with eventually consistent reads (items are ~200 bytes, well under 4KB). BatchGetItem saves latency (1 round-trip vs 3) with same RCU cost.

### Cache Effectiveness by Traffic Pattern

**High-frequency scenario (API gateway, 100 req/sec same entity):**
- 60s TTL × 100 req/sec = 6,000 requests per cache lifetime
- Cache hit rate: 99.98%
- Amortized cost: +0.00025 RCU per request

**Low-frequency scenario (20K unique users/day, 10 resources):**
- 200K unique cache keys (user × resource)
- ~1 request per key per 60s window
- Cache hit rate: ~0-10% (most requests are misses)
- Effective cost: +1.35 to +1.5 RCU per request

### Expected Usage Pattern

**Typical deployment:**
- System config: Global defaults (1 record per resource×limit)
- Resource config: Per-resource limits (10-50 records) — **most common source**
- Entity config: Premium users only (1-5% of users)

**Cost with negative caching (20K users, 5% premium):**

| User Type | First Request | Subsequent (60s window) |
|-----------|---------------|------------------------|
| Regular (95%) | 1.5 RCU | 1 RCU (negative cache hit) |
| Premium (5%) | 1.5 RCU | 0.5 RCU (entity cache hit) |

Average: **~1.05 RCU per request** (vs 1.5 RCU without negative caching)

### Monthly Cost Impact

| Scenario | Daily Requests | Monthly Cost Delta |
|----------|----------------|-------------------|
| High-freq API | 1M | +$0.004 |
| 20K users, sparse | 200K | +$0.07 |
| 20K users, with negative cache | 200K | +$0.025 |

### Positive Consequences

- Enables v0.5.0 config storage with clean flat schema
- Forward compatible with v0.6.0 cascade (entity-level schema already defined)
- Sets precedent: flat schema is the standard going forward
- Addresses [#180](https://github.com/zeroae/zae-limiter/issues/180) with clear v0.6.0 recommendation
- Consistent behavior across all distributed clients

### Negative Consequences

- v0.6.0 will require migration work to flatten existing records (entities, buckets, audit)
- Additional RCU cost (mitigated by caching and eventually consistent reads)

## Access Patterns Added

| Pattern | Query | Index |
|---------|-------|-------|
| Get system config | `PK=SYSTEM#, SK begins_with #LIMIT#{resource}#` | Primary |
| Get resource config | `PK=RESOURCE#{resource}, SK begins_with #LIMIT#` | Primary |
| Get entity config | `PK=ENTITY#{id}, SK begins_with #LIMIT#{resource}#` | Primary (existing) |
| Batch fetch configs | BatchGetItem with 3 keys | Primary |

## New Public APIs

```python
# RateLimiter methods
async def get_config(level: str, identifier: str | None, resource: str) -> LimiterConfig | None
async def set_config(level: str, config: LimiterConfig, identifier: str | None, resource: str) -> None
def invalidate_config_cache(entity_id: str | None = None, resource: str | None = None) -> None

# CLI commands
zae-limiter config get --level system|resource|entity [--identifier ID] [--resource NAME]
zae-limiter config set --level system|resource|entity [--identifier ID] [--resource NAME] --limits JSON
```

## Benchmark Test Cases

Add to `tests/benchmark/`:

1. `test_config_read_latency` - Baseline config fetch from DynamoDB (3 items)
2. `test_config_cache_hit` - Latency with warm cache (system + resource)
3. `test_acquire_with_config` - Full acquire() with config lookup
4. `test_config_sparse_traffic` - Simulate 20K unique users, measure effective hit rate
5. `test_config_negative_cache` - Verify negative caching reduces RCU for regular users

## Alternatives Considered

### 1. Nested `data.M` Schema

**Rejected because:**
- Inconsistent with flat snapshot pattern established in v0.4.0
- DynamoDB "overlapping document paths" limitation prevents future atomic counters
- Flat schema is the standard going forward (see v0.6.0 recommendation)

### 2. New `#CONFIG#` Key Pattern

**Rejected because:**
- Requires schema migration
- Separates limits from config (semantic split)
- More complex queries

**Chosen approach (extend `#LIMIT#`):**
- No migration needed for existing entities
- Limits and config are semantically related
- Backward compatible

### 3. No Caching

**Rejected because:**
- 1.5 RCU per acquire (unacceptable cost at scale)
- No cost savings
- Poor latency (batch query on every request)

## Implementation Checklist

!!! abstract "Phase 1: Core Infrastructure ([#130](https://github.com/zeroae/zae-limiter/issues/130), [#135](https://github.com/zeroae/zae-limiter/issues/135))"
    - Add `pk_resource()` to `schema.py` ([#130](https://github.com/zeroae/zae-limiter/issues/130))
    - Add `get_config()` / `set_config()` / `batch_get_configs()` to `repository.py` ([#130](https://github.com/zeroae/zae-limiter/issues/130))
    - Create `ConfigCache` class with TTL and negative caching ([#135](https://github.com/zeroae/zae-limiter/issues/135))
    - Add unit tests

!!! abstract "Phase 2: RateLimiter Integration ([#130](https://github.com/zeroae/zae-limiter/issues/130), [#131](https://github.com/zeroae/zae-limiter/issues/131), [#135](https://github.com/zeroae/zae-limiter/issues/135))"
    - Add `_config_cache` instance variable ([#135](https://github.com/zeroae/zae-limiter/issues/135))
    - Implement `_resolve_config()` with precedence ([#131](https://github.com/zeroae/zae-limiter/issues/131))
    - Change default: always resolve from stored config ([#130](https://github.com/zeroae/zae-limiter/issues/130))
    - Add `invalidate_config_cache()` method ([#135](https://github.com/zeroae/zae-limiter/issues/135))
    - Deprecate `use_stored_limits` parameter ([#130](https://github.com/zeroae/zae-limiter/issues/130))
    - Add integration tests

!!! abstract "Phase 3: Public API ([#130](https://github.com/zeroae/zae-limiter/issues/130))"
    - Add `get_config()` / `set_config()` to RateLimiter ([#130](https://github.com/zeroae/zae-limiter/issues/130))
    - Add sync wrappers to SyncRateLimiter ([#130](https://github.com/zeroae/zae-limiter/issues/130))
    - Add CLI commands (`config get/set`) ([#130](https://github.com/zeroae/zae-limiter/issues/130))
    - Add E2E tests

!!! abstract "Phase 4: Documentation ([#129](https://github.com/zeroae/zae-limiter/issues/129))"
    - Update CLAUDE.md with new access patterns
    - Update docs/performance.md with cost projections
    - Add docs/guide/centralized-config.md
    - Update API documentation

## References

### Project Issues
- [#123](https://github.com/zeroae/zae-limiter/issues/123) - v0.5.0: Central Config, IAM & Performance (milestone epic)
- [#124](https://github.com/zeroae/zae-limiter/issues/124) - v0.6.0: Schema Evolution & Cascade Redesign (milestone epic)
- [#129](https://github.com/zeroae/zae-limiter/issues/129) - Analyze DynamoDB access patterns (this analysis)
- [#130](https://github.com/zeroae/zae-limiter/issues/130) - Store system and resource config in DynamoDB
- [#131](https://github.com/zeroae/zae-limiter/issues/131) - Add system-level default limits
- [#135](https://github.com/zeroae/zae-limiter/issues/135) - Client-side config cache with configurable TTL
- [#168](https://github.com/zeroae/zae-limiter/issues/168) - Snapshot flat schema (precedent for atomic counters)
- [#179](https://github.com/zeroae/zae-limiter/issues/179) - Consumption counter design (hybrid schema precedent)
- [#180](https://github.com/zeroae/zae-limiter/issues/180) - Schema revalidation before v1.0.0

### AWS Documentation
- [DynamoDB Best Practices for Designing and Architecting](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/best-practices.html)
- [DynamoDB Read/Write Capacity Mode](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/HowItWorks.ReadWriteCapacityMode.html)
- [DynamoDB Pricing](https://aws.amazon.com/dynamodb/pricing/)
- [BatchGetItem Operation](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_BatchGetItem.html)
- [UpdateExpression Reference](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Expressions.UpdateExpressions.html)
