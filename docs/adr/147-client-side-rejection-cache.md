# ADR-147: Reject from the last bucket state seen, never admit from it

**Status:** Accepted
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
`consume` on every shard not known to have room. The cache must never be the basis of an
admission: it may only reject, or steer which shard a conditional write targets; every
admission is decided by DynamoDB exactly as without the cache. A steered draw is still a
uniform random draw, inside `select_shard()`, over the shards not known short (ADR-134), and
the slow path is still handed the shard actually written (ADR-133). The cache is an optional
part of a backend: the limiter looks for it by attribute (`_rejection_cache`), and a backend
without one never rejects locally. For a child whose
own bucket shows it cascades on the resource, the same rule must apply to the parent's
shards, and the child itself may be rejected locally only while a trusted parent state
shows the parent is not disabled.

The projection, its exceptions (a passed `vu`, a `wcu`-only shortfall, a differing
`limits=` override, a degraded limiter, a `disabled` stamp), shard selection around
known-short shards, the cascade rules, invalidation, memory bound and sync behaviour
are specified in the design document.

## Consequences

**Positive:**
- Repeat rejections inside the TTL cost 0 WCU and no round trip, and stop consuming
  partition throughput.
- Known-short shards are no longer probed, removing up to two failed writes per acquire.
- A repeat cascade rejection, parent known short, costs 0 WCU instead of 3 — when the child's
  own cached state shows it cascades and every cached parent shard is short. Measured, that is
  rare at mild over-demand (none of ~600 rejections at 2x) and common at heavy (177 of 1,140
  at 20x).
- The cached state is the input the multi-resource fast path (#675) builds on.

**Negative:**
- Admission can lag by up to `rejection_cache_ttl` per process when tokens return by a
  route the projection cannot see: another process's refund, an admin raising a limit or
  resetting a quota, an entity re-enabled, or another process doubling the shard count
  after every cached state was taken.
- The cache is per process; N processes each pay one real write per TTL. When each
  process sees a hot entity less often than once per TTL, it saves nothing.
- A local rejection reports a projected state, not a fresh image.
- A resource or parent disabled by another process can surface here as a 429
  (`RateLimitExceeded`) instead of ADR-125's 403 (`ResourceDisabled`) for up to
  `rejection_cache_ttl`. It is never an admission, so the kill switch holds.
- A client that retries a 429 immediately is no longer slowed by a DynamoDB round trip
  per retry, so it can spin on CPU (measured: 509 requests in 3 s on v0.15.1, ~47,000
  with the cache; same admissions, far fewer writes). Callers should honour
  `retry_after_seconds`.

## Alternatives Considered

### "Exhausted until T" flags
Rejected: they carry no balance, so they cannot predict a rejection from a success.

### Admit through one write built from the cached state ("phase 3")
Merged in #700 and withdrawn before release. When the cached state showed refill would
cover a request, the client sent the slow path's rf-locked write computed from that state,
with condition terms meant to catch any change since. Ten over-admissions were reproduced,
each from a writer or input the condition did not pin (a cascade stamp, a credit, a shard
doubling, a resource-level schedule, an entity created later under a parent, a missed
disable stamp, a debit smaller than the refill, a limit cut, a declared limit consumed at
0, a pre-#684 stamp); its saving over local rejection alone was narrow (~2x over-demand,
sharded entities), and it cost 9% more than v0.15.1 with 50 processes. The record, and
what a new proposal must show, is `docs/plans/2026-10-07-adr147-phase3-withdrawn.md`.

### Trust the projection with no age cap
Rejected: a missed refund would under-admit for as long as a quota's period.

### Reject when the drawn shard is known short
Rejected: it rejects while sibling shards may hold tokens, worse than today's probing.

### Read before writing
Rejected: a 429 would cost 0.5 RCU instead of 1 WCU, but every admit would pay the read.

### A shared cross-process cache
Rejected: new infrastructure and a network hop to save one write.
