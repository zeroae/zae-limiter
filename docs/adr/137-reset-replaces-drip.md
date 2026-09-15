# ADR-137: A limit drips or resets, never both

**Status:** Accepted
**Date:** 2026-09-15
**Issue:** [#222](https://github.com/zeroae/zae-limiter/issues/222)

## Context

The token bucket refills continuously and in proportion to elapsed time, so any positive
refill rate returns tokens constantly. `reset_schedule` (design §3.6) instead sets the balance
to the effective capacity at a calendar instant. Both mechanisms are refill; they were
specified independently and their interaction was never stated.

With both active on one limit, a caller obtains roughly twice the intended allowance in a
period: the reset grants a full balance, the caller spends it, and the drip then returns the
same allowance again over the remainder of the period. The reset is not at fault — it is a
set, not an add, so repeated or missed edges apply once. The surplus comes from the drip
running underneath it.

The two express different products. A drip expresses a **rate**: "no more than N per minute,
smoothed, and you recover gradually." A reset expresses an **allowance**: "N per calendar
period, and when it is gone you wait." The motivating case is a paid plan with session and
weekly caps, where exhausting a cap stops the caller until the reset rather than returning a
trickle. Configuration cannot currently express the second, because `Limit` requires
`refill_amount` to be positive, and lengthening the refill period only makes the drip slower,
never absent.

## Decision

A `refill_amount` of zero is valid, and means the limit does not drip; it is accepted only
when `reset_schedule` is non-empty, and rejected otherwise. A limit therefore recovers by drip
or by reset edge, never by both and never by neither.

## Consequences

**Positive:**
- Calendar allowances become expressible, which they were not before at any configuration.
- "No drip" is stated in the configuration rather than inferred from the presence of a reset,
  so a reader of `refill_amount` is never misled by a value that silently does nothing.
- Pairing the two in validation makes a bucket that can never recover unconstructible, and
  fails at construction rather than at the first exhausted acquire.
- Widening `> 0` to `>= 0` is additive: every existing caller passes a positive value and is
  unaffected, and no limit without a `reset_schedule` changes behaviour.
- Satisfies the project requirement that one bucket carry exactly one dynamic rate-limit
  mechanism.

**Negative:**
- `Limit` gains a cross-field validation rule, so the two fields can no longer be reasoned
  about independently, and the error message must explain the pairing rather than the field.
- Bucket TTL derives from time-to-fill, which divides by the refill amount and is therefore
  undefined at zero. ADR-136 confines the exposure: a bucket resolving its limits from entity
  configuration carries no TTL at all, so an entity-level reset limit is unaffected. Only
  resource- and system-level reset limits reach the formula, and #222 must give them an expiry
  that does not divide by the rate. ADR-136's statement that the time-to-fill formula is
  unchanged remains true for every limit that drips.
- The honest "retry after" for a reset-only limit is the next reset instant, not a rate
  computation. Until that lands, such a limit has no rate to fall back on at all.
- Declarative manifests must carry an explicit zero rather than omitting the field, since the
  shorthand defaults `refill_amount` to `capacity`.

## Alternatives Considered

### Keep the positive-rate rule and silently ignore the stored rate when a reset is present
Rejected because: it leaves a configured number that does nothing, which an operator reading
the limit will reasonably expect to apply.

### Let both mechanisms run and document the interaction
Rejected because: the obvious configuration silently grants double the intended allowance, and
nothing in the system would warn the operator.

### Subtract the drip's contribution at each reset
Rejected because: it makes the balance depend on consumption history rather than on the
configured allowance, and it cannot be computed from the bucket item alone.
