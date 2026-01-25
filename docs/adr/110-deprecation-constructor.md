# ADR-110: Deprecation Strategy for RateLimiter Constructor

**Status:** Accepted
**Date:** 2026-01-19
**Issue:** [#150](https://github.com/zeroae/zae-limiter/issues/150)
**Milestone:** v0.5.0

## Context

The `RateLimiter` constructor conflates business logic (`on_unavailable`) with data access configuration (`name`, `region`, `endpoint_url`, `stack_options`). Per ADR-108, Repository should own data access and infrastructure, while RateLimiter owns only business logic. This requires deprecating the old constructor parameters.

## Decision

Deprecate `name`, `region`, `endpoint_url`, and `stack_options` parameters on `RateLimiter` and `SyncRateLimiter`. Introduce a `repository` parameter accepting `RepositoryProtocol`.

**Deprecation rules:**
1. Old parameters emit `DeprecationWarning` with `stacklevel=2`
2. Passing both `repository` and `name` raises `ValueError`
3. When neither provided, default to `Repository(name="limiter")` for backward compatibility
4. Applies to both `RateLimiter` and `SyncRateLimiter`

**Timeline:**

| Version | Behavior |
|---------|----------|
| v0.4.x | Only old signature |
| v0.5.0 | Both work; old emits warning |
| v0.6.0–v1.x | Warning remains |
| v2.0.0 | Old parameters removed |

See [#150](https://github.com/zeroae/zae-limiter/issues/150) for migration guide and examples.

## Consequences

**Positive:**
- Clear separation of concerns
- Enables mock repositories for testing
- Gradual migration with 1+ major version cycle

**Negative:**
- More verbose construction
- Users must migrate before v2.0.0
- Docs must cover both patterns during transition

## Alternatives Considered

### Keep Parameters, Add Repository as Optional
Rejected: Creates ambiguity; doesn't achieve separation goal.

### Remove Immediately (Breaking Change)
Rejected: Violates semver; users face immediate breakage.

### Factory Method (`RateLimiter.from_repository()`)
Rejected: Two "right" ways; cleaner to have single constructor with deprecation.
