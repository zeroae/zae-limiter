# ADR-100: Centralized Configuration Access Patterns

**Status:** Proposed
**Date:** 2026-01-18
**Issue:** [#129](https://github.com/zeroae/zae-limiter/issues/129)
**Milestone:** v0.5.0

> **Note (post-v0.5.0):** This is the original index for the centralized-config design. Two sub-decisions have since been superseded — the hierarchy expanded from three to four levels ([ADR-118](118-four-level-config-hierarchy.md) supersedes [ADR-102](102-config-hierarchy.md)) and limit resolution/caching moved to the Repository protocol ([ADR-122](122-resolve-limits-on-repository.md) supersedes [ADR-103](103-config-caching.md)). The original per-limit `#LIMIT#` config keys were replaced by composite `#CONFIG` items ([ADR-114](114-composite-bucket-items.md), [ADR-115](115-add-based-writes-lazy-refill.md)). See the updated access patterns below.

## Context

zae-limiter is a distributed rate limiting library where multiple clients must behave consistently. Currently:

1. **Limits passed explicitly** - Each `acquire()` call requires limits
2. **No global defaults** - Cannot set system-wide or resource-level default limits
3. **No caching** - `use_stored_limits=True` queries DynamoDB on every call
4. **Scattered config** - Behavior settings are constructor-only, risking inconsistent fail-open/fail-closed behavior

## Decision

Implement centralized configuration with these architectural choices:

| Decision | ADR | Summary |
|----------|-----|---------|
| Schema format | [ADR-101](101-flat-schema-config.md) | Flat schema (no nested `data.M`) for atomic counters |
| Config hierarchy | [ADR-102](102-config-hierarchy.md) *(superseded by [ADR-118](118-four-level-config-hierarchy.md))* | Three levels: System > Resource > Entity — later expanded to four |
| Caching strategy | [ADR-103](103-config-caching.md) *(superseded by [ADR-122](122-resolve-limits-on-repository.md))* | 60s TTL with negative caching; resolution later moved to the Repository protocol |
| API behavior | [ADR-104](104-stored-limits-default.md) | Stored limits as default |
| Read consistency | [ADR-105](105-eventual-consistency.md) | Eventually consistent reads |

## Consequences

**Positive:**
- Consistent behavior across distributed clients
- Negligible cost with caching (~0.00025 RCU/request at scale)
- Enables per-resource and per-entity customization
- Clean upgrade path to v0.6.0 full schema migration

**Negative:**
- Max 60s staleness for config changes
- Additional complexity in resolution logic
- Breaking change for explicit-limits-only users

## Access Patterns Added

> The original per-limit sort keys (`SK begins_with #LIMIT#`) were replaced by composite config items ([ADR-114](114-composite-bucket-items.md), [ADR-118](118-four-level-config-hierarchy.md)): a single `#CONFIG` item per level, namespace-prefixed with `{ns}/`. Both are shown below.

| Pattern | Query (v0.5.0, original) | Current |
|---------|--------------------------|---------|
| Get system config | `PK=SYSTEM#, SK begins_with #LIMIT#` | `PK={ns}/SYSTEM#, SK=#CONFIG` |
| Get resource config | `PK=RESOURCE#{resource}, SK begins_with #LIMIT#` | `PK={ns}/RESOURCE#{resource}, SK=#CONFIG` |
| Get entity config | `PK=ENTITY#{id}, SK begins_with #LIMIT#` | `PK={ns}/ENTITY#{id}, SK=#CONFIG#{resource}` |

## Implementation

See linked issues for implementation details:

- [#130](https://github.com/zeroae/zae-limiter/issues/130) - Store system/resource config
- [#131](https://github.com/zeroae/zae-limiter/issues/131) - System-level default limits
- [#135](https://github.com/zeroae/zae-limiter/issues/135) - Client-side config cache
- [#180](https://github.com/zeroae/zae-limiter/issues/180) - v0.6.0 full schema flattening
