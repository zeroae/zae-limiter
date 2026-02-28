# ADR-125: Quota Enforcement via Entity Config Overrides

**Status:** Proposed
**Date:** 2026-02-14

## Context

With independent tables per region (ADR-123) and S3-based sync (ADR-124), each region's
sync Lambda computes a regional quota for each entity. This quota must be enforced by
the rate limiter's hot path without modifying the acquire flow.

Three enforcement mechanisms were evaluated:

1. **Entity config overrides:** Write adjusted limits via `set_limits()`, picked up by
   the existing config cache on the next resolve.
2. **Shadow counter on bucket item:** Write a `remote_tc` attribute on the bucket and
   add a condition to speculative writes.
3. **Direct token deduction:** `ADD b_rpm_tk -remote_delta` on the bucket item.

The shadow counter approach has a semantic mismatch: `total_consumed_milli` is a lifetime
monotonic counter incompatible with the per-window token bucket model. Direct token
deduction does not adjust the refill ceiling — each region's bucket refills at the full
global rate, causing N regions to provide Nx the intended refill.

## Decision

Regional quotas must be enforced by writing entity-level config overrides via the
existing `set_limits(entity_id, resource, limits)` API. The sync Lambda must compute
the allocated capacity per entity and write it as an entity config record. The rate
limiter's existing config resolution hierarchy (Entity > Resource > System) must be
the sole mechanism for quota enforcement.

## Consequences

**Positive:**
- Zero changes to the acquire hot path (speculative writes, optimistic lock, bucket math)
- Uses the existing config hierarchy; no new DynamoDB schema or access patterns
- Capacity adjustment naturally controls refill ceiling via token bucket math
- Config cache TTL provides built-in staleness tolerance (already accepted in ADR-105)

**Negative:**
- Token drain lag: if current tokens exceed the new reduced capacity, the entity can
  consume excess tokens until they drain naturally (bounded by consumption rate)
- Config writes are the dominant sync cost (~$40/month at 2,000 active entities with
  trigger-based filtering per ADR-126)
- Config cache TTL (default 60s) delays quota enforcement after a config write; the
  sync Lambda and application use separate Repository instances

## Alternatives Considered

### Shadow counter attribute on bucket item (remote_tc)
Rejected because: `total_consumed_milli` is a monotonic lifetime counter that cannot
be compared against a per-window capacity limit, and modifying the speculative write
condition changes the hot path for all users.

### Direct token deduction (ADD tk -remote_delta)
Rejected because: each region's bucket still refills at the full global rate, so N
regions produce Nx total refill — the deduction fights the bucket math without
correcting the underlying refill ceiling.

### In-memory client-side consumption map (no DynamoDB writes)
Rejected because: requires background polling threads and in-memory state, which works
for long-running services but not for Lambda-based rate limiting.
