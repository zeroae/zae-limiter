# ADR-102: Three-Level Configuration Hierarchy

**Status:** Accepted
**Date:** 2026-01-18
**Issues:** [#129](https://github.com/zeroae/zae-limiter/issues/129), [#130](https://github.com/zeroae/zae-limiter/issues/130), [#131](https://github.com/zeroae/zae-limiter/issues/131)

## Context

zae-limiter clients need consistent configuration across distributed instances. Currently:

1. **No global defaults** - Cannot set system-wide default limits that apply to all resources
2. **No resource defaults** - Cannot set per-resource limits without per-entity configuration
3. **Scattered config** - Behavior settings (`on_unavailable`) are constructor-only
4. **Risk of inconsistency** - Different clients may have different fail-open/fail-closed behavior

Operators need to set global defaults while allowing per-resource and per-entity overrides.

## Decision

Implement a **three-level configuration hierarchy** with precedence: Entity > Resource > System > Constructor defaults.

| Level | PK | SK | Purpose |
|-------|----|----|---------|
| System | `SYSTEM#` | `#LIMIT#{limit_name}` | Global defaults for ALL resources |
| System | `SYSTEM#` | `#CONFIG` | Behavior config (`on_unavailable`, etc.) |
| Resource | `RESOURCE#{resource}` | `#LIMIT#{limit_name}` | Per-resource overrides |
| Entity | `ENTITY#{id}` | `#LIMIT#{resource}#{limit_name}` | Per-entity+resource overrides |

**Key distinction:**
- **System limits** apply universally (no resource association)
- **Resource limits** override system defaults for a specific resource
- **Entity limits** override resource/system defaults for a specific entity+resource pair

**Config field scope:**

| Field | System | Resource | Entity |
|-------|--------|----------|--------|
| Limit fields (`capacity`, etc.) | ✅ | ✅ | ✅ |
| `on_unavailable` | ✅ | ❌ | ❌ |
| `auto_update`, `strict_version` | ✅ | ❌ | ❌ |

## Consequences

**Positive:**
- Consistent behavior across distributed clients
- Enables per-resource overrides (expensive model → lower limits)
- Premium users can have different limits via entity config
- Clean separation: system = global, resource = per-model, entity = per-user

**Negative:**
- 3 levels to check per cache miss (mitigated by caching, see ADR-103)
- More complex resolution logic

## Alternatives Considered

### Single-level (System only)
Rejected: No per-resource or per-entity customization; insufficient for real-world use cases.

### Two-level (System + Entity)
Rejected: Resource-level is common (different limits per model); would force entity-level duplication.

### System config keyed by resource (original design)
Rejected: Redundant with resource-level config; system should be truly global defaults.
