# ADR-010: Total Consumed Counter for Accurate Delta Tracking

**Status:** Accepted
**Date:** 2026-01-16
**PR:** [#169](https://github.com/zeroae/zae-limiter/pull/169)
**Issue:** [#179](https://github.com/zeroae/zae-limiter/issues/179)
**Milestone:** v0.4.0

## Context

The Lambda aggregator processes DynamoDB Streams to create usage snapshots. Initial implementation derived consumption from token bucket state changes:

```
consumption = old_tokens - new_tokens
```

This fails when token refill rate exceeds consumption rate. Example with 10M TPM limit:
- Refill during 100ms operation: ~16,667 tokens
- Actual consumption: 1,000 tokens
- Calculated delta: -15,667 (wrong!)

The fundamental problem: token bucket state conflates consumption with refill.

## Decision

Add `total_consumed_milli` counter that tracks net consumption independently of refill.

**Implementation:**
- Counter stored as **flat top-level attribute** (not nested in `data.M`)
- Consume: `counter += amount * 1000`
- Release/adjust(negative): `counter -= amount * 1000`
- Delta calculation: `new_counter - old_counter`

**Why flat attribute:** Enables atomic ADD operations in UpdateExpression without "overlapping document paths" error (same reason as ADR-006).

## Consequences

**Positive:**
- Accurate consumption tracking regardless of refill rate
- Works at any scale (tested with 10M+ TPM)
- Simple delta calculation in aggregator
- Net tracking handles both consume and release operations

**Negative:**
- Hybrid schema: most bucket fields nested, counter flat
- Additional attribute in every bucket record
- Existing buckets need migration (counter initialized to 0)

## Alternatives Considered

- **Track consumption in separate record**: Rejected; doubles write operations, transaction complexity
- **Calculate from refill formula**: Rejected; requires knowing exact timing, error-prone
- **Store last_consumed timestamp**: Rejected; doesn't handle variable consumption rates
