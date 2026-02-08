# ADR-122: Move Limit Resolution to Repository Protocol

**Status:** Accepted
**Date:** 2026-02-08
**Supersedes:** ADR-103

## Context

ADR-103 introduced client-side config caching as a `ConfigCache` class owned by `RateLimiter`. ADR-108 established the `RepositoryProtocol` for backend abstraction. Currently, `RateLimiter` orchestrates the 4-level hierarchy resolution (ADR-118) by threading repository fetch methods as callbacks into `ConfigCache`. This places data access orchestration logic — "fetch up to 4 keys, return the first hit" — in the business logic layer.

Analysis of how other backends would implement this resolution reveals it is a universal data access pattern: SQL solves it with `UNION ALL + ORDER BY + LIMIT 1` in one query, Redis with a Lua script evaluating multiple keys server-side, Cosmos DB with a stored procedure, and Firestore with `getAll()` + priority scan. Every backend resolves it in one round-trip using native caching (query cache, DAX, built-in TTL, offline persistence). The current design forces all backends to share a Python-side `ConfigCache` that only makes sense for DynamoDB.

## Decision

Add `resolve_limits()` to `RepositoryProtocol`. Each backend implementation must resolve the 4-level config hierarchy (ADR-118) and manage its own caching strategy. `ConfigCache` becomes an internal implementation detail of the DynamoDB `Repository`, not a limiter-owned component. Cache management methods (`invalidate_config_cache()`, `get_cache_stats()`) move to the repository protocol.

`RateLimiter` must not implement config resolution logic or own caching state. It calls `repository.resolve_limits(entity_id, resource)` and receives the effective limits, the `on_unavailable` action, and the config source.

## Consequences

**Positive:**
- Backends use native resolution and caching (SQL query cache, Redis TTL, DAX, Firestore offline persistence)
- `RateLimiter` loses ~100 lines of orchestration code and the `config_cache_ttl` constructor parameter
- New backends implement one method instead of understanding the `ConfigCache` callback protocol
- Caching strategy becomes a backend concern — DynamoDB uses in-memory TTL, Redis needs none

**Negative:**
- Every backend must implement the 4-level hierarchy correctly (testable via protocol conformance tests)
- `config_cache_ttl` moves from `RateLimiter` to `Repository` constructor (breaking change)
- `invalidate_config_cache()` semantics vary by backend (no-op for Redis, meaningful for DynamoDB)

## Alternatives Considered

### CachingRepository decorator (composition pattern)
Rejected because: Forces a Python-side cache on all backends, even those with native caching; adds an abstraction layer that most backends don't need.

### Separate ConfigResolverProtocol
Rejected because: Introduces a second protocol for users to understand; resolution is fundamentally a data access operation that belongs on the repository.

### Keep caching in RateLimiter
Rejected because: Forces every backend to expose 4 individual fetch methods and accept the Python-side caching strategy, preventing use of native backend capabilities.
