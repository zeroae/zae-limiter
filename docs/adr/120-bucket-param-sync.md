# ADR-120: Bucket Param Sync

**Status:** Accepted
**Date:** 2026-02-02
**Issue:** [#294](https://github.com/zeroae/zae-limiter/issues/294)

## Context

When entity limits change via `set_limits()`, existing buckets retain stale static parameters (capacity, burst, refill_amount, refill_period) until the bucket is recreated. This causes confusing behavior where operators update limits but the change doesn't take effect until the bucket expires or is manually deleted.

For example, an operator doubles an entity's rate limit from 100 to 200 RPM, but the entity continues to be limited at 100 RPM because the bucket still has the old capacity value.

## Decision

When `set_limits()` updates entity config, existing bucket static parameters (capacity, burst, refill_amount, refill_period) are synchronized via conditional update. The update uses `attribute_exists(PK)` to skip if the bucket doesn't exist yet.

## Consequences

**Positive:**
- Limit changes take effect immediately on existing buckets
- Operators get predictable behavior when updating limits
- No manual intervention required to apply new limits

**Negative:**
- Additional DynamoDB write on `set_limits()` (conditional update)
- Write may fail silently if bucket doesn't exist (by design)

## Alternatives Considered

### Require bucket deletion before limit change
Rejected because: Poor operator experience—requires manual intervention and loses current token state.

### Lazy sync on next acquire
Rejected because: Adds complexity to the hot path (`acquire`) and delays when the change takes effect.
