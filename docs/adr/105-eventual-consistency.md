# ADR-105: Eventually Consistent Reads for Config

**Status:** Accepted
**Date:** 2026-01-18
**Issue:** [#129](https://github.com/zeroae/zae-limiter/issues/129)

## Context

DynamoDB offers two read consistency modes:

| Consistency | Cost | Typical Latency |
|-------------|------|-----------------|
| Strongly consistent | 1 RCU / 4KB | Higher |
| Eventually consistent | 0.5 RCU / 4KB | Lower |

Config reads fetch 3 items per cache miss (System, Resource, Entity). With caching (ADR-103), we already accept 60s staleness for config changes.

## Decision

Use **eventually consistent reads** for all config fetches.

**Rationale:** Since the caching layer accepts 60s staleness, sub-second DynamoDB eventual consistency is negligible. This reduces config fetch cost from 3 RCU to 1.5 RCU per cache miss.

## Consequences

**Positive:**
- 50% RCU cost reduction for config reads
- Lower latency (eventually consistent reads are faster)
- Aligned with caching semantics (staleness already accepted)

**Negative:**
- Theoretical sub-second staleness on config reads (negligible given 60s cache TTL)

## Alternatives Considered

### Strongly Consistent Reads
Rejected: 2x cost for no practical benefit; caching already introduces 60s staleness window.

### Mixed Consistency (strong for entity, eventual for system/resource)
Rejected: Adds complexity; consistency should be uniform across config levels.
