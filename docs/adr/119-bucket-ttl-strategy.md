# ADR-119: Bucket TTL Strategy

**Status:** Accepted
**Date:** 2026-02-02
**Issue:** [#271](https://github.com/zeroae/zae-limiter/issues/271), [#296](https://github.com/zeroae/zae-limiter/issues/296)

## Context

Buckets using system or resource default limits should auto-expire to reduce DynamoDB storage costs for ephemeral entities (anonymous users, one-time callers). The original implementation used a flat 24-hour TTL, but this caused slow-refill buckets to expire before fully refilling—a bucket with 1000 capacity and 10 tokens/minute refill takes ~100 minutes to fill, but with infrequent access patterns could expire mid-refill.

Separately, entities with explicitly configured custom limits represent important accounts (premium users, key customers) whose state should persist indefinitely. Losing their bucket state grants an unintended rate limit reset.

## Decision

1. **Time-to-fill based TTL**: Bucket TTL is calculated as `max_time_to_fill × multiplier` where `time_to_fill = (capacity / refill_amount) × refill_period_seconds`. Default multiplier is 7.
2. **TTL by config source**: Buckets using entity custom limits have no TTL (persist indefinitely). Buckets using entity `_default_`, resource, or system defaults have TTL applied.

## Consequences

**Positive:**
- Slow-refill buckets have sufficient time to fully refill before expiring
- Important entities (custom limits) never lose rate limit state
- Ephemeral entities auto-cleanup, reducing storage costs

**Negative:**
- TTL removal/addition on upgrade/downgrade adds complexity to write path

## Alternatives Considered

### Flat 24-hour TTL for all default-limit buckets
Rejected because: Slow-refill limits (capacity >> refill_amount) expire before fully refilling, causing unexpected rate limit resets.

### TTL for all buckets including custom limits
Rejected because: Custom limits signal explicit intent—losing state for premium users is unacceptable.

### 3× or 14× multiplier
Rejected because: 7× balances storage cost (not too long) with refill safety (multiple full cycles). Configurable via `bucket_ttl_refill_multiplier` for edge cases.
