# ADR-156: Shard creation and raises for dripping limits

**Status:** Accepted
**Date:** 2026-11-07
**Issue:** Refs [#808](https://github.com/zeroae/zae-limiter/issues/808)
**Partially supersedes:** [ADR-133](133-client-shard-creation.md) for dripping limits (a raise creates every new shard at once, funded from its parent, instead of each shard being created at a full share on first use)
**Amends:** [ADR-151](151-non-enforcing-limits.md) (decision 8: a raise, by the client or the aggregator, copies a parent's `bypass` and soft deadlines onto its new shards, pinned on the parent's, so a copy never outlives the deadline it was copied from)
**Related:** [ADR-134](134-random-shard-selection.md), [ADR-136](136-entity-config-bucket-ttl.md), [ADR-140](140-duration-window-shard-coherence.md), [ADR-145](145-sharded-quota-conserves-allowance.md), [ADR-150](150-move-entity-to-new-parent.md), [ADR-153](153-admission-contract-for-dripping-limits.md), [ADR-155](155-fill-instant-writers.md)
**Design:** [Fill-instant proofs](../contributing/fill-instant-proofs.md) (§8)

## Context

ADR-133 creates a shard N>0 at a full share on first use and doubles `shard_count` without
touching the existing shards' balances, accepting a 1.5x transient that ADR-153 makes a defect.
ADR-155 allows only exact writes, so a raise must move each parent's balance above its new
share into the shard it splits off, reading every shard and committing one transaction pinned
on what it read.

Three things make that harder. A raise fires when a shard takes about 1000 writes a second, so
fast writes keep breaking its pins. The reserved `wcu` limit is a partition's own write budget,
not a share of a limit, so splitting it would leave every new shard unable to write and set off
further raises. And a shard can go missing on its own: each shard of a default-config bucket
carries its own TTL (ADR-136), and a bucket created before v0.18 may never have created some.

## Decision

A raise of a bucket holding a dripping limit must stamp a short-lived marker with a deadline on
every shard, which every write that moves a fill instant, the expression instant or the shard
count refuses while it is live, then read every shard strongly consistently and, in one
transaction pinned on what it read (the stamps a write it does not hold off can change
included), re-express each existing shard at the new count and create each new shard as a copy
of its parent holding the parent's dripping balance above the parent's new share; an expired
marker is ignored. A shard the client creates holds its dripping limits full only when it is
shard 0, the shard count the client knows is one, and a look-up finds no other shard of its
bucket; any other missing shard holds them empty, and every new shard's `wcu` starts full,
because that budget belongs to the partition.

## Consequences

**Positive:**
- No dripping share is created from nothing: a raise conserves the total balance exactly, and a
  missing shard adds nothing (proof §8).
- A raise lands even on the hottest bucket: while the marker is live, other writers wait rather
  than break its pins.
- Write sharding keeps working: every new shard has its partition's full write budget.

**Negative:**
- Every write that moves a dripping balance gains one condition term. One arriving during a
  raise waits until the marker's deadline (a debit, a return, a parameter sync, a reset) and is
  rebuilt from what the raise left, or is answered as contended (an acquire, #724). A
  `disabled`, owner or soft stamp fan-out is not held off: the raise is pinned on what it copies,
  and cancels.
- A raise costs a marker write and a strongly consistent read per shard, then one transaction
  as large as the new count (at most 32 items, plus ADR-150's owner check). It happens at most
  five times per bucket.
- A new shard copies its parent's item: owner, cascade and `disabled` stamps, schedules, `ttl`,
  and the `bypass` deadline and each dripping limit's soft deadline, pinned on the parent's, so a
  copy never outlives what it copied (ADR-151 let only the aggregator copy one). It carries no
  quota, session quotas included: its first slow pass seeds each, funded as ADR-145 funds a seed
  and joining shard 0's live window as ADR-140 requires. The raise freezes each legacy quota's
  grant size on the existing shards (ADR-145).
- A missing shard created empty under-admits until it refills: up to one share, once. A client
  that knows a count above one creates any missing shard empty; one that knows a count of one
  looks first (an index query, plus a consistent read when another shard lives), so a bucket
  whose every shard was swept starts full only for a client that knows of no other shard. On an
  item that also carries quotas, a created shard's quotas keep ADR-145's funding: a move off the
  sibling covering the slot, or a fresh grant when none covers it.
- The aggregator neither refills nor clones a dripping limit, and raises a bucket holding one
  only this way. A bucket with no dripping limit keeps ADR-133's and ADR-145's lazy creation.
  A quota reset on an item not yet converted moves `rf` past refill its dripping `tk` never
  received: an under-admission until its next slow pass converts it.

## Alternatives Considered

### Close the fast path with `vu = 0` and retry the raise
Rejected because: slow-path writes still move fill instants and break the pins, and `vu` is
shared with quotas (ADR-155).

### Create shards lazily, funded as ADR-145 funds quotas
Rejected because: a dripping limit has no period or grant count to plan from, and a raise that
creates every shard at once needs neither.

### Split `wcu` like a dripping limit
Rejected because: it is the partition's own write budget, and dividing it caps the whole bucket
at one partition's writes.

### Recreate a missing shard at a full share
Rejected because: it creates up to one share from nothing each time a shard is swept.
