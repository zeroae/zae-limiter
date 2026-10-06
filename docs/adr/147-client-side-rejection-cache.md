# ADR-147: Reject from the last bucket state seen, never admit from it

**Status:** Proposed
**Date:** 2026-10-06
**Issue:** [#695](https://github.com/zeroae/zae-limiter/issues/695)
**Related:** [ADR-134](134-random-shard-selection.md), [ADR-146](146-per-resource-cascade-policy.md), [#315](https://github.com/zeroae/zae-limiter/issues/315)
**Design:** `docs/plans/2026-10-06-rejection-cache-design.md`

## Context

Since #315 the project has documented a speculative fast rejection as costing 0 RCU and
0 WCU. It costs 1 WCU: "If a `ConditionExpression` evaluates to false during a
conditional write, DynamoDB still consumes write capacity from the table … (or a minimum
of 1)" ([AWS](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/WorkingWithItems.html#WorkingWithItems.ConditionalWrites.ReturnConsumedCapacity)).
A measurement on real DynamoDB on 2026-10-06 confirmed it: 1,000 failed conditional
writes consumed 1,000 WCU, exactly as many as 1,000 successful ones.

So every 429 costs a write, a cascade rejection costs three, and a client looping on
429s consumes the partition's physical write throughput without debiting the reserved
`wcu` limit that drives write sharding. Meanwhile every speculative response already
returns the full bucket state at no read cost (`ALL_NEW` on success, `ALL_OLD` on
failure), and the client discards it after one decision.

## Decision

`Repository` must keep the last bucket state seen per (namespace, entity, resource,
shard) for at most `rejection_cache_ttl` seconds (default 1.0, `0` disables), and the
limiter must raise `RateLimitExceeded` without a DynamoDB call when that state, projected
to now with the fast path's own refill arithmetic, cannot cover a limit declared in
`consume` on every shard not known to have room; the cache must never admit a request.
For a child whose own bucket shows it cascades on the resource, the same rule must apply
to the parent's shards, and the child itself may be rejected locally only while a
trusted parent state shows the parent is not disabled.

The projection, its exceptions (a passed `vu`, a `wcu`-only shortfall, a differing
`limits=` override, a degraded limiter, a `disabled` stamp), shard selection around
known-short shards, the cascade rules, invalidation, memory bound and sync behaviour
are specified in the design document.

## Consequences

**Positive:**
- Repeat rejections inside the TTL cost 0 WCU and no round trip, and stop consuming
  partition throughput.
- Known-short shards are no longer probed, removing up to two failed writes per acquire.
- A repeat cascade rejection, parent known short, costs 0 WCU instead of 3.
- The cached state is the input the refill-from-cached-state write (#695) and the
  multi-resource fast path (#675) build on.

**Negative:**
- Admission can lag by up to `rejection_cache_ttl` per process when tokens return by a
  route the projection cannot see: another process's refund, an admin raising a limit or
  resetting a quota, an entity re-enabled, or another process doubling the shard count
  after every cached state was taken.
- The cache is per process; N processes each pay one real write per TTL.
- A local rejection reports a projected state, not a fresh image.

## Alternatives Considered

### "Exhausted until T" flags
Rejected: they carry no balance, so they cannot predict a rejection from a success or
drive the refill-from-cached-state write.

### Trust the projection with no age cap
Rejected: a missed refund would under-admit for as long as a quota's period.

### Reject when the drawn shard is known short
Rejected: it rejects while sibling shards may hold tokens, worse than today's probing.

### Read before writing
Rejected: a 429 would cost 0.5 RCU instead of 1 WCU, but every admit would pay the read.

### A shared cross-process cache
Rejected: new infrastructure and a network hop to save one write.
