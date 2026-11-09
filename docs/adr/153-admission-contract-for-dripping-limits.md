# ADR-153: Admission contract for dripping limits

**Status:** Proposed
**Date:** 2026-11-06
**Issue:** Refs [#808](https://github.com/zeroae/zae-limiter/issues/808), [#587](https://github.com/zeroae/zae-limiter/issues/587), [#731](https://github.com/zeroae/zae-limiter/issues/731)
**Partially supersedes:** [ADR-133](133-client-shard-creation.md) for dripping limits (a new shard is funded by moving tokens, and the 1.5x doubling transient becomes a defect)
**Amends:** [ADR-135](135-scheduled-limits.md) (refill is integrated across schedule windows; a resource- or system-level change, a schedule change included, fans out to active buckets instead of waiting for their TTL to expire; the slow path takes a dripping limit's capacity, refill and schedule from the item, a quota's schedules and window length from config), [ADR-136](136-entity-config-bucket-ttl.md) (resource- and system-level changes fan out to active buckets, so TTL expiry is no longer how they arrive), [ADR-148](148-multi-resource-acquire.md) (`lease.consume()` debits when called)
**Related:** [ADR-100](100-centralized-config.md), [ADR-125](125-resource-disable.md), [ADR-141](141-reset-after-version-gate.md), [ADR-145](145-sharded-quota-conserves-allowance.md), [ADR-151](151-non-enforcing-limits.md), [ADR-154](154-fill-instant-fast-path.md), [ADR-155](155-fill-instant-writers.md), [ADR-156](156-dripping-shard-raises.md)
**Design:** [Fill-instant proofs](../contributing/fill-instant-proofs.md)

## Context

The project describes `acquire()` as never over-admitting, but no record says against what
bound, and each over-admission so far was judged against a different one: #587 ruled a new
dripping shard starting full acceptable because the long-run rate is unchanged, ADR-133 accepts
a 1.5x transient when a shard count doubles, and #808 treats any burst above capacity as a
defect. A review on 2026-11-06 reproduced, on moto with the real limiter and aggregator, bursts
near 2x capacity after a bucket idles to full (#808); 1252, 1402 and 1902 admitted against 1000
across shard doublings; 1000 against 117 across a schedule boundary that raises the rate; and
18 against 10 from two leases calling `consume()`. Reading the code showed a lowered resource-
or system-level limit never reaching a bucket used at least once per TTL period.

A long-run rate, or a cumulative bound since the bucket was created, lets an entity bank idle
refill without limit, so neither bounds a burst; the strict token bucket bounds every window.
No single DynamoDB write meets it for every state, but ADR-154 shows one write suffices when
the client knows whether the bucket has refilled to full, and two when no one else writes in
between.

Quotas keep ADR-145's per-period conservation (I8). Soft and bypassed limits (ADR-151),
`on_unavailable=ALLOW` and degraded leases are non-enforcing by design and are outside the
bound, as is the reserved `wcu` budget, which belongs to each partition (ADR-156). An operator
reset or top-up (ADR-149) adds what it grants. Configuration and stamp changes take effect
within the config cache TTL and `stamp_ttl`, except on a bucket a change's fan-out missed
(Consequences). The bound assumes every process sharing the table runs the release that ships it.

## Decision

For every dripping limit outside the exclusions above, tokens debited (by `acquire()`,
`lease.consume()` and positive adjustments) minus tokens returned (release, negative
adjustment, rollback) on one (entity, resource), summed over its shards and counted from the
start of any real-time window `w` to its last admission, must not exceed `C + R(w) + 2 r s`
plus what operator resets and top-ups grant in `w`, plus the debt positive adjustments leave on
shards other than the one that admits last (zero for one shard). Here `C` is the largest ceiling in force
from `s` before the window's start to its start, `R(w)` the refill the configured schedule
grants across `w`, `r` the largest refill rate in force across it, and `s` the largest gap
between a writer's clock reading and the instant its write lands (clock skew plus time in
flight, including the retries of an admission, which keep its reading); the fast path holds the
bound through ADR-154, every other writer through ADR-155, and shard raises through ADR-156.

## Consequences

**Positive:**
- One testable bound replaces the conflicting readings in #587, ADR-133 and #808.
- With ADR-154 the bound costs a second write only on a misprediction, so no burst slack is
  needed to keep the fast path.

**Negative:**
- #808 is a defect, and its fix is ADR-154's change of representation.
- A new shard of an existing dripping bucket is funded by moving tokens from the shard it splits
  off, and a missing one is created empty (ADR-156); only a shard 0 created when no other shard
  of its bucket is known or found, or a limit added after its item existed, starts full. This
  partially supersedes ADR-133 and reverses #587's ruling for dripping limits.
- Refill is integrated across schedule windows (amending ADR-135): a limit raised at a boundary
  accrues from there instead of arriving full.
- `lease.consume()` debits when called, admitted as `acquire()` admits a request (one write and
  one round trip when predicted), and can raise `RateLimitExceeded` (contended included) inside
  the caller's block, departing from #309 and from ADR-148's promise that the single-resource
  methods keep their meaning.
- A lowered resource- or system-level limit must reach active buckets: a fan-out ADR-136
  rejected as O(all buckets) replaces ADR-135's and ADR-100's arrival at TTL expiry; a
  namespace-wide fan-out is the decision ADR-125 deferred. The slow path does not take a
  dripping limit's capacity, refill or schedule from config: from a cached config it wrote an
  older change back over a newer one, and a fill instant expressed under a schedule other than
  its item's reads as full or as a vast debt (#841). A bucket created from configuration cached
  before a change, and missed by both of its discovery passes, keeps the old parameters, read
  consistently, until the level changes again or its TTL expires: an accepted residual.
- Clock skew reaches the ceiling too: a write stamped before a boundary that lowers capacity can
  land after it and spend the old ceiling, which is why `C` covers the `s` before the window.
- Each shard is its own bucket, so a positive adjustment's debt holds back only its shard while
  the others keep admitting, as before fill instants; spreading it would write every shard.
- A refund applied twice after an ambiguous write (#731) becomes a defect.
- There is no version gate. The release ships with no deployments to protect, so ADR-141's
  ratchet is not raised, and every client and Lambda sharing a table must run v0.18.0 or later.

## Alternatives Considered

### A long-run rate (#587's reading) or a cumulative bound since creation
Rejected because: either permits banking idle refill without bound, so neither limits bursts.

### A configurable burst slack, defaulting to today's fast path (up to 2x capacity)
Rejected because: ADR-154 makes the strict bound cost only a retry on a misprediction, and a
slack would hide the other defects inside it (1252 against 1000 fits within 2x).

### Gate today's fast path on the instant the bucket could be full
Rejected because: a caller below its limit refills to full between requests, so it would take
the slow path on every request.

### Disable speculative writes for callers who need strict bursts
Rejected because: it turns off write sharding (only fast-path writes drain the per-shard write
budget that triggers a doubling), and leaves the defects that do not involve the fast path.
