# ADR-002: Integer Arithmetic with Millitokens

**Status:** Accepted
**Date:** 2026-01-09
**Commit:** [3902c8c](https://github.com/zeroae/zae-limiter/commit/3902c8c)
**Milestone:** v0.1.0

## Context

Token bucket algorithms require tracking fractional tokens during refill calculations. In distributed systems, floating-point arithmetic can cause precision issues:

- IEEE 754 floating-point has representation errors (0.1 + 0.2 ≠ 0.3)
- Different clients/languages may have slightly different FP implementations
- Accumulated rounding errors cause bucket drift over time
- DynamoDB stores numbers as strings, adding serialization concerns

## Decision

Store all token values as **millitokens** (×1000) using integer arithmetic.

**Implementation:**
- Internal storage: `tokens_milli` (integer)
- User-facing API: `tokens` (float, converted at boundary)
- Refill stored as fraction: `refill_amount` / `refill_period_seconds`
- All bucket math uses integer operations

## Consequences

**Positive:**
- Exact arithmetic: no precision loss across any number of operations
- Deterministic: same calculation yields identical results everywhere
- Simple debugging: values are exact integers
- DynamoDB-friendly: integers serialize cleanly

**Negative:**
- API boundary conversion required (minor complexity)
- Sub-millitoken precision not supported (acceptable for rate limiting)
- Developers must remember internal representation when debugging

## Alternatives Considered

- **Decimal type**: Rejected; Python's Decimal doesn't map cleanly to DynamoDB Number type
- **Floating-point with rounding**: Rejected; accumulated errors in long-running buckets
- **Fixed-point with higher precision (microtokens)**: Rejected; millitokens sufficient for rate limiting use cases
