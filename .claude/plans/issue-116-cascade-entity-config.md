# Plan: Move `cascade` from per-call parameter to per-entity configuration

**Issue:** [#116](https://github.com/zeroae/zae-limiter/issues/116)
**Status:** Planning
**Created:** 2026-01-14

## Problem Statement

Currently, the `cascade` parameter is specified on every `acquire()` call:

```python
async with limiter.acquire(
    entity_id="key-1",
    resource="gpt-4",
    limits=[...],
    consume={"rpm": 1},
    cascade=True,  # <-- Must be specified on EVERY call
) as lease:
    ...
```

This creates a critical vulnerability:
1. **Accidental omission**: Developers can forget `cascade=True`, bypassing parent limits
2. **Multi-library coordination**: Independent libraries sharing a limiter cannot enforce consistent cascade behavior
3. **Architectural inconsistency**: Hierarchy (`parent_id`) is defined at entity creation, but enforcement (`cascade`) is scattered across every caller

## Current Implementation

### Entity Model (models.py:246-274)
```python
@dataclass
class Entity:
    id: str
    name: str | None = None
    parent_id: str | None = None  # Hierarchy defined here
    metadata: dict[str, str] = field(default_factory=dict)
    created_at: str | None = None
```

### Acquire Method (limiter.py:276-286)
```python
async def acquire(
    self,
    entity_id: str,
    resource: str,
    limits: list[Limit],
    consume: dict[str, int],
    cascade: bool = False,  # <-- Enforcement here (per-call)
    use_stored_limits: bool = False,
    failure_mode: FailureMode | None = None,
) -> AsyncIterator[Lease]:
```

### Cascade Logic (limiter.py:360-364)
```python
entity_ids = [entity_id]
if cascade:
    entity = await self._repository.get_entity(entity_id)
    if entity and entity.parent_id:
        entity_ids.append(entity.parent_id)
```

---

## Decision Point 1: Where to Store Cascade Configuration

### Option A: Add `cascade` field to Entity model (Recommended)

Add a boolean field directly to the Entity dataclass:

```python
@dataclass
class Entity:
    id: str
    name: str | None = None
    parent_id: str | None = None
    cascade: bool = False  # NEW: Enable cascade to parent
    metadata: dict[str, str] = field(default_factory=dict)
    created_at: str | None = None
```

**Pros:**
- Simple, single-field addition
- Natural co-location with `parent_id`
- Minimal schema change
- Easy to understand and document

**Cons:**
- Limited flexibility (boolean only)
- No room for future cascade options (e.g., cascade depth, selective resource cascade)

### Option B: Add `CascadePolicy` enum/model

Create an enum for cascade behavior:

```python
class CascadePolicy(Enum):
    NONE = "none"           # Never cascade
    PARENT = "parent"       # Cascade to immediate parent only
    ALL_ANCESTORS = "all"   # Cascade through entire hierarchy (future)

@dataclass
class Entity:
    ...
    cascade_policy: CascadePolicy = CascadePolicy.NONE
```

**Pros:**
- Extensible for future cascade modes
- Self-documenting enum values
- Room for `ALL_ANCESTORS` if multi-level hierarchy is added later

**Cons:**
- More complex than boolean
- YAGNI (You Aren't Gonna Need It) if multi-level cascade never ships
- Enum serialization adds complexity

### Option C: Nested configuration object

Create a dedicated configuration object:

```python
@dataclass
class CascadeConfig:
    enabled: bool = False
    depth: int = 1  # How many parent levels to cascade
    resources: list[str] | None = None  # Cascade only specific resources (None = all)

@dataclass
class Entity:
    ...
    cascade_config: CascadeConfig = field(default_factory=CascadeConfig)
```

**Pros:**
- Maximum flexibility
- Supports selective resource cascade
- Future-proof for complex requirements

**Cons:**
- Over-engineered for current needs
- Complex serialization/deserialization
- Harder to understand
- Increases DynamoDB storage per entity

### Recommendation: Option A

Start with a simple boolean field. The current implementation only supports single-level cascade, and there's no evidence multi-level cascade is needed. If requirements evolve, the field can be migrated to an enum or object later.

---

## Decision Point 2: API Parameter Handling

### Option A: Remove `cascade` parameter entirely (Recommended)

Remove the parameter from `acquire()`:

```python
async def acquire(
    self,
    entity_id: str,
    resource: str,
    limits: list[Limit],
    consume: dict[str, int],
    # cascade parameter removed
    use_stored_limits: bool = False,
    failure_mode: FailureMode | None = None,
) -> AsyncIterator[Lease]:
```

Behavior determined solely by `entity.cascade` field.

**Pros:**
- Clean API, no coordination needed
- Single source of truth
- Eliminates the vulnerability entirely
- Simplest long-term maintenance

**Cons:**
- Breaking change (requires major version bump if post-1.0)
- Cannot override per-call if needed
- Existing code must be updated

### Option B: Keep parameter as override

Keep the parameter but change semantics:

```python
async def acquire(
    self,
    entity_id: str,
    resource: str,
    limits: list[Limit],
    consume: dict[str, int],
    cascade: bool | None = None,  # None = use entity config
    ...
) -> AsyncIterator[Lease]:
```

Logic: `cascade if cascade is not None else entity.cascade`

**Pros:**
- Backward compatible
- Allows per-call override for edge cases
- Gradual migration path

**Cons:**
- Doesn't solve the core problem (callers can still set `cascade=False`)
- More complex behavior to document
- "Two sources of truth" can cause confusion

### Option C: Deprecation period with warnings

Keep parameter but emit deprecation warning:

```python
async def acquire(
    self,
    ...,
    cascade: bool | None = None,  # Deprecated
) -> AsyncIterator[Lease]:
    if cascade is not None:
        warnings.warn(
            "cascade parameter is deprecated, use Entity.cascade instead",
            DeprecationWarning,
            stacklevel=2,
        )
        # Still use the parameter value for backward compatibility
        effective_cascade = cascade
    else:
        entity = await self._repository.get_entity(entity_id)
        effective_cascade = entity.cascade if entity else False
```

**Pros:**
- Non-breaking initially
- Gives users time to migrate
- Clear migration path with warnings

**Cons:**
- Complexity during transition
- Must maintain both code paths
- Doesn't fully solve the problem until removed

### Recommendation: Option A (or Option C if post-1.0)

Since the schema version is currently `1.0.0`, we're at a good point for breaking changes. If we want to be cautious, use Option C for a deprecation period in 1.x, then remove in 2.0.

---

## Decision Point 3: Default Behavior for Existing Entities

### Option A: Default `cascade=False` (Recommended)

New field defaults to `False`, preserving current behavior for entities without explicit configuration:

```python
cascade: bool = False
```

**Pros:**
- Backward compatible
- No surprising behavior changes for existing users
- Safe default (fail-open is worse than fail-closed for rate limiting)

**Cons:**
- Users must explicitly enable cascade
- Doesn't solve the "forgot to set cascade" problem for new entities

### Option B: Default `cascade=True`

New field defaults to `True` when `parent_id` is set:

```python
@property
def cascade(self) -> bool:
    return self._cascade if self._cascade is not None else (self.parent_id is not None)
```

**Pros:**
- Secure by default
- Aligns with principle of least surprise (parent relationship implies enforcement)
- Prevents accidental bypass

**Cons:**
- Breaking change for existing entities that relied on `cascade=False` behavior
- May cause unexpected `RateLimitExceeded` errors after upgrade

### Option C: Require explicit cascade setting when parent_id is set

Validation enforces explicit decision:

```python
def create_entity(
    self,
    entity_id: str,
    parent_id: str | None = None,
    cascade: bool | None = None,  # Required if parent_id is set
):
    if parent_id is not None and cascade is None:
        raise ValidationError(
            "cascade must be explicitly set when parent_id is specified"
        )
```

**Pros:**
- Forces conscious decision
- No implicit behavior
- Self-documenting code

**Cons:**
- More verbose API
- May be annoying for simple use cases
- Backward incompatible for existing create_entity calls

### Recommendation: Option A

Default to `False` for backward compatibility. Document clearly that new child entities should explicitly set `cascade=True` if parent enforcement is desired. Consider adding a warning when creating entities with `parent_id` but `cascade=False`.

---

## Decision Point 4: Migration Strategy

### Option A: Add field with default value (no migration needed) (Recommended)

Add the field to the model with a default value. Handle missing attribute gracefully in repository:

```python
# In repository.py deserialize_entity()
cascade = item.get("cascade", {}).get("BOOL", False)
```

**Pros:**
- Zero downtime
- No migration script needed
- Works immediately after deployment
- Existing entities seamlessly gain default behavior

**Cons:**
- Schema has implicit defaults (not explicit in data)
- Requires careful handling of missing attributes everywhere

### Option B: Create migration to set cascade on existing entities

Add a migration in `migrations/v1_1_0.py`:

```python
async def migrate(repository: Repository) -> None:
    """Add cascade field to all existing entities."""
    # Scan all entities
    # Update each with cascade=False
```

**Pros:**
- Explicit data in DynamoDB
- Cleaner schema (no implicit defaults)
- Better for auditing

**Cons:**
- Requires running migration
- May take time for large datasets
- Additional deployment step

### Option C: Hybrid approach

Handle missing field gracefully initially, then run background migration:

```python
# Phase 1: Code handles missing cascade attribute
# Phase 2: Background job adds explicit cascade=False to all entities
# Phase 3: Code can assume cascade attribute always exists
```

**Pros:**
- Zero downtime deployment
- Eventually consistent explicit data
- Best of both worlds

**Cons:**
- More complex implementation
- Two-phase rollout
- Must track migration completion

### Recommendation: Option A

Since this is a simple boolean with a sensible default, handle missing attributes gracefully. The DynamoDB schema already handles optional attributes well.

---

## Decision Point 5: Validation Rules

### Option A: Allow cascade without parent_id (no-op)

Setting `cascade=True` on a root entity is allowed but has no effect:

```python
# Valid, but cascade has no effect (no parent to cascade to)
entity = Entity(id="root-1", cascade=True)
```

**Pros:**
- Flexible, doesn't require additional validation
- Entity config can be set uniformly
- Simple implementation

**Cons:**
- May be confusing (setting cascade=True "does nothing")
- Potential for user confusion

### Option B: Require parent_id when cascade=True

Validation enforces logical consistency:

```python
if cascade and parent_id is None:
    raise ValidationError(
        "cascade=True requires parent_id to be set"
    )
```

**Pros:**
- Enforces logical consistency
- Prevents configuration errors
- Clear error messages

**Cons:**
- More restrictive
- Cannot pre-configure cascade before parent is known
- Extra validation logic

### Option C: Warn but allow (Recommended)

Log a warning but don't reject:

```python
if cascade and parent_id is None:
    logger.warning(
        f"Entity {entity_id} has cascade=True but no parent_id. "
        "Cascade will have no effect."
    )
```

**Pros:**
- Flexible configuration
- Helpful feedback without hard failure
- Users can fix at their own pace

**Cons:**
- May mask configuration errors if warnings are ignored
- Requires logging infrastructure

### Recommendation: Option C

Log a warning for `cascade=True` without `parent_id` to help users catch configuration errors, but don't reject the operation. This balances flexibility with helpful feedback.

---

## Decision Point 6: DynamoDB Cost Mitigation

Moving cascade to entity configuration introduces a **performance concern**: we must now read the entity metadata on every `acquire()` call to determine if cascade is enabled.

### Current Cost Model

| Scenario | DynamoDB Operations |
|----------|---------------------|
| `cascade=False` parameter | N × GetItem (buckets only) |
| `cascade=True` parameter | 1 GetItem (entity) + 2N × GetItem (child + parent buckets) |

### Naive Implementation Cost

| Scenario | DynamoDB Operations |
|----------|---------------------|
| `entity.cascade=False` | **+1 GetItem (entity)** + N × GetItem (buckets) |
| `entity.cascade=True` | 1 GetItem (entity) + 2N × GetItem (buckets) |

**Problem:** Entities with `cascade=False` would pay **+1 DynamoDB read per acquire()** they don't pay today.

At 1,000 rpm: ~43,200 additional reads/day (~$0.11/day at PAY_PER_REQUEST pricing).

### Option A: In-Memory Entity Caching (Recommended)

Cache entity metadata in the `RateLimiter` instance:

```python
class RateLimiter:
    def __init__(self, ..., entity_cache_ttl: float = 60.0):
        self._entity_cache: dict[str, tuple[Entity, float]] = {}
        self._entity_cache_ttl = entity_cache_ttl

    async def _get_entity_cached(self, entity_id: str) -> Entity | None:
        cached = self._entity_cache.get(entity_id)
        now = time.time()
        if cached and (now - cached[1]) < self._entity_cache_ttl:
            return cached[0]

        entity = await self._repository.get_entity(entity_id)
        if entity:
            self._entity_cache[entity_id] = (entity, now)
        return entity
```

**Pros:**
- First call pays the cost, subsequent calls are **free** (in-memory)
- Works for both cascade=True and cascade=False
- Cache TTL allows cascade configuration changes to propagate
- Configurable TTL for different use cases

**Cons:**
- Memory usage grows with number of unique entities
- Stale data possible within TTL window (acceptable for cascade flag)
- Need cache invalidation on entity updates via same limiter instance

**Cost impact:** For typical workloads where the same entities are queried repeatedly:
- Cold start: +1 GetItem per unique entity
- Subsequent calls: 0 additional cost
- Net effect: **negligible** for steady-state workloads

### Option B: Query Entity + Buckets Together

Use DynamoDB Query to fetch entity metadata and buckets in one operation:

```python
# Entity and buckets share the same partition key:
# PK=ENTITY#key-1, SK=#META          → Entity metadata
# PK=ENTITY#key-1, SK=#BUCKET#gpt-4#rpm → Bucket

response = await client.query(
    KeyConditionExpression="PK = :pk",
    ExpressionAttributeValues={":pk": pk_entity(entity_id)},
)
# Filter client-side: separate entity from buckets
```

**Pros:**
- Single DynamoDB operation for entity + all buckets
- Actually **reduces** latency compared to current N+1 GetItems
- Zero additional cost (Query returns multiple items for same RCU cost)

**Cons:**
- Returns all buckets for entity, not just the requested resource
- Requires refactoring bucket fetching logic
- May return more data than needed (all resources, all limits)

**Cost impact:**
- Query cost: ~1 RCU per 4KB of data returned
- Current: N GetItems × 0.5 RCU = N/2 RCUs
- Proposed: 1 Query × ~1-2 RCU (depending on bucket count)
- Net effect: **reduced cost** for entities with multiple limits

### Option C: Denormalize Cascade into Bucket Records

Store cascade flag in each bucket record:

```python
{
    "PK": "ENTITY#key-1",
    "SK": "#BUCKET#gpt-4#rpm",
    "data": { ... },
    "cascade": true,  # Denormalized from entity
    "parent_id": "proj-1"  # Also denormalize for cascade lookup
}
```

**Pros:**
- Zero extra reads - cascade info comes with bucket
- No latency impact

**Cons:**
- Data duplication (must keep in sync)
- Updating cascade requires updating ALL bucket records
- Schema change is invasive
- Higher storage cost per bucket

### Option D: Hybrid Caching + Query Optimization

Combine Options A and B:

1. **Cache entity metadata** with configurable TTL
2. **Use Query** when cache miss to fetch entity + buckets together
3. **Populate cache** from Query result

```python
async def _do_acquire(self, entity_id, resource, ...):
    # Try cache first
    entity = self._entity_cache.get(entity_id)
    buckets = None

    if entity is None:
        # Cache miss: fetch entity + buckets together
        items = await self._query_entity_items(entity_id, resource)
        entity = self._extract_entity(items)
        buckets = self._extract_buckets(items, resource)

        if entity:
            self._entity_cache[entity_id] = (entity, time.time())

    if buckets is None:
        # Cache hit for entity, but still need buckets
        buckets = await self._get_buckets_for_resource(entity_id, resource)

    # Continue with cascade logic using entity.cascade
```

**Pros:**
- Best of both worlds: caching + efficient fetching
- Optimal for both cache hits and misses
- Reduced latency for first call (single round-trip)

**Cons:**
- Most complex implementation
- Requires significant refactoring of bucket fetching

### Recommendation: Option A (with Option D as enhancement)

Start with **Option A (In-Memory Caching)** for simplicity:
- Minimal code changes
- Eliminates cost for steady-state workloads
- Configurable TTL for different use cases

Consider **Option D** as a follow-up optimization if:
- Cold start latency is a concern
- Users frequently query new entities
- Benchmark data shows significant impact

### Cache Invalidation Strategy

When entity is updated via same limiter instance:

```python
async def update_entity(self, entity_id: str, cascade: bool | None = None, ...):
    # ... update in DynamoDB ...

    # Invalidate cache
    self._entity_cache.pop(entity_id, None)
```

Cross-instance invalidation is **not supported** - changes propagate via TTL expiry.

---

## Implementation Plan

### Phase 1: Entity Model Changes

1. Add `cascade: bool = False` field to `Entity` dataclass
2. Update `Entity.as_dict()` serialization
3. Update repository `_deserialize_entity()` to handle missing field
4. Add `cascade` parameter to `create_entity()` and `update_entity()` methods
5. Add warning when `cascade=True` but `parent_id=None`

### Phase 2: Limiter Changes

1. Add `entity_cache_ttl: float = 60.0` parameter to `RateLimiter.__init__()`
2. Implement `_get_entity_cached()` method with TTL-based caching
3. Remove `cascade` parameter from `acquire()` method signature
4. Update `_do_acquire()` to:
   - Call `_get_entity_cached()` to get entity (uses cache if available)
   - Read `entity.cascade` to determine cascade behavior
5. Add cache invalidation in `update_entity()` when cascade is changed
6. Update `SyncRateLimiter` to match async implementation

### Phase 3: CLI and Infrastructure

1. Update CLI to support `--cascade` flag for entity creation
2. Update any CloudFormation template parameters if needed
3. Update Lambda aggregator if it processes cascade (likely no changes needed)

### Phase 4: Documentation and Tests

1. Update docstrings and type hints
2. Update existing tests in `tests/unit/test_limiter.py`
3. Add new tests for entity-level cascade configuration
4. Update README and API documentation
5. Add migration guide for users

### Phase 5: Version and Release

1. Bump `CURRENT_SCHEMA_VERSION` to `"1.1.0"` (minor version for non-breaking schema addition)
2. If removing parameter is breaking, bump major version to `2.0.0`
3. Update CHANGELOG with migration notes

---

## Files to Modify

| File | Changes |
|------|---------|
| `src/zae_limiter/models.py` | Add `cascade` field to Entity |
| `src/zae_limiter/repository.py` | Update create/update/deserialize for cascade |
| `src/zae_limiter/limiter.py` | Remove cascade param, read from entity |
| `src/zae_limiter/schema.py` | No changes needed (field goes in entity item) |
| `src/zae_limiter/lease.py` | No changes needed |
| `src/zae_limiter/cli.py` | Add `--cascade` flag to entity commands |
| `src/zae_limiter/version.py` | Update CURRENT_SCHEMA_VERSION |
| `tests/unit/test_limiter.py` | Update cascade tests |
| `tests/unit/test_repository.py` | Add cascade serialization tests |
| `tests/e2e/test_localstack.py` | Update e2e cascade tests |
| `docs/` | Update API documentation |

---

## Risk Assessment

| Risk | Impact | Mitigation |
|------|--------|------------|
| Breaking change for existing users | High | Clear migration guide, deprecation warnings |
| Missing cascade attribute in old data | Low | Handle missing attribute with default `False` |
| Performance regression (always fetching entity) | **Medium** | In-memory caching with configurable TTL (Decision Point 6) |
| Stale cache data after entity update | Low | TTL expiry (60s default), local invalidation on update |
| Memory growth with many unique entities | Low | LRU eviction policy if needed (future enhancement) |
| Multi-library coordination still needed for stored limits | Medium | Out of scope for this issue |

---

## Open Questions

1. Should we support cascade override per-call for edge cases, or is entity-level configuration sufficient?
2. Should cascade be immutable after entity creation, or allow updates?
3. Do we need audit events for cascade configuration changes?

---

## Summary of Recommendations

| Decision Point | Recommended Option |
|----------------|-------------------|
| Storage location | **Option A**: Simple boolean field on Entity |
| API parameter | **Option A**: Remove parameter entirely |
| Default behavior | **Option A**: Default `cascade=False` |
| Migration strategy | **Option A**: Handle missing field gracefully |
| Validation rules | **Option C**: Warn but allow cascade without parent |
| DynamoDB cost mitigation | **Option A**: In-memory entity caching (TTL-based) |

---

## Alternative Considered: Per-Resource Cascade

Instead of entity-level cascade, cascade could be configured per-resource:

```python
limiter.set_resource_cascade("gpt-4", cascade=True)
```

This was rejected because:
- Adds complexity without clear benefit
- Issue specifically asks for entity-level configuration
- Resource cascade can be added later if needed
