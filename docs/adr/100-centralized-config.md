# ADR-100: Centralized Configuration Access Patterns

**Status:** Accepted
**Date:** 2026-01-18
**Issue:** [#129](https://github.com/zeroae/zae-limiter/issues/129)
**Milestone:** v0.5.0

> **Note (post-v0.5.0):** This is the original index for the centralized-config design. Two sub-decisions have since been superseded — the hierarchy expanded from three to four levels ([ADR-118](118-four-level-config-hierarchy.md) supersedes [ADR-102](102-config-hierarchy.md)) and limit resolution/caching moved to the Repository protocol ([ADR-122](122-resolve-limits-on-repository.md) supersedes [ADR-103](103-config-caching.md)). The original per-limit `#LIMIT#` config keys were replaced by a single composite `#CONFIG` item per level in the v0.8.0 composite-limits migration, applying [ADR-114](114-composite-bucket-items.md)'s composite shape to config records; no ADR of its own records that change. Config items are written full-replace, so [ADR-115](115-add-based-writes-lazy-refill.md)'s ADD-based write paths apply to buckets only. See the updated access patterns below.

## Context

zae-limiter is a distributed rate limiting library where multiple clients must behave consistently. Before v0.5.0:

1. **Limits passed explicitly** - Each `acquire()` call requires limits
2. **No global defaults** - Cannot set system-wide or resource-level default limits
3. **No caching** - `use_stored_limits=True` queries DynamoDB on every call
4. **Scattered config** - Behavior settings are constructor-only, risking inconsistent fail-open/fail-closed behavior

## Decision

> **Maintained index — exemption from [ADR-000](000-adr-format-standard.md).** Beyond the choice to
> centralize configuration at all, this record decides nothing itself: it indexes the sub-decisions
> below, each of which owns one architectural choice. Those records stay immutable once Accepted;
> *this index is maintained*, its rows corrected as sub-decisions are superseded or added.

Implement centralized configuration with these architectural choices:

| Decision | ADR | Summary |
|----------|-----|---------|
| Schema format | [ADR-101](101-flat-schema-config.md) | Flat schema (no nested `data.M`) for atomic counters |
| Config hierarchy | [ADR-102](102-config-hierarchy.md) *(superseded by [ADR-118](118-four-level-config-hierarchy.md))* | Three levels: Entity > Resource > System — later expanded to four |
| Caching strategy | [ADR-103](103-config-caching.md) *(superseded by [ADR-122](122-resolve-limits-on-repository.md))* | 60s TTL with negative caching; resolution later moved to the Repository protocol |
| API behavior | [ADR-104](104-stored-limits-default.md) | Stored limits as default |
| Read consistency | [ADR-105](105-eventual-consistency.md) | Eventually consistent reads |
| Disable flag | [ADR-125](125-resource-disable.md) | Tri-state `disabled` beside limits, resolved by an independent walk over the entity and resource levels |
| Bucket TTL | [ADR-136](136-entity-config-bucket-ttl.md) | The resolved level decides whether a bucket persists or expires |
| Schedules on config | [ADR-135](135-scheduled-limits.md) | Per-limit `sched`/`rsched`, with one `sched_tz` hoisted per item |

## Consequences

**Positive:**
- Consistent behavior across distributed clients
- Caching keeps config reads off the per-request cost path
- Enables per-resource, per-entity, and entity-wide customization
- Clean upgrade path to v0.6.0 full schema migration

**Negative:**
- Config resolution is stale for up to the cache TTL (60s by default; entity-level setters evict eagerly)
- Resource- and system-level changes reach an existing bucket only when its TTL expires, not within the cache TTL ([ADR-136](136-entity-config-bucket-ttl.md))
- Additional complexity in resolution logic
- Breaking change for explicit-limits-only users

## Access Patterns Added

> The original per-limit sort keys (`SK begins_with #LIMIT#`) were replaced by a single `#CONFIG` item per level, namespace-prefixed with `{ns}/`. The entity-level sort key carries the resource, and `_default_` is a legal value of that slot naming the entity-wide level ([ADR-118](118-four-level-config-hierarchy.md)). Both are shown below.

| Pattern | Query (v0.5.0, original) | Current |
|---------|--------------------------|---------|
| Get system config | `PK=SYSTEM#, SK begins_with #LIMIT#` | `PK={ns}/SYSTEM#, SK=#CONFIG` |
| Get resource config | `PK=RESOURCE#{resource}, SK begins_with #LIMIT#` | `PK={ns}/RESOURCE#{resource}, SK=#CONFIG` |
| Get entity config | `PK=ENTITY#{id}, SK begins_with #LIMIT#` | `PK={ns}/ENTITY#{id}, SK=#CONFIG#{resource}` |

## Alternatives Considered

### Explicit limits on every `acquire()` call
Rejected because: distributed clients drift apart, with no central place to change a limit.

### One ADR covering the whole centralized-config design
Rejected because: independent decisions folded into one record cannot be superseded
independently — which ADR-118, ADR-122 and ADR-136 have each since needed to do.

## Implementation

See linked issues for implementation details:

- [#130](https://github.com/zeroae/zae-limiter/issues/130) - Store system/resource config
- [#131](https://github.com/zeroae/zae-limiter/issues/131) - System-level default limits
- [#135](https://github.com/zeroae/zae-limiter/issues/135) - Client-side config cache
- [#180](https://github.com/zeroae/zae-limiter/issues/180) - v0.6.0 full schema flattening
