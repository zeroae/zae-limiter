# ADR-103: Client-Side Config Caching with TTL

**Status:** Proposed
**Date:** 2026-01-18
**Issue:** [#135](https://github.com/zeroae/zae-limiter/issues/135)

## Context

The three-level config hierarchy (ADR-102) requires fetching 3 DynamoDB items per `acquire()` call. Without caching:

- **Cost:** 1.5 RCU per request (unacceptable at scale)
- **Latency:** BatchGetItem round-trip on every request

Config changes are infrequent (typically during deployment or admin operations), making aggressive caching appropriate.

## Decision

Implement **in-memory TTL caching** per RateLimiter instance with 60-second TTL and **negative caching**.

**Negative caching:** Cache "no entity config exists" to avoid repeated misses for the 95%+ of users without custom limits.

**Cache invalidation:**
- Automatic: TTL expiry (60s)
- Manual: `limiter.invalidate_config_cache()` method

**No distributed invalidation:** Config changes propagate via TTL expiry (max 60s staleness). This avoids infrastructure complexity (SNS/EventBridge) for infrequent operations.

## Consequences

**Positive:**
- High-frequency traffic: 99.98% cache hit rate (100 req/sec × 60s = 6K hits per miss)
- Negligible amortized cost: +0.00025 RCU per request at scale
- Negative caching reduces cost for sparse traffic patterns

**Negative:**
- Max 60s staleness for config changes
- No cross-process invalidation (each instance has independent cache)
- Memory usage scales with unique entity×resource combinations

## Alternatives Considered

### No Caching
Rejected: 1.5 RCU per acquire is unacceptable; poor latency.

### Distributed Cache (Redis/ElastiCache)
Rejected: Adds infrastructure dependency; 60s staleness is acceptable for config.

### DynamoDB Streams for Invalidation
Rejected: Requires Lambda infrastructure; complexity not justified for config updates.
