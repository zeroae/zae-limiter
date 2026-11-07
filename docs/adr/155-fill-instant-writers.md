# ADR-155: Every write of a fill instant is exact

**Status:** Proposed
**Date:** 2026-11-07
**Issue:** Refs [#808](https://github.com/zeroae/zae-limiter/issues/808)
**Partially supersedes:** [ADR-120](120-bucket-param-sync.md) for dripping limits (a parameter sync is a read and a pinned write, not a blind update), [ADR-133](133-client-shard-creation.md) for dripping limits (a doubling creates every new shard at once, funded from its parent)
**Amends:** [ADR-149](149-reset-and-top-up.md) (a dripping limit's reset and top-up are writes exact on every item, outside the `rf` lock)
**Related:** [ADR-135](135-scheduled-limits.md), [ADR-145](145-sharded-quota-conserves-allowance.md), [ADR-150](150-move-entity-to-new-parent.md), [ADR-153](153-admission-contract-for-dripping-limits.md), [ADR-154](154-fill-instant-fast-path.md)
**Design:** [Fill-instant proofs](../contributing/fill-instant-proofs.md) (§8)

## Context

ADR-154 decides the fast path's two writes on a dripping limit's fill instant `z`. It leaves
every other writer of `z` open, and three conflict as they stand. ADR-120's parameter sync
overwrites capacity and rate with a blind update, but `z` only means something against the
parameters it was expressed under. ADR-149's reset and top-up write deltas under an `rf` lock
that the fast path's full write, a plain `SET`, no longer respects. ADR-133's shard creation
and the aggregator's clone give a new shard a full share or a copy of shard 0's `z`, creating
tokens. ADR-153 also needs refill integrated across schedule windows, which needs to know which
parameters each fill instant was last expressed against.

ADR-153's bound holds on a shard for any write that is an exact strict step on the item it
lands on, and DynamoDB offers three ways to be exact: a write whose condition proves its case
(ADR-154's two writes); a write exact on every item it can land on, since adding to `z` is
exactly a return, a positive adjustment or a top-up (capped at capacity by construction,
because every `z` before now reads as full) and setting `z` to the write's clock reading is
exactly a reset to full; and a write computed from a read and pinned on what it read, which
applies only to the item it was computed from. Each is exact only against the parameters it was
computed with, so each must also pin them.

## Decision

Every write of a dripping limit's fill instant must be pinned on the parameters, schedule and
shard count it was computed against, with `vu` after its clock reading, and must take one of
three forms: one of ADR-154's two conditional writes; a write exact on every item it can land
on (adding to the fill instant for a return, adjustment or top-up, or setting it to the write's
clock reading for a reset); or a write computed from a read and pinned on the fill instants it
read. A write that changes those parameters, schedule or shard count, or that lands after a
boundary `vu` marks, must re-express the item's fill instants in that same write and record the
instant it did so.

## Consequences

**Positive:**
- ADR-153's bound holds for every writer, not only the fast path (proof §8).
- Returns, adjustments, resets and top-ups need no read and no lock, and a return or top-up is
  capped at capacity by construction, which is ADR-149's headroom rule for free.
- Refill across schedule windows is exact: the first pass after a boundary re-expresses each
  fill instant at every boundary since its recorded instant (ADR-153, ADR-135).
- No share is created from nothing: a doubling moves each parent's balance above its new share
  to the shard it splits off, and the total balance is conserved exactly.

**Negative:**
- A parameter sync, and ADR-153's resource- and system-level fan-out, become a read and a
  pinned write per bucket, retried when a fast write lands in between.
- A shard-count raise reads every shard and, in one transaction pinned on what it read,
  re-expresses each at the new count and creates each new shard holding its parent's balance
  above the parent's new share. It writes as many items as the new count (at most 32, plus
  ADR-150's owner check); quotas on those items are planned as ADR-145 plans them.
- The aggregator neither refills nor clones a dripping limit, and doubles a bucket that holds
  one only through the same transaction.
- A credit computed before a raise or a boundary fails its pin and is resent as a read and a
  pinned write.
- The slow path sends ADR-154's writes from the image it read. The consumption-only retry, the
  `rf`-locked refill and the separate #633 seed go away for dripping limits; a limit configured
  after its item existed starts at a full share, as a new bucket does.
- A bucket's first shard is created full, in a transaction with ADR-150's owner check.
- Quotas keep ADR-149's `rf`-locked deltas, so a reset or top-up on an item carrying both
  writes both forms in one transaction.
- The item gains one attribute: the instant its fill instants were last expressed.

## Alternatives Considered

### Keep ADR-120's blind sync and close the fast path with `vu = 0`
Rejected because: the next pass cannot re-express a fill instant whose parameters were
overwritten.

### Create new shards lazily, planned as ADR-145 plans quotas
Rejected because: a dripping limit has no period or grant count to plan from, and a raise that
creates every new shard at once needs neither.

### Keep ADR-149's `rf`-locked deltas for dripping limits
Rejected because: the fast path's full write sets `z` without moving `rf`, so a delta computed
from a read can be overtaken unseen.

### Store the balance and its instant for administrative writers
Rejected because: two representations of one limit would have to be kept in step by every
writer, which is the problem this record removes.
