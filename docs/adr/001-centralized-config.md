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

Extend existing `#LIMIT#` key patterns with **flat schema** (no nested `data.M`) to enable atomic `config_version` increments:

| Level | PK | SK | Purpose |
|-------|----|----|---------|
| System | `SYSTEM#` | `#LIMIT#{resource}#{limit_name}` | Global defaults |
| Resource | `RESOURCE#{resource}` | `#LIMIT#{resource}#{limit_name}` | Resource-specific |
| Entity | `ENTITY#{id}` | `#LIMIT#{resource}#{limit_name}` | Entity-specific (existing pattern) |

**Record structure (FLAT):**
```python
{
    "PK": "RESOURCE#gpt-4",
    "SK": "#LIMIT#gpt-4#tpm",
    "resource": "gpt-4",
    "limit_name": "tpm",
    # Limit fields (flat)
    "capacity": 10000,
    "burst": 10000,
    "refill_amount": 10000,
    "refill_period_seconds": 60,
    # Config fields (flat)
    "on_unavailable": "block",
    "auto_update": True,
    "strict_version": False,
    "config_version": 1,  # Flat for atomic ADD
}
```

**Precedence:** Entity > Resource > System > Constructor defaults

### 2. Config Fields

**Centralized (stored in DynamoDB):**

| Field | Type | Description |
|-------|------|-------------|
| `capacity`, `burst`, `refill_*` | int | Limit definition |
| `on_unavailable` | string | "allow" or "block" on DynamoDB errors |
| `config_version` | int | Atomic counter for cache invalidation |
| `auto_update` | bool | Auto-update Lambda on version mismatch |
| `strict_version` | bool | Fail on client/schema version mismatch |

**Not centralized (StackOptions - infrastructure):**
- Lambda memory, timeout, alarms

### 3. API Change: Stored Limits as Default

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

### 4. Caching Strategy

| Level | TTL | Rationale |
|-------|-----|-----------|
| System | Indefinite | Invalidate on `config_version` change |
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

### 5. v0.6.0 Recommendation: Flatten All Records

This ADR recommends **flattening all existing records** (entities, limits, audit, version) in v0.6.0 to:
- Enable atomic operations everywhere
- Establish consistent patterns
- Simplify the codebase

See issue [#180](https://github.com/zeroae/zae-limiter/issues/180) for the full analysis.

## Consequences

### RCU Cost Analysis

**Config read cost (cache miss):** 3 RCU (one per level)

Each GetItem or BatchGetItem item = 1 RCU (items are ~400 bytes, well under 4KB). BatchGetItem saves latency (1 round-trip vs 3) but same RCU cost.

### Cache Effectiveness by Traffic Pattern

**High-frequency scenario (API gateway, 100 req/sec same entity):**
- 60s TTL × 100 req/sec = 6,000 requests per cache lifetime
- Cache hit rate: 99.98%
- Amortized cost: +0.0005 RCU per request

**Low-frequency scenario (20K unique users/day, 10 resources):**
- 200K unique cache keys (user × resource)
- ~1 request per key per 60s window
- Cache hit rate: ~0-10% (most requests are misses)
- Effective cost: +2.7 to +3 RCU per request

### Expected Usage Pattern

**Typical deployment:**
- System config: Global defaults (1 record per resource×limit)
- Resource config: Per-resource limits (10-50 records) — **most common source**
- Entity config: Premium users only (1-5% of users)

**Cost with negative caching (20K users, 5% premium):**

| User Type | First Request | Subsequent (60s window) |
|-----------|---------------|------------------------|
| Regular (95%) | 3 RCU | 2 RCU (negative cache hit) |
| Premium (5%) | 3 RCU | 1 RCU (entity cache hit) |

Average: **~2.1 RCU per request** (vs 3 RCU without negative caching)

### Monthly Cost Impact

| Scenario | Daily Requests | Monthly Cost Delta |
|----------|----------------|-------------------|
| High-freq API | 1M | +$0.0075 |
| 20K users, sparse | 200K | +$0.135 |
| 20K users, with negative cache | 200K | +$0.05 |

### Positive Consequences

- Enables v0.5.0 config storage with clean flat schema
- Forward compatible with v0.6.0 cascade (entity-level schema already defined)
- Sets precedent: flat schema is the standard going forward
- Addresses [#180](https://github.com/zeroae/zae-limiter/issues/180) with clear v0.6.0 recommendation
- Consistent behavior across all distributed clients

### Negative Consequences

- Temporary inconsistency: config is flat, existing records are nested (until v0.6.0)
- v0.6.0 will require migration work to flatten existing records
- Additional RCU cost (mitigated by caching)

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
4. `test_config_version_atomic` - Verify atomic config_version increment
5. `test_config_sparse_traffic` - Simulate 20K unique users, measure effective hit rate
6. `test_config_negative_cache` - Verify negative caching reduces RCU for regular users

## Alternatives Considered

### 1. Nested `data.M` Schema

**Rejected because:**
- Cannot atomically increment `config_version` counter
- Inconsistent with flat snapshot pattern
- DynamoDB "overlapping document paths" limitation

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
- 3 RCU per acquire (unacceptable cost at scale)
- No cost savings
- Poor latency (3 sequential or batch queries)

## Implementation Checklist

### Phase 1: Core Infrastructure
- [ ] Add `pk_resource()` to `schema.py`
- [ ] Add `get_config()` / `set_config()` / `batch_get_configs()` to `repository.py`
- [ ] Create `ConfigCache` class with TTL and negative caching
- [ ] Add unit tests

### Phase 2: RateLimiter Integration
- [ ] Add `_config_cache` instance variable
- [ ] Implement `_resolve_config()` with precedence
- [ ] Change default: always resolve from stored config
- [ ] Add `invalidate_config_cache()` method
- [ ] Deprecate `use_stored_limits` parameter
- [ ] Add integration tests

### Phase 3: Public API
- [ ] Add `get_config()` / `set_config()` to RateLimiter
- [ ] Add sync wrappers to SyncRateLimiter
- [ ] Add CLI commands (`config get/set`)
- [ ] Add E2E tests

### Phase 4: Documentation
- [ ] Update CLAUDE.md with new access patterns
- [ ] Update docs/performance.md with cost projections
- [ ] Add docs/guide/centralized-config.md
- [ ] Update API documentation

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
