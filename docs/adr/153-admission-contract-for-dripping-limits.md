# ADR-153: Admission contract for dripping limits

**Status:** Proposed
**Date:** 2026-11-06
**Issue:** Refs [#808](https://github.com/zeroae/zae-limiter/issues/808), [#587](https://github.com/zeroae/zae-limiter/issues/587), [#731](https://github.com/zeroae/zae-limiter/issues/731)
**Related:** ADR-133, ADR-135, ADR-145, ADR-151, [ADR-154](154-fill-instant-fast-path.md)

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
bound. Changes take effect within the config cache TTL and `stamp_ttl`. The bound is stated in
real time at the instant each write lands: the gap between a writer's clock reading and that
instant, clock skew plus time in flight, adds up to twice the refill rate times the largest gap.

## Decision

For every dripping limit, tokens admitted (by `acquire()` and `lease.consume()`) minus tokens
returned (release, negative adjustment, rollback) on one (entity, resource), summed over its
shards, must not exceed `C + R(w)` in any window `w`, where `C` is the ceiling in force at the
window's start and `R(w)` is the refill the configured schedule grants across `w`. Every path
must hold this bound; the fast path holds it through ADR-154.

## Consequences

**Positive:**
- One testable bound replaces the conflicting readings in #587, ADR-133 and #808; the review's
  sliding-window harness checks it.
- With ADR-154 the bound costs a second write only on a misprediction, so no burst slack is
  needed to keep the fast path.

**Negative:**
- #808 is a defect, and its fix is ADR-154's change of representation.
- A new dripping shard must be funded by moving tokens from the shard whose slot it splits, as
  ADR-145 does for quotas, so it can start nearly empty. This partially supersedes ADR-133's
  1.5x transient for dripping limits and reverses #587's ruling.
- A shard-count raise must close the fast path on every shard it raises until a materialising
  pass re-expresses its share, and the aggregator must not double again from its own doubling's
  record.
- Refill must be integrated across schedule windows, amending ADR-135: a limit raised at a
  boundary accrues from there instead of arriving full.
- `lease.consume()` must debit when called, one write and one round trip per call, instead of
  at exit as #309 designed.
- A lowered resource- or system-level limit must reach active buckets within the config cache
  TTL, which needs the slow path to take capacity and refill from config and a fan-out the TTL
  design (#271, #296) avoided.
- A refund applied twice after an ambiguous write (#731) becomes a defect.
- There is no version gate: every client and Lambda sharing a table must run v0.18.0 or later.

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
