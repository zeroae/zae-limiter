# ADR-100: Centralized Configuration Access Patterns

**Status:** Proposed
**Date:** 2026-01-18
**Issue:** [#129](https://github.com/zeroae/zae-limiter/issues/129)
**Milestone:** v0.5.0

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
| Config hierarchy | [ADR-102](102-config-hierarchy.md) | Three levels: System > Resource > Entity |
| Caching strategy | [ADR-103](103-config-caching.md) | 60s TTL with negative caching |
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

| Pattern | Query | Index |
|---------|-------|-------|
| Get system config | `PK=SYSTEM#, SK begins_with #LIMIT#` | Primary |
| Get resource config | `PK=RESOURCE#{resource}, SK begins_with #LIMIT#` | Primary |
| Get entity config | `PK=ENTITY#{id}, SK begins_with #LIMIT#` | Primary |

## Implementation

See linked issues for implementation details:

- [#130](https://github.com/zeroae/zae-limiter/issues/130) - Store system/resource config
- [#131](https://github.com/zeroae/zae-limiter/issues/131) - System-level default limits
- [#135](https://github.com/zeroae/zae-limiter/issues/135) - Client-side config cache
- [#180](https://github.com/zeroae/zae-limiter/issues/180) - v0.6.0 full schema flattening
