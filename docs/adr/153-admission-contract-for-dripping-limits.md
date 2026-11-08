# ADR-153: Admission contract for dripping limits

**Status:** Proposed
**Date:** 2026-11-06
**Issue:** Refs [#808](https://github.com/zeroae/zae-limiter/issues/808), [#587](https://github.com/zeroae/zae-limiter/issues/587), [#731](https://github.com/zeroae/zae-limiter/issues/731)
**Partially supersedes:** [ADR-133](133-client-shard-creation.md) for dripping limits (a new shard is funded by moving tokens, and the 1.5x doubling transient becomes a defect)
**Amends:** [ADR-135](135-scheduled-limits.md) (refill is integrated across schedule windows), [ADR-136](136-entity-config-bucket-ttl.md) (resource- and system-level changes reach active buckets within the config cache TTL), [ADR-148](148-multi-resource-acquire.md) (`lease.consume()` debits when called)
**Related:** [ADR-100](100-centralized-config.md), [ADR-125](125-resource-disable.md), [ADR-141](141-reset-after-version-gate.md), [ADR-145](145-sharded-quota-conserves-allowance.md), [ADR-151](151-non-enforcing-limits.md), [ADR-154](154-fill-instant-fast-path.md), [ADR-155](155-fill-instant-writers.md)

## Context

The project describes `acquire()` as never over-admitting, but no record says against what
bound, and each over-admission found so far was judged against a different one. #587 ruled
that a new dripping shard starting full is not a violation because the long-run rate is
unchanged; ADR-133 accepts a one-time 1.5x transient when a shard count doubles; #808 treats
any burst above capacity as a defect. A review on 2026-11-06 reproduced, on moto with the real
limiter and aggregator: bursts near 2x capacity after a bucket idles to full (#808); 1252 and
1402 admitted against 1000 across one doubling, and 1902 when the aggregator doubles twice from
one stale stream image; 1000 against 117 across a schedule boundary that raises the rate; 18
against 10 from two leases calling `consume()` on their own snapshots; and, from reading the
code, a lowered resource- or system-level limit never reaching a bucket used at least once per
TTL period.

A long-run rate, or a cumulative bound since the bucket was created, lets an entity bank idle
refill without limit: a day idle would permit a day's refill in one burst. Neither bounds a
burst, which is what a rate limiter is for. The strict token bucket bounds every window.
Today's fast path cannot meet it, and no single DynamoDB write can for every state; ADR-154
shows that one write suffices whenever the client knows whether the bucket has refilled to
full, and two when no other consumer writes in between.

Quotas keep ADR-145's per-period conservation (I8). Soft and bypassed limits (ADR-151),
`on_unavailable=ALLOW` and degraded leases are non-enforcing by design and are outside the
bound. An operator reset or top-up (ADR-149) adds what it grants. Configuration and stamp
changes take effect within the config cache TTL and `stamp_ttl`. The bound assumes every
process sharing the table runs the release that ships it.

## Decision

For every dripping limit outside the exclusions above, tokens debited (by `acquire()`,
`lease.consume()` and positive adjustments) minus tokens returned (release, negative
adjustment, rollback) on one (entity, resource), summed over its shards and counted from the
start of any real-time window `w` to its last admission, must not exceed `C + R(w) + 2 r s`.
Here `C` is the largest ceiling in force from `s` before the window's start to its start,
`R(w)` the refill the configured schedule grants across `w`, `r` the largest refill rate in
force across it, and `s` the largest gap between a writer's clock reading and the instant its
write lands (clock skew plus time in flight); the fast path holds the bound through ADR-154 and
every other writer through ADR-155.

## Consequences

**Positive:**
- One testable bound replaces the conflicting readings in #587, ADR-133 and #808; the review's
  sliding-window harness checks it.
- With ADR-154 the bound costs a second write only on a misprediction, so no burst slack is
  needed to keep the fast path.

**Negative:**
- #808 is a defect, and its fix is ADR-154's change of representation.
- A new shard of an existing dripping bucket must be funded by moving tokens from the shard it
  splits off (ADR-155), so it can start nearly empty; only a bucket's first shard starts full.
  This partially supersedes ADR-133 and reverses #587's ruling for dripping limits.
- A shard-count raise must re-express every shard it raises before the fast path spends from
  it, and the aggregator must not double again from its own doubling's record.
- Refill must be integrated across schedule windows, amending ADR-135: a limit raised at a
  boundary accrues from there instead of arriving full.
- `lease.consume()` must debit when called, one write and one round trip per call, and can
  raise `RateLimitExceeded` (contended included) inside the caller's block. That departs from
  #309's design and from ADR-148's promise that the single-resource methods keep their meaning.
- A lowered resource- or system-level limit must reach active buckets within the config cache
  TTL. That needs the slow path to take capacity and refill from config, and a fan-out ADR-136
  rejected as O(all buckets); it replaces ADR-135's and ADR-100's statement that such changes
  arrive at TTL expiry, and a namespace-wide fan-out is the decision ADR-125 deferred.
- A refund applied twice after an ambiguous write (#731) becomes a defect.
- Clock skew reaches the ceiling as well as the refill: a write stamped before a boundary that
  lowers capacity can land after it and spend the old ceiling before the re-expression trims
  it, so the bound reads `C` over the `s` before the window. Read at the window's start alone,
  an independent model of ADR-154 and ADR-155 exceeded it by 3 to 54 times the `2 r s` term.
- There is no version gate. The release ships with no deployments to protect, so ADR-141's
  `client_min_version` ratchet is not raised, and every client and Lambda sharing a table must
  run v0.18.0 or later.

## Alternatives Considered

### Long-run rate (#587's reading)
Rejected because: it permits banking idle refill without bound, so it does not limit bursts.

### Cumulative bound since the bucket was created
Rejected because: it permits the same banking, so a day idle allows a day's refill at once.

### A configurable burst slack, defaulting to today's fast path (up to 2x capacity)
Rejected because: ADR-154 makes the strict bound cost only a retry on a misprediction, and a
slack would hide the other defects inside it (1252 against 1000 fits within 2x).

### Gate today's fast path on the instant the bucket could be full
Rejected because: a caller below its limit refills to full between requests, so it would take
the slow path on every request.

### Disable speculative writes for callers who need strict bursts
Rejected because: it turns off write sharding (only fast-path writes drain the per-shard write
budget that triggers a doubling), and leaves the defects that do not involve the fast path.
