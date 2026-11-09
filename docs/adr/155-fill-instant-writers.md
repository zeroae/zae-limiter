# ADR-155: Every write of a fill instant is exact

**Status:** Accepted
**Date:** 2026-11-07
**Issue:** Refs [#808](https://github.com/zeroae/zae-limiter/issues/808)
**Partially supersedes:** [ADR-120](120-bucket-param-sync.md) for dripping limits (a parameter sync is a read and a pinned write, not a blind update)
**Amends:** [ADR-149](149-reset-and-top-up.md) (a dripping limit's reset and top-up set its fill instant from the read, pinned on it, instead of adding a token delta; both stay in ADR-149's `rf`-locked transaction)
**Related:** [ADR-135](135-scheduled-limits.md), [ADR-145](145-sharded-quota-conserves-allowance.md), [ADR-153](153-admission-contract-for-dripping-limits.md), [ADR-154](154-fill-instant-fast-path.md), [ADR-156](156-dripping-shard-raises.md)
**Design:** [Fill-instant proofs](../contributing/fill-instant-proofs.md) (§8)

## Context

ADR-154 decides the fast path's two writes on a dripping limit's fill instant `z` and leaves
every other writer open; two conflict as they stand. ADR-120's blind parameter sync overwrites
the parameters `z` is expressed against, and ADR-149's reset and top-up write deltas under an
`rf` lock that the fast path's full write, a plain `SET`, does not respect. ADR-153 also needs
refill integrated across schedule windows, which needs the parameters each fill instant was
last expressed against. What a created or raised shard holds is ADR-156's; a raise's write to
each existing shard is bound by this record like any other write to an existing item.

ADR-153's bound holds for any write that is an exact strict step on the item it lands on, and
DynamoDB offers three ways to be exact: a write whose condition proves its case (ADR-154's two
writes); a write exact on every item, since moving `z` earlier is exactly a return or a top-up
(capped by construction, as every `z` before now reads as full) and setting `z` to the write's
clock reading is exactly a reset; and a write computed from a read and pinned on what it read.
Each is exact only against the parameters it used, so each must pin them, and `vu` cannot do
that: quota writers stamp it too, to 0 or past a dripping boundary. A positive adjustment is a
debit: on a full bucket it must record `max(z, t) + c/r`, the same kink as an admission, and
adding `c/r` forgives the debt whenever `z + c/r` is still before the write's clock reading.

## Decision

Every write of a dripping limit's fill instant to an existing item must be pinned on the
parameters, schedule and shard count it was computed against and on the item's recorded
expression instant (which pins the schedule, since a write changing it records a new one), with
its clock reading before the next schedule boundary after that instant,
and must take one of three forms: one of ADR-154's two conditional writes, which a positive
adjustment uses without the upper bound, retrying with a fresh clock reading each time rather
than dropping it, because dropping it would forgive the debt; a write exact on every item
(moving the fill instant earlier for a return or a negative adjustment, or setting it to the
write's clock reading for a reset, which ADR-149's read also pins); or a write computed from a
read and pinned on the fill instants it read, which a top-up uses so its split is known. A
write that changes those parameters, schedule or shard count, or that moves `vu` past such a
boundary, must re-express the item's fill instants in that same write and record when it did so.

## Consequences

**Positive:**
- ADR-153's bound holds for every writer, not only the fast path (proof §8).
- Returns and negative adjustments need no read and no lock, and a return or top-up is capped
  at capacity by construction, which is ADR-149's headroom rule for free.
- Refill across schedule windows is exact: the first pass after a boundary re-expresses each
  fill instant at every boundary since its recorded instant (ADR-153, ADR-135).
- Quota writers keep stamping `vu` as they do: no dripping pin depends on it.

**Negative:**
- A positive adjustment (the LLM reconcile) is never rejected and never silently dropped while
  its bucket holds the limit; one whose bucket is gone or no longer holds the limit has no debt
  to keep and is skipped (#839). It predicts its side and re-stamps each retry, so its lateness
  never counts as clock gap; returns landing between its writes can delay a lease's exit, and
  one still not landed after a fixed number of writes, or meeting a backend error, raises from
  the exit as today (#682). A soft limit's debit after a fast-path admission is the same pair
  but is given up after a few failed writes or an error, losing its debt and `tc`: only
  reporting (ADR-154).
- A parameter sync, ADR-153's resource- and system-level fan-out, and an operator reset or
  top-up become a read and a pinned write per bucket, retried when a fast write lands in
  between; a credit computed before a re-expression fails its pin and is resent the same way.
- A writer that moves `vu` past a dripping boundary (the aggregator's quota re-stamp, a quota
  slow pass) must re-express the dripping limits on that item in the same write.
- The slow path sends ADR-154's writes from the image it read, or a write computed from that
  read and pinned on it where no pair reproduces the result (a conversion from `tk`, a
  re-expression). They ride in the item's one update, still under its `rf` lock, which still
  stamps the owner (ADR-146, with ADR-150's `pgen` / `pinc` pin) and sets, renews or removes the
  soft and bypass stamps (ADR-151 decisions 4 and 8). The consumption-only retry, the refill
  under the `rf` lock and the separate #633 seed go away for dripping limits: a lost pin retries
  through ADR-154's pair from a fresh image or re-plans once, and a limit configured after its
  item existed starts full on every shard.
- Quotas keep ADR-149's `rf`-locked deltas, so a reset or top-up on an item carrying both
  writes both forms in one transaction. A reset forgives a dripping limit's debt.
- The item gains one attribute: the instant its fill instants were last expressed.

## Alternatives Considered

### Keep ADR-120's blind sync and close the fast path with `vu = 0`
Rejected because: the next pass cannot re-express a fill instant whose parameters were
overwritten.

### Keep ADR-149's `rf`-locked deltas for dripping limits
Rejected because: the fast path's full write sets `z` without moving `rf`, so a delta computed
from a read can be overtaken unseen.

### Pin dripping writes on `vu`
Rejected because: a quota's `vu = 0` would force every return to read first, and a quota
writer moving `vu` would hide a dripping boundary.

### Record a positive adjustment as an unconditional add
Rejected because: it forgives the debt on a full bucket (the review measured 17 times the
`2 r s` term), the over-admission this record exists to remove.
