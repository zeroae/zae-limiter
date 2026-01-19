# ADR-102: Three-Level Configuration Hierarchy

**Status:** Proposed
**Date:** 2026-01-18
**Issue:** [#129](https://github.com/zeroae/zae-limiter/issues/129)

## Context

zae-limiter clients need consistent configuration across distributed instances. Currently:

1. **No global defaults** - Cannot set system-wide or resource-level default limits
2. **Scattered config** - Behavior settings (`on_unavailable`) are constructor-only
3. **Risk of inconsistency** - Different clients may have different fail-open/fail-closed behavior

Operators need to set defaults while allowing per-resource and per-entity overrides.

## Decision

Implement a **three-level configuration hierarchy** with precedence: Entity > Resource > System > Constructor defaults.

| Level | PK | SK | Purpose |
|-------|----|----|---------|
| System | `SYSTEM#` | `#LIMIT#{resource}#{limit_name}` | Global defaults |
| Resource | `RESOURCE#{resource}` | `#LIMIT#{resource}#{limit_name}` | Resource-specific |
| Entity | `ENTITY#{id}` | `#LIMIT#{resource}#{limit_name}` | Entity overrides |

**Config field scope:**

| Field | System | Resource | Entity |
|-------|--------|----------|--------|
| Limit fields (`capacity`, etc.) | ✅ | ✅ | ✅ |
| `on_unavailable` | ✅ | ✅ | ✅ |
| `auto_update`, `strict_version` | ✅ | ❌ | ❌ |

## Consequences

**Positive:**
- Consistent behavior across distributed clients
- Enables per-resource failure policies (expensive model → block, cheap → allow)
- Premium users can have different limits and failure behavior
- Zero additional cost: config fetched at all levels anyway

**Negative:**
- 3 items fetched per cache miss (mitigated by caching, see ADR-103)
- More complex resolution logic

## Alternatives Considered

### Single-level (System only)
Rejected: No per-resource or per-entity customization; insufficient for real-world use cases.

### Two-level (System + Entity)
Rejected: Resource-level is common (different limits per model); would force entity-level duplication.
