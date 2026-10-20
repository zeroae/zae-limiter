# ADR-148: One acquire may debit several resources, all or none

**Status:** Proposed
**Date:** 2026-10-09
**Issue:** [#675](https://github.com/zeroae/zae-limiter/issues/675)
**Related:** [ADR-125](125-resource-disable.md), [ADR-133](133-client-shard-creation.md), [ADR-134](134-random-shard-selection.md), [ADR-145](145-sharded-quota-conserves-allowance.md), [ADR-146](146-per-resource-cascade-policy.md), [ADR-147](147-client-side-rejection-cache.md), [#674](https://github.com/zeroae/zae-limiter/issues/674)
**Design:** `docs/plans/2026-10-09-multi-resource-acquire-design.md`

## Context

A lease covers one (entity, resource). The #674 use case debits a metered resource
(requests and units, cascading to the organisation) **and** a shared budget resource
(weekly and session quotas, per user) on every call. Today that takes two acquires: two
round trips one after the other, two leases, two reconciles, and no atomicity between
them — the second can reject after the first has debited, and the caller must refund by
hand.

The machinery for several bucket items in one lease already exists, because a cascade is
one: the fast path writes child and parent concurrently and refunds the one that landed
when the other fails, the slow path commits every item in one `TransactWriteItems`, and
the commit, adjustment and rollback code already group entries by (entity, resource,
shard). Each resource also already resolves its own limits, `disabled` (ADR-125),
cascade policy (ADR-146), shard (ADR-133, ADR-134) and quota grants (ADR-145)
independently. What is missing is a way to name more than one resource, and a rule for
what happens when the resources disagree.

Three constraints shape the rule. DynamoDB caps a transaction at 100 items. A failed
conditional write costs 1 WCU (ADR-147), so writing to a resource the client already
knows is short is waste. And the cascade fast path already debits before it knows the
outcome, refunding afterwards, so a transient debit is an accepted cost in this codebase,
never an over-admission.

## Decision

One acquire must be able to debit several resources for one entity and return one lease
covering all of them, and it must admit all of them or none: when any resource is
rejected, disabled or unavailable, every debit that acquire wrote must be refunded before
the exception reaches the caller. Each resource must resolve its limits, `disabled`,
cascade policy, shard and quota grants exactly as a single-resource acquire does; no
resource may be named twice in one acquire.

Before any write, the ADR-147 rejection cache must be consulted for every resource
(and its parent, where it cascades), and a resource known short must reject the acquire
with no DynamoDB call. Otherwise the fast-path writes for every resource must be issued
concurrently. Resources the fast path admits keep their debit; resources it cannot settle
go to the slow path, which must plan them and commit them, with their ADR-145 donor
debits, in one `TransactWriteItems`; when that commit rejects, the fast-path debits are
refunded. The planned transaction must never exceed 100 items: the number of resources is
capped at the API boundary, and a plan whose donor debits would overflow must be re-planned
without moves (an under-admission, never a failure). A rejected acquire whose slow-path
plan holds a quota move must commit that move with nothing consumed, as ADR-145 requires
of a single resource.

`RateLimitExceeded` must carry a status for every declared limit of every resource the
decision evaluated, each tagged with its resource; `ResourceDisabled` on any resource
outranks `RateLimitExceeded` on another. Adjustments, consumption and releases must be
addressable per resource without changing the meaning of the existing single-resource
lease methods, and the exit reconcile must write one item per bucket touched. The sync
twin must be generated from the async source (ADR-121).

## Consequences

**Positive**
- One lease and one exit reconcile per request; the fast path takes one round trip for
  all resources instead of one per resource.
- Atomic on the slow path; all-or-none on the fast path through refunds, as a cascade is.
- No new bucket write and no schema change: every write is an existing builder.

**Negative**
- WCU on success is unchanged (one per item); the saving is latency and code, not cost.
- A rejection with a cold rejection cache costs every concurrent write plus a refund for
  each one that landed, more than the single write a hand-ordered pair would spend.
- A debit is visible to concurrent callers for up to two round trips before it is
  refunded, which can under-admit them; it can never over-admit.
- A transactional commit costs 2 WCU per item, so a slow path covering several resources
  costs more than separate single-item writes would.
- The 100-item cap bounds how many resources one acquire may name.

## Alternatives Considered

- **Keep two acquires**: two round trips, two leases and a hand-written refund on every
  partial rejection; rejected by the owner during #674 planning.
- **Always refund the fast-path debits and redo every resource in one transaction**:
  atomic in one step, but turns every single-resource slow-path fallback into a
  multi-item transaction at roughly three times the WCU.
- **Write the resources one after another, cheapest to reject first**: saves refunds on a
  rejection, but adds a round trip per resource to every admission, the common case.
- **A single bucket item per entity holding every resource**: breaks the per-resource
  partition key that sharding, cascade, disable and the aggregator all key on.
